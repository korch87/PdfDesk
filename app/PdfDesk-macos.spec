"""Native-architecture, onedir PdfDesk.app bundle for macOS 12+.

Build this spec on the target architecture's Mac. PySide's wheel is universal2,
but the bundled PDFium library is selected for the build interpreter's host arch.
"""
from pathlib import Path
import os

from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs


root = Path(SPECPATH).resolve()
scanner_helper = root / "native" / "build" / "MacScanner"
if not scanner_helper.is_file():
    raise SystemExit(
        "Compiled scanner helper not found. Run scripts/build-macos.sh first."
    )
a = Analysis(
    [str(root / "main.py")],
    pathex=[str(root)],
    binaries=collect_dynamic_libs("pypdfium2_raw") + [(str(scanner_helper), "native")],
    datas=[
        (str(root / "assets"), "assets"),
        (os.environ.get('PDFDESK_LICENSES_DIR', str(root / "LICENSES")), "LICENSES"),
    ] + collect_data_files("pypdfium2"),
    hiddenimports=[
        "src.canvas", "src.pdf_backend", "src.theme", "src.recovery",
        "src.editor_window", "src.editor_theme", "src.editor_icons",
        "src.editor_canvas", "src.document_view", "src.content_editor",
        "src.document_import", "src.source_session", "src.source_edit_ui",
        "src.form_editor", "src.form_author_ui", "src.pdf_text",
        "src.text_selection", "src.stamp_library", "src.stamp_library_ui",
        "src.pdf_security", "src.security_dialog", "src.scanner_service",
        "src.scanner_dialog", "src.ocr_service", "src.ocr_dialog",
        "src.ocr_apply", "src.ocr_correction", "src.ocr_correction_dialog",
        "src.shape_geometry", "src.mac_scanner", "src.view_rotation", "src.interaction_defaults",
        "src.annotation_geometry", "src.nested_content", "src.nested_text_editor", "src.guide_dialog",
    ],
    hookspath=[], hooksconfig={}, runtime_hooks=[], excludes=[], noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, [], exclude_binaries=True, name="PdfDesk",
    debug=False, strip=False, upx=False, console=False,
)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="PdfDesk")
app = BUNDLE(
    coll,
    name="PdfDesk.app",
    bundle_identifier="com.pdfdesk.PdfDesk",
    info_plist={
        "CFBundleDisplayName": "PdfDesk",
        "CFBundleName": "PdfDesk",
        "CFBundleShortVersionString": "0.3.0",
        "LSMinimumSystemVersion": "12.0",
        "NSHighResolutionCapable": True,
    },
)
