#!/bin/bash
# Build the PdfDesk macOS scanner helper from app/native/MacScanner.swift.
#
# macOS ONLY: the helper links ImageCaptureCore and must be compiled on the
# architecture(s) it will run on. This script refuses to run anywhere else, and
# nothing in the Python adapter ever builds the helper off macOS.
#
# Usage:
#   app/native/build_mac_scanner.sh [--universal] [--arch x86_64|arm64] [--output PATH]
#
# Defaults: host architecture, output app/native/build/MacScanner, deployment
# target macOS 12.0. See BUILD-mac-scanner.md for packaging and signing notes.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SOURCE="${SCRIPT_DIR}/MacScanner.swift"
OUTPUT="${SCRIPT_DIR}/build/MacScanner"
HOST_ARCH="$(uname -m)"
TARGET_ARCH="${HOST_ARCH}"
UNIVERSAL=0
MACOS_MIN="${PDFDESK_MACOS_MIN:-12.0}"
SWIFT_VERSION="5"

while [ $# -gt 0 ]; do
    case "$1" in
        --universal) UNIVERSAL=1; shift ;;
        --arch) TARGET_ARCH="$2"; shift 2 ;;
        --output) OUTPUT="$2"; shift 2 ;;
        -h|--help) sed -n '2,12p' "$0"; exit 0 ;;
        *) echo "Unknown option: $1" >&2; exit 2 ;;
    esac
done

if [ "$(uname -s)" != "Darwin" ]; then
    echo "MacScanner can only be built on macOS: ImageCaptureCore is a macOS framework." >&2
    exit 1
fi
if [ ! -f "${SOURCE}" ]; then
    echo "Swift source not found: ${SOURCE}" >&2
    exit 1
fi
if ! command -v swiftc >/dev/null 2>&1; then
    echo "swiftc not found. Install the Xcode Command Line Tools: xcode-select --install" >&2
    exit 1
fi

frameworks=(-framework ImageCaptureCore -framework ImageIO -framework CoreGraphics)
common=(-O -parse-as-library -swift-version "${SWIFT_VERSION}" "${frameworks[@]}")

mkdir -p "$(dirname "${OUTPUT}")"

compile_slice() {
    local arch="$1"
    local destination="$2"
    echo "Compiling MacScanner for ${arch} (macOS ${MACOS_MIN}+)"
    swiftc "${common[@]}" -target "${arch}-apple-macos${MACOS_MIN}" -o "${destination}" "${SOURCE}"
}

if [ "${UNIVERSAL}" -eq 1 ]; then
    staging="$(mktemp -d)"
    trap 'rm -rf "${staging}"' EXIT
    compile_slice "arm64" "${staging}/MacScanner.arm64"
    compile_slice "x86_64" "${staging}/MacScanner.x86_64"
    if ! command -v lipo >/dev/null 2>&1; then
        echo "lipo not found; cannot create a universal binary." >&2
        exit 1
    fi
    lipo -create -output "${OUTPUT}" "${staging}/MacScanner.arm64" "${staging}/MacScanner.x86_64"
    rm -rf "${staging}"
    trap - EXIT
else
    case "${TARGET_ARCH}" in
        arm64|x86_64) ;;
        *) echo "Unsupported architecture: ${TARGET_ARCH} (use arm64, x86_64 or --universal)" >&2; exit 2 ;;
    esac
    compile_slice "${TARGET_ARCH}" "${OUTPUT}"
fi

chmod 755 "${OUTPUT}"

if command -v file >/dev/null 2>&1; then
    file "${OUTPUT}"
fi
if command -v lipo >/dev/null 2>&1; then
    lipo -archs "${OUTPUT}" || true
fi

# Nested executables must carry a valid signature to launch on Apple Silicon and
# to be notarised with the application. Ad-hoc signing is enough for local runs;
# set PDFDESK_CODESIGN_IDENTITY for a Developer ID signature.
if command -v codesign >/dev/null 2>&1; then
    identity="${PDFDESK_CODESIGN_IDENTITY:--}"
    echo "Signing MacScanner with identity: ${identity}"
    codesign --force --timestamp=none --sign "${identity}" "${OUTPUT}"
    codesign --verify --verbose=2 "${OUTPUT}" || true
fi

echo "Built ${OUTPUT}"
