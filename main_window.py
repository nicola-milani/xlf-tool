"""
Main application window for XLF Translator.
"""
import os
from pathlib import Path
from typing import Optional, List

from PySide6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QSplitter,
    QPushButton, QLabel, QLineEdit, QComboBox, QTableWidget,
    QTableWidgetItem, QFileDialog, QProgressBar, QCheckBox,
    QGroupBox, QStatusBar, QHeaderView, QMessageBox, QFrame,
)
from PySide6.QtCore import Qt, QThread, Signal, QObject, Slot
from PySide6.QtGui import QColor, QBrush, QAction

from xlf_parser import XlfParser, Segment, OutputMode, indent_file
from llm_client import OllamaClient
from project_manager import Project, glossary_exact, glossary_substitute
import ollama_manager

# ── Language list ──────────────────────────────────────────────────────────────

LANGUAGES = [
    ("Italian", "it-IT"), ("French", "fr-FR"), ("German", "de-DE"),
    ("Spanish", "es-ES"), ("Portuguese (Portugal)", "pt-PT"),
    ("Portuguese (Brazil)", "pt-BR"), ("Dutch", "nl-NL"), ("Polish", "pl-PL"),
    ("Russian", "ru-RU"), ("Chinese (Simplified)", "zh-CN"),
    ("Chinese (Traditional)", "zh-TW"), ("Japanese", "ja-JP"),
    ("Korean", "ko-KR"), ("Arabic", "ar-SA"), ("Turkish", "tr-TR"),
    ("Swedish", "sv-SE"), ("Danish", "da-DK"), ("Finnish", "fi-FI"),
    ("Norwegian", "nb-NO"), ("Czech", "cs-CZ"), ("Hungarian", "hu-HU"),
    ("Romanian", "ro-RO"), ("Bulgarian", "bg-BG"), ("Croatian", "hr-HR"),
    ("Slovak", "sk-SK"), ("Greek", "el-GR"), ("Ukrainian", "uk-UA"),
    ("Hebrew", "he-IL"), ("Vietnamese", "vi-VN"), ("Thai", "th-TH"),
    ("Indonesian", "id-ID"),
]

COL_ID     = 0
COL_SOURCE = 1
COL_TARGET = 2
COL_NOTE   = 3

COLOR_EMPTY      = QColor("#FFEBEE")
COLOR_TRANSLATED = QColor("#E8F5E9")
COLOR_ACTIVE     = QColor("#FFF9C4")
FG_EMPTY         = QColor("#C62828")
FG_TRANSLATED    = QColor("#2E7D32")


# ── Background translation worker ─────────────────────────────────────────────

class TranslationWorker(QObject):
    unit_done = Signal(str, str, str)   # unit_id, pc_id, translated_text
    progress  = Signal(int, int, str)   # current, total, unit_id
    finished  = Signal()
    error     = Signal(str)

    def __init__(
        self,
        segments:    List[Segment],
        client:      OllamaClient,
        source_lang: str,
        target_lang: str,
        glossary:    dict | None = None,
    ):
        super().__init__()
        self._segments    = segments
        self._client      = client
        self._source_lang = source_lang
        self._target_lang = target_lang
        self._glossary    = glossary or {}
        self._cancelled   = False

    def cancel(self) -> None:
        self._cancelled = True

    def run(self) -> None:
        from itertools import groupby
        groups = [
            (uid, list(grp))
            for uid, grp in groupby(self._segments, key=lambda s: s.unit_id)
        ]
        total = len(groups)
        for i, (uid, group_segs) in enumerate(groups):
            if self._cancelled:
                break
            self.progress.emit(i + 1, total, uid)
            try:
                if len(group_segs) == 1:
                    seg   = group_segs[0]
                    gloss = glossary_exact(seg.source, self._glossary)
                    if gloss is not None:
                        translated = gloss
                    else:
                        translated = self._client.translate(
                            seg.source, self._source_lang, self._target_lang
                        )
                        translated = glossary_substitute(translated, self._glossary)
                    self.unit_done.emit(seg.unit_id, seg.pc_id, translated)
                else:
                    results: list = [None] * len(group_segs)
                    pending_idx   = []
                    for j, seg in enumerate(group_segs):
                        gloss = glossary_exact(seg.source, self._glossary)
                        if gloss is not None:
                            results[j] = gloss
                        else:
                            pending_idx.append(j)
                    if pending_idx:
                        pending_texts = [group_segs[j].source for j in pending_idx]
                        if len(pending_texts) == 1:
                            llm = [self._client.translate(
                                pending_texts[0], self._source_lang, self._target_lang
                            )]
                        else:
                            llm = self._client.translate_batch(
                                pending_texts, self._source_lang, self._target_lang
                            )
                        for j, t in zip(pending_idx, llm):
                            results[j] = glossary_substitute(t, self._glossary)
                    for seg, translated in zip(group_segs, results):
                        self.unit_done.emit(seg.unit_id, seg.pc_id, translated)
            except Exception as exc:
                self.error.emit(str(exc))
                return
        self.finished.emit()


