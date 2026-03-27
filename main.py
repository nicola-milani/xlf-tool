"""
XLF Translator — entry point.
Translates XLIFF 1.2 / 2.0 files using a local Ollama LLM.

Desktop mode (default):
    python main.py

Web mode (intranet / cloud):
    python main.py --web [--host 0.0.0.0] [--port 8080]

First run (desktop only):
  If Ollama is not found in PATH or the app cache, a setup dialog is shown
  that downloads Ollama and a translation model — no admin privileges needed.

Requirements:
    pip install PySide6 requests            # desktop
    pip install fastapi uvicorn python-multipart  # web
"""
import sys

import ollama_manager


def _web_main(host: str, port: int) -> None:
    """Start the FastAPI web server."""
    try:
        import uvicorn
    except ImportError:
        print("uvicorn not installed. Run: pip install fastapi uvicorn python-multipart")
        sys.exit(1)
    from web_server import app  # noqa: local import

    # Ensure Ollama is running before accepting requests
    exe = ollama_manager.find_ollama()
    if exe and not ollama_manager.is_server_running():
        import threading
        threading.Thread(target=ollama_manager.start_server, args=(exe,), daemon=True).start()

    print(f"XLF Translator web UI → http://{host}:{port}")
    uvicorn.run(app, host=host, port=port)


def main() -> None:
    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )
    app = QApplication(sys.argv)
    app.setApplicationName("XLF Translator")
    app.setOrganizationName("xlf-translate")
    app.setStyle("Fusion")

    # ── First-run: Ollama not installed anywhere ───────────────────────────────
    exe = ollama_manager.find_ollama()
    if exe is None:
        from setup_dialog import SetupDialog
        dlg = SetupDialog()
        dlg.exec()
        exe = ollama_manager.find_ollama()  # may now exist after download

    # ── Ensure the server is running (non-blocking: best effort) ──────────────
    if exe and not ollama_manager.is_server_running():
        # Start server in a background thread so the UI is not blocked.
        # main_window._refresh_models() will retry automatically via the ↻ button.
        import threading
        threading.Thread(
            target=ollama_manager.start_server,
            args=(exe,),
            daemon=True,
        ).start()

    # ── Main window ───────────────────────────────────────────────────────────
    from main_window import MainWindow
    window = MainWindow()
    window.show()

    ret = app.exec()
    ollama_manager.stop_server()   # graceful shutdown of any server we started
    sys.exit(ret)


if __name__ == "__main__":
    args = sys.argv[1:]
    if "--web" in args:
        host = "127.0.0.1"
        port = 8080
        if "--host" in args:
            host = args[args.index("--host") + 1]
        if "--port" in args:
            port = int(args[args.index("--port") + 1])
        _web_main(host, port)
    else:
        main()
