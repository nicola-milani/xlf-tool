"""
Ollama management dialog.
Shows server status, installed models, and allows pulling / deleting models.
Models in the pull list are scored and colour-coded based on detected hardware.
"""
from __future__ import annotations

import json
import subprocess
import sys
from typing import Optional

import requests
from PySide6.QtCore import Qt, QObject, QThread, Signal, Slot
from PySide6.QtGui import QFont, QColor, QStandardItemModel, QStandardItem
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QProgressBar,
    QPushButton, QTableWidget, QTableWidgetItem, QHeaderView,
    QMessageBox, QGroupBox, QComboBox,
)

import ollama_manager
import hw_detect
from hw_detect import (
    HardwareInfo, ModelSpec, MODEL_CATALOGUE,
    TIER_GREAT, TIER_OK, TIER_SLOW, TIER_NO,
    TIER_LABEL, TIER_ORDER, score,
)

# Colours for each compatibility tier
TIER_FG = {
    TIER_GREAT: QColor("#1B5E20"),   # dark green
    TIER_OK:    QColor("#1A237E"),   # dark blue
    TIER_SLOW:  QColor("#E65100"),   # orange
    TIER_NO:    QColor("#B71C1C"),   # dark red
}
TIER_BG = {
    TIER_GREAT: QColor("#F1F8E9"),
    TIER_OK:    QColor("#E8EAF6"),
    TIER_SLOW:  QColor("#FFF3E0"),
    TIER_NO:    QColor("#FFEBEE"),
}


# ── Workers ────────────────────────────────────────────────────────────────────

class PullWorker(QObject):
    progress = Signal(str, int, int)
    finished = Signal()
    error    = Signal(str)

    def __init__(self, base_url: str, model: str):
        super().__init__()
        self._url   = base_url.rstrip("/")
        self._model = model

    def run(self) -> None:
        try:
            resp = requests.post(
                f"{self._url}/api/pull",
                json={"name": self._model, "stream": True},
                stream=True, timeout=3600,
            )
            resp.raise_for_status()
            for line in resp.iter_lines():
                if not line:
                    continue
                data = json.loads(line)
                self.progress.emit(
                    data.get("status", ""),
                    data.get("completed", 0),
                    data.get("total", 0),
                )
                if data.get("status") == "success":
                    break
            self.finished.emit()
        except Exception as exc:
            self.error.emit(str(exc))


class DeleteWorker(QObject):
    finished = Signal()
    error    = Signal(str)

    def __init__(self, base_url: str, model: str):
        super().__init__()
        self._url   = base_url.rstrip("/")
        self._model = model

    def run(self) -> None:
        try:
            resp = requests.delete(
                f"{self._url}/api/delete",
                json={"name": self._model},
                timeout=30,
            )
            resp.raise_for_status()
            self.finished.emit()
        except Exception as exc:
            self.error.emit(str(exc))


class StartServerWorker(QObject):
    finished = Signal(bool)

    def run(self) -> None:
        exe = ollama_manager.find_ollama()
        self.finished.emit(ollama_manager.start_server(exe) if exe else False)


# ── Dialog ─────────────────────────────────────────────────────────────────────

