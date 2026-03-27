"""
FastAPI web server for XLF Translator.

Start with:
    python main.py --web [--host 0.0.0.0] [--port 8080]

Each session gets an isolated directory under SESSIONS_DIR.
When a project ZIP is uploaded the directory follows the project structure:
    {session_dir}/
        glossario.csv
        input/file.xlf
        output/file_translated.xlf
        tmp/
        metadata.json

For plain XLF uploads the session dir holds input.xlf and translated.xlf directly.

Endpoints:
    GET    /                         Frontend
    POST   /upload                   Single XLF upload
    POST   /project/upload           Project ZIP upload
    GET    /project/{job_id}/info    Project metadata + file listing
    GET    /project/{job_id}/zip     Download project as ZIP
    GET    /models                   List Ollama models
    POST   /translate/{job_id}       Start translation
    GET    /progress/{job_id}        SSE progress stream
    POST   /cancel/{job_id}          Cancel translation
    PATCH  /update/{job_id}          Update one segment
    GET    /download/{job_id}        Download translated XLF
    GET    /diff/{job_id}            Source vs translated XML
    DELETE /session/{job_id}         Delete session directory
"""
import asyncio
import json
import shutil
import tempfile
import uuid
import sys as _sys
from dataclasses import dataclass
from itertools import groupby
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from pydantic import BaseModel

from llm_client import OllamaClient
from project_manager import Project, glossary_exact, glossary_substitute
from xlf_parser import OutputMode, XlfParser

# ── Directories ───────────────────────────────────────────────────────────────

_BASE = Path(_sys._MEIPASS) if getattr(_sys, "frozen", False) else Path(__file__).parent

STATIC_DIR   = _BASE / "static"
SESSIONS_DIR = Path(tempfile.gettempdir()) / "xlf-sessions"

STATIC_DIR.mkdir(exist_ok=True)
SESSIONS_DIR.mkdir(exist_ok=True)

# ── App ───────────────────────────────────────────────────────────────────────

app = FastAPI(title="XLF Translator")


@app.get("/", response_class=HTMLResponse)
async def root():
    index = STATIC_DIR / "index.html"
    if not index.exists():
        raise HTTPException(status_code=404, detail="Frontend not found")
    return HTMLResponse(index.read_text(encoding="utf-8"))


# ── Session store ─────────────────────────────────────────────────────────────


@dataclass
class Session:
    parser:         XlfParser
    session_dir:    Path           # root working dir for this session
    input_file:     Path           # uploaded XLF
    original_stem:  str            # filename without extension
    progress_queue: asyncio.Queue
    project:        Optional[Project] = None   # set when a project ZIP was uploaded
    cancelled:      bool = False
    translate_task: Optional[asyncio.Task] = None


_sessions: dict[str, Session] = {}


def _get_session(job_id: str) -> Session:
    s = _sessions.get(job_id)
    if s is None:
        raise HTTPException(status_code=404, detail="Session not found")
    return s


# ── Plain XLF upload ──────────────────────────────────────────────────────────


@app.post("/upload")
async def upload(file: UploadFile = File(...)):
    content       = await file.read()
    original_name = Path(file.filename or "file.xlf")
    suffix        = original_name.suffix or ".xlf"
    original_stem = original_name.stem or "file"

    job_id      = str(uuid.uuid4())
    session_dir = SESSIONS_DIR / job_id
    session_dir.mkdir()

    input_file = session_dir / f"input{suffix}"
    input_file.write_bytes(content)

    parser = XlfParser()
    try:
        parser.load(str(input_file))
    except Exception as exc:
        shutil.rmtree(session_dir, ignore_errors=True)
        raise HTTPException(status_code=422, detail=f"Parse error: {exc}")

    _sessions[job_id] = Session(
        parser=parser,
        session_dir=session_dir,
        input_file=input_file,
        original_stem=original_stem,
        progress_queue=asyncio.Queue(),
    )

    return _session_response(job_id, _sessions[job_id])


# ── Project ZIP upload ────────────────────────────────────────────────────────


