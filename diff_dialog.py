"""
Side-by-side diff view for source vs translated XLF content.

Changed lines are highlighted:
  red background  — line exists only in source (removed / modified original)
  green background — line exists only in translated (added / modified target)
"""
from __future__ import annotations

import difflib

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont, QTextCharFormat, QTextCursor
from PySide6.QtWidgets import (
    QDialog, QHBoxLayout, QLabel, QPlainTextEdit,
    QPushButton, QSplitter, QTextEdit, QVBoxLayout,
)


class DiffDialog(QDialog):
    def __init__(
        self,
        source_xml: str,
        translated_xml: str,
        parent=None,
    ):
        super().__init__(parent)
        self.setWindowTitle("XLF — Source vs Translated")
        self.resize(1400, 820)
        self._build_ui(source_xml, translated_xml)

    # ── UI ────────────────────────────────────────────────────────────────────

    def _build_ui(self, source_xml: str, translated_xml: str) -> None:
        layout = QVBoxLayout(self)
        layout.setSpacing(6)

        # Column headers
        hdr = QHBoxLayout()
        hdr.addWidget(QLabel("<b>Source (original)</b>"), stretch=1)
        hdr.addWidget(QLabel("<b>Translated</b>"), stretch=1)
        layout.addLayout(hdr)

        # Editors
        mono = QFont("Courier New", 10)
        mono.setStyleHint(QFont.Monospace)

        self._left = QPlainTextEdit()
        self._left.setReadOnly(True)
        self._left.setLineWrapMode(QPlainTextEdit.NoWrap)
        self._left.setFont(mono)

        self._right = QPlainTextEdit()
        self._right.setReadOnly(True)
        self._right.setLineWrapMode(QPlainTextEdit.NoWrap)
        self._right.setFont(mono)

        splitter = QSplitter(Qt.Horizontal)
        splitter.addWidget(self._left)
        splitter.addWidget(self._right)
        layout.addWidget(splitter, stretch=1)

        # Synchronized scrolling (vertical + horizontal)
        self._left.verticalScrollBar().valueChanged.connect(
            self._right.verticalScrollBar().setValue
        )
        self._right.verticalScrollBar().valueChanged.connect(
            self._left.verticalScrollBar().setValue
        )
        self._left.horizontalScrollBar().valueChanged.connect(
            self._right.horizontalScrollBar().setValue
        )
        self._right.horizontalScrollBar().valueChanged.connect(
            self._left.horizontalScrollBar().setValue
        )

        # Legend + close button
        bottom = QHBoxLayout()
        legend = QLabel(
            "<span style='background:#FFCCCC;padding:0 6px'>&nbsp;&nbsp;</span>"
            " Source-only line &nbsp;&nbsp;"
            "<span style='background:#CCFFCC;padding:0 6px'>&nbsp;&nbsp;</span>"
            " Translated-only line"
        )
        legend.setTextFormat(Qt.RichText)
        bottom.addWidget(legend, stretch=1)
        btn_close = QPushButton("Close")
        btn_close.setFixedWidth(90)
        btn_close.clicked.connect(self.accept)
        bottom.addWidget(btn_close)
        layout.addLayout(bottom)

        self._populate(source_xml, translated_xml)

    # ── diff computation ──────────────────────────────────────────────────────

    def _populate(self, source_xml: str, translated_xml: str) -> None:
        src_lines = source_xml.splitlines()
        tgt_lines = translated_xml.splitlines()

        self._left.setPlainText(source_xml)
        self._right.setPlainText(translated_xml)

        fmt_left = QTextCharFormat()
        fmt_left.setBackground(QColor("#FFCCCC"))

        fmt_right = QTextCharFormat()
        fmt_right.setBackground(QColor("#CCFFCC"))

        left_sels:  list[QTextEdit.ExtraSelection] = []
        right_sels: list[QTextEdit.ExtraSelection] = []

        matcher = difflib.SequenceMatcher(None, src_lines, tgt_lines, autojunk=False)
        for tag, i1, i2, j1, j2 in matcher.get_opcodes():
            if tag == "equal":
                continue
            if tag in ("replace", "delete"):
                for ln in range(i1, i2):
                    left_sels.append(self._sel(self._left, ln, fmt_left))
            if tag in ("replace", "insert"):
                for ln in range(j1, j2):
                    right_sels.append(self._sel(self._right, ln, fmt_right))

        self._left.setExtraSelections(left_sels)
        self._right.setExtraSelections(right_sels)

    @staticmethod
    def _sel(
        editor: QPlainTextEdit,
        line_no: int,
        fmt: QTextCharFormat,
    ) -> QTextEdit.ExtraSelection:
        sel = QTextEdit.ExtraSelection()
        block = editor.document().findBlockByLineNumber(line_no)
        sel.cursor = QTextCursor(block)
        sel.cursor.select(QTextCursor.LineUnderCursor)
        sel.format = fmt
        return sel
