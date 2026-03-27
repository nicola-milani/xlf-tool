"""
Hardware detection: RAM, GPU/VRAM, Apple Silicon.
No external dependencies — uses platform-specific system calls.
"""
from __future__ import annotations

import platform
import subprocess
import sys
from dataclasses import dataclass, field
from typing import List


@dataclass
class GpuInfo:
    name: str
    vram_gb: float
    is_unified: bool = False   # True for Apple Silicon (shared RAM/VRAM)


@dataclass
class HardwareInfo:
    ram_gb: float
    cpu_cores: int
    cpu_name: str
    gpus: List[GpuInfo] = field(default_factory=list)

    @property
    def best_vram_gb(self) -> float:
        """Largest discrete VRAM, or 0 if none."""
        return max((g.vram_gb for g in self.gpus if not g.is_unified), default=0.0)

    @property
    def effective_memory_gb(self) -> float:
        """
        Memory effectively available for a model.
        Apple Silicon: GPU uses unified RAM → full RAM usable.
        NVIDIA/AMD discrete GPU: VRAM is the bottleneck for GPU inference.
        CPU fallback: ~70% of RAM (leave headroom for OS + app).
        """
        unified = next((g for g in self.gpus if g.is_unified), None)
        if unified:
            return unified.vram_gb * 0.85          # unified memory, slight headroom
        if self.best_vram_gb > 0:
            return self.best_vram_gb               # discrete GPU: VRAM is the limit
        return self.ram_gb * 0.70                  # CPU-only: 70% of RAM


# ── RAM ────────────────────────────────────────────────────────────────────────

def _ram_gb() -> float:
    try:
        import psutil                              # optional but preferred
        return psutil.virtual_memory().total / 1_073_741_824
    except ImportError:
        pass

    if sys.platform == "darwin":
        out = subprocess.run(
            ["sysctl", "-n", "hw.memsize"],
            capture_output=True, text=True
        )
        if out.returncode == 0:
            return int(out.stdout.strip()) / 1_073_741_824

    elif sys.platform.startswith("linux"):
        try:
            with open("/proc/meminfo") as f:
                for line in f:
                    if line.startswith("MemTotal:"):
                        return int(line.split()[1]) / 1_048_576
        except OSError:
            pass

    elif sys.platform == "win32":
        import ctypes
        class _MEMSTATUS(ctypes.Structure):
            _fields_ = [
                ("dwLength",                ctypes.c_ulong),
                ("dwMemoryLoad",            ctypes.c_ulong),
                ("ullTotalPhys",            ctypes.c_ulonglong),
                ("ullAvailPhys",            ctypes.c_ulonglong),
                ("ullTotalPageFile",        ctypes.c_ulonglong),
                ("ullAvailPageFile",        ctypes.c_ulonglong),
                ("ullTotalVirtual",         ctypes.c_ulonglong),
                ("ullAvailVirtual",         ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]
        st = _MEMSTATUS()
        st.dwLength = ctypes.sizeof(_MEMSTATUS)
        ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st))
        return st.ullTotalPhys / 1_073_741_824

    return 8.0   # safe fallback


# ── CPU ────────────────────────────────────────────────────────────────────────

def _cpu_info() -> tuple[int, str]:
    """Return (logical_cores, cpu_name_string)."""
    import os
    cores = os.cpu_count() or 1

    name = platform.processor() or platform.machine()

    if sys.platform == "darwin":
        out = subprocess.run(
            ["sysctl", "-n", "machdep.cpu.brand_string"],
            capture_output=True, text=True
        )
        if out.returncode == 0 and out.stdout.strip():
            name = out.stdout.strip()

    elif sys.platform.startswith("linux"):
        try:
            with open("/proc/cpuinfo") as f:
                for line in f:
                    if line.startswith("model name"):
                        name = line.split(":", 1)[1].strip()
                        break
        except OSError:
            pass

    return cores, name


# ── GPU detection ──────────────────────────────────────────────────────────────

def _detect_apple_silicon(ram_gb: float) -> List[GpuInfo]:
    """On Apple Silicon, GPU uses unified memory."""
    if sys.platform != "darwin" or platform.machine() != "arm64":
        return []
    out = subprocess.run(
        ["sysctl", "-n", "machdep.cpu.brand_string"],
        capture_output=True, text=True
    )
    chip = out.stdout.strip() if out.returncode == 0 else "Apple Silicon"
    return [GpuInfo(name=chip, vram_gb=ram_gb, is_unified=True)]


