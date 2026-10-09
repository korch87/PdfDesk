#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd)"
APP_DIR="${REPO_ROOT}/app"
HOST_ARCH="$(uname -m)"
case "${HOST_ARCH}" in
    arm64|x86_64) ;;
    *) echo "Unsupported Mac architecture: ${HOST_ARCH}" >&2; exit 2 ;;
esac

APP_BUNDLE="${1:-${APP_DIR}/dist-macos/${HOST_ARCH}/PdfDesk.app}"
APP_BUNDLE="$(cd -- "$(dirname -- "${APP_BUNDLE}")" && pwd)/$(basename -- "${APP_BUNDLE}")"
EXECUTABLE="${APP_BUNDLE}/Contents/MacOS/PdfDesk"
SCANNER_HELPER="${APP_BUNDLE}/Contents/Frameworks/native/MacScanner"
if [[ ! -d "${APP_BUNDLE}" || ! -x "${EXECUTABLE}" ]]; then
    echo "PdfDesk.app or its executable is missing: ${APP_BUNDLE}" >&2
    exit 2
fi
if [[ ! -x "${SCANNER_HELPER}" ]]; then
    echo "The expected nested scanner helper is missing: ${SCANNER_HELPER}" >&2
    exit 2
fi

RUN_ID="$(date '+%Y%m%dT%H%M%S')-$$"
VERIFY_ROOT="${APP_DIR}/tmp/mac-verification/${HOST_ARCH}/${RUN_ID}"
mkdir -p "${VERIFY_ROOT}/cache" "${VERIFY_ROOT}/config" \
    "${VERIFY_ROOT}/data" "${VERIFY_ROOT}/recovery" \
    "${VERIFY_ROOT}/tmp"
REPORT="${VERIFY_ROOT}/smoke-report.json"
LOG="${VERIFY_ROOT}/smoke.log"
SUMMARY="${VERIFY_ROOT}/verification-report.txt"

fail() {
    echo "FAIL: $*" | tee -a "${SUMMARY}" >&2
    echo "Log: ${LOG}" >&2
    exit 1
}
record() {
    echo "$*" | tee -a "${SUMMARY}"
}

{
    echo "PdfDesk macOS verification"
    echo "Date: $(date '+%Y-%m-%dT%H:%M:%S%z')"
    echo "Host architecture: ${HOST_ARCH}"
    echo "App bundle: ${APP_BUNDLE}"
} > "${SUMMARY}"

for binary in "${EXECUTABLE}" "${SCANNER_HELPER}"; do
    if ! lipo -archs "${binary}" | tr ' ' '\n' | grep -Fxq "${HOST_ARCH}"; then
        fail "Binary does not contain host architecture ${HOST_ARCH}: ${binary}"
    fi
done
MIN_OS="$(/usr/libexec/PlistBuddy -c 'Print :LSMinimumSystemVersion' "${APP_BUNDLE}/Contents/Info.plist")"
if [[ "${MIN_OS}" != "12.0" ]]; then
    fail "Expected LSMinimumSystemVersion 12.0, found ${MIN_OS}"
fi
record "Bundle minimum OS: ${MIN_OS}"

# Verify each code signature explicitly. The outer app was signed after the
# nested helper, so a deep/recursive signature rewrite is neither needed nor
# safe for third-party Qt/Python code.
if ! codesign --verify --strict --verbose=2 "${SCANNER_HELPER}" >>"${LOG}" 2>&1; then
    fail "Nested MacScanner signature verification failed"
fi
if ! codesign --verify --strict --verbose=2 "${APP_BUNDLE}" >>"${LOG}" 2>&1; then
    fail "PdfDesk.app signature verification failed"
fi
record "Code signatures: nested helper and outer app verified"

find_office() {
    local candidate
    for candidate in soffice libreoffice; do
        if command -v "${candidate}" >/dev/null 2>&1; then command -v "${candidate}"; return; fi
    done
    candidate="/Applications/LibreOffice.app/Contents/MacOS/soffice"
    if [[ -x "${candidate}" ]]; then echo "${candidate}"; return; fi
    echo "unavailable"
}

find_tesseract() {
    local candidate
    for candidate in "${PDFDESK_TESSERACT:-}" "${TESSERACT_CMD:-}"; do
        if [[ -n "${candidate}" && -x "${candidate}" ]]; then echo "${candidate}"; return; fi
    done
    if command -v tesseract >/dev/null 2>&1; then command -v tesseract; return; fi
    for candidate in /opt/homebrew/bin/tesseract /usr/local/bin/tesseract; do
        if [[ -x "${candidate}" ]]; then echo "${candidate}"; return; fi
    done
    echo "unavailable"
}