@app.post("/project/upload")
async def upload_project(file: UploadFile = File(...)):
    """
    Accept a ZIP of a project folder.  Extracts it, detects the XLF inside
    input/, loads the glossary, and returns the same segment payload as /upload.
    """
    content = await file.read()
    job_id  = str(uuid.uuid4())
    base    = SESSIONS_DIR / job_id
    base.mkdir()

    zip_path = base / "upload.zip"
    zip_path.write_bytes(content)

    try:
        project = await asyncio.to_thread(Project.from_zip, zip_path, base)
        zip_path.unlink(missing_ok=True)
        xlf = project.find_xlf()
        if xlf is None:
            shutil.rmtree(base, ignore_errors=True)
            raise HTTPException(
                status_code=422,
                detail="No .xlf file found in input/ folder of the project ZIP.",
            )
    except HTTPException:
        raise
    except Exception as exc:
        shutil.rmtree(base, ignore_errors=True)
        raise HTTPException(status_code=422, detail=f"Project error: {exc}")

    parser = XlfParser()
    try:
        parser.load(str(xlf))
    except Exception as exc:
        shutil.rmtree(base, ignore_errors=True)
        raise HTTPException(status_code=422, detail=f"Parse error: {exc}")

    _sessions[job_id] = Session(
        parser=parser,
        session_dir=base,
        input_file=xlf,
        original_stem=xlf.stem,
        progress_queue=asyncio.Queue(),
        project=project,
    )

    resp = _session_response(job_id, _sessions[job_id])
    resp["project"] = project.list_files()
    resp["project_name"] = project.name
    return resp


def _session_response(job_id: str, session: Session) -> dict:
    parser = session.parser
    segments = [
        {
            "unit_id":    s.unit_id,
            "source":     s.source,
            "target":     s.target,
            "note":       s.note,
            "unit_type":  s.unit_type,
            "pc_id":      s.pc_id,
            "is_alt_text": s.unit_id.endswith(".AltText"),
        }
        for s in parser.segments
    ]
    return {
        "job_id":      job_id,
        "source_lang": parser.source_lang,
        "target_lang": parser.target_lang,
        "version":     parser.version,
        "original":    parser.original,
        "segments":    segments,
    }


# ── Project info & ZIP download ───────────────────────────────────────────────


@app.get("/project/{job_id}/info")
async def project_info(job_id: str):
    session = _get_session(job_id)
    if session.project is None:
        raise HTTPException(status_code=400, detail="Session has no project")
    info = await asyncio.to_thread(session.project.list_files)
    info["project_name"] = session.project.name
    return info


@app.get("/project/{job_id}/zip")
async def project_zip(job_id: str):
    session = _get_session(job_id)
    if session.project is None:
        raise HTTPException(status_code=400, detail="Session has no project")
    zip_path = await asyncio.to_thread(session.project.make_zip)
    return FileResponse(
        path=str(zip_path),
        media_type="application/zip",
        filename=f"{session.project.name}.zip",
    )


# ── Models ────────────────────────────────────────────────────────────────────


@app.get("/models")
async def list_models(url: str = "http://127.0.0.1:11434"):
    models = await asyncio.to_thread(OllamaClient(base_url=url).list_models)
    return {"models": models}


# ── Translation ───────────────────────────────────────────────────────────────


class TranslateRequest(BaseModel):
    ollama_url:  str  = "http://127.0.0.1:11434"
    model:       str  = "llama3.2"
    target_lang: str  = "it-IT"
    seg_filter:  str  = "skip_alt"
    empty_only:  bool = True
    output_mode: str  = "replace"


def _translation_worker(
    session: Session,
    req:     TranslateRequest,
    loop:    asyncio.AbstractEventLoop,
) -> None:
    parser   = session.parser
    client   = OllamaClient(base_url=req.ollama_url, model=req.model)
    glossary = session.project.load_glossary() if session.project else {}

    segments = list(parser.segments)
    if req.empty_only:
        segments = [s for s in segments if not s.target]
    if req.seg_filter == "skip_alt":
        segments = [s for s in segments if not s.unit_id.endswith(".AltText")]
    elif req.seg_filter == "only_alt":
        segments = [s for s in segments if s.unit_id.endswith(".AltText")]

    groups = [(uid, list(grp)) for uid, grp in groupby(segments, key=lambda s: s.unit_id)]
    total  = len(groups)

    for i, (uid, group_segs) in enumerate(groups):
        if session.cancelled:
            break
        try:
            if len(group_segs) == 1:
                seg   = group_segs[0]
                gloss = glossary_exact(seg.source, glossary)
                if gloss is not None:
                    translated = gloss
                else:
                    translated = client.translate(seg.source, parser.source_lang, req.target_lang)
                    translated = glossary_substitute(translated, glossary)
                parser.update_target(seg.unit_id, translated, seg.pc_id)
                done_segs = [{"unit_id": seg.unit_id, "pc_id": seg.pc_id, "target": translated}]
            else:
                results: list = [None] * len(group_segs)
                pending_idx   = []
                for j, seg in enumerate(group_segs):
                    gloss = glossary_exact(seg.source, glossary)
                    if gloss is not None:
                        results[j] = gloss
                    else:
                        pending_idx.append(j)
                if pending_idx:
                    pending_texts = [group_segs[j].source for j in pending_idx]
                    llm = (
                        [client.translate(pending_texts[0], parser.source_lang, req.target_lang)]
                        if len(pending_texts) == 1
                        else client.translate_batch(pending_texts, parser.source_lang, req.target_lang)
                    )
                    for j, t in zip(pending_idx, llm):
                        results[j] = glossary_substitute(t, glossary)
                done_segs = []
                for seg, translated in zip(group_segs, results):
                    parser.update_target(seg.unit_id, translated, seg.pc_id)
                    done_segs.append({"unit_id": seg.unit_id, "pc_id": seg.pc_id, "target": translated})
        except Exception as exc:
            loop.call_soon_threadsafe(session.progress_queue.put_nowait, {"error": str(exc)})
            return

        loop.call_soon_threadsafe(
            session.progress_queue.put_nowait,
            {"current": i + 1, "total": total, "unit_id": uid, "translations": done_segs},
        )

    loop.call_soon_threadsafe(session.progress_queue.put_nowait, {"done": True})


