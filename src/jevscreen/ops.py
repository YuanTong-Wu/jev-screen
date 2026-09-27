"""Networked steps run from inside another command (quickstart's worker, the on-demand hook) with the exact rules of
the CLI commands of the same name: consent, the 24 h cooldown after a block, the per-host rate-budget lock, the
`runs` journal under the standard command names (cli.RUN_COMMANDS) and the cooldown marker on a block.

- run_networked(command, cfg, work) -> (exit_code, summary). It never prints and never prompts. `work()` makes
  the adapter call and opens its own short store sessions. Exit codes are the CLI's: 0 ok/partial, 1 error,
  2 blocked or in cooldown (summary['retry_after']), 3 store busy, 4 the rate budget is held by another process,
  12 consent missing or declined.
- So a block met by quickstart (e.g. refresh-universe) makes the plain `jevscreen refresh-universe` refuse for 24 h
  too, and the reverse.
- scan_secrets(cfg, text) -> the names of configured secrets (API keys, the SEC User-Agent and its e-mail) that
  appear in `text`; the job file and the result page are scanned before they are written.

The helpers (_mark_block, _journal_detached, recent_block, _result_status, _redact) stay in cli.py and are used
from there, so the CLI and these steps can never drift apart.
"""
from __future__ import annotations

import datetime as dt
from typing import Any, Callable, Sequence

EXIT_CONSENT = 12


def retry_after(blocked_at: Any, hours: float) -> str | None:
    """ISO time (UTC, 'Z') when a cooldown that started at blocked_at ends."""
    if isinstance(blocked_at, str):
        try:
            blocked_at = dt.datetime.fromisoformat(blocked_at)
        except ValueError:
            return None
    if not isinstance(blocked_at, dt.datetime):
        return None
    if blocked_at.tzinfo is not None:
        blocked_at = blocked_at.astimezone(dt.timezone.utc).replace(tzinfo=None)
    return (blocked_at + dt.timedelta(hours=hours)).replace(microsecond=0).isoformat() + "Z"


def cooldown(cfg, command: str) -> dict[str, Any] | None:
    """The active cooldown of `command` ({'blocked_at', 'retry_after', 'note'}) or None: the marker file first
    (no DuckDB lock needed), then the runs journal (a short read-only session; a locked store counts as none)."""
    from . import cli, guard, store
    hit = guard.recent_block_marker(cfg, command, cli.COOLDOWN_HOURS)
    if hit is None and cfg.db_path.exists():
        try:
            with store.session(cfg, read_only=True, wait_s=10.0) as con:
                hit = cli.recent_block(con, command)
        except Exception:  # noqa: BLE001 - locked or no runs table yet: the marker is authoritative
            hit = None
    if hit is None:
        return None
    return {"blocked_at": str(hit["blocked_at"]), "retry_after": retry_after(hit["blocked_at"], cli.COOLDOWN_HOURS),
            "note": hit.get("note")}


def run_networked(command: str, cfg, work: Callable[[], Any], *, client: Any = None, secrets: Sequence[Any] = (),
                  after_block: bool = False, consent_source: str | None = None) -> tuple[int, dict[str, Any]]:
    """Run one networked adapter call under `command`'s rules; see the module docstring."""
    from . import cli, consent, guard, store
    from .http import Blocked
    if consent_source is not None:
        try:
            consent.require_gray_sources(cfg, consent_source)
        except consent.ConsentRequired as e:
            return EXIT_CONSENT, {"command": command, "status": "consent_required", "consent": e.state}
    if not after_block:
        cd = cooldown(cfg, command)
        if cd is not None:
            return cli.EXIT_BLOCKED, {"command": command, "status": "cooldown", **cd}
    budget = guard.RATE_BUDGETS.get(command) or cli.CLI_RATE_BUDGETS.get(command, command)
    requests = lambda: getattr(client, "requests_made", None) if client is not None else None  # noqa: E731
    try:
        with guard.budget_lock(cfg, budget, reentrant=True):
            started = store.now_utc()
            try:
                result = work()
            except Blocked as e:
                cli._mark_block(cfg, command, e, secrets)
                cli._journal_detached(cfg, command, started, "blocked", requests(), str(e), secrets)
                cd = cooldown(cfg, command) or {}
                return cli.EXIT_BLOCKED, {"command": command, "status": "blocked", "http_status": e.status,
                                          "reason": e.reason, "retry_after": cd.get("retry_after")}
            except store.StoreLocked as e:
                return cli.EXIT_LOCKED, {"command": command, "status": "store_busy", "error": str(e)[:200]}
            except consent.ConsentRequired as e:
                return EXIT_CONSENT, {"command": command, "status": "consent_required", "consent": e.state}
            except Exception as e:  # noqa: BLE001 - journaled and reported, never raised into the worker
                text = cli._redact(f"{type(e).__name__}: {e}", secrets)[:300]
                cli._journal_detached(cfg, command, started, "error", requests(), text, secrets)
                return cli.EXIT_ERROR, {"command": command, "status": "error", "error": text}
            except BaseException as e:   # Ctrl-C / SIGTERM: journaled 'interrupted', then propagated
                cli._journal_detached(cfg, command, started, "interrupted", requests(), type(e).__name__, secrets)
                raise
            status = cli._result_status(result)
            if status == "blocked":
                r = result if isinstance(result, dict) else {}
                if guard.recent_block_marker(cfg, command, cli.COOLDOWN_HOURS) is None:
                    cli._mark_block(cfg, command, None, secrets)
                cli._journal_detached(cfg, command, started, "blocked", requests(), result, secrets)
                cd = cooldown(cfg, command) or {}
                return cli.EXIT_BLOCKED, {"command": command, "status": "blocked", "result": r,
                                          "retry_after": cd.get("retry_after")}
            cli._after_run(cfg, command, status)
            cli._journal_detached(cfg, command, started, status, requests(), result, secrets)
            return cli.RESULT_EXIT.get(status, cli.EXIT_ERROR), {"command": command, "status": status,
                                                                  "result": result}
    except guard.Busy as e:
        return cli.EXIT_BUSY, {"command": command, "status": "busy", "error": str(e)[:200]}


def secret_values(cfg) -> list[tuple[str, str]]:
    """(name, value) of every configured secret, read at call time and never returned to a caller that prints."""
    out: list[tuple[str, str]] = []
    getters = (("openrouter", cfg.openrouter_key), ("sec-email", cfg.sec_user_agent),
               ("edinet", cfg.edinet_api_key), ("opendart", cfg.opendart_api_key))
    for name, fn in getters:
        try:
            v = fn()
        except OSError:
            v = None
        if v:
            out.append((name, v))
            if name == "sec-email":
                out += [(name, t.strip("<>()[],;")) for t in v.split() if "@" in t]
    return out


def scan_secrets(cfg, text: str) -> list[str]:
    """Names of the configured secrets that occur in `text` (verbatim or JSON-escaped). Values of fewer than 6
    characters are ignored (too short to be distinctive)."""
    from .config import secret_variants
    hits: list[str] = []
    for name, value in secret_values(cfg):
        if len(value) < 6:
            continue
        if any(v in text for v in secret_variants(value)) and name not in hits:
            hits.append(name)
    return hits