has_traineddata() {
    local folder="$1" model
    [[ -d "${folder}" ]] || return 1
    for model in "${folder}"/*.traineddata; do
        [[ -f "${model}" ]] && return 0
    done
    return 1
}

find_tessdata() {
    local candidate executable_dir
    for candidate in "${PDFDESK_TESSDATA_DIR:-}" "${TESSDATA_PREFIX:-}"; do
        [[ -n "${candidate}" ]] || continue
        if has_traineddata "${candidate}"; then echo "${candidate}"; return; fi
        if has_traineddata "${candidate}/tessdata"; then echo "${candidate}/tessdata"; return; fi
    done
    if [[ "${TESSERACT}" != "unavailable" ]]; then
        executable_dir="$(cd -- "$(dirname -- "${TESSERACT}")" && pwd)"
        for candidate in "${executable_dir}/tessdata" "${executable_dir}" \
            "${executable_dir}/../share/tessdata"; do
            if has_traineddata "${candidate}"; then echo "${candidate}"; return; fi
        done
    fi
    echo ""
}

OFFICE="$(find_office)"
TESSERACT="$(find_tesseract)"
if [[ "${OFFICE}" == "unavailable" ]]; then
    record "Office conversion: unavailable (optional; local LibreOffice not found)"
else
    record "Office conversion: detected ${OFFICE} (runtime conversion not exercised)"
fi
if [[ "${TESSERACT}" == "unavailable" ]]; then
    record "OCR: unavailable (optional; local Tesseract not found)"
else
    record "OCR engine: detected ${TESSERACT}"
    LANG_OUTPUT="${VERIFY_ROOT}/tesseract-languages.txt"
    TESSDATA_DIR="$(find_tessdata)"
    if [[ -n "${TESSDATA_DIR}" ]]; then
        record "OCR traineddata folder: ${TESSDATA_DIR}"
        if ! "${TESSERACT}" --list-langs --tessdata-dir "${TESSDATA_DIR}" >"${LANG_OUTPUT}" 2>&1; then
            record "OCR language data: unavailable (Tesseract could not list local traineddata)"
            LANG_OUTPUT=""
        fi
    elif ! "${TESSERACT}" --list-langs >"${LANG_OUTPUT}" 2>&1; then
        record "OCR language data: unavailable (Tesseract could not list local traineddata)"
        LANG_OUTPUT=""
    fi
    if [[ -n "${LANG_OUTPUT}" ]]; then
        missing=()
        for language in rus eng ukr pol kat srp srp_latn mlt; do
            if ! awk -v wanted="${language}" '$0 == wanted { found=1 } END { exit !found }' "${LANG_OUTPUT}"; then
                missing+=("${language}")
            fi
        done
        if (( ${#missing[@]} == 0 )); then
            record "OCR language data: all app-requestable languages detected"
        else
            record "OCR language data: missing ${missing[*]} (install matching local traineddata to enable those languages)"
        fi
    fi
fi

record "Smoke report: ${REPORT}"
record "Smoke log: ${LOG}"
if ! env \
    TMPDIR="${VERIFY_ROOT}/tmp" \
    XDG_CACHE_HOME="${VERIFY_ROOT}/cache" \
    XDG_CONFIG_HOME="${VERIFY_ROOT}/config" \
    XDG_DATA_HOME="${VERIFY_ROOT}/data" \
    PDFDESK_SETTINGS="${VERIFY_ROOT}/settings.ini" \
    PDFDESK_RECOVERY_DIR="${VERIFY_ROOT}/recovery" \
    PDFDESK_SMOKE_REPORT="${REPORT}" \
    QT_QPA_PLATFORM="offscreen" \
    "${EXECUTABLE}" --smoke-test >"${LOG}" 2>&1; then
    fail "Packaged executable smoke launch failed (see smoke.log)"
fi
if [[ ! -s "${REPORT}" ]]; then
    fail "Packaged executable exited without writing its smoke report"
fi
if ! python3 - "${REPORT}" <<'PY'
import json
import sys

try:
    with open(sys.argv[1], encoding="utf-8") as stream:
        report = json.load(stream)
except Exception as exc:
    print(f"Invalid smoke report: {exc}", file=sys.stderr)
    raise SystemExit(1)
errors = report.get("errors")
if not isinstance(errors, list) or errors:
    print(f"Smoke report has errors or no error list: {errors!r}", file=sys.stderr)
    raise SystemExit(1)
if not isinstance(report.get("page_count"), int) or not isinstance(report.get("canvas"), str):
    print("Smoke report is missing page_count or canvas", file=sys.stderr)
    raise SystemExit(1)
print(f"UI smoke passed: {report['page_count']} pages, canvas={report['canvas']}")
PY
then
    fail "Smoke report validation failed (see smoke-report.json and smoke.log)"
fi
cat "${LOG}" >> "${SUMMARY}"
record "Result: PASS (application startup/UI smoke only; hardware scanning and external engine workflows not exercised)"
echo "Verification report: ${SUMMARY}"
