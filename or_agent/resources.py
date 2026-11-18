"""Read-only native monitoring and bounded admission before local LLM work."""

from dataclasses import dataclass
import ctypes
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import shutil
import sys
import time
from urllib.error import URLError
from urllib.request import urlopen


GIB = 1024 ** 3


@dataclass
class WaitBudget:
    """Shared waiting allowance; one instance belongs to one experiment campaign."""

    total_seconds: float = 1800
    operation_seconds: float = 600
    interval_seconds: float = 60
    used_seconds: float = 0

    def __post_init__(self):
        for name in ("total_seconds", "operation_seconds", "interval_seconds", "used_seconds"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
                raise ValueError(f"{name} must be a finite nonnegative number")
        if self.total_seconds > 1800 or self.operation_seconds > 600:
            raise ValueError("Waiting cannot exceed 600 seconds per operation or 1800 seconds per campaign")
        if not self.interval_seconds or self.interval_seconds > 60:
            raise ValueError("interval_seconds must be positive and at most 60")
        if self.used_seconds > self.total_seconds:
            raise ValueError("used_seconds exceeds the campaign allowance")


def _linux_snapshot(proc: Path = Path("/proc")) -> dict:
    result = {key: None for key in (
        "ram_total_bytes", "ram_available_bytes", "commit_total_bytes",
        "commit_limit_bytes", "commit_available_bytes", "memory_status_commit_limit_bytes",
        "memory_status_commit_available_bytes", "pagefile_usage_bytes", "paging_activity_bytes_per_second",
        "cpu_percent", "process_count", "cpu_intensive_process_count",
    )}
    diagnostics = result["diagnostics"] = []
    try:
        fields = {}
        for line in (proc / "meminfo").read_text(encoding="utf-8").splitlines():
            name, separator, value = line.partition(":")
            if separator:
                fields[name] = int(value.split()[0]) * 1024
        total = fields["MemTotal"]
        available = fields["MemAvailable"]
        result.update(ram_total_bytes=total, ram_available_bytes=available)
    except (OSError, KeyError, ValueError, IndexError) as error:
        diagnostics.append(f"/proc/meminfo: {error}")

    try:
        policy = int((proc / "sys/vm/overcommit_memory").read_text(encoding="ascii").strip())
        if policy not in {0, 1, 2}:
            raise ValueError(f"unexpected overcommit mode {policy}")
        result["commit_limit_enforced"] = policy == 2
        result["overcommit_mode"] = policy
        if policy == 2:
            limit = fields["CommitLimit"]
            committed = fields["Committed_AS"]
            result.update(commit_total_bytes=committed, commit_limit_bytes=limit,
                          commit_available_bytes=max(0, limit - committed))
        else:
            diagnostics.append(f"Linux overcommit mode {policy} has no strict commit limit")
    except (OSError, KeyError, ValueError) as error:
        result["commit_limit_enforced"] = None
        diagnostics.append(f"Linux overcommit policy: {error}")

    def cpu_times():
        line = (proc / "stat").read_text(encoding="ascii").splitlines()[0].split()
        if not line or line[0] != "cpu":
            raise ValueError("aggregate CPU row is missing")
        values = [int(value) for value in line[1:]]
        return sum(values), values[3] + (values[4] if len(values) > 4 else 0)

    try:
        first_total, first_idle = cpu_times()
        time.sleep(0.1)
        last_total, last_idle = cpu_times()
        elapsed = last_total - first_total
        if elapsed <= 0:
            raise ValueError("CPU sampling interval returned no elapsed time")
        result["cpu_percent"] = max(0.0, min(100.0, 100 * (elapsed - last_idle + first_idle) / elapsed))
    except (OSError, ValueError, IndexError) as error:
        diagnostics.append(f"/proc/stat: {error}")
    try:
        result["process_count"] = sum(path.name.isdigit() for path in proc.iterdir())
    except OSError as error:
        diagnostics.append(f"/proc process count: {error}")
    diagnostics.append("Per-process CPU intensity is not measured; no external process is changed")
    return result


def _native_snapshot() -> dict:
    result = {key: None for key in (
        "ram_total_bytes", "ram_available_bytes", "commit_total_bytes",
        "commit_limit_bytes", "commit_available_bytes", "memory_status_commit_limit_bytes",
        "memory_status_commit_available_bytes", "pagefile_usage_bytes", "paging_activity_bytes_per_second",
        "cpu_percent", "process_count", "cpu_intensive_process_count",
    )}
    diagnostics = result["diagnostics"] = []
    if os.name != "nt":
        if sys.platform.startswith("linux") and os.getenv("OR_AGENT_LINUX_NATIVE") == "1":
            return _linux_snapshot()
        diagnostics.append("Native Windows monitoring is unavailable on this platform")
        if sys.platform.startswith("linux"):
            diagnostics.append("Set OR_AGENT_LINUX_NATIVE=1 only when OR-Agent and Ollama share the Linux host limits")
        return result

    from ctypes import wintypes

    class MemoryStatus(ctypes.Structure):
        _fields_ = [("length", wintypes.DWORD), ("load", wintypes.DWORD)] + [
            (name, ctypes.c_ulonglong) for name in (
                "total_physical", "available_physical", "total_pagefile",
                "available_pagefile", "total_virtual", "available_virtual", "extended_virtual",
            )]

    class PerformanceInfo(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD)] + [
            (name, ctypes.c_size_t) for name in (
                "commit_total", "commit_limit", "commit_peak", "physical_total",
                "physical_available", "system_cache", "kernel_total", "kernel_paged",
                "kernel_nonpaged", "page_size",
            )] + [(name, wintypes.DWORD) for name in ("handle_count", "process_count", "thread_count")]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    status = MemoryStatus()
    status.length = ctypes.sizeof(status)
    kernel.GlobalMemoryStatusEx.argtypes = [ctypes.POINTER(MemoryStatus)]
    kernel.GlobalMemoryStatusEx.restype = wintypes.BOOL
    if kernel.GlobalMemoryStatusEx(ctypes.byref(status)):
        result.update(ram_total_bytes=status.total_physical,
                      ram_available_bytes=status.available_physical,
                      memory_status_commit_limit_bytes=status.total_pagefile,
                      memory_status_commit_available_bytes=status.available_pagefile)
    else:
        diagnostics.append(f"GlobalMemoryStatusEx failed: {ctypes.get_last_error()}")

    performance = PerformanceInfo()
    performance.size = ctypes.sizeof(performance)
    psapi = ctypes.WinDLL("psapi", use_last_error=True)
    psapi.GetPerformanceInfo.argtypes = [ctypes.POINTER(PerformanceInfo), wintypes.DWORD]
    psapi.GetPerformanceInfo.restype = wintypes.BOOL
    if psapi.GetPerformanceInfo(ctypes.byref(performance), performance.size):
        result.update(commit_total_bytes=performance.commit_total * performance.page_size,
                      commit_limit_bytes=performance.commit_limit * performance.page_size,
                      commit_available_bytes=(performance.commit_limit - performance.commit_total) * performance.page_size,
                      process_count=performance.process_count)
    else:
        diagnostics.append(f"GetPerformanceInfo failed: {ctypes.get_last_error()}")

    kernel.GetSystemTimes.argtypes = [ctypes.POINTER(wintypes.FILETIME)] * 3
    kernel.GetSystemTimes.restype = wintypes.BOOL

    def system_times():
        idle, system, user = wintypes.FILETIME(), wintypes.FILETIME(), wintypes.FILETIME()
        if not kernel.GetSystemTimes(ctypes.byref(idle), ctypes.byref(system), ctypes.byref(user)):
            raise OSError(ctypes.get_last_error(), "GetSystemTimes failed")
        return tuple((v.dwHighDateTime << 32) | v.dwLowDateTime for v in (idle, system, user))

    try:
        first = system_times()
        time.sleep(0.1)
        last = system_times()
        idle, system, user = (b - a for a, b in zip(first, last))
        total = system + user
        if total > 0:
            result["cpu_percent"] = max(0.0, min(100.0, 100 * (total - idle) / total))
        else:
            diagnostics.append("CPU sampling interval returned no elapsed system time")
    except OSError as error:
        diagnostics.append(str(error))
    diagnostics.append("Per-process CPU intensity is not measured; no external process is changed")
    diagnostics.append("GlobalMemoryStatusEx pagefile fields measure commit limits, not actual paging-file usage; paging activity is unknown")
    return result


