# CLAUDE.md — XLF Translator

## Panoramica

Applicazione per tradurre file XLIFF (1.2 e 2.0) con un LLM locale via Ollama.
Supporta due modalità di avvio dallo stesso eseguibile:

- **Desktop** (default): GUI PySide6/Qt
- **Web** (`--web`): server FastAPI accessibile da browser, pensato per intranet/cloud

## Comandi principali

```bash
# Avvio desktop
python main.py

# Avvio web (sviluppo locale)
python main.py --web

# Avvio web su rete locale
python main.py --web --host 0.0.0.0 --port 8080

# Build distribuzione
pyinstaller --onefile --windowed --name xlf-translate \
    --add-data "static:static" \
    --hidden-import uvicorn.logging \
    --hidden-import uvicorn.loops --hidden-import uvicorn.loops.auto \
    --hidden-import uvicorn.protocols --hidden-import uvicorn.protocols.http \
    --hidden-import uvicorn.protocols.http.auto \
    --hidden-import uvicorn.lifespan --hidden-import uvicorn.lifespan.on \
    main.py
```

## Architettura

```
main.py                  # Entry point — sceglie modalità desktop o web
├── main_window.py       # GUI PySide6 (solo modalità desktop)
├── web_server.py        # FastAPI app (solo modalità web)
│   └── static/index.html   # SPA vanilla JS
├── xlf_parser.py        # Parser/writer XLIFF 1.2 e 2.0 — condiviso
├── project_manager.py   # Gestione cartella progetto e glossario — condiviso
├── llm_client.py        # Client HTTP Ollama — condiviso
├── ollama_manager.py    # Download e lifecycle del processo Ollama
├── ollama_dialog.py     # Dialog gestione modelli (desktop)
├── diff_dialog.py       # Dialog diff sorgente/tradotto (desktop)
├── setup_dialog.py      # Wizard primo avvio (desktop)
└── hw_detect.py         # Rilevamento hardware, raccomandazione modelli
```

**Principio chiave**: `xlf_parser`, `llm_client`, `project_manager` sono completamente
disaccoppiati dalla UI e riusati identicamente da entrambe le modalità.

## Modalità progetto

Un "progetto" è una cartella con questa struttura:

```
myproject/
├── glossario.csv     # Opzionale — coppie source,target
├── metadata.json     # Auto-gestito
├── input/file.xlf    # Esattamente un file XLF
├── output/           # Creato dall'app — XLF tradotto
└── tmp/              # File temporanei (esclusi dallo ZIP)
```

- **Desktop**: File → Open Project… (Ctrl+Shift+O)
- **Web**: upload ZIP della cartella progetto su `POST /project/upload`

### Glossario (`glossario.csv`)

Strategia a due livelli applicata in `_translation_worker` (web) e `TranslationWorker.run()` (desktop):
1. **Exact match** (`glossary_exact`): se il segmento corrisponde esattamente, si salta l'LLM
2. **Substitution** (`glossary_substitute`): i termini trovati nell'output LLM vengono sostituiti

## Web server — sessioni

Ogni upload crea una directory isolata:
```
{tempdir}/xlf-sessions/{job_id}/
├── input.xlf           # XLF caricato (plain upload)
├── translated.xlf      # Generato su /download
└── {project_name}/     # Estratto da ZIP (project upload)
    ├── input/ output/ tmp/
    └── metadata.json
```

`DELETE /session/{job_id}` fa `shutil.rmtree` della directory.

## SSE (progresso traduzione)

- `POST /translate/{job_id}` cattura il loop con `asyncio.get_running_loop()` e lo passa al thread
- Il worker chiama `loop.call_soon_threadsafe(queue.put_nowait, event)` dopo ogni segmento tradotto
- `GET /progress/{job_id}` consuma la queue e scrive `data: {...}\n\n`
- Ogni evento contiene `{current, total, unit_id, translations: [{unit_id, pc_id, target}]}`
- Il frontend aggiorna la cella Target immediatamente ad ogni evento

## PyInstaller — frozen executable

```python
_BASE = Path(sys._MEIPASS) if getattr(sys, "frozen", False) else Path(__file__).parent
STATIC_DIR = _BASE / "static"
```

Il flag `--add-data "static:static"` include la cartella nel bundle.

## CI/CD

`.github/workflows/build.yml` — matrice su 5 target:
- `ubuntu-latest` → linux-x86_64
- `ubuntu-24.04-arm` → linux-arm64
- `macos-latest` → macos-arm64 (Intel supportato via Rosetta 2)
- `windows-latest` → windows-x86_64
- `windows-11-arm` → windows-arm64 (public beta)

Trigger: push di tag `v*` → build + GitHub Release automatica.