def _detect_nvidia() -> List[GpuInfo]:
    try:
        out = subprocess.run(
            ["nvidia-smi",
             "--query-gpu=name,memory.total",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5
        )
        if out.returncode != 0:
            return []
        gpus = []
        for line in out.stdout.strip().splitlines():
            parts = line.split(",")
            if len(parts) >= 2:
                name    = parts[0].strip()
                vram_mb = float(parts[1].strip())
                gpus.append(GpuInfo(name=name, vram_gb=vram_mb / 1024))
        return gpus
    except Exception:
        return []


def _detect_amd_linux() -> List[GpuInfo]:
    """Check /sys/class/drm for AMD VRAM info."""
    gpus: List[GpuInfo] = []
    import glob, os
    for mem_path in glob.glob("/sys/class/drm/card*/device/mem_info_vram_total"):
        try:
            vram_bytes = int(open(mem_path).read().strip())
            name_path  = os.path.join(os.path.dirname(mem_path), "product_name")
            name = open(name_path).read().strip() if os.path.exists(name_path) else "AMD GPU"
            gpus.append(GpuInfo(name=name, vram_gb=vram_bytes / 1_073_741_824))
        except Exception:
            pass
    return gpus


# ── Public API ─────────────────────────────────────────────────────────────────

def detect() -> HardwareInfo:
    """Detect and return hardware info. Never raises."""
    try:
        ram_gb = _ram_gb()
        cores, cpu_name = _cpu_info()

        gpus: List[GpuInfo] = []
        gpus += _detect_apple_silicon(ram_gb)
        if not gpus:
            gpus += _detect_nvidia()
        if not gpus and sys.platform.startswith("linux"):
            gpus += _detect_amd_linux()

        return HardwareInfo(
            ram_gb=ram_gb,
            cpu_cores=cores,
            cpu_name=cpu_name,
            gpus=gpus,
        )
    except Exception:
        return HardwareInfo(ram_gb=8.0, cpu_cores=4, cpu_name="Unknown")


# ── Model catalogue & scoring ──────────────────────────────────────────────────

@dataclass
class ModelSpec:
    tag: str
    label: str          # human-readable name
    size_gb: float      # approximate download / disk size
    min_ram_gb: float   # absolute minimum to load
    rec_ram_gb: float   # recommended for comfortable use
    description: str = ""


MODEL_CATALOGUE: List[ModelSpec] = [
    ModelSpec("llama3.2:1b",   "llama3.2 · 1B",    1.3,  1.5,  3.0,  "Fastest, basic quality"),
    ModelSpec("llama3.2",      "llama3.2 · 3B",    2.0,  3.0,  5.0,  "Good balance speed/quality"),
    ModelSpec("phi4-mini",     "phi4-mini · 3.8B", 2.5,  3.5,  6.0,  "Microsoft, strong reasoning"),
    ModelSpec("gemma3",        "gemma3 · 4B",      3.0,  4.0,  6.0,  "Google, multilingual"),
    ModelSpec("mistral",       "mistral · 7B",     4.1,  5.0,  8.0,  "Strong translation quality"),
    ModelSpec("llama3.1",      "llama3.1 · 8B",    4.7,  6.0,  10.0, "Meta, excellent quality"),
    ModelSpec("phi4",          "phi4 · 14B",       8.0,  10.0, 16.0, "Microsoft, high quality"),
    ModelSpec("gemma3:12b",    "gemma3 · 12B",     8.0,  10.0, 16.0, "Google, very high quality"),
    ModelSpec("mistral-nemo",  "mistral-nemo · 12B", 7.0, 9.0, 14.0, "Mistral, strong multilingual"),
    ModelSpec("qwen2.5:7b",    "qwen2.5 · 7B",     4.5,  5.0,  8.0,  "Alibaba, great for Asian langs"),
    ModelSpec("deepseek-r1:7b","deepseek-r1 · 7B", 4.5,  5.0,  8.0,  "Strong reasoning"),
    ModelSpec("llama3.1:70b",  "llama3.1 · 70B",  40.0, 42.0, 80.0,  "Best quality, needs lots of RAM"),
]

TIER_GREAT = "great"   # fits comfortably
TIER_OK    = "ok"      # fits but tight
TIER_SLOW  = "slow"    # will run but very slowly (CPU only, barely fits)
TIER_NO    = "no"      # won't fit


def score(spec: ModelSpec, hw: HardwareInfo) -> str:
    mem = hw.effective_memory_gb
    if mem >= spec.rec_ram_gb:
        return TIER_GREAT
    if mem >= spec.min_ram_gb:
        return TIER_OK
    if hw.ram_gb >= spec.min_ram_gb:   # might work via CPU even if GPU mem low
        return TIER_SLOW
    return TIER_NO


TIER_LABEL = {
    TIER_GREAT: "★ Recommended",
    TIER_OK:    "✓ Compatible",
    TIER_SLOW:  "⚠ May be slow",
    TIER_NO:    "✗ Insufficient RAM",
}
TIER_ORDER = {TIER_GREAT: 0, TIER_OK: 1, TIER_SLOW: 2, TIER_NO: 3}
