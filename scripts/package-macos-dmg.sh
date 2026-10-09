#!/usr/bin/env bash
# PdfDesk macOS DMG packaging for local testing on one Mac architecture.
#
# This script wraps an already built PdfDesk.app into a compressed, read-only
# UDZO disk image that contains the full bundle, an "Applications" drop target,
# and a short Russian installation note. The application itself is never
# modified, and no code signature is created or replaced here.
#
# It runs only on macOS: the work is done by hdiutil, ditto, lipo, codesign
# (read-only inspection) and shasum, and application verification is delegated
# to the existing scripts/verify-macos.sh. The script performs no network
# access, installs nothing, needs no credentials, and does not change
# scripts/build-macos.sh or scripts/verify-macos.sh.
#
# Publication rules:
#   * An existing DMG or build report with the same name is never replaced
#     silently; the run stops unless --force is given.
#   * The validated image is published atomically (hard link, or a rename inside
#     the destination folder), so a partial file never appears at the final path.
#   * All staging happens in a unique folder under app/tmp/macos-dmg, and only
#     that validated folder is removed when the script exits.
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd)"
APP_DIR="${REPO_ROOT}/app"
VERIFY_SCRIPT="${SCRIPT_DIR}/verify-macos.sh"
STAGING_PARENT="${APP_DIR}/tmp/macos-dmg"
DEFAULT_OUTPUT_DIR="${REPO_ROOT}/outputs/releases"
README_NAME="Установка PdfDesk.txt"

# 1. Platform guard. Nothing is parsed, created, or copied before this point.
if [[ "$(uname -s)" != "Darwin" ]]; then
    echo "This DMG packaging step must run on macOS; hdiutil and ditto are not available on $(uname -s)." >&2
    echo "Run it on the target Mac after scripts/build-macos.sh, as described in docs/macos-dmg-packaging.txt." >&2
    exit 2
fi
HOST_ARCH="$(uname -m)"
case "${HOST_ARCH}" in
    x86_64|arm64) ;;
    *) echo "Unsupported Mac architecture: ${HOST_ARCH}" >&2; exit 2 ;;
esac

die() {
    local code="$1"
    shift
    echo "ERROR: $*" >&2
    exit "${code}"
}

usage() {
    cat <<'USAGE'
Package a built and verified PdfDesk.app into a compressed read-only DMG.

Usage:
  bash scripts/package-macos-dmg.sh --app <path to PdfDesk.app> [options]

Required:
  --app <path>        Explicit path to the built app bundle, for example
                      app/dist-macos/x86_64/PdfDesk.app. The bundle must be a
                      single-architecture bundle for this Mac and must pass
                      scripts/verify-macos.sh before anything is packaged.

Options:
  --output-dir <dir>  Folder for the DMG and the build report. The path must
                      stay inside the project root (default: outputs/releases).
  --force             Replace an existing DMG/build report of the same name.
                      Without this flag the script stops instead of touching
                      existing files. Replacement is published atomically.
  --keep-staging      Keep the staging folder under app/tmp/macos-dmg for
                      inspection instead of removing it on exit.
  -h, --help          Show this help.

Output:
  outputs/releases/PdfDesk-macos-<arch>.dmg
  outputs/releases/PdfDesk-macos-<arch>-dmg-build-report.txt

The image holds the full .app bundle, an "Applications" symlink for drag
installation, and a short Russian installation note with the factual ad hoc
signing status. The app is not notarized; the image is for local testing and is
not a public release artifact.
USAGE
}

for tool in hdiutil ditto shasum lipo codesign sw_vers; do
    if ! command -v "${tool}" >/dev/null 2>&1; then
        die 2 "Required macOS tool is missing: ${tool}"
    fi
done
if [[ ! -f "${VERIFY_SCRIPT}" ]]; then
    die 2 "The existing verifier is missing: ${VERIFY_SCRIPT}"
fi

