"""Entry point for PdfDesk on Windows and macOS."""
import os
import sys
import traceback
import json
from pathlib import Path


def main():
    from PySide6.QtCore import QTimer
    from PySide6.QtGui import QIcon, QFont, QFontDatabase
    from PySide6.QtWidgets import QApplication, QMessageBox
    app = QApplication(sys.argv)
    app.setApplicationName("PdfDesk")
    app.setOrganizationName("PdfDesk")
    app.setStyle("Fusion")
    from src.interaction_defaults import install_tooltip_delay
    install_tooltip_delay(app)
    # Sandboxed Windows launches can lack registered system font families.
    if os.name == 'nt':
        font_path = Path(os.environ.get('WINDIR', 'C:/Windows')) / 'Fonts' / 'segoeui.ttf'
        if font_path.is_file():
            QFontDatabase.addApplicationFont(str(font_path))
        app.setFont(QFont('Segoe UI', 10))
    smoke = '--smoke-test' in sys.argv
    smoke_errors = []
    if smoke:
        QMessageBox.critical = lambda *args: smoke_errors.append(str(args[-1]))
    from src.editor_window import EditorWindow as MainWindow
    window = MainWindow()
    asset_root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    icon = asset_root / "assets" / "pdfdesk.ico"
    if icon.exists():
        app.setWindowIcon(QIcon(str(icon)))
    window.show()
    for value in sys.argv[1:]:
        if value.lower().endswith(".pdf"):
            window.open_path(value)
            break
    if smoke:
        def finish_check():
            report = {'page_count': window.page_count,
                      'canvas': type(window._canvas).__name__,
                      'errors': smoke_errors}
            if os.environ.get('PDFDESK_SMOKE_SOURCE') == '1':
                try:
                    from src.source_session import SourceContentSession
                    session = window._session
                    fields_before = {f.key: f.value for f in session.form_fields()}
                    window.set_mode('edit')
                    window.set_tool('content')
                    blocks = SourceContentSession.inspect(session, 0)
                    block = next(b for b in blocks if b.kind == 'text')
                    SourceContentSession.apply(session, 0, block.id, 'replace',
                                               text='PdfDesk smoke TEST', font_name='Arial',
                                               font_size=12, width=120)
                    assert 'PdfDesk smoke TEST' in ' '.join(session.page_text(0).split())
                    assert {f.key: f.value for f in session.form_fields()} == fields_before
                    assert session.undo()
                    assert 'PdfDesk smoke TEST' not in ' '.join(session.page_text(0).split())
                    report['source_edit_and_undo'] = True
                    report['source_blocks'] = len(blocks)
                    report['form_fields'] = len(fields_before)
                except Exception:
                    smoke_errors.append(traceback.format_exc())
            if os.environ.get('PDFDESK_SMOKE_NATIVE_CREATE') == '1':
                try:
                    import io
                    from PIL import Image
                    from src.pdf_backend import PdfSession
                    from src.source_session import SourceContentSession
                    from src.annotation_geometry import AnnotationGeometryDialog
                    from src.contracts import Annotation
                    from src.pdf_security import protect_pdf, inspect_pdf_protection
                    from src.ocr_apply import apply_ocr_results
                    from src.ocr_service import OCRWord, OCRResult, create_searchable_overlay_pdf
                    session = PdfSession()
                    try:
                        session.new(300, 400)
                        SourceContentSession.add_text(session, 0, 'Native Unicode: Русский',
                                                      (30, 290, 240, 60))
                        image = io.BytesIO()
                        Image.new('RGBA', (40, 40), (20, 150, 170, 170)).save(image, format='PNG')
                        SourceContentSession.add_image(session, 0, image.getvalue(), (30, 150, 50, 50))
                        assert 'Русский' in session.page_text(0)
                        words = (OCRWord('Recognized', (30, 40, 100, 20), 99),)
                        layer = create_searchable_overlay_pdf(words, (600, 800), dpi=144)
                        apply_ocr_results(session, {0: OCRResult(words, (600, 800), ('eng',), layer)})
                        assert 'Recognized' in session.page_text(0)
                        encrypted = protect_pdf(session._serialize(), 'reader', 'owner')
                        assert inspect_pdf_protection(encrypted, 'reader')['encrypted']
                        session.open_bytes(encrypted, password='reader')
                        assert session.render(0, include_annotations=True).width > 0
                        report['native_creation_ocr_layer_encrypted_render'] = True
                        placement = AnnotationGeometryDialog(Annotation(
                            'text', x=10, y=20, width=100, height=40, text='Placement'), window)
                        placement.inputs['x'].setValue(25.4)
                        assert abs(placement.result_annotation().x - 72) < .001
                        placement.deleteLater()
                        report['precise_annotation_geometry'] = True
                    finally:
                        session.close()
                except Exception:
                    smoke_errors.append(traceback.format_exc())
            nested_text_input = os.environ.get('PDFDESK_SMOKE_NESTED_TEXT_FILE')
            if nested_text_input:
                try:
                    from src.pdf_backend import PdfSession
                    from src.source_session import SourceContentSession
                    from src.guide_dialog import GuideDialog
                    nested_text = PdfSession()
                    try:
                        nested_text.open(nested_text_input)
                        before = nested_text.render(0).tobytes()
                        sibling = nested_text.render(1).tobytes()
                        occurrence = max(SourceContentSession.inspect_form_occurrences(nested_text, 0),
                                         key=lambda item: len(item.path))
                        target = next(item for item in SourceContentSession.inspect_nested(
                            nested_text, 0, occurrence.identity)
                            if item.editable and item.local_block.kind == 'text')
                        SourceContentSession.apply_nested(nested_text, target, 'replace',
                            text='Frozen Москва', font_name='Arial', font_size=12, width=140)
                        assert 'Москва' in nested_text.page_text(0)
                        assert nested_text.render(1).tobytes() == sibling
                        assert nested_text.undo()
                        assert nested_text.render(0).tobytes() == before
                        report['nested_native_text_and_undo'] = True
                        guides = GuideDialog((300, 400), [('v', 72.0)], parent=window)
                        guides.deleteLater()
                        report['precise_guides_module'] = True
                    finally:
                        nested_text.close()
                except Exception:
                    smoke_errors.append(traceback.format_exc())
            nested_input = os.environ.get('PDFDESK_SMOKE_NESTED_FILE')
            if nested_input:
                try:
                    from src.pdf_backend import PdfSession
                    from src.source_session import SourceContentSession
                    from src.nested_content import page_delta_to_form_parent
                    nested = PdfSession()
                    try:
                        nested.open(nested_input)
                        sibling = nested.render(1).tobytes()
                        occurrence = next(item for item in SourceContentSession.inspect_form_occurrences(nested, 0)
                                          if len(item.path) > 1)
                        dx, dy = page_delta_to_form_parent(occurrence, 12, 0)
                        SourceContentSession.translate_form_occurrence(nested, 0, occurrence.identity, dx, dy)
                        assert nested.render(1).tobytes() == sibling
                        assert nested.undo()
                        report['nested_group_edit_and_undo'] = True
                    finally:
                        nested.close()
                except Exception:
                    smoke_errors.append(traceback.format_exc())
            ink_input = os.environ.get('PDFDESK_SMOKE_INK_FILE')
            if ink_input:
                try:
                    import copy
                    from src.pdf_backend import PdfSession
                    from src.document_view import DocumentView
                    ink_session = PdfSession()
                    ink_view = None
                    try:
                        ink_session.open(ink_input)
                        index, ink = next((i, item) for i, item in
                            enumerate(ink_session.get_annotations(0))
                            if item.kind == 'ink' and item.stroke_lengths)
                        lengths = list(ink.stroke_lengths)
                        changed = copy.deepcopy(ink)
                        changed.color = '#C0392B'
                        ink_session.update_annotation(0, index, changed)
                        ink_session.open_bytes(ink_session._serialize_for_render())
                        reopened = next(item for item in ink_session.get_annotations(0)
                                        if item.kind == 'ink' and item.stroke_lengths)
                        assert reopened.stroke_lengths == lengths
                        assert reopened.color.upper() == '#C0392B'
                        ink_view = DocumentView(ink_session)
                        ink_view.canvases[0].set_guides([('v', 25.0)], True)
                        ink_session.rotate(0)
                        ink_view.reload()
                        assert ink_view.canvases[0].guides == [('h', 25.0)]
                        assert ink_session.undo()
                        ink_view.reload()
                        assert ink_view.canvases[0].guides == [('v', 25.0)]
                        report['multistroke_ink_roundtrip'] = lengths
                        report['guide_saved_rotation_and_undo'] = True
                    finally:
                        if ink_view is not None:
                            ink_view.close()
                        ink_session.close()
                except Exception:
                    smoke_errors.append(traceback.format_exc())
            if os.environ.get('PDFDESK_SMOKE_ENGINES') == '1':
                try:
                    from PIL import Image, ImageDraw, ImageFont
                    from src.ocr_service import TesseractOCRService, LANGUAGES
                    from src.document_import import DocumentImportService
                    from src.pdf_backend import PdfSession
                    engine = TesseractOCRService()
                    assert engine.available
                    assert set(LANGUAGES).issubset(engine.installed_languages())
                    sample = Image.new('RGB', (1200, 230), 'white')
                    from src.pdf_backend import find_font_file
                    font = ImageFont.truetype(find_font_file('Arial'), 70)
                    ImageDraw.Draw(sample).text((40, 50), 'Hello Москва', font=font, fill='black')
                    result = engine.recognize(sample, ('eng', 'rus'), dpi=300)
                    text = ' '.join(word.text for word in result.words)
                    assert 'Hello' in text and 'Москва' in text, text
                    office_input = os.environ.get('PDFDESK_SMOKE_OFFICE_FILE')
                    if office_input:
                        converted = DocumentImportService().convert_to_pdf(office_input)
                        session = PdfSession()
                        try:
                            session.open_bytes(converted)
                            assert session.page_count > 0
                            assert session.page_text(0).strip()
                            report['office_pages'] = session.page_count
                        finally:
                            session.close()
                    report['real_bundled_ocr'] = text
                    report['ocr_languages'] = sorted(engine.installed_languages())
                except Exception:
                    smoke_errors.append(traceback.format_exc())
            report_path = os.environ.get('PDFDESK_SMOKE_REPORT')
            if report_path:
                Path(report_path).write_text(json.dumps(report, ensure_ascii=False),encoding='utf-8')
            app.exit(1 if smoke_errors else 0)
        QTimer.singleShot(1500, finish_check)
    return app.exec()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        error = traceback.format_exc()
        report_path = os.environ.get('PDFDESK_SMOKE_REPORT')
        if '--smoke-test' in sys.argv:
            if report_path:
                Path(report_path).write_text(error, encoding='utf-8')
            sys.exit(1)
        import tempfile
        log = Path(tempfile.gettempdir()) / "PdfDesk-error.log"
        log.write_text(error, encoding="utf-8")
        from PySide6.QtWidgets import QApplication, QMessageBox
        app = QApplication.instance() or QApplication([])
        QMessageBox.critical(None, "PdfDesk", f"Не удалось запустить приложение.\nПодробности сохранены в {log}")
        sys.exit(1)
