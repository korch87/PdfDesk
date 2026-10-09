#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd)"
APP_DIR="${REPO_ROOT}/app"
PYTHON_BIN="${PYTHON_BIN:-python3.12}"

if [[ "$(uname -s)" != "Darwin" ]]; then
    echo "This build must run on macOS; cross-building is not supported." >&2
    exit 2
fi
if ! command -v "${PYTHON_BIN}" >/dev/null 2>&1; then
    echo "Python 3.12 was not found. Set PYTHON_BIN to a Python 3.12 executable." >&2
    exit 2
fi
if ! xcode-select -p >/dev/null 2>&1; then
    echo "Xcode Command Line Tools are required; install/select Xcode first." >&2
    exit 2
fi

HOST_ARCH="$(uname -m)"
case "${HOST_ARCH}" in
    arm64|x86_64) ;;
    *) echo "Unsupported Mac architecture: ${HOST_ARCH}" >&2; exit 2 ;;
esac
PYTHON_ARCH="$("${PYTHON_BIN}" -c 'import platform; print(platform.machine())')"
PYTHON_VERSION="$("${PYTHON_BIN}" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
if [[ "${PYTHON_VERSION}" != "3.12" ]]; then
    echo "Python 3.12 is required for this pinned Mac build (found ${PYTHON_VERSION})." >&2
    exit 2
fi
if [[ "${PYTHON_ARCH}" != "${HOST_ARCH}" ]]; then
    echo "Python architecture ${PYTHON_ARCH} does not match this build host ${HOST_ARCH}." >&2
    echo "Use a native Python install; do not run an Intel build under Rosetta on Apple Silicon." >&2
    exit 2
fi

# PyInstaller packages this executable into Contents/Frameworks/native. Build it
# first on the selected host architecture; ImageCaptureCore cannot be cross-built.
bash "${APP_DIR}/native/build_mac_scanner.sh" --arch "${HOST_ARCH}"
SCANNER_HELPER="${APP_DIR}/native/build/MacScanner"
if [[ ! -x "${SCANNER_HELPER}" ]]; then
    echo "The compiled MacScanner helper is missing or not executable: ${SCANNER_HELPER}" >&2
    exit 1
fi

BUILD_ROOT="${APP_DIR}/tmp/macos-build-${HOST_ARCH}"
VENV_DIR="${BUILD_ROOT}/venv"
DIST_DIR="${APP_DIR}/dist-macos/${HOST_ARCH}"
WORK_DIR="${BUILD_ROOT}/pyinstaller-work"
mkdir -p "${BUILD_ROOT}" "${DIST_DIR}"
if [[ ! -x "${VENV_DIR}/bin/python" ]]; then
    "${PYTHON_BIN}" -m venv "${VENV_DIR}"
fi
BUILD_PYTHON="${VENV_DIR}/bin/python"
"${BUILD_PYTHON}" -m pip install --disable-pip-version-check -r "${APP_DIR}/requirements-macos.txt"
"${BUILD_PYTHON}" -c 'import PySide6, pypdfium2, pypdf, reportlab, PIL, cryptography; print("Runtime imports OK")'
export PDFDESK_LICENSES_DIR="${BUILD_ROOT}/licenses"
"${BUILD_PYTHON}" "${REPO_ROOT}/scripts/collect-licenses.py" "${PDFDESK_LICENSES_DIR}"

cd "${APP_DIR}"
"${BUILD_PYTHON}" -m PyInstaller --noconfirm --clean \
    --distpath "${DIST_DIR}" --workpath "${WORK_DIR}" PdfDesk-macos.spec

APP_BUNDLE="${DIST_DIR}/PdfDesk.app"
EXECUTABLE="${APP_BUNDLE}/Contents/MacOS/PdfDesk"
BUNDLED_SCANNER="${APP_BUNDLE}/Contents/Frameworks/native/MacScanner"
if [[ ! -x "${EXECUTABLE}" ]]; then
    echo "PyInstaller did not produce the expected app executable: ${EXECUTABLE}" >&2
    exit 1
fi
if [[ ! -x "${BUNDLED_SCANNER}" ]]; then
    echo "PyInstaller did not preserve the scanner helper at ${BUNDLED_SCANNER}" >&2
    exit 1
fi
if ! lipo -archs "${EXECUTABLE}" | tr ' ' '\n' | grep -Fxq "${HOST_ARCH}"; then
    echo "The generated app executable does not contain host architecture ${HOST_ARCH}." >&2
    exit 1
fi
if ! lipo -archs "${BUNDLED_SCANNER}" | tr ' ' '\n' | grep -Fxq "${HOST_ARCH}"; then
    echo "The bundled scanner helper does not contain host architecture ${HOST_ARCH}." >&2
    exit 1
fi
SIGN_IDENTITY="${PDFDESK_CODESIGN_IDENTITY:--}"
# Sign the modified nested helper first, then seal the completed outer bundle.
# Do not use --deep: it can rewrite/sign third-party nested code with an
# identity or entitlements that were not selected for those components.
codesign --force --timestamp=none --sign "${SIGN_IDENTITY}" "${BUNDLED_SCANNER}"
codesign --force --timestamp=none --sign "${SIGN_IDENTITY}" "${APP_BUNDLE}"

echo "Built native ${HOST_ARCH} bundle: ${APP_BUNDLE}"
bash "${SCRIPT_DIR}/verify-macos.sh" "${APP_BUNDLE}"
