"""
Manages a portable Ollama installation in ~/.xlf-translator/ollama/.
No administrator / root privileges required.
"""
import os
import sys
import platform
import shutil
import subprocess
import tarfile
import zipfile
import time
from pathlib import Path
from typing import Optional, Callable, Tuple
import requests

# ── App cache paths ────────────────────────────────────────────────────────────
CACHE_ROOT    = Path.home() / ".xlf-translator"
OLLAMA_HOME   = CACHE_ROOT / "ollama"
OLLAMA_BIN    = OLLAMA_HOME / "bin"
OLLAMA_MODELS = OLLAMA_HOME / "models"

GITHUB_API = "https://api.github.com/repos/ollama/ollama/releases/latest"

# ── Executable helpers ─────────────────────────────────────────────────────────

def _exe_name() -> str:
    return "ollama.exe" if sys.platform == "win32" else "ollama"

def _local_exe() -> Path:
    return OLLAMA_BIN / _exe_name()

def find_ollama() -> Optional[Path]:
    """Return the ollama executable: system PATH first, then app cache."""
    found = shutil.which("ollama")
    if found:
        return Path(found)
    local = _local_exe()
    return local if local.exists() else None

def is_server_running(base_url: str = "http://127.0.0.1:11434") -> bool:
    try:
        requests.get(f"{base_url}/api/tags", timeout=3)
        return True
    except Exception:
        return False

# ── Download ───────────────────────────────────────────────────────────────────

def _asset_pattern() -> str:
    """GitHub release asset name substring for this platform/arch."""
    if sys.platform == "win32":
        return "windows-amd64"
    if sys.platform == "darwin":
        return "darwin"
    machine = platform.machine().lower()
    if "arm" in machine or "aarch" in machine:
        return "linux-arm64"
    return "linux-amd64"

def get_latest_release() -> Tuple[str, str]:
    """Return (download_url, version_tag) for the current platform."""
    resp = requests.get(
        GITHUB_API, timeout=15,
        headers={"Accept": "application/vnd.github+json"}
    )
    resp.raise_for_status()
    data    = resp.json()
    version = data["tag_name"]
    pattern = _asset_pattern()

    for asset in data["assets"]:
        name = asset["name"].lower()
        if any(skip in name for skip in ("setup", "sha256", ".txt", "install")):
            continue
        if pattern in name:
            return asset["browser_download_url"], version

    raise RuntimeError(
        f"No Ollama asset found for platform '{pattern}' in release {version}"
    )

def download_and_install(
    progress_cb: Optional[Callable[[str, int, int], None]] = None
) -> Path:
    """
    Download the Ollama binary into ~/.xlf-translator/ollama/bin/.
    Calls progress_cb(message, bytes_done, bytes_total) during download.
    Returns the Path to the executable.
    """
    OLLAMA_BIN.mkdir(parents=True, exist_ok=True)
    OLLAMA_MODELS.mkdir(parents=True, exist_ok=True)

    def emit(msg: str, done: int = 0, total: int = 0) -> None:
        if progress_cb:
            progress_cb(msg, done, total)

    emit("Fetching Ollama release info from GitHub…")
    url, version   = get_latest_release()
    asset_filename = url.split("/")[-1]
    tmp_path       = OLLAMA_BIN / f"_tmp_{asset_filename}"

    emit(f"Downloading Ollama {version}…")
    resp = requests.get(url, stream=True, timeout=60)
    resp.raise_for_status()
    total_size = int(resp.headers.get("content-length", 0))
    downloaded = 0

    with open(tmp_path, "wb") as f:
        for chunk in resp.iter_content(chunk_size=131072):
            f.write(chunk)
            downloaded += len(chunk)
            emit(f"Downloading Ollama {version}…", downloaded, total_size)

    emit("Extracting…", downloaded, total_size)
    exe_path = _local_exe()
    lname    = asset_filename.lower()

    if lname.endswith(".zip"):
        with zipfile.ZipFile(tmp_path) as zf:
            for member in zf.namelist():
                ml = member.lower().replace("\\", "/")
                if ml.endswith("ollama.exe") or ml in ("ollama", "bin/ollama"):
                    exe_path.write_bytes(zf.read(member))
                    break
        tmp_path.unlink(missing_ok=True)

    elif lname.endswith((".tgz", ".tar.gz")):
        with tarfile.open(tmp_path) as tf:
            for member in tf.getmembers():
                ml = member.name.lower()
                if ml.endswith("ollama") and not ml.endswith(".exe"):
                    fobj = tf.extractfile(member)
                    if fobj:
                        exe_path.write_bytes(fobj.read())
                    break
        tmp_path.unlink(missing_ok=True)

    else:
        # Plain binary (e.g. ollama-darwin, ollama-linux-amd64)
        shutil.move(str(tmp_path), str(exe_path))

    if sys.platform != "win32":
        exe_path.chmod(0o755)

    emit(f"Ollama {version} ready.", downloaded, total_size)
    return exe_path

# ── Server lifecycle ───────────────────────────────────────────────────────────

_server_proc: Optional[subprocess.Popen] = None

def start_server(
    exe: Optional[Path] = None,
    host: str = "127.0.0.1",
    port: int = 11434,
) -> bool:
    """
    Start 'ollama serve'.  Sets OLLAMA_MODELS to the app cache so models are
    stored in ~/.xlf-translator/ollama/models/ rather than ~/.ollama/models/.
    Returns True when the server becomes reachable (up to 10 s).
    """
    global _server_proc

    if exe is None:
        exe = find_ollama()
    if exe is None:
        return False

    env = os.environ.copy()
    env["OLLAMA_MODELS"] = str(OLLAMA_MODELS)
    env["OLLAMA_HOST"]   = f"{host}:{port}"

    kwargs: dict = {"env": env}
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    else:
        kwargs["stdout"] = subprocess.DEVNULL
        kwargs["stderr"] = subprocess.DEVNULL

    _server_proc = subprocess.Popen([str(exe), "serve"], **kwargs)

    base_url = f"http://{host}:{port}"
    for _ in range(20):
        time.sleep(0.5)
        try:
            requests.get(f"{base_url}/api/tags", timeout=2)
            return True
        except Exception:
            pass
    return False

def stop_server() -> None:
    global _server_proc
    if _server_proc and _server_proc.poll() is None:
        _server_proc.terminate()
        try:
            _server_proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            _server_proc.kill()
    _server_proc = None
