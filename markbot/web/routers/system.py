from __future__ import annotations

import os
import platform
import time
from datetime import datetime, timezone

from fastapi import APIRouter
from starlette.responses import JSONResponse

router = APIRouter()


def _get_process_info():
    """Get current process and child process info."""
    import psutil
    try:
        proc = psutil.Process()
        children = proc.children(recursive=True)

        def _proc_summary(p: "psutil.Process") -> dict:
            try:
                with p.oneshot():
                    cpu = p.cpu_percent(interval=0.0)
                    mem = p.memory_info()
                    create_time = p.create_time()
                    return {
                        "pid": p.pid,
                        "name": p.name(),
                        "cpu_percent": cpu,
                        "memory_rss": mem.rss,
                        "memory_vms": mem.vms,
                        "create_time": create_time,
                        "uptime": time.time() - create_time,
                        "cmdline": " ".join(p.cmdline()[:8]) if p.cmdline() else "",
                        "status": p.status(),
                    }
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                return {"pid": p.pid, "name": "?", "cpu_percent": 0, "memory_rss": 0,
                        "memory_vms": 0, "create_time": 0, "uptime": 0, "cmdline": "", "status": "?"}

        return {
            "current": _proc_summary(proc),
            "children": [_proc_summary(c) for c in children],
            "count": 1 + len(children),
        }
    except Exception as e:
        return {"current": None, "children": [], "count": 0, "error": str(e)}


def _get_load_average() -> list[float] | None:
    try:
        import psutil
        la = psutil.getloadavg()
        return [round(x, 2) for x in la]
    except Exception:
        return None


def _get_network_info() -> dict:
    try:
        import psutil
        net = psutil.net_io_counters()
        return {
            "bytes_sent": net.bytes_sent,
            "bytes_recv": net.bytes_recv,
            "packets_sent": net.packets_sent,
            "packets_recv": net.packets_recv,
        }
    except Exception:
        return {}


def _get_cpu_info() -> dict:
    try:
        import psutil
        return {
            "count_physical": psutil.cpu_count(logical=False) or 0,
            "count_logical": psutil.cpu_count(logical=True) or 0,
            "frequency_mhz": getattr(psutil.cpu_freq(), "current", None),
            # interval=None returns immediately with the value since last call
            "percent": psutil.cpu_percent(interval=None),
            "percent_per_cpu": psutil.cpu_percent(interval=None, percpu=True),
        }
    except Exception:
        return {"count_physical": 0, "count_logical": 0, "percent": 0}


@router.get("/api/system/stats")
async def system_stats():
    from markbot import __version__
    import psutil
    import asyncio

    # Run blocking psutil calls in a thread to avoid blocking the event loop
    def _gather():
        vm = psutil.virtual_memory()
        swap = psutil.swap_memory()
        disk = psutil.disk_usage("/")
        boot_time = psutil.boot_time()
        return vm, swap, disk, boot_time

    vm, swap, disk, boot_time = await asyncio.to_thread(_gather)

    return JSONResponse({
        "version": __version__,
        "python_version": platform.python_version(),
        "platform": platform.platform(),
        "hostname": platform.node(),
        "system": platform.system(),
        "release": platform.release(),
        "architecture": platform.machine(),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "boot_time": boot_time,
        "uptime": time.time() - boot_time,
        "cpu": await asyncio.to_thread(_get_cpu_info),
        "cpu_percent": await asyncio.to_thread(lambda: psutil.cpu_percent(interval=None)),
        "memory": {
            "total": vm.total,
            "available": vm.available,
            "used": vm.used,
            "free": vm.free,
            "percent": vm.percent,
        },
        "swap": {
            "total": swap.total,
            "used": swap.used,
            "free": swap.free,
            "percent": swap.percent,
        },
        "disk": {
            "total": disk.total,
            "used": disk.used,
            "free": disk.free,
            "percent": disk.percent,
        },
        "load_average": _get_load_average(),
        "network": _get_network_info(),
        "process": _get_process_info(),
    })


@router.get("/api/system/process")
async def system_process():
    """Detailed process info, refreshable independently for lower overhead."""
    return JSONResponse({
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "process": _get_process_info(),
    })
