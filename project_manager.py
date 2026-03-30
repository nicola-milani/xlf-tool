"""
Project folder management for XLF Translator.

A project is a directory with this structure:

    myproject/
    ├── glossario.csv       optional — source,target term pairs
    ├── metadata.json       auto-managed — stats and timestamps
    ├── input/
    │   └── <file>.xlf      exactly one XLF file
    ├── output/
    │   └── <file>_translated.xlf   written on save/download
    └── tmp/                temporary working files (ignored in zip)

Usage
-----
    p = Project(Path("/path/to/myproject"))
    p.setup_dirs()                  # create subfolders if missing
    xlf = p.find_xlf()              # Path or None
    glossary = p.load_glossary()    # dict[str, str]
    p.save_metadata({...})
    zip_path = p.make_zip()         # written to tmp/
"""
import csv
import json
import shutil
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Optional


def _fmt_size(n: int) -> str:
    """Human-readable file size."""
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


class Project:
    def __init__(self, folder: Path):
        self.folder        = Path(folder)
        self.name          = self.folder.name
        self.input_dir     = self.folder / "input"
        self.output_dir    = self.folder / "output"
        self.tmp_dir       = self.folder / "tmp"
        self.glossary_file = self.folder / "glossario.csv"
        self.metadata_file = self.folder / "metadata.json"

    # ── Folder setup ──────────────────────────────────────────────────────────

    def setup_dirs(self) -> None:
        """Create input/, output/, tmp/ if they do not exist."""
        self.input_dir.mkdir(parents=True, exist_ok=True)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.tmp_dir.mkdir(parents=True, exist_ok=True)

    # ── XLF detection ─────────────────────────────────────────────────────────

    def find_xlf(self) -> Optional[Path]:
        """
        Return the single .xlf/.xliff file inside input/, or None.
        Raises ValueError if more than one is found.
        """
        if not self.input_dir.exists():
            return None
        xlfs = [
            p for p in self.input_dir.iterdir()
            if p.is_file() and p.suffix.lower() in (".xlf", ".xliff")
        ]
        if len(xlfs) == 0:
            return None
        if len(xlfs) > 1:
            raise ValueError(
                f"Multiple XLF files found in input/: {[p.name for p in xlfs]}"
            )
        return xlfs[0]

    # ── Glossary ──────────────────────────────────────────────────────────────

    def load_glossary(self) -> dict[str, str]:
        """
        Parse glossario.csv and return {source: target}.

        Accepts comma or semicolon separators.
        Skips a header row when the first column looks like a field name.
        """
        if not self.glossary_file.exists():
            return {}
        glossary: dict[str, str] = {}
        try:
            raw = self.glossary_file.read_bytes()
            text = raw.decode("utf-8-sig")   # strip BOM if present
            sep = ";" if text.count(";") > text.count(",") else ","
            reader = csv.reader(text.splitlines(), delimiter=sep)
            header_skipped = False
            for row in reader:
                if len(row) < 2:
                    continue
                src, tgt = row[0].strip(), row[1].strip()
                if not src:
                    continue
                # Skip header row: first cell is a known field name
                if not header_skipped and src.lower() in (
                    "source", "src", "original", "testo", "term", "termine"
                ):
                    header_skipped = True
                    continue
                header_skipped = True
                glossary[src] = tgt
        except Exception:
            pass
        return glossary

    # ── Metadata ──────────────────────────────────────────────────────────────

    def load_metadata(self) -> dict:
        if not self.metadata_file.exists():
            return {}
        try:
            return json.loads(self.metadata_file.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def save_metadata(self, data: dict) -> None:
        existing = self.load_metadata()
        existing.update(data)
        existing["last_modified"] = datetime.now().isoformat(timespec="seconds")
        if "created" not in existing:
            existing["created"] = existing["last_modified"]
        self.metadata_file.write_text(
            json.dumps(existing, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    # ── File listing & size ───────────────────────────────────────────────────

    def folder_size(self) -> int:
        """Total size in bytes of all files under the project folder."""
        return sum(
            f.stat().st_size
            for f in self.folder.rglob("*")
            if f.is_file()
        )

    def folder_size_str(self) -> str:
        return _fmt_size(self.folder_size())

    def list_files(self) -> dict:
        """
        Return a dict with categorised file info:
        {
            "input":  [{"name": str, "size": str}],
            "output": [{"name": str, "size": str}],
            "glossary": {"exists": bool, "size": str},
            "metadata": {...},
            "total_size": str,
        }
        """
        def _entry(p: Path) -> dict:
            return {"name": p.name, "size": _fmt_size(p.stat().st_size)}

        return {
            "input": [
                _entry(p) for p in sorted(self.input_dir.iterdir())
                if p.is_file()
            ] if self.input_dir.exists() else [],
            "output": [
                _entry(p) for p in sorted(self.output_dir.iterdir())
                if p.is_file()
            ] if self.output_dir.exists() else [],
            "glossary": {
                "exists": self.glossary_file.exists(),
                "size": _fmt_size(self.glossary_file.stat().st_size)
                if self.glossary_file.exists() else "—",
                "terms": len(self.load_glossary()),
            },
            "metadata": self.load_metadata(),
            "total_size": self.folder_size_str(),
        }

    def output_path_for_lang(self, lang: str) -> Path:
        """Return the output path for a specific target language."""
        xlf = self.find_xlf()
        stem = xlf.stem if xlf else "translated"
        return self.output_dir / f"{stem}_{lang}.xlf"

    # ── ZIP ───────────────────────────────────────────────────────────────────

    def make_zip(self) -> Path:
        """
        Create a ZIP of the project (excluding tmp/) inside tmp/.
        Returns the path to the generated zip file.
        """
        self.tmp_dir.mkdir(exist_ok=True)
        zip_path = self.tmp_dir / f"{self.name}.zip"
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for f in sorted(self.folder.rglob("*")):
                if not f.is_file():
                    continue
                # exclude tmp/ from the archive
                try:
                    f.relative_to(self.tmp_dir)
                    continue   # inside tmp/
                except ValueError:
                    pass
                zf.write(f, Path(self.name) / f.relative_to(self.folder))
        return zip_path

    # ── Import from ZIP ───────────────────────────────────────────────────────

    @classmethod
    def from_zip(cls, zip_path: Path, dest_parent: Path) -> "Project":
        """
        Extract a project ZIP into dest_parent and return the Project.

        Accepts two ZIP layouts:
        - Flat: all files at root → extracted into dest_parent/<zip_stem>/
        - Nested: a single top-level folder → extracted as-is
        """
        with zipfile.ZipFile(zip_path) as zf:
            names = [n for n in zf.namelist() if not n.endswith("/")]
            roots = {n.split("/")[0] for n in zf.namelist()}
            # Remove MacOS artifacts
            roots.discard("__MACOSX")

            if len(roots) == 1:
                root_name = roots.pop()
                zf.extractall(dest_parent)
                project_dir = dest_parent / root_name
            else:
                # Flat archive — wrap in a named folder
                root_name = zip_path.stem
                project_dir = dest_parent / root_name
                project_dir.mkdir(exist_ok=True)
                zf.extractall(project_dir)

        proj = cls(project_dir)
        proj.setup_dirs()
        return proj


# ── Glossary helpers (used by translation workers) ────────────────────────────

def glossary_exact(text: str, glossary: dict[str, str]) -> Optional[str]:
    """
    Return the glossary translation for *text* if an exact match exists
    (case-insensitive).  Returns None when no match is found.
    """
    if not glossary:
        return None
    stripped = text.strip()
    if stripped in glossary:
        return glossary[stripped]
    lower = stripped.lower()
    for src, tgt in glossary.items():
        if src.lower() == lower:
            return tgt
    return None


def glossary_substitute(text: str, glossary: dict[str, str]) -> str:
    """
    Replace all occurrences of glossary source terms in *text* with their
    translations.  Matching is word-boundary aware and case-insensitive.
    """
    if not glossary:
        return text
    import re
    # Sort by length descending so longer terms are matched first
    for src in sorted(glossary, key=len, reverse=True):
        tgt = glossary[src]
        pattern = r"(?<!\w)" + re.escape(src) + r"(?!\w)"
        text = re.sub(pattern, tgt, text, flags=re.IGNORECASE)
    return text
