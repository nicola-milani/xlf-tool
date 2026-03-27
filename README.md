# XLF Translator

Desktop and web application for translating XLIFF files (1.2 and 2.0) using a local LLM via [Ollama](https://ollama.com). No cloud services required — everything runs on your machine.

## Features

- Load and save XLIFF 1.2 and 2.0 files (compatible with Articulate Storyline)
- Translate segments using a local LLM (no internet required after model download)
- Manual editing of translated segments
- Side-by-side source/translated XML diff view
- **Project mode**: open a project folder with glossary, input XLF, and structured output
  - Glossary (`glossario.csv`): exact-match terms bypass LLM; partial matches applied post-translation
  - Auto-detects the XLF inside `input/`, writes translated file to `output/`
  - ZIP download of the full project folder
- **Web mode** (`--web`): browser-accessible interface for intranet/cloud deployment
  - Multiple concurrent sessions, each isolated in its own temporary directory
  - Real-time translation progress via Server-Sent Events
- Automatic Ollama download — no administrator privileges needed
- Model manager with hardware-aware recommendations based on your RAM/GPU
- Cross-platform: Windows, macOS, Linux

---

## Requirements

| Requirement | Version |
|-------------|---------|
| Python | 3.10 or later |
| PySide6 | 6.6.0 or later (desktop mode) |
| FastAPI + Uvicorn | 0.111 / 0.29 or later (web mode) |
| requests | 2.31.0 or later |
| Internet | Required only for first-time Ollama and model download |

---

## Development setup

```bash
# Clone or copy the project
cd xlf-translate

# Create a virtual environment
python3 -m venv .venv

# Activate it
# macOS / Linux:
source .venv/bin/activate
# Windows:
.venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt

# Desktop mode
python main.py

# Web mode
python main.py --web [--host 0.0.0.0] [--port 8080]
```

On first run in desktop mode, if Ollama is not found on the system, a setup wizard will appear to download it and pull a translation model.

---

## Project structure

```
xlf-translate/
├── main.py              # Entry point (desktop + web modes)
├── main_window.py       # Desktop application window (PySide6)
├── web_server.py        # FastAPI web server
├── xlf_parser.py        # XLIFF 1.2 / 2.0 parser and writer
├── project_manager.py   # Project folder management and glossary
├── llm_client.py        # Ollama HTTP client (translation)
├── ollama_manager.py    # Ollama binary download, server lifecycle
├── ollama_dialog.py     # Model manager dialog (desktop)
├── setup_dialog.py      # First-run setup wizard (desktop)
├── diff_dialog.py       # Side-by-side diff dialog (desktop)
├── hw_detect.py         # Hardware detection and model recommendations
├── static/
│   └── index.html       # Single-page frontend (web mode)
└── requirements.txt
```

---

## Project folder structure

When working in project mode, the folder must follow this layout:

```
myproject/
├── glossario.csv        # Optional — source,target term pairs
├── metadata.json        # Auto-managed — stats and timestamps
├── input/
│   └── file.xlf         # Exactly one XLF file (any name)
├── output/
│   └── file_translated.xlf   # Written on save/download
└── tmp/                 # Temporary files (excluded from ZIP)
```

**Desktop**: File → Open Project… (Ctrl+Shift+O) — picks the project folder directly.
**Web**: upload the project folder as a ZIP via the "Open Project ZIP" button.

### Glossary format (`glossario.csv`)

Comma or semicolon-separated, optional header row, UTF-8 (BOM optional):

```csv
source,target
Customer Portal,Portale Clienti
Submit,Invia
```

Terms are applied in two passes:
1. **Exact match** — if the entire segment matches a glossary term, the LLM is skipped.
2. **Substitution** — glossary terms found inside an LLM translation are replaced.

---

## Web mode

Start the server:

```bash
python main.py --web --host 0.0.0.0 --port 8080
```

Colleagues on the same network open `http://<server-ip>:8080` in any browser.

### Web API endpoints

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/` | Frontend |
| `POST` | `/upload` | Upload single XLF |
| `POST` | `/project/upload` | Upload project ZIP |
| `GET` | `/project/{job_id}/info` | Project metadata and file listing |
| `GET` | `/project/{job_id}/zip` | Download project as ZIP |
| `GET` | `/models` | List available Ollama models |
| `POST` | `/translate/{job_id}` | Start translation (async) |
| `GET` | `/progress/{job_id}` | SSE progress stream |
| `POST` | `/cancel/{job_id}` | Cancel running translation |
| `PATCH` | `/update/{job_id}` | Update a single segment manually |
| `GET` | `/download/{job_id}` | Download translated XLF |
| `GET` | `/diff/{job_id}` | Source vs translated XML |
| `DELETE` | `/session/{job_id}` | Delete session and temp files |

Session files are stored under `{tmpdir}/xlf-sessions/{job_id}/`.

### Nginx reverse proxy (optional)

```nginx
location / {
    proxy_pass http://127.0.0.1:8080;
    proxy_set_header X-Accel-Buffering no;   # required for SSE
}
```

---

## Distribution

### Option 1 — Run from source (recommended for developers)

Share the source folder. The end user only needs Python 3.10+ installed.

```bash
pip install -r requirements.txt
python main.py          # desktop
python main.py --web    # web
```

### Option 2 — Standalone executable with PyInstaller

Build a single-file executable that bundles Python, PySide6, FastAPI, and all dependencies.
No Python installation required on the target machine.
The same executable supports both desktop (default) and web (`--web`) mode.

#### Install PyInstaller

```bash
pip install pyinstaller
```

#### Build

```bash
# macOS / Linux — produces dist/xlf-translate
pyinstaller --onefile --windowed --name xlf-translate \
    --add-data "static:static" \
    --hidden-import uvicorn.logging \
    --hidden-import uvicorn.loops \
    --hidden-import uvicorn.loops.auto \
    --hidden-import uvicorn.protocols \
    --hidden-import uvicorn.protocols.http \
    --hidden-import uvicorn.protocols.http.auto \
    --hidden-import uvicorn.lifespan \
    --hidden-import uvicorn.lifespan.on \
    main.py

# Windows CMD — same flags, backslash separator for --add-data
pyinstaller --onefile --windowed --name xlf-translate ^
    --add-data "static;static" ^
    --hidden-import uvicorn.logging ^
    --hidden-import uvicorn.loops ^
    --hidden-import uvicorn.loops.auto ^
    --hidden-import uvicorn.protocols ^
    --hidden-import uvicorn.protocols.http ^
    --hidden-import uvicorn.protocols.http.auto ^
    --hidden-import uvicorn.lifespan ^
    --hidden-import uvicorn.lifespan.on ^
    main.py
```

The output will be in the `dist/` folder.

```bash
# Desktop mode (default)
./dist/xlf-translate

# Web mode — accessible from any browser on the same network
./dist/xlf-translate --web --host 0.0.0.0 --port 8080
```

> **Note:** On macOS, `--windowed` suppresses the terminal window. On Windows, use `--noconsole` as an alias.
> The `--add-data` separator is `:` on macOS/Linux and `;` on Windows.

#### macOS .app bundle

```bash
pyinstaller --windowed \
    --name "XLF Translator" \
    --osx-bundle-identifier com.yourorg.xlf-translate \
    --add-data "static:static" \
    main.py
```

This produces `dist/XLF Translator.app` which can be dragged to `/Applications`.

#### Sign the macOS app (optional, recommended for distribution)

```bash
codesign --deep --force --sign "Developer ID Application: Your Name (TEAMID)" \
    "dist/XLF Translator.app"
```

### Option 3 — Windows installer with Inno Setup

1. Build with PyInstaller (without `--onefile` to get a folder):
   ```
   pyinstaller --windowed --name xlf-translate --add-data "static;static" main.py
   ```
2. Point Inno Setup at `dist/xlf-translate/` to produce a standard `.exe` installer.
   A minimal Inno Setup script is shown below:

```iss
[Setup]
AppName=XLF Translator
AppVersion=1.0
DefaultDirName={autopf}\XLF Translator
DefaultGroupName=XLF Translator
OutputBaseFilename=XLF-Translator-Setup

[Files]
Source: "dist\xlf-translate\*"; DestDir: "{app}"; Flags: recursesubdirs

[Icons]
Name: "{group}\XLF Translator"; Filename: "{app}\xlf-translate.exe"
Name: "{commondesktop}\XLF Translator"; Filename: "{app}\xlf-translate.exe"
```

### Option 4 — macOS DMG with create-dmg

```bash
pip install create-dmg   # or brew install create-dmg
create-dmg \
    --volname "XLF Translator" \
    --window-size 600 400 \
    --app-drop-link 450 200 \
    "XLF-Translator.dmg" \
    "dist/XLF Translator.app"
```

---

## Ollama cache location

Ollama is stored inside the user profile — no administrator rights required:

| Platform | Path |
|----------|------|
| macOS / Linux | `~/.xlf-translator/ollama/` |
| Windows | `%USERPROFILE%\.xlf-translator\ollama\` |

Models are stored in `~/.xlf-translator/ollama/models/`. To free disk space, open **Ollama > Manage models** in the app and delete unused models, or delete the folder manually.

---

## Recommended models

The app automatically scores models based on your hardware. Approximate sizes:

| Model | Size | Min RAM | Notes |
|-------|------|---------|-------|
| llama3.2:1b | ~1.3 GB | 1.5 GB | Fastest |
| llama3.2 | ~2 GB | 3 GB | Good balance |
| phi4-mini | ~2.5 GB | 3.5 GB | Strong reasoning |
| gemma3 | ~3 GB | 4 GB | Multilingual |
| mistral | ~4.1 GB | 5 GB | Strong translation |
| llama3.1 | ~4.7 GB | 6 GB | Excellent quality |
| phi4 | ~8 GB | 10 GB | High quality |
| llama3.1:70b | ~40 GB | 42 GB | Best quality |

---

## Troubleshooting

**Ollama server does not start**
Run `ollama serve` manually in a terminal to see the error. Check that port 11434 is not in use.

**Model download is slow or fails**
The model is downloaded from Ollama's CDN. Check your internet connection or try again later. You can resume an interrupted download by clicking "Download model" again.

**App crashes on first run (Windows)**
Make sure the Microsoft Visual C++ Redistributable is installed. PyInstaller bundles most things but relies on the system MSVC runtime.

**Translation quality is poor**
Try a larger model. Open **Ollama > Manage models**, download a higher-tier model, then select it from the model dropdown in the main window.

**SSE progress stops in web mode behind a proxy**
Add `proxy_set_header X-Accel-Buffering no;` to your Nginx location block. Some proxies buffer responses and break the event stream.
