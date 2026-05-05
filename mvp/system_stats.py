from __future__ import annotations

import ctypes
import platform
import shutil
from pathlib import Path
from typing import Any

from .utils import run_subprocess_capture


class _MemoryStatusEx(ctypes.Structure):
    _fields_ = [
        ("dwLength", ctypes.c_ulong),
        ("dwMemoryLoad", ctypes.c_ulong),
        ("ullTotalPhys", ctypes.c_ulonglong),
        ("ullAvailPhys", ctypes.c_ulonglong),
        ("ullTotalPageFile", ctypes.c_ulonglong),
        ("ullAvailPageFile", ctypes.c_ulonglong),
        ("ullTotalVirtual", ctypes.c_ulonglong),
        ("ullAvailVirtual", ctypes.c_ulonglong),
        ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
    ]


def _gb(value: int | float) -> float:
    return round(float(value) / (1024**3), 2)


def _read_windows_memory() -> dict[str, Any]:
    status = _MemoryStatusEx()
    status.dwLength = ctypes.sizeof(_MemoryStatusEx)
    ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status))
    total = float(status.ullTotalPhys)
    free = float(status.ullAvailPhys)
    used = max(total - free, 0.0)
    percent = round((used / total) * 100.0, 1) if total else 0.0
    return {
        "total_gb": _gb(total),
        "used_gb": _gb(used),
        "free_gb": _gb(free),
        "used_percent": percent,
    }


def _read_workspace_disk(workspace_root: Path) -> dict[str, Any]:
    usage = shutil.disk_usage(workspace_root)
    used = usage.total - usage.free
    percent = round((used / usage.total) * 100.0, 1) if usage.total else 0.0
    return {
        "path": str(workspace_root.drive or workspace_root.anchor or workspace_root),
        "total_gb": _gb(usage.total),
        "used_gb": _gb(used),
        "free_gb": _gb(usage.free),
        "used_percent": percent,
    }


def _run_command(command: list[str], timeout: int = 10) -> str:
    completed = run_subprocess_capture(command, cwd=Path.cwd(), timeout=timeout)
    if completed.returncode != 0:
        return ""
    return completed.stdout.strip()


def _read_gpu() -> list[dict[str, Any]]:
    if platform.system() != "Windows":
        return []

    output = _run_command(
        [
            "nvidia-smi",
            "--query-gpu=name,utilization.gpu,memory.used,memory.total,temperature.gpu",
            "--format=csv,noheader,nounits",
        ],
        timeout=6,
    )
    if not output:
        return []

    rows: list[dict[str, Any]] = []
    for line in output.splitlines():
        parts = [part.strip() for part in line.split(",")]
        if len(parts) < 5:
            continue
        try:
            memory_used = float(parts[2])
            memory_total = float(parts[3])
            memory_percent = round((memory_used / memory_total) * 100.0, 1) if memory_total else 0.0
        except ValueError:
            memory_used = 0.0
            memory_total = 0.0
            memory_percent = 0.0
        rows.append(
            {
                "name": parts[0],
                "utilization_percent": float(parts[1] or 0),
                "memory_used_mb": memory_used,
                "memory_total_mb": memory_total,
                "memory_percent": memory_percent,
                "temperature_c": float(parts[4] or 0),
            }
        )
    return rows


def _read_cpu_percent() -> float | None:
    if platform.system() != "Windows":
        return None

    output = _run_command(
        [
            "powershell.exe",
            "-NoProfile",
            "-Command",
            r"Get-Counter '\Processor(_Total)\% Processor Time' | Select-Object -ExpandProperty CounterSamples | Select-Object -ExpandProperty CookedValue",
        ],
        timeout=8,
    )
    if not output:
        return None
    try:
        return round(float(output.splitlines()[-1].strip()), 1)
    except ValueError:
        return None


def collect_system_stats(workspace_root: Path) -> dict[str, Any]:
    memory = _read_windows_memory()
    disk = _read_workspace_disk(workspace_root)
    gpu = _read_gpu()
    cpu_percent = _read_cpu_percent()

    warnings: list[str] = []
    if memory["used_percent"] >= 82:
        warnings.append("内存压力偏高，建议切到省配额或本地优先。")
    if disk["free_gb"] <= 25:
        warnings.append("磁盘剩余空间偏少，报告和会话历史需要留意。")
    if gpu and max(item["memory_percent"] for item in gpu) >= 85:
        warnings.append("GPU 显存占用较高，长任务建议减少并发或切轻量模型。")
    if cpu_percent is not None and cpu_percent >= 85:
        warnings.append("CPU 占用偏高，后台联调刷新应适当降频。")

    recommended_mode = "balanced"
    if warnings:
        recommended_mode = "cheap"
    elif gpu and max(item["utilization_percent"] for item in gpu) <= 25 and memory["used_percent"] <= 65:
        recommended_mode = "premium"

    return {
        "memory": memory,
        "disk": disk,
        "gpu": gpu,
        "cpu_percent": cpu_percent,
        "warnings": warnings,
        "recommended_mode": recommended_mode,
    }