class OllamaDialog(QDialog):
    """Manage Ollama server and locally installed models."""

    def __init__(self, base_url: str = "http://127.0.0.1:11434", parent=None):
        super().__init__(parent)
        self.base_url = base_url
        self._hw: HardwareInfo = hw_detect.detect()

        # Keep thread/worker refs on self — prevents GC crashes
        self._pull_thread:   Optional[QThread] = None
        self._pull_worker:   Optional[PullWorker]        = None
        self._delete_thread: Optional[QThread] = None
        self._delete_worker: Optional[DeleteWorker]      = None
        self._srv_thread:    Optional[QThread] = None
        self._srv_worker:    Optional[StartServerWorker] = None

        self.setWindowTitle("Ollama — Model Manager")
        self.setMinimumSize(680, 560)
        self._build_ui()
        self._refresh()

    # ── UI ─────────────────────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setSpacing(10)

        layout.addWidget(self._build_server_group())
        layout.addWidget(self._build_hw_banner())
        layout.addWidget(self._build_models_group(), stretch=1)
        layout.addWidget(self._build_pull_group())

        btn_close = QPushButton("Close")
        btn_close.clicked.connect(self.accept)
        layout.addWidget(btn_close, alignment=Qt.AlignRight)

    # ── Server group ───────────────────────────────────────────────────────────

    def _build_server_group(self) -> QGroupBox:
        grp = QGroupBox("Server")
        row = QHBoxLayout(grp)

        self.lbl_status = QLabel()
        self.lbl_status.setFont(QFont("", -1, QFont.Bold))
        row.addWidget(self.lbl_status, stretch=1)

        self.lbl_location = QLabel()
        self.lbl_location.setStyleSheet("color: gray; font-size: 11px;")
        row.addWidget(self.lbl_location, stretch=2)

        self.btn_start_stop = QPushButton()
        self.btn_start_stop.setFixedWidth(110)
        self.btn_start_stop.clicked.connect(self._toggle_server)
        row.addWidget(self.btn_start_stop)

        btn_folder = QPushButton("Open cache folder")
        btn_folder.setFixedWidth(140)
        btn_folder.clicked.connect(self._open_folder)
        row.addWidget(btn_folder)
        return grp

    # ── Hardware banner ────────────────────────────────────────────────────────

    def _build_hw_banner(self) -> QGroupBox:
        grp = QGroupBox("Your hardware")
        row = QHBoxLayout(grp)
        row.setSpacing(20)

        hw = self._hw

        # RAM
        ram_lbl = QLabel(f"<b>RAM</b><br>{hw.ram_gb:.1f} GB")
        ram_lbl.setAlignment(Qt.AlignCenter)
        row.addWidget(ram_lbl)

        self._add_vline(row)

        # CPU
        cpu_lbl = QLabel(f"<b>CPU</b><br>{hw.cpu_cores} cores<br>"
                         f"<small>{hw.cpu_name[:50]}</small>")
        cpu_lbl.setAlignment(Qt.AlignCenter)
        row.addWidget(cpu_lbl, stretch=1)

        self._add_vline(row)

        # GPU / accelerator
        if hw.gpus:
            for gpu in hw.gpus:
                tag  = "Unified memory" if gpu.is_unified else f"{gpu.vram_gb:.1f} GB VRAM"
                name = gpu.name[:50]
                lbl  = QLabel(f"<b>GPU</b><br>{tag}<br><small>{name}</small>")
                lbl.setAlignment(Qt.AlignCenter)
                row.addWidget(lbl, stretch=1)
        else:
            lbl = QLabel("<b>GPU</b><br>Not detected<br><small>CPU inference</small>")
            lbl.setAlignment(Qt.AlignCenter)
            row.addWidget(lbl)

        self._add_vline(row)

        # Effective memory
        eff = hw.effective_memory_gb
        eff_lbl = QLabel(
            f"<b>Available for model</b><br>"
            f"<span style='font-size:16px;color:#1565C0'><b>~{eff:.1f} GB</b></span>"
        )
        eff_lbl.setTextFormat(Qt.RichText)
        eff_lbl.setAlignment(Qt.AlignCenter)
        row.addWidget(eff_lbl)

        return grp

    @staticmethod
    def _add_vline(layout: QHBoxLayout) -> None:
        from PySide6.QtWidgets import QFrame
        line = QFrame()
        line.setFrameShape(QFrame.VLine)
        line.setFrameShadow(QFrame.Sunken)
        layout.addWidget(line)

    # ── Installed models group ─────────────────────────────────────────────────

    def _build_models_group(self) -> QGroupBox:
        grp = QGroupBox("Installed models")
        ml  = QVBoxLayout(grp)

        self.table = QTableWidget()
        self.table.setColumnCount(4)
        self.table.setHorizontalHeaderLabels(["Model", "Size", "Parameters", "Quantization"])
        hdr = self.table.horizontalHeader()
        hdr.setSectionResizeMode(0, QHeaderView.Stretch)
        hdr.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(3, QHeaderView.ResizeToContents)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.setAlternatingRowColors(True)
        ml.addWidget(self.table)

        btns = QHBoxLayout()
        self.btn_refresh = QPushButton("↻  Refresh")
        self.btn_refresh.clicked.connect(self._refresh)
        self.btn_delete = QPushButton("Delete selected model")
        self.btn_delete.setEnabled(False)
        self.btn_delete.setStyleSheet(
            "QPushButton{color:#C62828;} QPushButton:disabled{color:#aaa;}"
        )
        self.btn_delete.clicked.connect(self._delete_model)
        self.table.itemSelectionChanged.connect(
            lambda: self.btn_delete.setEnabled(bool(self.table.selectedItems()))
        )
        btns.addWidget(self.btn_refresh)
        btns.addStretch()
        btns.addWidget(self.btn_delete)
        ml.addLayout(btns)
        return grp

    # ── Pull new model group ───────────────────────────────────────────────────

    def _build_pull_group(self) -> QGroupBox:
        grp = QGroupBox("Download a new model")
        pl  = QVBoxLayout(grp)

        # Legend
        legend_row = QHBoxLayout()
        legend_row.setSpacing(14)
        for tier in (TIER_GREAT, TIER_OK, TIER_SLOW, TIER_NO):
            lbl = QLabel(TIER_LABEL[tier])
            lbl.setStyleSheet(
                f"color: {TIER_FG[tier].name()}; "
                f"background: {TIER_BG[tier].name()}; "
                "padding: 2px 6px; border-radius: 3px; font-size: 11px;"
            )
            legend_row.addWidget(lbl)
        legend_row.addStretch()
        pl.addLayout(legend_row)

        # Combo box with hardware-aware model list
        pull_row = QHBoxLayout()
        self.combo_pull = QComboBox()
        self.combo_pull.setEditable(True)
        self.combo_pull.setInsertPolicy(QComboBox.NoInsert)
        self.combo_pull.lineEdit().setPlaceholderText(
            "Select or type a model name  (e.g. llama3.2, mistral…)"
        )
        self._populate_model_combo()
        self.combo_pull.setCurrentIndex(-1)
        self.combo_pull.lineEdit().clear()
        self.combo_pull.currentIndexChanged.connect(self._on_model_selected)

        self.btn_pull = QPushButton("Download")
        self.btn_pull.setFixedWidth(100)
        self.btn_pull.setStyleSheet(
            "QPushButton{background:#1565C0;color:white;border-radius:3px;}"
            "QPushButton:disabled{background:#ccc;color:#888;}"
            "QPushButton:hover:!disabled{background:#0D47A1;}"
        )
        self.btn_pull.clicked.connect(self._pull_model)

        self.btn_pull_cancel = QPushButton("Cancel")
        self.btn_pull_cancel.setFixedWidth(70)
        self.btn_pull_cancel.setEnabled(False)
        self.btn_pull_cancel.clicked.connect(self._cancel_pull)

        pull_row.addWidget(self.combo_pull, stretch=1)
        pull_row.addWidget(self.btn_pull)
        pull_row.addWidget(self.btn_pull_cancel)
        pl.addLayout(pull_row)

        # Description of selected model
        self.lbl_model_desc = QLabel("")
        self.lbl_model_desc.setWordWrap(True)
        self.lbl_model_desc.setStyleSheet("font-size: 11px; color: #444; padding: 2px 0;")
        pl.addWidget(self.lbl_model_desc)

        self.lbl_pull_status = QLabel("")
        self.lbl_pull_status.setWordWrap(True)
        pl.addWidget(self.lbl_pull_status)

        self.bar_pull = QProgressBar()
        self.bar_pull.setTextVisible(True)
        self.bar_pull.setFormat("")
        self.bar_pull.setFixedHeight(14)
        self.bar_pull.setVisible(False)
        pl.addWidget(self.bar_pull)

        return grp

    def _populate_model_combo(self) -> None:
        """Fill the combo with models sorted and coloured by hardware compatibility."""
        hw      = self._hw
        specs   = sorted(MODEL_CATALOGUE, key=lambda s: TIER_ORDER[score(s, hw)])
        model   = QStandardItemModel(self.combo_pull)

        for spec in specs:
            tier  = score(spec, hw)
            label = f"{TIER_LABEL[tier]}   {spec.label}   (~{spec.size_gb:.1f} GB)"
            item  = QStandardItem(label)
            item.setData(spec.tag, Qt.UserRole)
            item.setData(spec, Qt.UserRole + 1)
            item.setForeground(TIER_FG[tier])
            item.setBackground(TIER_BG[tier])
            font = QFont()
            if tier == TIER_GREAT:
                font.setBold(True)
            item.setFont(font)
            item.setToolTip(
                f"{spec.label}\n"
                f"Minimum RAM: {spec.min_ram_gb:.0f} GB\n"
                f"Recommended: {spec.rec_ram_gb:.0f} GB\n"
                f"{spec.description}"
            )
            model.appendRow(item)

        self.combo_pull.setModel(model)

    @Slot(int)
    def _on_model_selected(self, index: int) -> None:
        if index < 0:
            self.lbl_model_desc.setText("")
            return
        item = self.combo_pull.model().item(index)
        if item is None:
            return
        spec: Optional[ModelSpec] = item.data(Qt.UserRole + 1)
        if spec is None:
            return
        tier = score(spec, self._hw)
        mem  = self._hw.effective_memory_gb
        self.lbl_model_desc.setText(
            f"{spec.description}  ·  "
            f"Needs ~{spec.min_ram_gb:.0f} GB, available ~{mem:.1f} GB  ·  "
            f"{TIER_LABEL[tier]}"
        )
        self.lbl_model_desc.setStyleSheet(
            f"font-size: 11px; color: {TIER_FG[tier].name()}; padding: 2px 0;"
        )

    # ── Server helpers ─────────────────────────────────────────────────────────

    def _refresh_status(self) -> None:
        running = ollama_manager.is_server_running(self.base_url)
        exe     = ollama_manager.find_ollama()
        if running:
            self.lbl_status.setText("● Server running")
            self.lbl_status.setStyleSheet("color: #2E7D32;")
            self.btn_start_stop.setText("Stop server")
            self.btn_start_stop.setEnabled(True)
        elif exe:
            self.lbl_status.setText("● Server offline")
            self.lbl_status.setStyleSheet("color: #C62828;")
            self.btn_start_stop.setText("Start server")
            self.btn_start_stop.setEnabled(True)
        else:
            self.lbl_status.setText("● Ollama not installed")
            self.lbl_status.setStyleSheet("color: #C62828;")
            self.btn_start_stop.setText("Start server")
            self.btn_start_stop.setEnabled(False)
        self.lbl_location.setText(str(exe) if exe else str(ollama_manager.OLLAMA_BIN))

    def _toggle_server(self) -> None:
        if ollama_manager.is_server_running(self.base_url):
            ollama_manager.stop_server()
            self._refresh_status()
        else:
            self.btn_start_stop.setEnabled(False)
            self.btn_start_stop.setText("Starting…")
            self._srv_worker = StartServerWorker()
            self._srv_thread = QThread()
            self._srv_worker.moveToThread(self._srv_thread)
            self._srv_thread.started.connect(self._srv_worker.run)
            self._srv_worker.finished.connect(self._on_server_toggled)
            self._srv_worker.finished.connect(self._srv_thread.quit)
            self._srv_thread.start()

    @Slot(bool)
    def _on_server_toggled(self, ok: bool) -> None:
        if not ok:
            QMessageBox.warning(self, "Server", "Could not start the Ollama server.")
        self._refresh()

    # ── Model list ─────────────────────────────────────────────────────────────

    def _refresh(self) -> None:
        self._refresh_status()
        self._load_models()

    def _load_models(self) -> None:
        self.table.setRowCount(0)
        try:
            resp = requests.get(f"{self.base_url}/api/tags", timeout=5)
            resp.raise_for_status()
            models = resp.json().get("models", [])
        except Exception:
            return

        self.table.setRowCount(len(models))
        for row, m in enumerate(models):
            details  = m.get("details", {})
            size_gb  = m.get("size", 0) / 1_073_741_824
            self.table.setItem(row, 0, QTableWidgetItem(m.get("name", "")))
            self.table.setItem(row, 1, QTableWidgetItem(f"{size_gb:.2f} GB"))
            self.table.setItem(row, 2, QTableWidgetItem(details.get("parameter_size", "—")))
            self.table.setItem(row, 3, QTableWidgetItem(details.get("quantization_level", "—")))

    # ── Delete ─────────────────────────────────────────────────────────────────

    def _delete_model(self) -> None:
        row = self.table.currentRow()
        if row < 0:
            return
        name = self.table.item(row, 0).text()
        if QMessageBox.question(
            self, "Delete model",
            f"Delete <b>{name}</b>?<br>The model files will be removed from disk.",
            QMessageBox.Yes | QMessageBox.No,
        ) != QMessageBox.Yes:
            return
        self.btn_delete.setEnabled(False)
        self._delete_worker = DeleteWorker(self.base_url, name)
        self._delete_thread = QThread()
        self._delete_worker.moveToThread(self._delete_thread)
        self._delete_thread.started.connect(self._delete_worker.run)
        self._delete_worker.finished.connect(self._on_delete_done)
        self._delete_worker.error.connect(self._on_delete_error)
        self._delete_worker.finished.connect(self._delete_thread.quit)
        self._delete_worker.error.connect(self._delete_thread.quit)
        self._delete_thread.start()

    @Slot()
    def _on_delete_done(self) -> None:
        self._refresh()

    @Slot(str)
    def _on_delete_error(self, msg: str) -> None:
        self.btn_delete.setEnabled(True)
        QMessageBox.critical(self, "Delete failed", f"Could not delete model:\n{msg}")

    # ── Pull ───────────────────────────────────────────────────────────────────

    def _pull_model(self) -> None:
        # If a catalogue item is selected, use its tag; otherwise use typed text
        idx   = self.combo_pull.currentIndex()
        model = ""
        if idx >= 0:
            item_data = self.combo_pull.model().item(idx)
            if item_data:
                model = item_data.data(Qt.UserRole) or ""
        if not model:
            model = self.combo_pull.currentText().strip()
        if not model:
            QMessageBox.warning(self, "Pull model", "Enter a model name first.")
            return
        if not ollama_manager.is_server_running(self.base_url):
            QMessageBox.warning(self, "Server offline",
                                "Start the Ollama server before downloading a model.")
            return
        self.btn_pull.setEnabled(False)
        self.btn_pull_cancel.setEnabled(True)
        self.bar_pull.setVisible(True)
        self.bar_pull.setRange(0, 0)
        self.lbl_pull_status.setText(f"Pulling {model}…")

        self._pull_worker = PullWorker(self.base_url, model)
        self._pull_thread = QThread()
        self._pull_worker.moveToThread(self._pull_thread)
        self._pull_thread.started.connect(self._pull_worker.run)
        self._pull_worker.progress.connect(self._on_pull_progress)
        self._pull_worker.finished.connect(self._on_pull_done)
        self._pull_worker.error.connect(self._on_pull_error)
        self._pull_worker.finished.connect(self._pull_thread.quit)
        self._pull_worker.error.connect(self._pull_thread.quit)
        self._pull_thread.start()

    def _cancel_pull(self) -> None:
        if self._pull_thread and self._pull_thread.isRunning():
            self._pull_thread.terminate()
            self._pull_thread.wait()
        self._reset_pull_ui()
        self.lbl_pull_status.setText("Cancelled.")

    @Slot(str, int, int)
    def _on_pull_progress(self, status: str, done: int, total: int) -> None:
        self.lbl_pull_status.setText(status)
        if total > 0:
            self.bar_pull.setRange(0, total)
            self.bar_pull.setValue(done)
            self.bar_pull.setFormat(
                f"{done/1_073_741_824:.2f} / {total/1_073_741_824:.2f} GB"
            )
        else:
            self.bar_pull.setRange(0, 0)
            self.bar_pull.setFormat("")

    @Slot()
    def _on_pull_done(self) -> None:
        self._reset_pull_ui()
        idx  = self.combo_pull.currentIndex()
        item = self.combo_pull.model().item(idx) if idx >= 0 else None
        name = (item.data(Qt.UserRole) if item else None) or self.combo_pull.currentText()
        self.lbl_pull_status.setText(f"✓ {name} downloaded successfully.")
        self._load_models()

    @Slot(str)
    def _on_pull_error(self, msg: str) -> None:
        self._reset_pull_ui()
        self.lbl_pull_status.setText(f"Error: {msg}")
        QMessageBox.critical(self, "Pull failed", f"Could not pull model:\n{msg}")

    def _reset_pull_ui(self) -> None:
        self.btn_pull.setEnabled(True)
        self.btn_pull_cancel.setEnabled(False)
        self.bar_pull.setVisible(False)
        self.bar_pull.setRange(0, 1)
        self.bar_pull.setValue(0)

    # ── Cache folder ───────────────────────────────────────────────────────────

    def _open_folder(self) -> None:
        path = str(ollama_manager.OLLAMA_HOME)
        if sys.platform == "darwin":
            subprocess.Popen(["open", path])
        elif sys.platform == "win32":
            subprocess.Popen(["explorer", path])
        else:
            subprocess.Popen(["xdg-open", path])