# ── Main window ────────────────────────────────────────────────────────────────

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("XLF Translator")
        self.resize(1280, 720)
        self._parser:   Optional[XlfParser]         = None
        self._filepath: Optional[str]               = None
        self._project:  Optional[Project]           = None
        self._thread:   Optional[QThread]           = None
        self._worker:   Optional[TranslationWorker] = None

        self._build_ui()
        self._refresh_models()

    # ── UI ─────────────────────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        self._build_menu()

        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(8, 8, 8, 4)
        root.setSpacing(6)

        # ── Project info bar (hidden until a project is loaded) ──────────────
        self._project_bar = QFrame()
        self._project_bar.setFrameShape(QFrame.StyledPanel)
        self._project_bar.setStyleSheet(
            "QFrame{background:#E8F5E9;border:1px solid #A5D6A7;border-radius:4px;}"
        )
        pb = QHBoxLayout(self._project_bar)
        pb.setContentsMargins(10, 4, 10, 4)
        pb.setSpacing(16)
        self._lbl_proj_name  = QLabel()
        self._lbl_proj_name.setStyleSheet("font-weight:bold;color:#1B5E20;")
        self._lbl_proj_files = QLabel()
        self._lbl_proj_files.setStyleSheet("color:#2E7D32;font-size:12px;")
        self._lbl_proj_size  = QLabel()
        self._lbl_proj_size.setStyleSheet("color:#2E7D32;font-size:12px;")
        self._lbl_glossary   = QLabel()
        self._lbl_glossary.setStyleSheet("color:#1565C0;font-size:12px;")
        btn_save_proj = QPushButton("Save to output/")
        btn_save_proj.setFixedHeight(24)
        btn_save_proj.setStyleSheet(
            "QPushButton{background:#2E7D32;color:white;border-radius:3px;"
            "padding:0 8px;font-size:12px;}"
            "QPushButton:hover{background:#1B5E20;}"
        )
        btn_save_proj.clicked.connect(self._save_to_project_output)
        pb.addWidget(QLabel("📁"))
        pb.addWidget(self._lbl_proj_name)
        pb.addWidget(self._lbl_proj_files)
        pb.addWidget(self._lbl_proj_size)
        pb.addWidget(self._lbl_glossary)
        pb.addStretch()
        pb.addWidget(btn_save_proj)
        self._project_bar.setVisible(False)
        root.addWidget(self._project_bar)

        # ── Top bar ──────────────────────────────────────────────────────────
        top = QHBoxLayout()
        self.btn_open = QPushButton("Open XLF…")
        self.btn_open.setFixedHeight(32)
        self.btn_open.clicked.connect(self._open_file)
        self.lbl_file = QLabel("No file loaded")
        self.lbl_file.setStyleSheet("color:gray;font-style:italic;")
        self.lbl_version = QLabel("")
        self.lbl_version.setStyleSheet("color:#555;font-size:11px;")
        self.btn_save = QPushButton("Save translated…")
        self.btn_save.setFixedHeight(32)
        self.btn_save.setEnabled(False)
        self.btn_save.clicked.connect(self._save_file)
        self.btn_compare = QPushButton("Compare…")
        self.btn_compare.setFixedHeight(32)
        self.btn_compare.setEnabled(False)
        self.btn_compare.clicked.connect(self._compare_files)
        top.addWidget(self.btn_open)
        top.addWidget(self.lbl_file, stretch=1)
        top.addWidget(self.lbl_version)
        top.addWidget(self.btn_compare)
        top.addWidget(self.btn_save)
        root.addLayout(top)

        # ── Splitter ─────────────────────────────────────────────────────────
        splitter = QSplitter(Qt.Horizontal)
        root.addWidget(splitter, stretch=1)

        left = QWidget()
        left.setFixedWidth(270)
        lv = QVBoxLayout(left)
        lv.setContentsMargins(0, 0, 8, 0)
        lv.setSpacing(10)

        grp_ollama = QGroupBox("Ollama  (local LLM)")
        go = QVBoxLayout(grp_ollama)
        go.addWidget(QLabel("Server URL:"))
        self.edit_url = QLineEdit("http://localhost:11434")
        self.edit_url.editingFinished.connect(self._refresh_models)
        go.addWidget(self.edit_url)
        go.addWidget(QLabel("Model:"))
        mr = QHBoxLayout()
        self.combo_model = QComboBox()
        self.btn_refresh = QPushButton("↻")
        self.btn_refresh.setFixedWidth(32)
        self.btn_refresh.clicked.connect(self._refresh_models)
        mr.addWidget(self.combo_model, stretch=1)
        mr.addWidget(self.btn_refresh)
        go.addLayout(mr)
        self.lbl_status_ollama = QLabel("● Checking…")
        self.lbl_status_ollama.setStyleSheet("color:gray;")
        go.addWidget(self.lbl_status_ollama)
        lv.addWidget(grp_ollama)

        grp_trans = QGroupBox("Translation")
        gt = QVBoxLayout(grp_trans)
        gt.addWidget(QLabel("Source language (from file):"))
        self.edit_src_lang = QLineEdit()
        self.edit_src_lang.setReadOnly(True)
        self.edit_src_lang.setStyleSheet("background:#f5f5f5;")
        gt.addWidget(self.edit_src_lang)
        gt.addWidget(QLabel("Target language:"))
        self.combo_tgt = QComboBox()
        for name, code in LANGUAGES:
            self.combo_tgt.addItem(f"{name}  ({code})", code)
        gt.addWidget(self.combo_tgt)
        self.chk_empty_only = QCheckBox("Only translate empty segments")
        self.chk_empty_only.setChecked(True)
        gt.addWidget(self.chk_empty_only)
        gt.addWidget(QLabel("Segment filter:"))
        self.combo_seg_filter = QComboBox()
        self.combo_seg_filter.addItem("All segments", "all")
        self.combo_seg_filter.addItem("Skip AltText", "skip_alt")
        self.combo_seg_filter.addItem("AltText only", "only_alt")
        self.combo_seg_filter.setCurrentIndex(1)
        gt.addWidget(self.combo_seg_filter)
        gt.addWidget(QLabel("Output mode:"))
        self.combo_output_mode = QComboBox()
        self.combo_output_mode.addItem("Source replaced  (Articulate Storyline)", OutputMode.REPLACE)
        self.combo_output_mode.addItem("Target added  (standard XLIFF)", OutputMode.TARGET)
        gt.addWidget(self.combo_output_mode)
        lv.addWidget(grp_trans)

        self.btn_translate = QPushButton("Translate")
        self.btn_translate.setFixedHeight(38)
        self.btn_translate.setEnabled(False)
        self.btn_translate.setStyleSheet(
            "QPushButton{background:#4CAF50;color:white;font-weight:bold;"
            "border-radius:4px;font-size:13px;}"
            "QPushButton:disabled{background:#ccc;color:#888;}"
            "QPushButton:hover:!disabled{background:#43A047;}"
        )
        self.btn_translate.clicked.connect(self._start_translation)
        lv.addWidget(self.btn_translate)

        self.btn_cancel = QPushButton("Cancel")
        self.btn_cancel.setFixedHeight(30)
        self.btn_cancel.setEnabled(False)
        self.btn_cancel.clicked.connect(self._cancel_translation)
        lv.addWidget(self.btn_cancel)
        lv.addStretch()
        splitter.addWidget(left)

        self.table = QTableWidget()
        self.table.setColumnCount(4)
        self.table.setHorizontalHeaderLabels(["ID", "Source", "Target", "Note"])
        hdr = self.table.horizontalHeader()
        hdr.setSectionResizeMode(COL_ID,     QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(COL_SOURCE, QHeaderView.Stretch)
        hdr.setSectionResizeMode(COL_TARGET, QHeaderView.Stretch)
        hdr.setSectionResizeMode(COL_NOTE,   QHeaderView.ResizeToContents)
        self.table.setWordWrap(True)
        self.table.setEditTriggers(
            QTableWidget.DoubleClicked | QTableWidget.SelectedClicked
        )
        self.table.itemChanged.connect(self._on_cell_changed)
        splitter.addWidget(self.table)
        splitter.setSizes([270, 1010])

        self.progress_bar = QProgressBar()
        self.progress_bar.setFixedHeight(14)
        self.progress_bar.setVisible(False)
        root.addWidget(self.progress_bar)

        self.status_bar = QStatusBar()
        self.setStatusBar(self.status_bar)
        self.status_bar.showMessage("Ready — open an XLF file or a project folder.")

    # ── Menu ───────────────────────────────────────────────────────────────────

    def _build_menu(self) -> None:
        bar = self.menuBar()
        file_menu = bar.addMenu("File")

        act_open_proj = QAction("Open Project…", self)
        act_open_proj.setShortcut("Ctrl+Shift+O")
        act_open_proj.triggered.connect(self._open_project)
        file_menu.addAction(act_open_proj)

        act_open = QAction("Open XLF…", self)
        act_open.setShortcut("Ctrl+O")
        act_open.triggered.connect(self._open_file)
        file_menu.addAction(act_open)

        file_menu.addSeparator()

        act_save = QAction("Save translated…", self)
        act_save.setShortcut("Ctrl+S")
        act_save.triggered.connect(self._save_file)
        file_menu.addAction(act_save)

        act_save_proj = QAction("Save to project output/", self)
        act_save_proj.setShortcut("Ctrl+Shift+S")
        act_save_proj.triggered.connect(self._save_to_project_output)
        file_menu.addAction(act_save_proj)

        act_compare = QAction("Compare source/translated…", self)
        act_compare.setShortcut("Ctrl+D")
        act_compare.triggered.connect(self._compare_files)
        file_menu.addAction(act_compare)

        act_format = QAction("Format XLF…", self)
        act_format.setShortcut("Ctrl+Shift+F")
        act_format.triggered.connect(self._format_file)
        file_menu.addAction(act_format)

        file_menu.addSeparator()
        act_quit = QAction("Quit", self)
        act_quit.setShortcut("Ctrl+Q")
        act_quit.triggered.connect(self.close)
        file_menu.addAction(act_quit)

        ollama_menu = bar.addMenu("Ollama")
        act_manage = QAction("Manage models…", self)
        act_manage.setShortcut("Ctrl+M")
        act_manage.triggered.connect(self._open_ollama_dialog)
        ollama_menu.addAction(act_manage)
        ollama_menu.addSeparator()
        for label, slot in [("Start server", self._menu_start_server),
                             ("Stop server",  self._menu_stop_server)]:
            a = QAction(label, self)
            a.triggered.connect(slot)
            ollama_menu.addAction(a)
        ollama_menu.addSeparator()
        act_ref = QAction("Refresh model list", self)
        act_ref.triggered.connect(self._refresh_models)
        ollama_menu.addAction(act_ref)

    def _open_ollama_dialog(self) -> None:
        from ollama_dialog import OllamaDialog
        dlg = OllamaDialog(base_url=self.edit_url.text().strip(), parent=self)
        dlg.exec()
        self._refresh_models()

    def _menu_start_server(self) -> None:
        if ollama_manager.is_server_running():
            self.status_bar.showMessage("Ollama server is already running.")
            return
        self.status_bar.showMessage("Starting Ollama server…")
        import threading
        def _start():
            exe = ollama_manager.find_ollama()
            ok  = ollama_manager.start_server(exe) if exe else False
            from PySide6.QtCore import QMetaObject, Q_ARG
            QMetaObject.invokeMethod(self, "_on_server_started_menu",
                Qt.QueuedConnection, Q_ARG(bool, ok))
        threading.Thread(target=_start, daemon=True).start()

    @Slot(bool)
    def _on_server_started_menu(self, ok: bool) -> None:
        if ok:
            self.status_bar.showMessage("Ollama server started.")
            self._refresh_models()
        else:
            self.status_bar.showMessage("Could not start Ollama server.")

    def _menu_stop_server(self) -> None:
        ollama_manager.stop_server()
        self.status_bar.showMessage("Ollama server stopped.")
        self._refresh_models()

    # ── Project ────────────────────────────────────────────────────────────────

    def _open_project(self) -> None:
        folder = QFileDialog.getExistingDirectory(
            self, "Open Project Folder", "", QFileDialog.ShowDirsOnly
        )
        if not folder:
            return
        try:
            project = Project(Path(folder))
            project.setup_dirs()
            xlf = project.find_xlf()
            if xlf is None:
                QMessageBox.warning(self, "No XLF found",
                    "No .xlf file found inside the input/ subfolder.\n"
                    "Place the source file in  <project>/input/  and retry.")
                return
            self._project = project
            self._load_xlf(str(xlf))
            self._refresh_project_bar()
        except ValueError as exc:
            QMessageBox.critical(self, "Project error", str(exc))
        except Exception as exc:
            QMessageBox.critical(self, "Open project error", f"Could not open project:\n{exc}")

    def _refresh_project_bar(self) -> None:
        if not self._project:
            self._project_bar.setVisible(False)
            return
        info  = self._project.list_files()
        n_in  = len(info["input"])
        n_out = len(info["output"])
        g     = info["glossary"]
        self._lbl_proj_name.setText(f"Project: {self._project.name}")
        self._lbl_proj_files.setText(
            f"input: {n_in} file{'s' if n_in!=1 else ''}  |  "
            f"output: {n_out} file{'s' if n_out!=1 else ''}"
        )
        self._lbl_proj_size.setText(f"({info['total_size']})")
        if g["exists"]:
            self._lbl_glossary.setText(f"📖 Glossary: {g['terms']} terms  ({g['size']})")
        else:
            self._lbl_glossary.setText("📖 No glossary")
        self._project_bar.setVisible(True)

    def _save_to_project_output(self) -> None:
        if not self._project or not self._parser:
            QMessageBox.information(self, "No project", "Open a project first.")
            return
        try:
            xlf      = self._project.find_xlf()
            stem     = xlf.stem if xlf else "translated"
            out_path = self._project.output_dir / f"{stem}_translated.xlf"
            mode     = self.combo_output_mode.currentData()
            self._parser.set_target_language(self.combo_tgt.currentData())
            self._parser.save(str(out_path), mode)
            total      = len(self._parser.segments)
            translated = sum(1 for s in self._parser.segments if s.target.strip())
            self._project.save_metadata({
                "source_lang":         self._parser.source_lang,
                "target_lang":         self.combo_tgt.currentData(),
                "segments_total":      total,
                "segments_translated": translated,
                "xlf_filename":        Path(self._filepath).name if self._filepath else "",
            })
            self._refresh_project_bar()
            self.status_bar.showMessage(f"Saved → output/{out_path.name}")
        except Exception as exc:
            QMessageBox.critical(self, "Save error", f"Could not save:\n{exc}")

    # ── File operations ────────────────────────────────────────────────────────

    def _open_file(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Open XLF / XLIFF file", "",
            "XLIFF files (*.xlf *.xliff);;All files (*)"
        )
        if path:
            self._project = None
            self._project_bar.setVisible(False)
            self._load_xlf(path)

    def _load_xlf(self, path: str) -> None:
        try:
            parser = XlfParser()
            parser.load(path)
            self._parser   = parser
            self._filepath = path
            self.lbl_file.setText(os.path.basename(path))
            self.lbl_file.setStyleSheet("color:black;font-style:normal;font-weight:bold;")
            self.lbl_version.setText(f"[XLIFF {parser.version}]")
            self.edit_src_lang.setText(parser.source_lang)
            if parser.target_lang:
                idx = self.combo_tgt.findData(parser.target_lang)
                if idx >= 0:
                    self.combo_tgt.setCurrentIndex(idx)
            self._populate_table()
            self.btn_translate.setEnabled(True)
            self.btn_save.setEnabled(True)
            self.btn_compare.setEnabled(True)
            total      = len(parser.segments)
            translated = sum(1 for s in parser.segments if s.target.strip())
            self.status_bar.showMessage(
                f"Loaded {total} segments  ({translated} translated, {total - translated} empty)"
            )
        except Exception as exc:
            QMessageBox.critical(self, "Load error", f"Could not parse file:\n{exc}")

    def _save_file(self) -> None:
        if not self._parser:
            return
        base, ext = os.path.splitext(self._filepath)
        path, _ = QFileDialog.getSaveFileName(
            self, "Save translated XLF", f"{base}_translated{ext}",
            "XLIFF files (*.xlf *.xliff);;All files (*)"
        )
        if not path:
            return
        try:
            self._parser.set_target_language(self.combo_tgt.currentData())
            self._parser.save(path, self.combo_output_mode.currentData())
            self.status_bar.showMessage(f"Saved → {os.path.basename(path)}")
        except Exception as exc:
            QMessageBox.critical(self, "Save error", f"Could not save file:\n{exc}")

    def _compare_files(self) -> None:
        if not self._parser:
            return
        try:
            from diff_dialog import DiffDialog
            mode = self.combo_output_mode.currentData()
            dlg  = DiffDialog(
                self._parser.get_source_xml(),
                self._parser.get_translated_xml(mode),
                parent=self,
            )
            dlg.exec()
        except Exception as exc:
            QMessageBox.critical(self, "Compare error", f"Could not generate diff:\n{exc}")

    def _format_file(self) -> None:
        src, _ = QFileDialog.getOpenFileName(
            self, "Format XLF — select source file", "",
            "XLIFF files (*.xlf *.xliff);;All files (*)"
        )
        if not src:
            return
        base, ext = os.path.splitext(src)
        dst, _ = QFileDialog.getSaveFileName(
            self, "Format XLF — save formatted file", f"{base}_formatted{ext}",
            "XLIFF files (*.xlf *.xliff);;All files (*)"
        )
        if not dst:
            return
        try:
            indent_file(src, dst)
            self.status_bar.showMessage(f"Formatted → {os.path.basename(dst)}")
        except Exception as exc:
            QMessageBox.critical(self, "Format error", f"Could not format file:\n{exc}")

    # ── Table ──────────────────────────────────────────────────────────────────

    def _populate_table(self) -> None:
        self.table.blockSignals(True)
        self.table.clearContents()
        self.table.setRowCount(len(self._parser.segments))
        for row, seg in enumerate(self._parser.segments):
            self._fill_row(row, seg)
        self.table.resizeRowsToContents()
        self.table.blockSignals(False)

    def _fill_row(self, row: int, seg: Segment) -> None:
        has_target = bool(seg.target.strip())
        color = COLOR_TRANSLATED if has_target else COLOR_EMPTY

        def make(text: str, editable: bool = False) -> QTableWidgetItem:
            item = QTableWidgetItem(text)
            if not editable:
                item.setFlags(item.flags() & ~Qt.ItemIsEditable)
            item.setBackground(QBrush(color))
            return item

        id_item = make(seg.unit_id)
        id_item.setData(Qt.UserRole, seg.pc_id)
        if seg.unit_id.endswith(".AltText"):
            id_item.setToolTip("AltText — skipped by default filter")
        elif seg.unit_type:
            id_item.setToolTip(seg.unit_type)
        self.table.setItem(row, COL_ID,     id_item)
        self.table.setItem(row, COL_SOURCE, make(seg.source))
        self.table.setItem(row, COL_TARGET, make(seg.target, editable=True))
        self.table.setItem(row, COL_NOTE,   make(seg.note))

    def _set_row_color(self, row: int, color: QColor) -> None:
        for col in range(self.table.columnCount()):
            item = self.table.item(row, col)
            if item:
                item.setBackground(QBrush(color))

    def _row_for_unit(self, unit_id: str, pc_id: str = "") -> int:
        for row in range(self.table.rowCount()):
            item = self.table.item(row, COL_ID)
            if item and item.text() == unit_id and item.data(Qt.UserRole) == pc_id:
                return row
        return -1

    def _on_cell_changed(self, item: QTableWidgetItem) -> None:
        if item.column() != COL_TARGET or not self._parser:
            return
        row      = item.row()
        uid_item = self.table.item(row, COL_ID)
        if not uid_item:
            return
        new_target = item.text()
        pc_id      = uid_item.data(Qt.UserRole) or ""
        self._parser.update_target(uid_item.text(), new_target, pc_id)
        self.table.blockSignals(True)
        self._set_row_color(row, COLOR_TRANSLATED if new_target.strip() else COLOR_EMPTY)
        self.table.blockSignals(False)

    # ── Ollama ─────────────────────────────────────────────────────────────────

    def _refresh_models(self) -> None:
        url    = self.edit_url.text().strip()
        models = OllamaClient(base_url=url).list_models()
        self.combo_model.clear()
        if models:
            for m in models:
                self.combo_model.addItem(m)
            self.lbl_status_ollama.setText("● Connected")
            self.lbl_status_ollama.setStyleSheet("color:#2E7D32;font-weight:bold;")
        elif ollama_manager.find_ollama() is not None:
            self.combo_model.addItem("(server offline — click ↻ to retry)")
            self.lbl_status_ollama.setText("● Server offline")
            self.lbl_status_ollama.setStyleSheet("color:#F57C00;font-weight:bold;")
        else:
            self.combo_model.addItem("(ollama not found)")
            self.lbl_status_ollama.setText("● Not found")
            self.lbl_status_ollama.setStyleSheet("color:#C62828;font-weight:bold;")

    # ── Translation ────────────────────────────────────────────────────────────

    def _start_translation(self) -> None:
        if not self._parser:
            return
        model = self.combo_model.currentText()
        if not model or model.startswith("("):
            QMessageBox.warning(self, "No model", "Select a valid Ollama model first.")
            return

        segments = self._parser.segments
        if self.chk_empty_only.isChecked():
            segments = [s for s in segments if not s.target.strip()]
        seg_filter = self.combo_seg_filter.currentData()
        if seg_filter == "skip_alt":
            segments = [s for s in segments if not s.unit_id.endswith(".AltText")]
        elif seg_filter == "only_alt":
            segments = [s for s in segments if s.unit_id.endswith(".AltText")]
        if not segments:
            note = {"skip_alt": " (AltText excluded)", "only_alt": " (AltText only)"}.get(seg_filter, "")
            QMessageBox.information(self, "Nothing to do",
                f"No segments to translate{note}.\nAll matching segments are already translated.")
            return

        glossary = self._project.load_glossary() if self._project else {}
        client   = OllamaClient(base_url=self.edit_url.text().strip(), model=model)
        src_lang = self.edit_src_lang.text().strip() or "English"
        tgt_lang = self.combo_tgt.currentData()

        self._worker = TranslationWorker(segments, client, src_lang, tgt_lang, glossary)
        self._thread = QThread()
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.progress.connect(self._on_progress)
        self._worker.unit_done.connect(self._on_unit_done)
        self._worker.finished.connect(self._on_finished)
        self._worker.error.connect(self._on_error)
        self._worker.finished.connect(self._thread.quit)
        self._worker.error.connect(self._thread.quit)

        from itertools import groupby
        n_groups = sum(1 for _ in groupby(segments, key=lambda s: s.unit_id))
        self.progress_bar.setRange(0, n_groups)
        self.progress_bar.setValue(0)
        self.progress_bar.setVisible(True)
        self.btn_translate.setEnabled(False)
        self.btn_cancel.setEnabled(True)
        self.btn_open.setEnabled(False)
        self._thread.start()

    def _cancel_translation(self) -> None:
        if self._worker:
            self._worker.cancel()
        self.status_bar.showMessage("Cancelling…")
        self.btn_cancel.setEnabled(False)

    @Slot(int, int, str)
    def _on_progress(self, current: int, total: int, unit_id: str) -> None:
        self.progress_bar.setValue(current)
        row = self._row_for_unit(unit_id)
        if row >= 0:
            self.table.blockSignals(True)
            self._set_row_color(row, COLOR_ACTIVE)
            self.table.blockSignals(False)
            self.table.scrollToItem(self.table.item(row, COL_ID))
        self.status_bar.showMessage(f"Translating {current}/{total}  [{unit_id}]")

    @Slot(str, str, str)
    def _on_unit_done(self, unit_id: str, pc_id: str, translated: str) -> None:
        self._parser.update_target(unit_id, translated, pc_id)
        row = self._row_for_unit(unit_id, pc_id)
        if row >= 0:
            self.table.blockSignals(True)
            tgt = self.table.item(row, COL_TARGET)
            if tgt:
                tgt.setText(translated)
            self._set_row_color(row, COLOR_TRANSLATED)
            self.table.blockSignals(False)
            self.table.resizeRowToContents(row)

    @Slot()
    def _on_finished(self) -> None:
        self.progress_bar.setVisible(False)
        self.btn_translate.setEnabled(True)
        self.btn_cancel.setEnabled(False)
        self.btn_open.setEnabled(True)
        total      = len(self._parser.segments)
        translated = sum(1 for s in self._parser.segments if s.target.strip())
        self.status_bar.showMessage(
            f"Done — {translated}/{total} segments translated.  "
            "Use 'Save translated…' or 'Save to output/' to export."
        )

    @Slot(str)
    def _on_error(self, msg: str) -> None:
        self.progress_bar.setVisible(False)
        self.btn_translate.setEnabled(True)
        self.btn_cancel.setEnabled(False)
        self.btn_open.setEnabled(True)
        QMessageBox.critical(self, "Translation error",
            f"The translation stopped due to an error:\n\n{msg}\n\n"
            "Make sure Ollama is running and the selected model is available.")
        self.status_bar.showMessage("Translation failed.")