@app.post("/translate/{job_id}", status_code=202)
async def start_translate(job_id: str, req: TranslateRequest):
    session = _get_session(job_id)
    if session.translate_task and not session.translate_task.done():
        raise HTTPException(status_code=409, detail="Translation already running")
    session.cancelled = False
    while not session.progress_queue.empty():
        session.progress_queue.get_nowait()
    loop = asyncio.get_running_loop()
    session.translate_task = asyncio.create_task(
        asyncio.to_thread(_translation_worker, session, req, loop)
    )
    return {"status": "started"}


@app.post("/cancel/{job_id}")
async def cancel_translate(job_id: str):
    _get_session(job_id).cancelled = True
    return {"status": "cancel_requested"}


# ── SSE progress ──────────────────────────────────────────────────────────────


@app.get("/progress/{job_id}")
async def progress_stream(job_id: str):
    session = _get_session(job_id)

    async def event_generator():
        while True:
            try:
                event = await asyncio.wait_for(session.progress_queue.get(), timeout=30)
            except asyncio.TimeoutError:
                yield "data: {\"heartbeat\": true}\n\n"
                continue
            yield f"data: {json.dumps(event)}\n\n"
            if event.get("done") or event.get("error"):
                break

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ── Manual update ─────────────────────────────────────────────────────────────


class UpdateRequest(BaseModel):
    unit_id: str
    pc_id:   str = ""
    target:  str


@app.patch("/update/{job_id}")
async def update_segment(job_id: str, body: UpdateRequest):
    _get_session(job_id).parser.update_target(body.unit_id, body.target, body.pc_id)
    return {"status": "ok"}


# ── Download translated XLF ───────────────────────────────────────────────────


@app.get("/download/{job_id}")
async def download(job_id: str, mode: str = "replace", target_lang: str = ""):
    session = _get_session(job_id)
    parser  = session.parser

    if target_lang:
        parser.set_target_language(target_lang)

    output_mode = OutputMode.REPLACE if mode == "replace" else OutputMode.TARGET

    # Save to project output/ if available, else to session root
    if session.project:
        out_dir = session.project.output_dir
        out_dir.mkdir(exist_ok=True)
    else:
        out_dir = session.session_dir

    out_file      = out_dir / "translated.xlf"
    download_name = f"{session.original_stem}_translated.xlf"

    try:
        await asyncio.to_thread(parser.save, str(out_file), output_mode)
        if session.project:
            total      = len(parser.segments)
            translated = sum(1 for s in parser.segments if s.target)
            session.project.save_metadata({
                "target_lang":         target_lang or parser.target_lang,
                "segments_total":      total,
                "segments_translated": translated,
            })
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))

    return FileResponse(
        path=str(out_file),
        media_type="application/xml",
        filename=download_name,
    )


# ── Diff ──────────────────────────────────────────────────────────────────────


@app.get("/diff/{job_id}")
async def get_diff(job_id: str, mode: str = "replace"):
    session     = _get_session(job_id)
    output_mode = OutputMode.REPLACE if mode == "replace" else OutputMode.TARGET
    source_xml     = await asyncio.to_thread(session.parser.get_source_xml)
    translated_xml = await asyncio.to_thread(session.parser.get_translated_xml, output_mode)
    return {"source": source_xml, "translated": translated_xml}


# ── Session cleanup ───────────────────────────────────────────────────────────


@app.delete("/session/{job_id}")
async def delete_session(job_id: str):
    session = _sessions.pop(job_id, None)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")
    if session.translate_task and not session.translate_task.done():
        session.cancelled = True
    shutil.rmtree(session.session_dir, ignore_errors=True)
    return {"status": "deleted"}