def snapshot(base_url: str = "http://localhost:11434", timeout: float = 3.0) -> dict:
    """Record measured values; unknown GPU capacity and service activity stay explicit."""
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("timeout must be a finite positive number")
    try:
        result = _native_snapshot()
    except (OSError, AttributeError) as error:
        result = {"ram_total_bytes": None, "ram_available_bytes": None,
                  "commit_total_bytes": None, "commit_limit_bytes": None,
                  "commit_available_bytes": None, "cpu_percent": None,
                  "diagnostics": [f"Native monitoring failed: {error}"]}
    result["timestamp_utc"] = datetime.now(timezone.utc).isoformat()
    result["disk_path"] = str(Path(__file__).resolve().parent.parent)
    try:
        disk = shutil.disk_usage(result["disk_path"])
        result.update(disk_total_bytes=disk.total, disk_used_bytes=disk.used, disk_free_bytes=disk.free)
    except OSError as error:
        result.update(disk_total_bytes=None, disk_used_bytes=None, disk_free_bytes=None)
        result["diagnostics"].append(f"Disk measurement failed: {error}")
    result["gpu"] = {"vram_total_bytes": None, "vram_free_bytes": None,
                     "utilization_percent": None,
                     "diagnostic": "GPU/AMD VRAM is unknown; AdapterRAM is not used"}
    ollama = result["ollama"] = {"available": False, "models": [], "loaded_models": None,
                                 "base_url": base_url, "error": None}
    for endpoint, key in (("tags", "models"), ("ps", "loaded_models")):
        try:
            with urlopen(f"{base_url.rstrip('/')}/api/{endpoint}", timeout=timeout) as response:
                payload = json.load(response)
            if not isinstance(payload, dict) or not isinstance(payload.get("models"), list):
                raise ValueError("Ollama response has no models array")
            ollama[key] = payload["models"]
            if endpoint == "tags":
                ollama["available"] = True
        except (OSError, URLError, ValueError) as error:
            ollama["error"] = str(error)
            result["diagnostics"].append(f"Ollama {endpoint}: {error}")
    result["diagnostics"].append("Ollama ps reports residency, not concurrent request activity")
    return result


