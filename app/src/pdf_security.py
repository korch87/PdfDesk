"""Password protection helpers for PDF byte streams.

Encrypted PDFs use AES-256 by default. pypdf delegates AES to an optional
cryptographic provider; this module fails closed when that provider is absent
and never silently downgrades to legacy RC4 encryption.
"""
from __future__ import annotations

import io
from typing import Any

from pypdf import PdfReader, PdfWriter
from pypdf.constants import UserAccessPermissions
from pypdf.errors import DependencyError, PdfReadError


class PdfSecurityError(RuntimeError):
    """Base error for invalid or unsupported password-protection operations."""


class InvalidPdfPassword(PdfSecurityError):
    """The supplied password did not authenticate the encrypted PDF."""


class PdfEncryptionUnavailable(PdfSecurityError):
    """The installed pypdf build has no AES provider."""


def aes_encryption_available() -> bool:
    """Return whether pypdf selected a provider that implements AES."""
    try:
        from pypdf._crypt_providers import crypt_provider
        return bool(crypt_provider and crypt_provider[0] != "local_crypt_fallback")
    except Exception:
        return False


def _as_pdf_bytes(data: bytes | bytearray | memoryview) -> bytes:
    if not isinstance(data, (bytes, bytearray, memoryview)):
        raise TypeError("Данные PDF должны быть байтовой последовательностью.")
    raw = bytes(data)
    if not raw.startswith(b"%PDF"):
        raise PdfSecurityError("Данные не являются PDF-файлом.")
    return raw


def _reader(data: bytes) -> PdfReader:
    try:
        return PdfReader(io.BytesIO(data), strict=False)
    except PdfReadError as exc:
        raise PdfSecurityError("Не удалось разобрать PDF.") from exc
    except Exception as exc:
        raise PdfSecurityError("Не удалось разобрать PDF.") from exc


def _password(value: str, label: str) -> str:
    if not isinstance(value, str):
        raise TypeError("Параметр %s должен быть строкой." % label)
    if "\x00" in value:
        raise ValueError("Параметр %s не может содержать нулевой символ." % label)
    return value


def _protect_with_algorithm(data: bytes, open_password: str, owner_password: str,
                             allow_print: bool, allow_fill_forms: bool,
                             algorithm: str) -> bytes:
    """Internal algorithm parameter keeps tests independent of optional AES."""
    source = _as_pdf_bytes(data)
    if not open_password and not owner_password:
        raise ValueError("Задайте пароль открытия или пароль владельца.")
    if open_password and owner_password and open_password == owner_password:
        raise ValueError("Пароль открытия и пароль владельца должны различаться.")
    if not isinstance(allow_print, bool) or not isinstance(allow_fill_forms, bool):
        raise TypeError("Параметры прав доступа должны быть логическими значениями.")
    reader = _reader(source)
    if reader.is_encrypted:
        raise PdfSecurityError("PDF уже защищён. Сначала откройте его с паролем и снимите защиту.")
    if not reader.pages:
        raise PdfSecurityError("В PDF нет страниц.")

    writer = PdfWriter(clone_from=reader)
    if owner_password:
        # Viewing is always permitted. These two bits grant print and form-fill;
        # editing, annotations, extraction and page assembly stay disabled.
        permissions = UserAccessPermissions(0)
        if allow_print:
            permissions |= UserAccessPermissions.PRINT
        if allow_fill_forms:
            permissions |= UserAccessPermissions.FILL_FORM_FIELDS
        writer.encrypt(open_password, owner_password,
                       permissions_flag=permissions, algorithm=algorithm)
    else:
        # Without a distinct owner credential, permission flags cannot protect
        # against the user acting as the owner. Keep pypdf's permissive default.
        writer.encrypt(open_password, None, algorithm=algorithm)
    output = io.BytesIO()
    try:
        writer.write(output)
    except DependencyError as exc:
        raise PdfEncryptionUnavailable(
            "Для AES-256 нужен криптографический модуль pypdf; менее стойкое шифрование не применялось."
        ) from exc
    result = output.getvalue()
    if not result.startswith(b"%PDF"):
        raise PdfSecurityError("Не удалось создать корректный PDF.")
    return result


def protect_pdf(data: bytes | bytearray | memoryview, open_password: str = "",
                owner_password: str = "", allow_print: bool = True,
                allow_fill_forms: bool = True) -> bytes:
    """Return a protected copy of ``data`` encrypted with AES-256.

    A nonempty ``open_password`` is required to open the file. A nonempty,
    distinct ``owner_password`` grants unrestricted owner access. User access
    with an owner password is limited to viewing plus form filling and printing
    by default. With no owner password, pypdf's permissive default applies
    because a distinct owner credential is needed to enforce restrictions.
    Input bytes are read only and are never changed.
    """
    opening = _password(open_password, "open_password")
    owner = _password(owner_password, "owner_password")
    # Validate caller input before reporting a missing optional provider.
    if not opening and not owner:
        raise ValueError("Задайте пароль открытия или пароль владельца.")
    if opening and owner and opening == owner:
        raise ValueError("Пароль открытия и пароль владельца должны различаться.")
    if not isinstance(allow_print, bool) or not isinstance(allow_fill_forms, bool):
        raise TypeError("Параметры прав доступа должны быть логическими значениями.")
    if not aes_encryption_available():
        raise PdfEncryptionUnavailable(
            "Защита AES-256 недоступна в этой сборке."
        )
    return _protect_with_algorithm(bytes(data), opening, owner,
                                   allow_print, allow_fill_forms, "AES-256")


