from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX fallback
    fcntl = None

try:
    import msvcrt
except ImportError:  # pragma: no cover - non-Windows fallback
    msvcrt = None

_WATCHDOG_OWNER_FD: int | None = None
_WATCHDOG_OWNER_PATH: Path | None = None
_WATCHDOG_OWNER_NAME: str | None = None
_WATCHDOG_LOCK_BYTE_OFFSET = 2048


def _normalize_repo_root(repo_root: str | Path | None = None) -> Path:
    return Path(repo_root or Path.cwd()).resolve()


def _watchdog_owner_path(repo_root: str | Path | None = None) -> Path:
    return _normalize_repo_root(repo_root) / ".tmp" / "watchdog.owner.lock"


def _process_exists(pid: int | None) -> bool:
    """Is `pid` alive? Delegates to the ONE liveness implementation.

    HARDEN-DATA-OPS (watchdog-oskill-windows): this used to hand-roll
    ``os.kill(pid, 0)``. CORRECTION to the audit note that motivated this — the
    probe does NOT terminate the target: CPython's Windows os.kill special-cases
    signal 0 (OpenProcess, then return; verified on 3.11.9, where sig 0 leaves
    the process running and sig 9 kills it). What IS wrong with it is the access
    mask: os.kill opens the handle with PROCESS_ALL_ACCESS, which
    ACCESS_DENIEDs on a process owned by another user or running elevated, so
    liveness came back only via the PermissionError branch. runtime_health's
    ``pid_exists`` probes with PROCESS_QUERY_LIMITED_INFORMATION (and keeps the
    os.kill(0) branch for POSIX) — one implementation, correct across integrity
    levels, instead of a fifth private copy here.
    """
    if not isinstance(pid, int) or pid <= 0:
        return False
    from forven.runtime_health import pid_exists

    return bool(pid_exists(pid))


def _coerce_int(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _read_owner_payload(lock_path: Path) -> dict[str, Any]:
    try:
        raw = lock_path.read_text(encoding="utf-8").strip()
    except OSError:
        return {}
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        pid = _coerce_int(raw)
        return {"pid": pid} if pid else {}
    return parsed if isinstance(parsed, dict) else {}


def _write_owner_payload(fd: int, *, owner_name: str) -> None:
    payload = {
        "pid": os.getpid(),
        "owner_name": str(owner_name or "").strip() or "watchdog",
        "acquired_at": datetime.now(timezone.utc).isoformat(),
    }
    encoded = json.dumps(payload, sort_keys=True).encode("utf-8")
    os.ftruncate(fd, 0)
    os.lseek(fd, 0, os.SEEK_SET)
    os.write(fd, encoded)


def _try_lock_fd(fd: int) -> bool:
    try:
        if fcntl is not None:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        elif msvcrt is not None:
            os.lseek(fd, _WATCHDOG_LOCK_BYTE_OFFSET, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:  # pragma: no cover - unsupported platform
            return False
    except (BlockingIOError, OSError):
        return False
    return True


def _unlock_fd(fd: int) -> None:
    if fcntl is not None:
        fcntl.flock(fd, fcntl.LOCK_UN)
        return
    if msvcrt is not None:
        os.lseek(fd, _WATCHDOG_LOCK_BYTE_OFFSET, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)


def _is_lock_held(lock_path: Path) -> bool:
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        if not _try_lock_fd(fd):
            return True
        _unlock_fd(fd)
        return False
    finally:
        os.close(fd)


def get_watchdog_owner_status(repo_root: str | Path | None = None) -> dict[str, Any]:
    lock_path = _watchdog_owner_path(repo_root)
    payload = _read_owner_payload(lock_path)
    active_pid = _coerce_int(payload.get("pid"))
    owner_name = str(payload.get("owner_name") or "").strip() or None
    held_by_current_process = _WATCHDOG_OWNER_FD is not None and _WATCHDOG_OWNER_PATH == lock_path

    if held_by_current_process:
        active_pid = os.getpid()
        owner_name = _WATCHDOG_OWNER_NAME or owner_name
        lock_held = True
        active_pid_running = True
    else:
        lock_held = _is_lock_held(lock_path)
        active_pid_running = _process_exists(active_pid)

    stale_pid = bool(active_pid and not active_pid_running)
    other_process_active = bool(lock_held and active_pid_running and active_pid != os.getpid())
    return {
        "repo_root": str(_normalize_repo_root(repo_root)),
        "lock_path": str(lock_path),
        "active_pid": active_pid,
        "active_pid_running": active_pid_running,
        "lock_held": lock_held,
        "held_by_current_process": held_by_current_process,
        "other_process_active": other_process_active,
        "stale_pid": stale_pid,
        "owner_name": owner_name,
        "acquired_at": payload.get("acquired_at"),
    }


def acquire_watchdog_owner_lock(
    repo_root: str | Path | None = None,
    *,
    owner_name: str = "start_all",
) -> bool:
    global _WATCHDOG_OWNER_FD, _WATCHDOG_OWNER_NAME, _WATCHDOG_OWNER_PATH

    lock_path = _watchdog_owner_path(repo_root)
    if _WATCHDOG_OWNER_FD is not None:
        return _WATCHDOG_OWNER_PATH == lock_path

    status = get_watchdog_owner_status(lock_path.parent.parent)
    if status.get("other_process_active"):
        return False
    if status.get("stale_pid"):
        try:
            lock_path.unlink(missing_ok=True)
        except OSError:
            pass

    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
    if not _try_lock_fd(fd):
        os.close(fd)
        return False

    try:
        _write_owner_payload(fd, owner_name=owner_name)
    except Exception:
        try:
            _unlock_fd(fd)
        finally:
            os.close(fd)
        raise

    _WATCHDOG_OWNER_FD = fd
    _WATCHDOG_OWNER_PATH = lock_path
    _WATCHDOG_OWNER_NAME = str(owner_name or "").strip() or "watchdog"
    return True


def release_watchdog_owner_lock(repo_root: str | Path | None = None) -> None:
    global _WATCHDOG_OWNER_FD, _WATCHDOG_OWNER_NAME, _WATCHDOG_OWNER_PATH

    if _WATCHDOG_OWNER_FD is None:
        return
    expected_path = _watchdog_owner_path(repo_root) if repo_root is not None else _WATCHDOG_OWNER_PATH
    if expected_path is not None and _WATCHDOG_OWNER_PATH is not None and expected_path != _WATCHDOG_OWNER_PATH:
        return

    try:
        os.ftruncate(_WATCHDOG_OWNER_FD, 0)
    except OSError:
        pass
    try:
        _unlock_fd(_WATCHDOG_OWNER_FD)
    except OSError:
        pass
    try:
        os.close(_WATCHDOG_OWNER_FD)
    except OSError:
        pass

    _WATCHDOG_OWNER_FD = None
    _WATCHDOG_OWNER_PATH = None
    _WATCHDOG_OWNER_NAME = None