def gate(model: str, base_url: str = "http://localhost:11434", *,
         wait_budget: WaitBudget | None = None, snapshot_fn=None, sleep_fn=time.sleep,
         deadline: float | None = None) -> dict:
    """Refuse unknown/structural limits; wait only for temporary measured pressure."""
    if deadline is not None and (isinstance(deadline, bool) or not isinstance(deadline, (int, float)) or not math.isfinite(deadline)):
        raise ValueError("deadline must be a finite monotonic timestamp")
    budget = wait_budget if wait_budget is not None else WaitBudget()
    records = []
    waited = measured_wait = 0.0
    estimated = None
    full_estimated = None
    reused = False
    margin = None

    def result(allowed, reason, limiting_resource=None):
        return {"allowed": allowed, "reason": reason, "limiting_resource": limiting_resource,
                "snapshots": records, "wait_seconds": waited,
                "measured_wait_seconds": measured_wait, "estimated_bytes": estimated,
                "full_estimated_bytes": full_estimated, "loaded_model_reused": reused,
                "wait_overrun_seconds": max(0.0, measured_wait - waited),
                "safety_margin_bytes": margin, "campaign_wait_seconds": budget.used_seconds}

    while True:
        now = time.monotonic()
        if deadline is not None and now >= deadline:
            return result(False, "elapsed_budget_exhausted")
        if snapshot_fn is not None:
            current = snapshot_fn()
        else:
            timeout = 3.0 if deadline is None else max(0.001, min(3.0, (deadline - now - 0.1) / 2))
            current = snapshot(base_url, timeout=timeout)
        records.append(current)
        if deadline is not None and time.monotonic() >= deadline:
            return result(False, "elapsed_budget_exhausted")
        ollama = current.get("ollama", {})
        if not ollama.get("available"):
            return result(False, "ollama_unavailable")
        names = {model, model + ":latest"} if ":" not in model else {model}
        installed = next((entry for entry in ollama.get("models", [])
                          if isinstance(entry, dict) and (entry.get("name") in names or entry.get("model") in names)), None)
        if installed is None:
            return result(False, "model_not_installed")
        size = installed.get("size")
        if type(size) is not int or size <= 0:
            return result(False, "model_size_unknown")
        # Resident weights are already reflected in free RAM/commit; reserve new context/runtime only.
        reused = any(isinstance(entry, dict) and (entry.get("name") in names or entry.get("model") in names)
                     for entry in ollama.get("loaded_models") or [])
        full_estimated = (size * 13 + 9) // 10 + GIB
        estimated = GIB if reused else full_estimated
        total, available = current.get("ram_total_bytes"), current.get("ram_available_bytes")
        commit_limit, commit_available = current.get("commit_limit_bytes"), current.get("commit_available_bytes")
        commit_enforced = current.get("commit_limit_enforced", True)
        if not all(type(v) is int and v >= 0 for v in (total, available)) or total <= 0 or available > total:
            return result(False, "ram_unknown")
        if commit_enforced is None or type(commit_enforced) is not bool:
            return result(False, "commit_policy_unknown")
        if commit_enforced and (not all(type(v) is int and v >= 0 for v in (commit_limit, commit_available))
                                or commit_limit <= 0 or commit_available > commit_limit):
            return result(False, "commit_unknown")
        margin = max(GIB, (total + 9) // 10)
        needed = estimated + margin
        if needed > total:
            return result(False, "structural_ram_limit", "ram")
        if commit_enforced and needed > commit_limit:
            return result(False, "structural_commit_limit", "commit")
        cpu = current.get("cpu_percent")
        if available < needed:
            pressure = "ram"
        elif commit_enforced and commit_available < needed:
            pressure = "commit"
        elif isinstance(cpu, (int, float)) and not isinstance(cpu, bool) and math.isfinite(cpu) and cpu >= 95:
            pressure = "cpu"
        elif current.get("disk_free_bytes") == 0:
            return result(False, "disk_full", "disk")
        else:
            return result(True, "resources_available")
        allowance = min(budget.interval_seconds, budget.operation_seconds - waited,
                        budget.total_seconds - budget.used_seconds)
        if deadline is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return result(False, "elapsed_budget_exhausted", pressure)
            allowance = min(allowance, remaining)
        if allowance <= 0:
            return result(False, "wait_budget_exhausted", pressure)
        before = time.monotonic()
        sleep_fn(allowance)
        elapsed = max(0.0, time.monotonic() - before)
        # Account requested sleep even with a fake sleeper; record measured elapsed separately.
        charged = max(allowance, elapsed)
        waited = min(budget.operation_seconds, waited + charged)
        measured_wait += elapsed
        budget.used_seconds = min(budget.total_seconds, budget.used_seconds + charged)
