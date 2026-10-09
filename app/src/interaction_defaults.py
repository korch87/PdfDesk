"""Small cross-platform Qt interaction defaults shared by application startup."""
from __future__ import annotations

from PySide6.QtWidgets import QApplication, QProxyStyle, QStyle


TOOLTIP_WAKE_DELAY_MS = 1250


class ToolTipDelayStyle(QProxyStyle):
    """Preserve the active style while using PdfDesk's tooltip wake delay."""

    def styleHint(self, hint, option=None, widget=None, returnData=None):  # noqa: N802 - Qt API
        if hint == QStyle.StyleHint.SH_ToolTip_WakeUpDelay:
            return TOOLTIP_WAKE_DELAY_MS
        return super().styleHint(hint, option, widget, returnData)


def install_tooltip_delay(application: QApplication) -> ToolTipDelayStyle:
    """Install an application-wide 1.25-second tooltip delay, idempotently."""
    # QApplication.style() may be Qt's private QStyleSheetStyle wrapper after
    # any stylesheet has been applied. Keep the proxy on the QApplication
    # wrapper so later calls can find it without wrapping that private style
    # again. The app owns the native style; this reference only preserves the
    # Python wrapper and provides a stable idempotence key.
    existing = getattr(application, "_pdfdesk_tooltip_delay_style", None)
    if isinstance(existing, ToolTipDelayStyle):
        return existing

    current = application.style()
    if isinstance(current, ToolTipDelayStyle):
        style = current
    else:
        # QProxyStyle takes ownership of the active style. Qt's stylesheet
        # machinery may wrap this proxy again when stylesheets are later set;
        # the QApplication attribute above prevents wrapping that wrapper.
        style = ToolTipDelayStyle(current)
        application.setStyle(style)
    setattr(application, "_pdfdesk_tooltip_delay_style", style)
    return style