APP_BUNDLE=""
OUTPUT_DIR_INPUT="${DEFAULT_OUTPUT_DIR}"
FORCE=0
KEEP_STAGING=0
# Bare positional arguments are accepted as a shortcut for --app; the loop keeps
# this portable to the bash 3.2 that macOS still ships (no empty-array tricks).
while (( $# > 0 )); do
    case "$1" in
        --app)
            [[ $# -ge 2 ]] || die 2 "--app requires a path to PdfDesk.app"
            APP_BUNDLE="$2"
            shift 2
            ;;
        --app=*)
            APP_BUNDLE="${1#--app=}"
            shift
            ;;
        --output-dir)
            [[ $# -ge 2 ]] || die 2 "--output-dir requires a folder path"
            OUTPUT_DIR_INPUT="$2"
            shift 2
            ;;
        --output-dir=*)
            OUTPUT_DIR_INPUT="${1#--output-dir=}"
            shift
            ;;
        --force)
            FORCE=1
            shift
            ;;
        --keep-staging)
            KEEP_STAGING=1
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        --)
            shift
            if [[ -z "${APP_BUNDLE}" && $# -eq 1 ]]; then
                APP_BUNDLE="$1"
                shift
            elif [[ $# -gt 0 ]]; then
                die 2 "Unexpected argument(s) after --: $* (use --app for the bundle path)"
            fi
            ;;
        -*)
            die 2 "Unknown option: $1 (use --help)"
            ;;
        *)
            if [[ -z "${APP_BUNDLE}" ]]; then
                APP_BUNDLE="$1"
            else
                die 2 "Unexpected argument: $1 (use --app for the bundle path)"
            fi
            shift
            ;;
    esac
done
[[ -n "${APP_BUNDLE}" ]] || die 2 "--app <path to PdfDesk.app> is required; nothing was packaged."

# 2. Validate the app bundle. Every later step only reads it.
while [[ "${APP_BUNDLE}" == */ ]]; do
    APP_BUNDLE="${APP_BUNDLE%/}"
done
case "${APP_BUNDLE}" in
    *.app) ;;
    *) die 2 "The app bundle path must end with .app: ${APP_BUNDLE}" ;;
esac
if [[ ! -d "${APP_BUNDLE}" ]]; then
    die 2 "App bundle not found or not a directory: ${APP_BUNDLE}"