def _algorithm_name(reader: PdfReader) -> str | None:
    if not reader.is_encrypted:
        return None
    encryption = reader.trailer.get("/Encrypt")
    encryption = encryption.get_object() if hasattr(encryption, "get_object") else encryption
    if not hasattr(encryption, "get"):
        return "unknown"
    try:
        version = int(encryption.get("/V", 0))
        revision = int(encryption.get("/R", 0))
    except (TypeError, ValueError):
        version, revision = 0, 0
    if version == 5:
        return "AES-256-R%d" % revision
    if version == 4:
        crypt_filters = encryption.get("/CF")
        crypt_filters = crypt_filters.get_object() if hasattr(crypt_filters, "get_object") else crypt_filters
        standard = crypt_filters.get("/StdCF") if hasattr(crypt_filters, "get") else None
        standard = standard.get_object() if hasattr(standard, "get_object") else standard
        method = str(standard.get("/CFM", "")) if hasattr(standard, "get") else ""
        return {"/AESV2": "AES-128", "/V2": "RC4-128"}.get(method, "unknown")
    if version == 2:
        return "RC4-128"
    if version == 1:
        return "RC4-40"
    return "unknown"


def _permission_map(value: Any) -> dict[str, bool]:
    try:
        bits = int(value)
    except (TypeError, ValueError):
        bits = 0
    return {
        "print": bool(bits & int(UserAccessPermissions.PRINT)),
        "fill_forms": bool(bits & int(UserAccessPermissions.FILL_FORM_FIELDS)),
        "modify": bool(bits & int(UserAccessPermissions.MODIFY)),
        "annotate": bool(bits & int(UserAccessPermissions.ADD_OR_MODIFY)),
        "extract": bool(bits & int(UserAccessPermissions.EXTRACT)),
        "extract_text_and_graphics": bool(bits & int(UserAccessPermissions.EXTRACT_TEXT_AND_GRAPHICS)),
        "assemble": bool(bits & int(UserAccessPermissions.ASSEMBLE_DOC)),
        "print_high_quality": bool(bits & int(UserAccessPermissions.PRINT_TO_REPRESENTATION)),
    }


def inspect_pdf_protection(data: bytes | bytearray | memoryview,
                           password: str | None = None) -> dict[str, Any]:
    """Describe encryption and allowed user operations without exposing secrets.

    If ``password`` is supplied, authentication is checked and the result marks
    whether it matched the user or owner credential. Without a password,
    permission bits are reported but remain unverified for AES-256 until a
    caller authenticates (pypdf validates the encrypted ``/Perms`` block then).
    """
    raw = _as_pdf_bytes(data)
    reader = _reader(raw)
    if not reader.is_encrypted:
        return {"encrypted": False, "algorithm": None, "authenticated_as": None,
                "permissions": {key: True for key in _permission_map(0)},
                "permissions_valid": None}
    encryption = reader.trailer.get("/Encrypt")
    encryption = encryption.get_object() if hasattr(encryption, "get_object") else encryption
    raw_permissions = encryption.get("/P", 0) if hasattr(encryption, "get") else 0
    authenticated_as = None
    permissions_valid = None
    if password is not None:
        supplied = _password(password, "password")
        try:
            status = reader.decrypt(supplied)
        except DependencyError as exc:
            raise PdfEncryptionUnavailable(
                "Эта сборка не поддерживает алгоритм AES, которым защищён PDF."
            ) from exc
        if not status:
            raise InvalidPdfPassword("Неверный пароль PDF.")
        authenticated_as = "owner" if int(status) == 2 else "user"
        permissions_valid = getattr(reader, "are_permissions_valid", None)
    try:
        raw_permissions = int(raw_permissions)
    except (TypeError, ValueError):
        raw_permissions = 0
    return {"encrypted": True, "algorithm": _algorithm_name(reader),
            "authenticated_as": authenticated_as,
            "permissions": _permission_map(raw_permissions),
            "permissions_valid": permissions_valid}


def decrypt_pdf(data: bytes | bytearray | memoryview, password: str) -> bytes:
    """Authenticate and return a decrypted copy of an encrypted PDF.

    This function accepts either the user or owner password. The caller must
    use :func:`inspect_pdf_protection` to retain and enforce user permissions
    in its editing UI; removing encryption here does not grant owner rights.
    An already unencrypted document is returned byte-for-byte unchanged.
    """
    raw = _as_pdf_bytes(data)
    supplied = _password(password, "password")
    reader = _reader(raw)
    if not reader.is_encrypted:
        return raw
    try:
        status = reader.decrypt(supplied)
    except DependencyError as exc:
        raise PdfEncryptionUnavailable(
                "Эта сборка не поддерживает алгоритм AES, которым защищён PDF."
        ) from exc
    if not status:
        raise InvalidPdfPassword("Неверный пароль PDF.")
    writer = PdfWriter(clone_from=reader)
    output = io.BytesIO()
    try:
        writer.write(output)
    except DependencyError as exc:
        raise PdfEncryptionUnavailable("Эта сборка не может снять защиту с PDF.") from exc
    result = output.getvalue()
    check = _reader(result)
    if check.is_encrypted:
        raise PdfSecurityError("После снятия защиты PDF всё ещё зашифрован.")
    return result
