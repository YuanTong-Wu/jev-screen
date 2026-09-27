"""Process-level guards for networked commands that must work without the DuckDB lock.

- budget_lock(cfg, budget): an advisory lock file per rate budget (<home>/locks/<budget>.lock, fcntl.flock). Each
  process has its own http.Client limiter, so two processes on the same host would together exceed the polite rate;
  holding this lock for the whole run makes a second copy refuse to start (Busy). Adapters that spend a budget take
  it themselves with reentrant=True, so library callers (scripts, notebooks) are covered too while the CLI, which
  already holds it, re-enters without a second lock. A non-reentrant acquire of a budget this process already holds
  raises Busy.
- Cooldown markers (<home>/cooldown/<command>.json): written the moment http.Blocked is seen (crawl-descriptions: by
  the worker thread that got it), before any database write or wait, so the 24 h cooldown survives a run whose final DuckDB write failed (StoreLocked, crash, SIGKILL after the
  marker). The CLI checks the marker as well as the `runs` journal and clears it after a clean ('ok') run.
Markers hold only the time, command, URL, HTTP status and reason (never headers or the SEC User-Agent).
"""
from __future__ import annotations

import contextlib
import datetime as dt
import json
import os
import re
import threading
from pathlib import Path
from typing import Any, Iterator

from .config import Config

# CLI command names (used by adapters for cooldown markers) and the rate budget each one spends.
CMD_SYNC_SEC = "sync-sec"
CMD_CRAWL_DESCRIPTIONS = "crawl-descriptions"
CMD_REFRESH_UNIVERSE = "refresh-universe"
RATE_BUDGETS = {
    CMD_SYNC_SEC: "sec.gov",                            # www.sec.gov + data.sec.gov share one budget
    CMD_CRAWL_DESCRIPTIONS: "www.tradingview.com",
    CMD_REFRESH_UNIVERSE: "scanner.tradingview.com",
}


class Busy(RuntimeError):
    """Another process already holds this rate budget's lock."""


_held: dict[str, int] = {}          # lock path -> reentrant depth held by THIS process
_held_mutex = threading.Lock()


def _safe(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.\-]+", "_", name) or "_"


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)


@contextlib.contextmanager
def budget_lock(cfg: Config, budget: str, *, reentrant: bool = False) -> Iterator[Path]:
    """Hold <home>/locks/<budget>.lock exclusively for the block; raise Busy at once if another process holds it.

    With reentrant=True, a lock this process already holds is simply re-entered (no second lock). Without it, an
    in-process holder also means Busy. The OS releases the lock when the process exits, so a crash never leaves a
    stale lock behind."""
    folder = Path(cfg.home) / "locks"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{_safe(budget)}.lock"
    key = str(path.resolve())
    with _held_mutex:
        depth = _held.get(key, 0)
        if depth and reentrant:
            _held[key] = depth + 1
    if depth and reentrant:
        try:
            yield path
        finally:
            with _held_mutex:
                _held[key] -= 1
        return
    if depth:
        raise Busy(f"this process is already using the {budget} rate budget ({path})")
    with _exclusive(path, budget):
        with _held_mutex:
            _held[key] = 1
        try:
            yield path
        finally:
            with _held_mutex:
                _held.pop(key, None)


@contextlib.contextmanager
def _exclusive(path: Path, budget: str) -> Iterator[None]:
    fh = open(path, "a+")
    try:
        try:
            import fcntl
        except ImportError:   # non-POSIX: best effort with msvcrt, else no lock
            fcntl = None
        if fcntl is not None:
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except (BlockingIOError, PermissionError) as e:
                raise Busy(f"another process is using the {budget} rate budget ({path})") from e
            unlock = lambda: fcntl.flock(fh.fileno(), fcntl.LOCK_UN)  # noqa: E731
        else:
            try:
                import msvcrt
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
                unlock = lambda: (fh.seek(0), msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1))  # noqa: E731
            except ImportError:
                unlock = lambda: None  # noqa: E731
            except OSError as e:
                raise Busy(f"another process is using the {budget} rate budget ({path})") from e
        try:
            fh.seek(0)
            fh.truncate()
            fh.write(f"{os.getpid()}\n")
            fh.flush()
            yield
        finally:
            unlock()
    finally:
        fh.close()


def _marker_path(cfg: Config, command: str) -> Path:
    return Path(cfg.home) / "cooldown" / f"{_safe(command)}.json"


def mark_blocked(cfg: Config, command: str, *, url: str | None = None, status: int | None = None,
                 reason: str | None = None, at: dt.datetime | None = None) -> Path | None:
    """Record a block for `command` on disk (atomic replace). Never raises: returns None if it could not write."""
    try:
        path = _marker_path(cfg, command)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"command": command, "blocked_at": (at or _now()).isoformat(), "url": url,
                                   "http_status": status, "reason": reason}, sort_keys=True))
        os.replace(tmp, path)
        return path
    except OSError:
        return None


def clear_blocked(cfg: Config, command: str) -> None:
    with contextlib.suppress(OSError):
        _marker_path(cfg, command).unlink()


def recent_block_marker(cfg: Config, command: str, hours: float = 24) -> dict[str, Any] | None:
    """The marker for `command` if it is younger than `hours`, else None (unreadable markers are ignored)."""
    try:
        data = json.loads(_marker_path(cfg, command).read_text())
        at = dt.datetime.fromisoformat(str(data["blocked_at"]))
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if at.tzinfo is not None:            # a marker written by hand with an offset ('...+00:00'): compare in UTC
        at = at.astimezone(dt.timezone.utc).replace(tzinfo=None)
    if _now() - at >= dt.timedelta(hours=hours):
        return None
    return {"blocked_at": at, "note": f"blocked: {data.get('reason')} (status={data.get('http_status')}) at "
                                      f"{data.get('url')}", "source": "marker"}