fi
APP_BUNDLE="$(cd -- "${APP_BUNDLE}" && pwd -P)"
case "${APP_BUNDLE}" in
    "${STAGING_PARENT}"|"${STAGING_PARENT}"/*)
        die 2 "Refusing to package a path inside the staging folder: ${APP_BUNDLE}"
        ;;
esac
EXECUTABLE="${APP_BUNDLE}/Contents/MacOS/PdfDesk"
INFO_PLIST="${APP_BUNDLE}/Contents/Info.plist"
if [[ ! -x "${EXECUTABLE}" ]]; then
    die 2 "The app executable is missing or not executable: ${EXECUTABLE}"
fi
if [[ ! -f "${INFO_PLIST}" ]]; then
    die 2 "The app bundle has no Contents/Info.plist: ${APP_BUNDLE}"
fi

# The DMG name follows the bundle that is actually being packaged, not the host
# by assumption; scripts/verify-macos.sh then requires the native architecture.
LIPO_ARCHS="$(lipo -archs "${EXECUTABLE}")"
BUNDLE_ARCH_COUNT="$(printf '%s\n' "${LIPO_ARCHS}" | tr ' ' '\n' | sed '/^$/d' | wc -l | tr -d '[:space:]')"
if [[ "${BUNDLE_ARCH_COUNT}" != "1" ]]; then
    die 2 "Expected a single-architecture bundle, found: ${LIPO_ARCHS} (build per architecture, see docs/macos-build.txt)"
fi
BUNDLE_ARCH="$(printf '%s\n' "${LIPO_ARCHS}" | tr -d '[:space:]')"
case "${BUNDLE_ARCH}" in
    x86_64|arm64) ;;
    *) die 2 "Unsupported bundle architecture: ${BUNDLE_ARCH}" ;;
esac
if [[ "${BUNDLE_ARCH}" != "${HOST_ARCH}" ]]; then
    die 2 "The bundle is ${BUNDLE_ARCH} but this Mac is ${HOST_ARCH}; scripts/verify-macos.sh requires the native architecture."
fi

# 3. Resolve the output folder and refuse to write anywhere outside the project.
case "${OUTPUT_DIR_INPUT}" in
    /*) OUTPUT_DIR_CANDIDATE="${OUTPUT_DIR_INPUT}" ;;
    *) OUTPUT_DIR_CANDIDATE="${REPO_ROOT}/${OUTPUT_DIR_INPUT}" ;;
esac
case "${OUTPUT_DIR_CANDIDATE}" in
    "${REPO_ROOT}/"*) ;;
    *) die 2 "Refusing to write the DMG outside the project root: ${OUTPUT_DIR_CANDIDATE}" ;;
esac
case "${OUTPUT_DIR_CANDIDATE}" in
    *"/../"*|*"/..") die 2 "The output folder must not contain '..': ${OUTPUT_DIR_CANDIDATE}" ;;
esac
mkdir -p -- "${OUTPUT_DIR_CANDIDATE}"
OUTPUT_DIR="$(cd -- "${OUTPUT_DIR_CANDIDATE}" && pwd -P)"
case "${OUTPUT_DIR}" in
    "${REPO_ROOT}/"*) ;;
    *) die 2 "The resolved output folder escapes the project root: ${OUTPUT_DIR}" ;;
esac

DMG_NAME="PdfDesk-macos-${BUNDLE_ARCH}.dmg"
DMG_PATH="${OUTPUT_DIR}/${DMG_NAME}"
REPORT_PATH="${OUTPUT_DIR}/PdfDesk-macos-${BUNDLE_ARCH}-dmg-build-report.txt"
REPLACED_EXISTING="no"
for existing in "${DMG_PATH}" "${REPORT_PATH}"; do
    if [[ -e "${existing}" ]]; then
        if [[ "${FORCE}" != "1" ]]; then
            die 3 "Refusing to overwrite existing file: ${existing} (move it away, or pass --force to replace it atomically)."
        fi
        REPLACED_EXISTING="yes"
    fi
done

# 4. Unique staging folder inside app/tmp. Only this folder is ever deleted.
mkdir -p -- "${STAGING_PARENT}"
STAGING_DIR="$(mktemp -d "${STAGING_PARENT}/dmg-$(date '+%Y%m%dT%H%M%S')-$$.XXXXXX")"
case "${STAGING_DIR}" in
    "${STAGING_PARENT}/"*) ;;
    *) die 1 "Unexpected staging folder: ${STAGING_DIR}" ;;
esac

cleanup() {
    local status=$?
    trap - EXIT
    if [[ -n "${STAGING_DIR:-}" && "${STAGING_DIR}" == "${STAGING_PARENT}/"* ]]; then
        if [[ "${KEEP_STAGING}" == "1" ]]; then
            echo "Staging folder kept for inspection: ${STAGING_DIR}"
        elif [[ -d "${STAGING_DIR}" ]]; then
            if ! rm -rf -- "${STAGING_DIR}"; then
                echo "Warning: could not remove the staging folder: ${STAGING_DIR}" >&2
            fi
        fi
    fi
    exit "${status}"
}
trap cleanup EXIT

# 5. Re-run the existing verifier on the completed bundle before packaging it.
echo "Verifying ${APP_BUNDLE} with scripts/verify-macos.sh ..."
VERIFY_LOG="${STAGING_DIR}/verify-macos.log"
if ! bash "${VERIFY_SCRIPT}" "${APP_BUNDLE}" 2>&1 | tee "${VERIFY_LOG}"; then
    die 1 "scripts/verify-macos.sh failed; no DMG was created. The verifier report path is printed above; re-run with --keep-staging to keep ${VERIFY_LOG}."
fi
if ! grep -q '^Result: PASS' "${VERIFY_LOG}"; then
    die 1 "scripts/verify-macos.sh did not report PASS; no DMG was created."
fi
VERIFY_REPORT="$(sed -n 's/^Verification report: //p' "${VERIFY_LOG}" | tail -n 1)"
[[ -n "${VERIFY_REPORT}" ]] || VERIFY_REPORT="not reported"

# 6. Stage the DMG content: full bundle copy, Applications symlink, Russian note.
DMG_CONTENT="${STAGING_DIR}/PdfDesk"
mkdir -p -- "${DMG_CONTENT}"
STAGED_APP="${DMG_CONTENT}/PdfDesk.app"

echo "Copying the verified bundle with ditto (bundle files, links, and metadata preserved) ..."
ditto "${APP_BUNDLE}" "${STAGED_APP}"

count_entries() { find "$1" -print | wc -l | tr -d '[:space:]'; }
count_symlinks() { find "$1" -type l -print | wc -l | tr -d '[:space:]'; }

SOURCE_ENTRIES="$(count_entries "${APP_BUNDLE}")"
STAGED_ENTRIES="$(count_entries "${STAGED_APP}")"
SOURCE_LINKS="$(count_symlinks "${APP_BUNDLE}")"
STAGED_LINKS="$(count_symlinks "${STAGED_APP}")"
if [[ "${SOURCE_ENTRIES}" != "${STAGED_ENTRIES}" || "${SOURCE_LINKS}" != "${STAGED_LINKS}" ]]; then
    die 1 "The staged copy does not match the source bundle (entries ${STAGED_ENTRIES}/${SOURCE_ENTRIES}, symlinks ${STAGED_LINKS}/${SOURCE_LINKS}); no DMG was created."
fi
if [[ ! -x "${STAGED_APP}/Contents/MacOS/PdfDesk" ]]; then
    die 1 "The staged bundle executable is missing: ${STAGED_APP}/Contents/MacOS/PdfDesk"
fi
if ! codesign --verify --strict "${STAGED_APP}" >>"${STAGING_DIR}/codesign-staged.log" 2>&1; then
    die 1 "The staged copy no longer passes signature verification; no DMG was created."
fi

ln -s /Applications "${DMG_CONTENT}/Applications"
if [[ ! -L "${DMG_CONTENT}/Applications" ]] || [[ "$(readlink "${DMG_CONTENT}/Applications")" != "/Applications" ]]; then
    die 1 "Could not create the /Applications symlink in the staging folder."
fi

# Read (never change) the signature facts of the exact copy that goes into the
# image, so the Russian note and the report state what was actually signed.
codesign --display --verbose=2 "${STAGED_APP}" >"${STAGING_DIR}/codesign-state.log" 2>&1 || true
SIGN_STATE="$(grep -E '^(Authority|Signature|TeamIdentifier)=' "${STAGING_DIR}/codesign-state.log" | tr '\n' ' ' | sed -e 's/[[:space:]]*$//' || true)"
[[ -n "${SIGN_STATE}" ]] || SIGN_STATE="could not be read"
if [[ "${SIGN_STATE}" == *"Signature=adhoc"* ]]; then
    SIGN_STATUS_RU="Приложение подписано ad hoc: локальная подпись без сертификата Developer ID."
    NOTARIZED_RU="Нотаризация Apple не выполнялась."
    GATEKEEPER_RU="Поэтому Gatekeeper может не разрешить запуск: это ожидаемое поведение сборки без Developer ID и без нотаризации."
else
    SIGN_STATUS_RU="Фактическое состояние подписи: ${SIGN_STATE}."
    NOTARIZED_RU="Нотаризация Apple этим шагом упаковки не выполнялась."
    GATEKEEPER_RU="Без нотаризации Gatekeeper может не разрешить запуск."
fi

cat > "${DMG_CONTENT}/${README_NAME}" <<README
PdfDesk — установка в macOS
===========================

1. Перетащите PdfDesk.app в папку «Программы» (Applications).
2. Запускайте программу из папки «Программы», а не из этого образа: образ
   только для чтения и не предназначен для работы прямо из него.
3. Образ нужен только для установки; после копирования его можно извлечь.

Архитектура этой сборки: ${BUNDLE_ARCH}. Универсальной (universal2) сборкой она
не является и работает на Mac с этой архитектурой.

Статус подписи (только факты)
-----------------------------
* ${SIGN_STATUS_RU}
* ${NOTARIZED_RU}
* ${GATEKEEPER_RU}
* Это локальная тестовая сборка, а не публичный релиз.

Инструкций по обходу защиты здесь нет: подпись заново не создаётся, карантин
не отключается. Для распространения нужна подпись Developer ID и нотаризация
Apple.
README

# 7. Create and verify the compressed read-only image inside the staging folder.
VOLUME_NAME="PdfDesk ${BUNDLE_ARCH}"
STAGED_DMG="${STAGING_DIR}/${DMG_NAME}"
echo "Creating the compressed read-only image ..."
hdiutil create -fs HFS+ -format UDZO -imagekey zlib-level=9 \
    -volname "${VOLUME_NAME}" -srcfolder "${DMG_CONTENT}" "${STAGED_DMG}"
if [[ ! -s "${STAGED_DMG}" ]]; then
    die 1 "hdiutil did not produce the expected image: ${STAGED_DMG}"
fi
echo "Verifying the created image ..."
if ! hdiutil verify "${STAGED_DMG}"; then
    die 1 "hdiutil verify failed; no DMG was published."
fi

# 8. Publish validated files as new files. Hard-linking is atomic and refuses to
# clobber; the copy/rename fallback only runs for a different filesystem and
# still renames inside the destination folder, never onto a half-written file.
publish_file() {
    local source="$1"
    local destination="$2"
    if [[ "${FORCE}" != "1" && -e "${destination}" ]]; then
        die 3 "Refusing to overwrite existing file: ${destination} (move it away, or pass --force to replace it atomically)."
    fi
    if ln "${source}" "${destination}" 2>/dev/null; then
        rm -f -- "${source}"
        return 0
    fi
    if [[ "${FORCE}" != "1" && -e "${destination}" ]]; then
        die 3 "Another process created ${destination}; refusing to overwrite it."
    fi
    local incoming="${destination}.incoming-$$"
    if ! cp -f "${source}" "${incoming}"; then
        rm -f -- "${incoming}"
        die 1 "Could not prepare the publication copy for ${destination}"
    fi
    if ! mv -f "${incoming}" "${destination}"; then
        rm -f -- "${incoming}"
        die 1 "Could not publish ${destination}"
    fi
    rm -f -- "${source}"
}

echo "Publishing the completed image ..."
publish_file "${STAGED_DMG}" "${DMG_PATH}"

DMG_SHA256="$(shasum -a 256 "${DMG_PATH}" | awk '{print $1}')"
DMG_SIZE="$(wc -c < "${DMG_PATH}" | tr -d '[:space:]')"
EXECUTABLE_SHA256="$(shasum -a 256 "${EXECUTABLE}" | awk '{print $1}')"
MIN_OS="$(/usr/libexec/PlistBuddy -c 'Print :LSMinimumSystemVersion' "${INFO_PLIST}" 2>/dev/null || true)"
[[ -n "${MIN_OS}" ]] || MIN_OS="not reported"

# 9. Build report: facts recorded for the tester, published next to the image.
REPORT_STAGED="${STAGING_DIR}/build-report.txt"
{
    echo "PdfDesk macOS DMG build report"
    echo "=============================="
    echo "Generated: $(date '+%Y-%m-%dT%H:%M:%S%z')"
    echo "Host: macOS $(sw_vers -productVersion) ($(sw_vers -buildVersion)) on ${HOST_ARCH}"
    echo "Project root: ${REPO_ROOT}"
    echo "Source app bundle: ${APP_BUNDLE}"
    echo "Bundle architecture: ${BUNDLE_ARCH} (single architecture, lipo -archs)"
    echo "Bundle LSMinimumSystemVersion: ${MIN_OS}"
    echo "Bundle executable: ${EXECUTABLE}"
    echo "Bundle executable SHA256: ${EXECUTABLE_SHA256}"
    echo "Bundle entries: ${SOURCE_ENTRIES} (symlinks: ${SOURCE_LINKS}); staged copy matched"
    echo "Recorded signature state of the staged copy (read only; nothing was signed or re-signed): ${SIGN_STATE}"
    echo "Pre-packaging verification: scripts/verify-macos.sh reported PASS on this Mac"
    echo "Verification report: ${VERIFY_REPORT}"
    echo "Image content: PdfDesk.app (full ditto copy), Applications symlink, ${README_NAME}"
    echo "Volume name: ${VOLUME_NAME}"
    echo "Image format: UDZO (compressed, read-only), filesystem HFS+"
    echo "hdiutil verify: PASSED"
    echo "DMG path: ${DMG_PATH}"
    echo "DMG size (bytes): ${DMG_SIZE}"
    echo "DMG SHA256 (shasum -a 256): ${DMG_SHA256}"
    echo "Existing DMG/report replaced: ${REPLACED_EXISTING}"
    echo "Rerun behaviour: without --force this script stops when these files already exist."
    echo "Network access during packaging: none (no downloads, no installs, no credentials)."
    echo
    echo "Limitations (factual):"
    echo "* Recorded signing state: ${SIGN_STATE}; this step does not notarize or re-sign."
    echo "  An ad hoc signed, not notarized build supports local testing only, and"
    echo "  Gatekeeper may refuse to launch it on other Macs."
    echo "* The verification above covers application startup and UI initialization only."
    echo "  Document workflows, printing, hardware scanning, OCR, and Office conversion"
    echo "  are not exercised by this packaging step."
    echo "* This image is for local testing on the same Mac architecture; it is not a"
    echo "  public release and must not be described as a distribution-ready build."
} > "${REPORT_STAGED}"
publish_file "${REPORT_STAGED}" "${REPORT_PATH}"

echo
echo "DMG created: ${DMG_PATH}"
echo "DMG SHA256: ${DMG_SHA256}"
echo "Build report: ${REPORT_PATH}"
echo "Recorded signing state: ${SIGN_STATE}"
echo "This image is not notarized and is for local testing only; it is not a distribution build."
