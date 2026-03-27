"""
First-run setup dialog.
Downloads Ollama into the app cache and optionally pulls a translation model.
No administrator privileges are required.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

import requests
from PySide6.QtCore import Qt, QObject, QThread, Signal, Slot
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QLabel, QProgressBar,
    QPushButton, QComboBox, QGroupBox, QMessageBox,
)

import ollama_manager

SUGGESTED_MODELS = [
    ("llama3.2  · 3B · ~2 GB  (recommended)", "llama3.2"),
    ("llama3.2:1b  · 1B · ~1 GB  (fastest, lower quality)", "llama3.2:1b"),
    ("llama3.1  · 8B · ~5 GB  (best quality)", "llama3.1"),
    ("mistral  · 7B · ~4 GB", "mistral"),
    ("gemma3  · 4B · ~3 GB", "gemma3"),
    ("phi4-mini  · 3.8B · ~2.5 GB", "phi4-mini"),
]


# ── Background workers ─────────────────────────────────────────────────────────

class DownloadOllamaWorker(QObject):
    progress = Signal(str, int, int)
    finished = Signal(object)   # Path on success
    error    = Signal(str)

    def run(self) -> None:
        try:
            exe = ollama_manager.download_and_install(
                lambda msg, d, t: self.progress.emit(msg, d, t)
            )
            self.finished.emit(exe)
        except Exception as exc:
            self.error.emit(str(exc))


class StartServerWorker(QObject):
    finished = Signal(bool)

    def __init__(self, exe: Path):
        super().__init__()
        self._exe = exe

    def run(self) -> None:
        ok = ollama_manager.start_server(self._exe)
        self.finished.emit(ok)


class PullModelWorker(QObject):
    progress = Signal(str, int, int)
    finished = Signal()
    error    = Signal(str)

    def __init__(self, base_url: str, model: str):
        super().__init__()
        self._url   = base_url.rstrip("/")
        self._model = model

    def run(self) -> None:
        try:
            self.progress.emit("Connecting to Ollama…", 0, 0)
            resp = requests.post(
                f"{self._url}/api/pull",
                json={"name": self._model, "stream": True},
                stream=True,
                timeout=3600,
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


# ── Dialog ─────────────────────────────────────────────────────────────────────

class SetupDialog(QDialog):
    """
    Shown the first time the app runs when Ollama is not found.
    Step 1 — download Ollama binary  (no admin needed)
    Step 2 — pull a translation model
    """

    def __init__(self, base_url: str = "http://127.0.0.1:11434", parent=None):
        super().__init__(parent)
        self.base_url     = base_url
        self._ollama_exe: Optional[Path] = None

        # Keep explicit references to every thread/worker so Python's GC
        # never destroys them while they are still running.
        self._dl_thread:   Optional[QThread] = None
        self._dl_worker:   Optional[DownloadOllamaWorker] = None
        self._srv_thread:  Optional[QThread] = None
        self._srv_worker:  Optional[StartServerWorker]    = None
        self._pull_thread: Optional[QThread] = None
        self._pull_worker: Optional[PullModelWorker]      = None

        self.setWindowTitle("XLF Translator — First-time Setup")
        self.setMinimumWidth(500)
        self.setWindowFlag(Qt.WindowCloseButtonHint, False)
        self._build_ui()

    # ── UI ─────────────────────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setSpacing(14)

        intro = QLabel(
            "<b>Ollama is not installed on this system.</b><br><br>"
            "XLF Translator will download Ollama into your user profile — "
            "no administrator password is needed.<br>"
            f"<small>Location: <code>{ollama_manager.OLLAMA_HOME}</code></small>"
        )
        intro.setTextFormat(Qt.RichText)
        intro.setWordWrap(True)
        layout.addWidget(intro)

        # Step 1 ───────────────────────────────────────────────────────────────
        grp1 = QGroupBox("Step 1 — Download Ollama runtime (~50 MB)")
        g1   = QVBoxLayout(grp1)
        self.lbl1 = QLabel("Press the button below to start.")
        self.lbl1.setWordWrap(True)
        g1.addWidget(self.lbl1)
        self.bar1 = QProgressBar()
        self.bar1.setTextVisible(True)
        self.bar1.setFormat("")
        g1.addWidget(self.bar1)
        self.btn_download = QPushButton("Download Ollama")
        self.btn_download.setFixedHeight(32)
        self.btn_download.clicked.connect(self._start_download)
        g1.addWidget(self.btn_download)
        layout.addWidget(grp1)

        # Step 2 ───────────────────────────────────────────────────────────────
        self.grp2 = QGroupBox("Step 2 — Download a translation model")
        self.grp2.setEnabled(False)
        g2 = QVBoxLayout(self.grp2)

        note = QLabel(
            "Choose a model. Larger models produce better translations "
            "but require more disk space and RAM."
        )
        note.setWordWrap(True)
        g2.addWidget(note)

        self.combo_model = QComboBox()
        for label, tag in SUGGESTED_MODELS:
            self.combo_model.addItem(label, tag)
        g2.addWidget(self.combo_model)

        self.lbl2 = QLabel("")
        self.lbl2.setWordWrap(True)
        g2.addWidget(self.lbl2)

        self.bar2 = QProgressBar()
        self.bar2.setTextVisible(True)
        self.bar2.setFormat("")
        g2.addWidget(self.bar2)

        self.btn_pull = QPushButton("Download model")
        self.btn_pull.setFixedHeight(32)
        self.btn_pull.clicked.connect(self._start_pull)
        g2.addWidget(self.btn_pull)

        self.btn_skip = QPushButton("Skip — I will add a model later")
        self.btn_skip.setFixedHeight(28)
        self.btn_skip.clicked.connect(self._skip_model)
        g2.addWidget(self.btn_skip)

        layout.addWidget(self.grp2)

        # Finish ───────────────────────────────────────────────────────────────
        self.btn_start = QPushButton("Start using XLF Translator")
        self.btn_start.setFixedHeight(38)
        self.btn_start.setEnabled(False)
        self.btn_start.setStyleSheet(
            "QPushButton{background:#4CAF50;color:white;font-weight:bold;"
            "border-radius:4px;font-size:13px;}"
            "QPushButton:disabled{background:#ccc;color:#888;}"
            "QPushButton:hover:!disabled{background:#43A047;}"
        )
        self.btn_start.clicked.connect(self.accept)
        layout.addWidget(self.btn_start)

    # ── Step 1: download Ollama ────────────────────────────────────────────────

    def _start_download(self) -> None:
        self.btn_download.setEnabled(False)
        self.bar1.setRange(0, 0)

        self._dl_worker = DownloadOllamaWorker()
        self._dl_thread = QThread()
        self._dl_worker.moveToThread(self._dl_thread)
        self._dl_thread.started.connect(self._dl_worker.run)
        self._dl_worker.progress.connect(self._on_dl_progress)
        self._dl_worker.finished.connect(self._on_dl_done)
        self._dl_worker.error.connect(self._on_dl_error)
        # Quit the thread's event loop when the worker signals done/error,
        # but do NOT use deleteLater — we keep the Python objects alive via self.
        self._dl_worker.finished.connect(self._dl_thread.quit)
        self._dl_worker.error.connect(self._dl_thread.quit)
        self._dl_thread.start()

    @Slot(str, int, int)
    def _on_dl_progress(self, msg: str, done: int, total: int) -> None:
        self.lbl1.setText(msg)
        if total > 0:
            self.bar1.setRange(0, total)
            self.bar1.setValue(done)
            self.bar1.setFormat(f"{done/1_048_576:.1f} / {total/1_048_576:.1f} MB")
        else:
            self.bar1.setRange(0, 0)
            self.bar1.setFormat("")

    @Slot(object)
    def _on_dl_done(self, exe: object) -> None:
        self._ollama_exe = exe  # type: ignore[assignment]
        self.bar1.setRange(0, 1)
        self.bar1.setValue(1)
        self.bar1.setFormat("Done")
        self.lbl1.setText("Ollama downloaded. Starting server…")
        self._start_server()

    @Slot(str)
    def _on_dl_error(self, msg: str) -> None:
        self.bar1.setRange(0, 1)
        self.bar1.setValue(0)
        self.lbl1.setText(f"Error: {msg}")
        self.btn_download.setEnabled(True)
        QMessageBox.critical(self, "Download failed",
                             f"Could not download Ollama:\n\n{msg}")

    # ── Start server ───────────────────────────────────────────────────────────

    def _start_server(self) -> None:
        self._srv_worker = StartServerWorker(self._ollama_exe)
        self._srv_thread = QThread()
        self._srv_worker.moveToThread(self._srv_thread)
        self._srv_thread.started.connect(self._srv_worker.run)
        self._srv_worker.finished.connect(self._on_server_ready)
        self._srv_worker.finished.connect(self._srv_thread.quit)
        self._srv_thread.start()

    @Slot(bool)
    def _on_server_ready(self, ok: bool) -> None:
        if ok:
            self.lbl1.setText("Ollama server is running.")
        else:
            self.lbl1.setText(
                "Ollama downloaded, but the server did not respond. "
                "You can start it manually with:  ollama serve"
            )
        self.grp2.setEnabled(True)

    # ── Step 2: pull model ─────────────────────────────────────────────────────

    def _start_pull(self) -> None:
        model = self.combo_model.currentData()
        self.btn_pull.setEnabled(False)
        self.btn_skip.setEnabled(False)
        self.bar2.setRange(0, 0)

        self._pull_worker = PullModelWorker(self.base_url, model)
        self._pull_thread = QThread()
        self._pull_worker.moveToThread(self._pull_thread)
        self._pull_thread.started.connect(self._pull_worker.run)
        self._pull_worker.progress.connect(self._on_pull_progress)
        self._pull_worker.finished.connect(self._on_pull_done)
        self._pull_worker.error.connect(self._on_pull_error)
        self._pull_worker.finished.connect(self._pull_thread.quit)
        self._pull_worker.error.connect(self._pull_thread.quit)
        self._pull_thread.start()

    @Slot(str, int, int)
    def _on_pull_progress(self, msg: str, done: int, total: int) -> None:
        self.lbl2.setText(msg)
        if total > 0:
            self.bar2.setRange(0, total)
            self.bar2.setValue(done)
            self.bar2.setFormat(f"{done/1_048_576:.1f} / {total/1_048_576:.1f} MB")
        else:
            self.bar2.setRange(0, 0)
            self.bar2.setFormat("")

    @Slot()
    def _on_pull_done(self) -> None:
        self.bar2.setRange(0, 1)
        self.bar2.setValue(1)
        self.bar2.setFormat("Done")
        self.lbl2.setText(f"Model '{self.combo_model.currentData()}' is ready.")
        self.btn_start.setEnabled(True)

    @Slot(str)
    def _on_pull_error(self, msg: str) -> None:
        self.bar2.setRange(0, 1)
        self.lbl2.setText(f"Error: {msg}")
        self.btn_pull.setEnabled(True)
        self.btn_skip.setEnabled(True)
        QMessageBox.critical(self, "Model download failed",
                             f"Could not pull model:\n\n{msg}")

    def _skip_model(self) -> None:
        self.btn_start.setEnabled(True)
