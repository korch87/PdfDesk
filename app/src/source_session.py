"""Bridge native content edits into PdfSession's byte-backed undo history."""
from __future__ import annotations

import dataclasses
import hashlib
import io
from decimal import Decimal, InvalidOperation
from typing import Any, Callable

from pypdf import PdfReader
from pypdf.generic import (BooleanObject, ByteStringObject, FloatObject,
                           NameObject, NullObject, NumberObject,
                           StreamObject, TextStringObject)
from .content_editor import (ContentBlock, add_native_image, add_native_text,
                             apply_content_edit, inspect_content)
from .nested_content import (FormOccurrence, delete_form_occurrence as _delete_form_occurrence,
                             discover_form_occurrences,
                             resize_form_occurrence as _resize_form_occurrence,
                             translate_form_occurrence as _translate_form_occurrence)


class SourceContentSession:
    """Small adapter which keeps source edits inside the normal session lifecycle."""

    @staticmethod
    def inspect(session: Any, page_index: int) -> list[ContentBlock]:
        snapshot = session._make_snapshot()
        return inspect_content(snapshot.data, page_index)

    @staticmethod
    def inspect_form_occurrences(session: Any, page_index: int) -> list[FormOccurrence]:
        """List page Form invocations with stable paths for one document revision."""
        snapshot = session._make_snapshot()
        return discover_form_occurrences(snapshot.data, page_index)

    @staticmethod
    def inspect_nested(session: Any, page_index: int, identity: str) -> list[Any]:
        """Inspect one leaf invocation against the current complete snapshot."""
        from .nested_text_editor import inspect_nested_content
        snapshot = session._make_snapshot()
        candidates = [item for item in discover_form_occurrences(snapshot.data, page_index)
                      if item.identity == identity]
        if len(candidates) != 1:
            raise ValueError("Выбранная группа PDF устарела. Выберите её заново.")
        return inspect_nested_content(snapshot.data, candidates[0])

    @staticmethod
    def apply_nested(session: Any, target: Any, operation: str, **kwargs: Any) -> None:
        """Commit a revision-bound leaf edit through the normal undo/permission guard."""
        from .nested_text_editor import apply_nested_content_edit
        SourceContentSession._commit(
            session, lambda data: apply_nested_content_edit(data, target, operation, **kwargs))

    @staticmethod
    def _apply_form_occurrence(session: Any, page_index: int, identity: str,
                               operation: str, **kwargs: Any) -> None:
        def build(data: bytes) -> bytes:
            candidates = [item for item in discover_form_occurrences(data, page_index)
                          if item.identity == identity]
            if len(candidates) != 1:
                raise ValueError("Выбранный экземпляр Form устарел. Выберите его заново.")
            occurrence = candidates[0]
            if operation == "delete":
                return _delete_form_occurrence(data, occurrence)
            if operation == "translate":
                return _translate_form_occurrence(data, occurrence,
                                                  float(kwargs["dx"]), float(kwargs["dy"]))
            if operation == "resize":
                return _resize_form_occurrence(data, occurrence,
                                               float(kwargs["sx"]), float(kwargs["sy"]),
                                               origin=kwargs.get("origin", (0.0, 0.0)))
            raise ValueError("Неподдерживаемая операция с Form-группой.")

        SourceContentSession._commit(session, build)

    @staticmethod
    def delete_form_occurrence(session: Any, page_index: int, identity: str) -> None:
        SourceContentSession._apply_form_occurrence(session, page_index, identity, "delete")

    @staticmethod
    def translate_form_occurrence(session: Any, page_index: int, identity: str,
                                 dx: float, dy: float) -> None:
        if float(dx) == 0.0 and float(dy) == 0.0:
            return
        SourceContentSession._apply_form_occurrence(session, page_index, identity,
                                                   "translate", dx=dx, dy=dy)

    @staticmethod
    def resize_form_occurrence(session: Any, page_index: int, identity: str,
                               sx: float, sy: float, *,
                               origin: tuple[float, float] = (0.0, 0.0)) -> None:
        if float(sx) == 1.0 and float(sy) == 1.0:
            return
        SourceContentSession._apply_form_occurrence(session, page_index, identity,
                                                   "resize", sx=sx, sy=sy, origin=origin)

    @staticmethod
    def _rotation(value: Any) -> int:
        try:
            return int(float(value)) % 360
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _canonical_pdf_object(value: Any,
                              page_identity_by_object: dict[int, int] | None = None,
                              page_reference_to_index: dict[tuple[int, int], int] | None = None) -> tuple:
        """Return a stable semantic fingerprint of a PDF object graph.

        Indirect object numbers and stream compression are storage details, so
        neither enters the fingerprint. Stream data is decoded and hashed;
        resources and all other dictionary entries are visited recursively.
        Backreferences are represented by their traversal path, not object ID,
        making cycles deterministic across a reader/writer round trip. ``/P``
        is intentionally skipped on every dictionary: it points back to the
        page and would otherwise include editable page content in a widget's
        or annotation's signature.
        """
        stack: dict[int, tuple] = {}

        def visit(item: Any, path: tuple) -> tuple:
            reference_key = (getattr(item, "idnum", None),
                             getattr(item, "generation", None))
            if (page_reference_to_index is not None
                    and reference_key[0] is not None
                    and reference_key in page_reference_to_index):
                return ("page-target", page_reference_to_index[reference_key])
            if hasattr(item, "get_object"):
                try:
                    resolved = item.get_object()
                except Exception:  # noqa: BLE001 - retain corrupt-reference details
                    return ("indirect-error", type(item).__name__)
                if resolved is not item:
                    return visit(resolved, path)

            if isinstance(item, dict) and page_identity_by_object is not None:
                page_index = page_identity_by_object.get(id(item))
                if page_index is not None:
                    # Internal /Dest and /GoTo references name a page; they
                    # must not pull its editable /Contents or /Resources into
                    # the annotation fingerprint.
                    return ("page-target", page_index)

            if item is None or isinstance(item, NullObject):
                return ("null",)
            if isinstance(item, (bool, BooleanObject)):
                return ("bool", bool(item))
            if isinstance(item, NameObject):
                return ("name", str(item))
            if isinstance(item, TextStringObject):
                return ("text", str(item))
            if isinstance(item, ByteStringObject):
                data = bytes(item)
                return ("byte-string", hashlib.sha256(data).hexdigest(), len(data))
            if isinstance(item, (NumberObject, FloatObject, int, float, Decimal)):
                try:
                    number = Decimal(str(item))
                    normalized = "0" if number == 0 else str(number.normalize())
                except (InvalidOperation, ValueError):
                    normalized = str(item)
                return ("number", normalized)
            if isinstance(item, str):
                return ("string", item)
            if isinstance(item, (bytes, bytearray, memoryview)):
                data = bytes(item)
                return ("bytes", hashlib.sha256(data).hexdigest(), len(data))

            is_stream = isinstance(item, StreamObject)
            is_mapping = isinstance(item, dict)
            is_array = isinstance(item, (list, tuple))
            if is_mapping or is_array:
                identity = id(item)
                if identity in stack:
                    return ("cycle", stack[identity])
                stack[identity] = path
                try:
                    if is_array:
                        return ("array", tuple(visit(child, path + (index,))
                                                for index, child in enumerate(item)))
                    ignored_stream_keys = {"/Length", "/Filter", "/DecodeParms"} if is_stream else set()
                    entries = []
                    for raw_key, child in item.items():
                        key = str(raw_key)
                        if key == "/P" or key in ignored_stream_keys:
                            continue
                        entries.append((key, visit(child, path + (key,))))
                    entries.sort(key=lambda pair: pair[0])
                    if not is_stream:
                        return ("dictionary", tuple(entries))
                    try:
                        decoded = bytes(item.get_data())
                        stream_data = ("decoded", hashlib.sha256(decoded).hexdigest(), len(decoded))
                    except Exception as exc:  # noqa: BLE001 - malformed/unsupported stream filter
                        raw_data = getattr(item, "_data", b"")
                        raw_data = bytes(raw_data) if isinstance(raw_data, (bytes, bytearray)) else b""
                        stream_data = ("decode-error", type(exc).__name__,
                                       hashlib.sha256(raw_data).hexdigest(), len(raw_data))
                    return ("stream", tuple(entries), stream_data)
                finally:
                    stack.pop(id(item), None)

            # pypdf wrappers outside the standard PDF object hierarchy are
            # uncommon, but retaining their type/value is safer than ignoring
            # an unknown foreign object.
            return ("other", type(item).__name__, str(item))

        return visit(value, ())

    @staticmethod
    def _annotation_rows(page: Any, page_identity_by_object: dict[int, int] | None = None,
                         page_reference_to_index: dict[tuple[int, int], int] | None = None) -> tuple[tuple, ...]:
        """Fingerprint every annotation in original page order, known or not."""
        rows = []
        for reference in page.get("/Annots", ()) or ():
            rows.append(SourceContentSession._canonical_pdf_object(
                reference, page_identity_by_object, page_reference_to_index))
        return tuple(rows)

    @staticmethod
    def _document_signature(data: bytes):
        """Everything a source-stream edit must leave byte-for-byte equivalent.

        Page count, page boxes (/CropBox and /MediaBox) and rotation, every
        annotation (including unknown third-party types) and every form value
        are compared, so an engine that dropped or flattened unrelated objects
        is rejected before the session history is touched.
        """
        reader = PdfReader(io.BytesIO(data), strict=False)
        fields = reader.get_fields() or {}
        values = tuple(sorted((str(key), str(value.get("/V", "")))
                              for key, value in fields.items()))
        catalog = reader.trailer.get("/Root", {})
        page_objects = list(reader.pages)
        page_identity_by_object = {id(page): index for index, page in enumerate(page_objects)}
        page_reference_to_index = {}
        for index, page in enumerate(page_objects):
            reference = getattr(page, "indirect_reference", None)
            if reference is not None:
                page_reference_to_index[(reference.idnum, reference.generation)] = index
        acroform_signature = SourceContentSession._canonical_pdf_object(
            catalog.get("/AcroForm") if hasattr(catalog, "get") else None,
            page_identity_by_object, page_reference_to_index)
        widgets = 0
        pages = []
        for page in page_objects:
            try:
                crop = tuple(round(float(value), 2) for value in page.cropbox)
            except Exception:  # noqa: BLE001 - malformed /CropBox
                crop = ()
            try:
                media = tuple(round(float(value), 2) for value in page.mediabox)
            except Exception:  # noqa: BLE001 - malformed /MediaBox
                media = ()
            for reference in page.get("/Annots", ()) or ():
                annot = reference.get_object() if hasattr(reference, "get_object") else reference
                if str(annot.get("/Subtype", "")) == "/Widget":
                    widgets += 1
            rotation = SourceContentSession._rotation(page.get_inherited("/Rotate", 0) or 0)
            pages.append((crop, media, rotation,
                          SourceContentSession._annotation_rows(page, page_identity_by_object,
                                                               page_reference_to_index)))
        return len(reader.pages), values, widgets, acroform_signature, tuple(pages)

    @staticmethod
    def _commit(session: Any, build: Callable[[bytes], bytes]) -> None:
        """Apply ``build`` to a snapshot and swap it in as one undoable revision.

        Source-stream edits are never covered by the permission to fill forms.
        The whole replacement is produced before history changes; if the engine
        refuses, the open document, its bytes and its undo stack stay intact.
        """
        session._require_edit_permission()
        before = session._make_snapshot()
        changed = build(before.data)
        if SourceContentSession._document_signature(changed) != \
                SourceContentSession._document_signature(before.data):
            raise RuntimeError("Изменение затронуло структуру страниц или поля формы")
        session._push_undo()
        replacement = dataclasses.replace(before, data=changed, size=len(changed))
        session._restore(replacement)
        session._touch(doc=True)

    @staticmethod
    def apply(session: Any, page_index: int, block_id: str, operation: str,
              **kwargs: Any) -> None:
        SourceContentSession._commit(
            session,
            lambda data: apply_content_edit(data, page_index, block_id, operation, **kwargs))

    @staticmethod
    def apply_rich_text(session: Any, page_index: int, block_id: str, text: str,
                        rich_spans: list[dict[str, Any]], **layout: Any) -> None:
        """Replace one native paragraph with typed per-run style attributes.

        Each span is ``{text, font_name, font_size, color, bold, italic,
        underline}``; layout accepts width, height, alignment, and line_height.
        The operation uses the same single byte-backed undo entry as ``apply``.
        """
        SourceContentSession.apply(session, page_index, block_id, "replace",
                                   text=text, rich_spans=rich_spans, **layout)

    @staticmethod
    def add_image(session: Any, page_index: int, image_data: Any,
                  rect: tuple[float, float, float, float]) -> None:
        """Insert a native PDF image at (x, y, width, height) in PDF points."""
        SourceContentSession._commit(
            session,
            lambda data: add_native_image(data, page_index, image_data, rect))

    @staticmethod
    def add_text(session: Any, page_index: int, text: str,
                 rect: tuple[float, float, float, float], *,
                 font_name: str = "Arial", font_size: float = 14.0,
                 color: str = "#000000", bold: bool = False,
                 alignment: str = "left", orientation: str = "display") -> None:
        """Insert searchable native paragraph text at (x,y,width,height) PDF points."""
        SourceContentSession._commit(
            session,
            lambda data: add_native_text(data, page_index, text, rect,
                                         font_name=font_name, font_size=font_size,
                                         color=color, bold=bold, alignment=alignment,
                                         orientation=orientation))
