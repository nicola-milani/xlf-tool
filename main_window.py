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
    QScrollArea, QDialog, QListWidget, QListWidgetItem,
)
from PySide6.QtCore import Qt, QThread, Signal, QObject, Slot
from PySide6.QtGui import QColor, QBrush, QAction

from xlf_parser import XlfParser, Segment, OutputMode, indent_file
from llm_client import OllamaClient
from project_manager import Project, glossary_exact, glossary_substitute
import ollama_manager
from config import MULTI_LANG_ENABLED

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


class TargetLanguagesDialog(QDialog):
    """Dialog for selecting multiple target languages via dropdown + list."""
    def __init__(self, current_selection=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Project Target Languages")
        self.resize(360, 360)

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Add languages to the project:"))

        # Dropdown + Add row
        add_row = QHBoxLayout()
        self._combo = QComboBox()
        for name, code in LANGUAGES:
            self._combo.addItem(f"{name}  ({code})", code)
        btn_add = QPushButton("Add")
        btn_add.setFixedWidth(60)
        btn_add.clicked.connect(self._add_selected)
        add_row.addWidget(self._combo, stretch=1)
        add_row.addWidget(btn_add)
        layout.addLayout(add_row)

        layout.addWidget(QLabel("Selected languages:"))
        self._list = QListWidget()
        layout.addWidget(self._list, stretch=1)

        btn_remove = QPushButton("Remove selected")
        btn_remove.clicked.connect(self._remove_selected)
        layout.addWidget(btn_remove)

        # Pre-populate
        for code in (current_selection or []):
            self._add_by_code(code)

        buttons = QHBoxLayout()
        btn_ok = QPushButton("OK")
        btn_ok.setDefault(True)
        btn_ok.clicked.connect(self.accept)
        btn_cancel = QPushButton("Cancel")
        btn_cancel.clicked.connect(self.reject)
        buttons.addStretch()
        buttons.addWidget(btn_ok)
        buttons.addWidget(btn_cancel)
        layout.addLayout(buttons)

    def _add_selected(self):
        code = self._combo.currentData()
        for i in range(self._list.count()):
            if self._list.item(i).data(Qt.UserRole) == code:
                return
        item = QListWidgetItem(self._combo.currentText())
        item.setData(Qt.UserRole, code)
        self._list.addItem(item)

    def _add_by_code(self, code: str):
        for name, c in LANGUAGES:
            if c == code:
                item = QListWidgetItem(f"{name}  ({c})")
                item.setData(Qt.UserRole, c)
                self._list.addItem(item)
                break

    def _remove_selected(self):
        for item in self._list.selectedItems():
            self._list.takeItem(self._list.row(item))

    def selected_langs(self) -> list:
        return [self._list.item(i).data(Qt.UserRole) for i in range(self._list.count())]


# ── Background translation worker ─────────────────────────────────────────────

class TranslationWorker(QObject):
    unit_done   = Signal(str, str, str)   # unit_id, pc_id, translated_text
    progress    = Signal(int, int, str)   # current, total, unit_id
    finished    = Signal()
    error       = Signal(str)
    cache_stats = Signal(int, int)        # cache_hits, total_segs

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
        translation_cache: dict[str, str] = {}
        cache_hits = 0
        total_segs = 0
        for i, (uid, group_segs) in enumerate(groups):
            if self._cancelled:
                break
            self.progress.emit(i + 1, total, uid)
            try:
                if len(group_segs) == 1:
                    seg   = group_segs[0]
                    total_segs += 1
                    gloss = glossary_exact(seg.source, self._glossary)
                    if seg.source in translation_cache:
                        translated = translation_cache[seg.source]
                        cache_hits += 1
                    elif gloss is not None:
                        translated = gloss
                        translation_cache[seg.source] = translated
                    else:
                        translated = self._client.translate(
                            seg.source, self._source_lang, self._target_lang
                        )
                        translated = glossary_substitute(translated, self._glossary)
                        translation_cache[seg.source] = translated
                    self.unit_done.emit(seg.unit_id, seg.pc_id, translated)
                else:
                    results: list = [None] * len(group_segs)
                    pending_idx   = []
                    for j, seg in enumerate(group_segs):
                        total_segs += 1
                        gloss = glossary_exact(seg.source, self._glossary)
                        if seg.source in translation_cache:
                            results[j] = translation_cache[seg.source]
                            cache_hits += 1
                        elif gloss is not None:
                            results[j] = gloss
                            translation_cache[seg.source] = gloss
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
                            translation_cache[group_segs[j].source] = results[j]
                    for seg, translated in zip(group_segs, results):
                        self.unit_done.emit(seg.unit_id, seg.pc_id, translated)
            except Exception as exc:
                self.error.emit(str(exc))
                return
        self.cache_stats.emit(cache_hits, total_segs)
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
        self._project_metadata: dict = {}
        self._pending_translate_langs: list = []
        self._auto_save_on_finish: bool = False
        self._parallel_workers: list = []   # [(worker, thread, parser, lang), ...]
        self._parallel_remaining: int = 0
        self._last_cache_stats: tuple[int, int] = (0, 0)   # (cache_hits, total_segs)
        self._web_server = None   # uvicorn.Server instance when web UI is running
        self._web_thread = None   # daemon thread running the web server

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
        self._lbl_proj_langs = QLabel()
        self._lbl_proj_langs.setStyleSheet("color:#555;font-size:12px;")
        self._lbl_proj_langs.setVisible(MULTI_LANG_ENABLED)
        btn_manage_langs = QPushButton("Languages…")
        btn_manage_langs.setFixedHeight(24)
        btn_manage_langs.setStyleSheet(
            "QPushButton{background:#1565C0;color:white;border-radius:3px;"
            "padding:0 8px;font-size:12px;}"
            "QPushButton:hover{background:#0D47A1;}"
        )
        btn_manage_langs.clicked.connect(self._manage_languages)
        btn_manage_langs.setVisible(MULTI_LANG_ENABLED)
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
        pb.addWidget(self._lbl_proj_langs)
        pb.addWidget(btn_manage_langs)
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
        gt.addWidget(QLabel("Segment type:"))
        self.combo_seg_type = QComboBox()
        self.combo_seg_type.addItem("All types", "all")
        self.combo_seg_type.addItem("Plain text only", "only_plain")
        self.combo_seg_type.addItem("Document state only", "only_doc")
        gt.addWidget(self.combo_seg_type)
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

        self.btn_translate_all = QPushButton("Translate All Languages")
        self.btn_translate_all.setFixedHeight(32)
        self.btn_translate_all.setEnabled(False)
        self.btn_translate_all.setStyleSheet(
            "QPushButton{background:#1565C0;color:white;font-weight:bold;"
            "border-radius:4px;font-size:12px;}"
            "QPushButton:disabled{background:#ccc;color:#888;}"
            "QPushButton:hover:!disabled{background:#0D47A1;}"
        )
        self.btn_translate_all.clicked.connect(self._translate_all_languages)
        lv.addWidget(self.btn_translate_all)
        self.btn_translate_all.setVisible(MULTI_LANG_ENABLED)
        self.chk_parallel = QCheckBox("Run all languages in parallel")
        self.chk_parallel.setToolTip(
            "Start a separate worker thread for each language simultaneously.\n"
            "No per-segment progress is shown; each language saves when done."
        )
        lv.addWidget(self.chk_parallel)
        self.chk_parallel.setVisible(MULTI_LANG_ENABLED)

        self.btn_cancel = QPushButton("Cancel")
        self.btn_cancel.setFixedHeight(30)
        self.btn_cancel.setEnabled(False)
        self.btn_cancel.clicked.connect(self._cancel_translation)
        lv.addWidget(self.btn_cancel)
        lv.addStretch()

        # ── Web UI ───────────────────────────────────────────────────────────
        grp_web = QGroupBox("Web UI")
        gw = QVBoxLayout(grp_web)
        port_row = QHBoxLayout()
        port_row.addWidget(QLabel("Port:"))
        self.edit_web_port = QLineEdit("8080")
        self.edit_web_port.setFixedWidth(60)
        port_row.addWidget(self.edit_web_port)
        port_row.addStretch()
        gw.addLayout(port_row)
        self.btn_web = QPushButton("Launch Web UI")
        self.btn_web.setFixedHeight(30)
        self.btn_web.setStyleSheet(
            "QPushButton{background:#0277BD;color:white;border-radius:4px;"
            "font-size:12px;}"
            "QPushButton:hover{background:#01579B;}"
            "QPushButton[running=true]{background:#B71C1C;}"
            "QPushButton[running=true]:hover{background:#7F0000;}"
        )
        self.btn_web.clicked.connect(self._toggle_web_ui)
        gw.addWidget(self.btn_web)
        self.lbl_web_status = QLabel("")
        self.lbl_web_status.setStyleSheet("font-size:11px;color:#0277BD;")
        self.lbl_web_status.setWordWrap(True)
        gw.addWidget(self.lbl_web_status)
        lv.addWidget(grp_web)
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
            # Show language dialog if no languages configured yet
            existing = self._project_metadata.get("target_langs", [])
            if not existing:
                dlg = TargetLanguagesDialog(parent=self)
                if dlg.exec() == QDialog.Accepted:
                    langs = dlg.selected_langs()
                    if langs:
                        self._project.save_metadata({"target_langs": langs})
                        self._project_metadata = self._project.load_metadata()
                        self._refresh_project_bar()
        except ValueError as exc:
            QMessageBox.critical(self, "Project error", str(exc))
        except Exception as exc:
            QMessageBox.critical(self, "Open project error", f"Could not open project:\n{exc}")

    def _manage_languages(self) -> None:
        if not self._project:
            return
        current = self._project_metadata.get("target_langs", [])
        dlg = TargetLanguagesDialog(current_selection=current, parent=self)
        if dlg.exec() != QDialog.Accepted:
            return
        langs = dlg.selected_langs()
        self._project.save_metadata({"target_langs": langs})
        self._project_metadata = self._project.load_metadata()
        self._refresh_project_bar()
        self.status_bar.showMessage(
            f"Languages saved: {', '.join(langs) if langs else 'none'}"
        )

    def _refresh_project_bar(self) -> None:
        if not self._project:
            self._project_bar.setVisible(False)
            return
        info  = self._project.list_files()
        n_in  = len(info["input"])
        n_out = len(info["output"])
        g     = info["glossary"]
        self._project_metadata = info.get("metadata", {})
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
        langs = self._project_metadata.get("target_langs", [])
        if langs:
            self._lbl_proj_langs.setText(f"🌍 {',  '.join(langs)}")
        else:
            self._lbl_proj_langs.setText("🌍 No languages set")
        self._project_bar.setVisible(True)
        has_langs = bool(langs) and self._parser is not None
        self.btn_translate_all.setEnabled(has_langs)

    def _save_to_project_output(self) -> None:
        if not self._project or not self._parser:
            QMessageBox.information(self, "No project", "Open a project first.")
            return
        try:
            lang     = self.combo_tgt.currentData()
            out_path = self._project.output_path_for_lang(lang)
            mode     = self.combo_output_mode.currentData()
            self._parser.set_target_language(lang)
            self._parser.save(str(out_path), mode)
            total      = len(self._parser.segments)
            translated = sum(1 for s in self._parser.segments if s.target.strip())
            # Update per-lang translation counts
            meta = self._project.load_metadata()
            segs_by_lang = meta.get("segments_translated", {})
            if not isinstance(segs_by_lang, dict):
                segs_by_lang = {}
            segs_by_lang[lang] = translated
            self._project.save_metadata({
                "source_lang":         self._parser.source_lang,
                "segments_total":      total,
                "segments_translated": segs_by_lang,
                "xlf_filename":        Path(self._filepath).name if self._filepath else "",
            })
            self._project_metadata = self._project.load_metadata()
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
        from diff_dialog import DiffDialog
        mode = self.combo_output_mode.currentData()

        if self._project:
            langs = self._project_metadata.get("target_langs", [])
            available = {
                lang: self._project.output_path_for_lang(lang)
                for lang in langs
                if self._project.output_path_for_lang(lang).exists()
            }
            if available:
                if len(available) > 1:
                    from PySide6.QtWidgets import QInputDialog
                    lang, ok = QInputDialog.getItem(
                        self, "Compare — Select Language",
                        "Select translated language to compare:",
                        list(available.keys()),
                        editable=False,
                    )
                    if not ok:
                        return
                else:
                    lang = list(available.keys())[0]
                try:
                    dlg = DiffDialog(
                        self._parser.get_source_xml(),
                        available[lang].read_text(encoding="utf-8"),
                        parent=self,
                    )
                    dlg.setWindowTitle(f"XLF — Source vs {lang}")
                    dlg.exec()
                    return
                except Exception as exc:
                    QMessageBox.critical(self, "Compare error", f"Could not generate diff:\n{exc}")
                    return

        try:
            dlg = DiffDialog(
                self._parser.get_source_xml(),
                self._parser.get_translated_xml(mode),
                parent=self,
            )
            dlg.exec()
        except Exception as exc:
            QMessageBox.critical(self, "Compare error", f"Could not generate diff:\n{exc}")

    def _translate_all_languages(self) -> None:
        if not self._project or not self._parser:
            return
        langs = self._project_metadata.get("target_langs", [])
        if not langs:
            QMessageBox.information(self, "No languages",
                "No target languages configured.\nUse 'Languages…' to set them.")
            return
        model = self.combo_model.currentText()
        if not model or model.startswith("("):
            QMessageBox.warning(self, "No model", "Select a valid Ollama model first.")
            return
        if self.chk_parallel.isChecked():
            self._translate_all_parallel(langs)
        else:
            self._pending_translate_langs = list(langs)
            self._auto_save_on_finish = True
            self._translate_next_pending()

    def _translate_next_pending(self) -> None:
        if not self._pending_translate_langs:
            return
        lang = self._pending_translate_langs.pop(0)
        idx = self.combo_tgt.findData(lang)
        if idx >= 0:
            self.combo_tgt.setCurrentIndex(idx)
        # Reload parser fresh from input
        xlf = self._project.find_xlf()
        if xlf is None:
            self.status_bar.showMessage(f"No XLF found — skipping {lang}")
            self._translate_next_pending()
            return
        try:
            p = XlfParser()
            p.load(str(xlf))
            self._parser = p
            self._populate_table()
        except Exception as exc:
            self.status_bar.showMessage(f"Error loading XLF for {lang}: {exc}")
            self._translate_next_pending()
            return
        self._auto_save_on_finish = True
        self._start_translation()

    def _translate_all_parallel(self, langs: list) -> None:
        """Start one worker thread per language simultaneously."""
        xlf = self._project.find_xlf()
        if xlf is None:
            QMessageBox.warning(self, "No XLF", "No XLF file found in input/.")
            return
        model   = self.combo_model.currentText()
        glossary = self._project.load_glossary()
        client   = OllamaClient(base_url=self.edit_url.text().strip(), model=model)
        src_lang = self.edit_src_lang.text().strip() or "English"

        _DOC_STATE = ("x-DocumentState", "Articulate:DocumentState")
        seg_filter = self.combo_seg_filter.currentData()
        seg_type   = self.combo_seg_type.currentData()
        empty_only = self.chk_empty_only.isChecked()

        self._parallel_workers   = []
        self._parallel_remaining = len(langs)
        self.progress_bar.setRange(0, len(langs))
        self.progress_bar.setValue(0)
        self.progress_bar.setVisible(True)
        self.btn_translate.setEnabled(False)
        self.btn_translate_all.setEnabled(False)
        self.btn_cancel.setEnabled(True)
        self.btn_open.setEnabled(False)

        for lang in langs:
            try:
                p = XlfParser()
                p.load(str(xlf))
            except Exception as exc:
                self.status_bar.showMessage(f"Failed to load XLF for {lang}: {exc}")
                self._parallel_remaining -= 1
                continue

            segs = list(p.segments)
            if empty_only:
                segs = [s for s in segs if not s.target.strip()]
            if seg_filter == "skip_alt":
                segs = [s for s in segs if not s.unit_id.endswith(".AltText")]
            elif seg_filter == "only_alt":
                segs = [s for s in segs if s.unit_id.endswith(".AltText")]
            if seg_type == "only_plain":
                segs = [s for s in segs if s.unit_type not in _DOC_STATE]
            elif seg_type == "only_doc":
                segs = [s for s in segs if s.unit_type in _DOC_STATE]

            worker = TranslationWorker(segs, client, src_lang, lang, glossary)
            thread = QThread()
            worker.moveToThread(thread)
            thread.started.connect(worker.run)
            worker.finished.connect(thread.quit)
            # Capture lang and p by value
            worker.finished.connect(
                lambda lang=lang, p=p: self._on_parallel_lang_done(lang, p)
            )
            worker.error.connect(
                lambda msg, t=thread: (t.quit(), self._on_error(msg))
            )
            self._parallel_workers.append((worker, thread, p, lang))
            thread.start()

        self.status_bar.showMessage(
            f"Translating {len(langs)} languages in parallel…"
        )

    @Slot()
    def _on_parallel_lang_done(self, lang: str, parser: XlfParser) -> None:
        """Called when one parallel worker finishes. Saves output and checks if all done."""
        if self._project:
            try:
                out_path = self._project.output_path_for_lang(lang)
                mode = self.combo_output_mode.currentData()
                parser.set_target_language(lang)
                parser.save(str(out_path), mode)
                total      = len(parser.segments)
                translated = sum(1 for s in parser.segments if s.target.strip())
                meta = self._project.load_metadata()
                segs = meta.get("segments_translated", {})
                if not isinstance(segs, dict):
                    segs = {}
                segs[lang] = translated
                self._project.save_metadata({"segments_translated": segs})
            except Exception as exc:
                self.status_bar.showMessage(f"Save error for {lang}: {exc}")

        self._parallel_remaining -= 1
        done = len(self._parallel_workers) - self._parallel_remaining
        self.progress_bar.setValue(done)
        self.status_bar.showMessage(
            f"Parallel translation: {done}/{len(self._parallel_workers)} languages done…"
        )

        if self._parallel_remaining <= 0:
            self.progress_bar.setVisible(False)
            self.btn_translate.setEnabled(True)
            self.btn_translate_all.setEnabled(True)
            self.btn_cancel.setEnabled(False)
            self.btn_open.setEnabled(True)
            self._parallel_workers = []
            self._refresh_project_bar()
            self.status_bar.showMessage("All languages translated.")

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

    # ── Web UI ─────────────────────────────────────────────────────────────────

    def _toggle_web_ui(self) -> None:
        if self._web_server is None:
            self._launch_web_ui()
        else:
            self._stop_web_ui()

    def _launch_web_ui(self) -> None:
        try:
            import uvicorn
        except ImportError:
            QMessageBox.warning(
                self, "Missing dependency",
                "uvicorn is not installed.\n\nRun: pip install fastapi uvicorn python-multipart",
            )
            return

        try:
            port = int(self.edit_web_port.text().strip())
        except ValueError:
            port = 8080

        from web_server import app as web_app  # noqa: local import
        import threading

        config = uvicorn.Config(web_app, host="127.0.0.1", port=port, log_level="warning")
        server = uvicorn.Server(config)
        self._web_server = server

        def _run():
            import asyncio
            asyncio.run(server.serve())

        self._web_thread = threading.Thread(target=_run, daemon=True)
        self._web_thread.start()

        # Open browser after a short delay to let the server start
        import threading as _t
        def _open_browser():
            import time, webbrowser
            time.sleep(1.2)
            webbrowser.open(f"http://127.0.0.1:{port}")
        _t.Thread(target=_open_browser, daemon=True).start()

        url = f"http://127.0.0.1:{port}"
        self.btn_web.setText("Stop Web UI")
        self.btn_web.setStyleSheet(
            "QPushButton{background:#B71C1C;color:white;border-radius:4px;font-size:12px;}"
            "QPushButton:hover{background:#7F0000;}"
        )
        self.edit_web_port.setEnabled(False)
        self.lbl_web_status.setText(f"Running at {url}")
        self.status_bar.showMessage(f"Web UI started → {url}")

    def _stop_web_ui(self) -> None:
        if self._web_server is not None:
            self._web_server.should_exit = True
            self._web_server = None
            self._web_thread = None
        self.btn_web.setText("Launch Web UI")
        self.btn_web.setStyleSheet(
            "QPushButton{background:#0277BD;color:white;border-radius:4px;font-size:12px;}"
            "QPushButton:hover{background:#01579B;}"
        )
        self.edit_web_port.setEnabled(True)
        self.lbl_web_status.setText("")
        self.status_bar.showMessage("Web UI stopped.")

    def closeEvent(self, event) -> None:
        self._stop_web_ui()
        super().closeEvent(event)

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

        _DOC_STATE = ("x-DocumentState", "Articulate:DocumentState")
        seg_type = self.combo_seg_type.currentData()
        if seg_type == "only_plain":
            segments = [s for s in segments if s.unit_type not in _DOC_STATE]
        elif seg_type == "only_doc":
            segments = [s for s in segments if s.unit_type in _DOC_STATE]

        if not segments:
            QMessageBox.information(self, "Nothing to do",
                "No segments to translate with the current filters.\n"
                "All matching segments are already translated.")
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
        self._worker.cache_stats.connect(self._on_cache_stats)
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
        for w, t, p, lang in self._parallel_workers:
            w.cancel()
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

    @Slot(int, int)
    def _on_cache_stats(self, cache_hits: int, total_segs: int) -> None:
        self._last_cache_stats = (cache_hits, total_segs)

    @Slot()
    def _on_finished(self) -> None:
        self.progress_bar.setVisible(False)
        self.btn_translate.setEnabled(True)
        self.btn_cancel.setEnabled(False)
        self.btn_open.setEnabled(True)
        total      = len(self._parser.segments)
        translated = sum(1 for s in self._parser.segments if s.target.strip())

        if self._auto_save_on_finish and self._project:
            self._auto_save_on_finish = False
            self._save_to_project_output()

        if self._pending_translate_langs:
            self._translate_next_pending()
        else:
            cache_hits, total_segs = self._last_cache_stats
            cache_msg = ""
            if total_segs > 0 and cache_hits > 0:
                pct = round(cache_hits * 100 / total_segs)
                cache_msg = f"  |  cache: {cache_hits}/{total_segs} segmenti ({pct}% risparmiati)"
            self.status_bar.showMessage(
                f"Done — {translated}/{total} segments translated.{cache_msg}  "
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
