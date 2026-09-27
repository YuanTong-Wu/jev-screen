"""`jevscreen doctor`: is this install ready for a first screen, and if not, what is the next command?

Rules:
- Read-only. It never creates the store or a folder, never writes a file and never sends a request, except
  `--check-jev`, which makes ONE free request to the active Jev provider (no model call): OpenRouter GET
  https://openrouter.ai/api/v1/key (key validity and what is left of the key's own spending limit), Vercel AI Gateway
  GET https://ai-gateway.vercel.sh/v1/credits (key validity and the team's credit balance), TypeSafe GET
  https://api.typesafe.ai/v1/models (key validity and whether jev-1.13.0 is listed; no balance is published).
- Secrets: key presence is checked by stat only (keys.presence); no key value is read, except by --check-jev, which
  sends the active provider's key in its Authorization header and redacts it from every error text.
- Jev provider: one check `jev_provider` names the active provider (jev.resolve_provider: JEVSCREEN_JEV_PROVIDER, else
  the human's saved choice - the last Jev key set, or `keys use` - else the first configured of openrouter, typesafe,
  vercel), its model id and whether that id is pinned to one version (Vercel AI Gateway is always named with "version
  cannot be pinned": jev.provider_title; `shown_as` / `shown_as_zh`). With no Jev key at all it fails with ask_human
  and the one account question (human_question / _zh) plus key_commands; the text output prints them too.
- The store is opened read-only in one short connection (_read_only_store: duckdb directly, NOT store.session, which
  creates the data folders), waiting up to STORE_WAIT_S for a lock. A lock held by another process is a warning,
  and the data checks are skipped, not failed.
- Every fix_command and next_command can be run verbatim: no placeholders, no shell operators, and never a consent
  write. A human decision (consent) is a check with fix_command null, ask_human true, the exact `human_question`
  and `record_answer_commands` (one ready command per answer; run only the one matching the human's own words).
  After a recorded 'no' on gray-private sources, next_command is null.
- Result: {ok, ready_for, checks: [{id, status, detail, fix_command, ask_human}], next_command, summary}. status is
  ok | warn | fail | skip. ok is true when no check failed (exit 0); next_command is the fix of the first failed
  check that has one (null after a recorded 'no'), else the suggested first screen (a dry run with the first-run defaults). Schema: docs/AGENT_API.md.
"""
from __future__ import annotations

import contextlib
import datetime as dt
import json
import os
import shlex
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable

from . import consent, keys
from .config import Config, redact

FIRST_RUN_MIN_MCAP = 1e9          # USD, first-run default
FIRST_RUN_BUDGET = 1.0            # USD, first-run cap; above it the agent asks the human
FRESH_DAYS, STALE_DAYS = 7, 30    # universe market data age: ok <= 7 d, warn above, loud warn above 30 d
DESCRIPTION_OK_SHARE = 0.8        # share of >= $1B companies with a description below which doctor warns
STORE_WAIT_S = 5.0
COOLDOWN_HOURS = 24
JEV_CHECK_URL = "https://openrouter.ai/api/v1/key"            # OpenRouter (the key's own limit)
JEV_CHECK_URLS = {"openrouter": JEV_CHECK_URL,
                  "vercel": "https://ai-gateway.vercel.sh/v1/credits",    # {"balance": "95.50", "total_used": ...}
                  "typesafe": "https://api.typesafe.ai/v1/models"}       # the models the key may use
JEV_CHECK_TIMEOUT_S = 15.0
KEY_PLACEHOLDER = "<jev-api-key>"
FIRST_SCREEN = (f'jevscreen screen "<idea in plain words>" --min-mcap {FIRST_RUN_MIN_MCAP:.0e} '
                f'--budget {FIRST_RUN_BUDGET:g} --dry-run').replace("e+0", "e")
QUICKSTART = 'jevscreen quickstart "<idea in plain words>" --json'     # the fast path (AGENTS.md)
CN_FILL = "jevscreen crawl-descriptions --countries CN --min-mcap 1e9"
STATUSES = ("ok", "warn", "fail", "skip")
# The exact question of AGENTS.md step 5, and one ready command per answer (never a `yes|no` template: in a shell
# that is a pipe that records "yes").
GRAY_QUESTION = consent.STATEMENTS[consent.GRAY_SOURCES]["en"]          # the current statement, recorded with the answer
GRAY_STATEMENT_VERSION = consent.STATEMENT_VERSION
GRAY_QUESTION_ZH = consent.STATEMENTS[consent.GRAY_SOURCES]["zh"]
GRAY_RECORD_COMMANDS = [f"jevscreen consent set gray-sources {v}" for v in ("yes", "no")]
GRAY_ASK = {"human_question": GRAY_QUESTION, "human_question_zh": GRAY_QUESTION_ZH,
            "record_answer_commands": GRAY_RECORD_COMMANDS}


def _check(cid: str, status: str, detail: str, fix: str | None = None, ask_human: bool = False,
           **extra: Any) -> dict[str, Any]:
    assert status in STATUSES
    return {"id": cid, "status": status, "detail": detail, "fix_command": fix, "ask_human": ask_human, **extra}


def _pct(n: int, total: int) -> str:
    return f"{n}/{total} ({(100.0 * n / total):.0f}%)" if total else f"{n}/0"


# --------------------------------------------------------------------------------------------------- environment

def check_python(version: tuple[int, ...] | None = None) -> dict[str, Any]:
    v = tuple(version or sys.version_info[:3])
    text = ".".join(str(x) for x in v)
    if v >= (3, 10):
        return _check("python", "ok", f"Python {text}")
    return _check("python", "fail", f"Python {text} is too old; jev-screen needs 3.10 or newer. The human installs a "
                  "newer Python; then run: python3 -m pip install -e .", None, ask_human=True)


DISTRIBUTIONS = {"duckdb": "duckdb", "fitz": "pymupdf", "pypdf": "pypdf"}


def _import_version(module: str) -> str | None:
    """Installed version without importing the (large, native) module: find_spec + package metadata."""
    import importlib.metadata
    import importlib.util
    try:
        if importlib.util.find_spec(module) is None:
            return None
    except (ImportError, ValueError):
        return None
    try:
        return importlib.metadata.version(DISTRIBUTIONS.get(module, module))
    except importlib.metadata.PackageNotFoundError:
        return "installed"


def check_dependencies(importer: Callable[[str], str | None] = _import_version) -> list[dict[str, Any]]:
    out = []
    duck = importer("duckdb")
    out.append(_check("dep_duckdb", "ok", f"duckdb {duck}") if duck else
               _check("dep_duckdb", "fail", "duckdb is not installed (the store needs it)", "python3 -m pip install -e ."))
    fitz = importer("fitz")
    if fitz:
        out.append(_check("dep_pymupdf", "ok", f"pymupdf {fitz} (CNINFO PDF text)"))
    elif (pypdf := importer("pypdf")):
        # pypdf (BSD, the [pdf] extra) is enough; PyMuPDF stays optional (AGPL-3.0; better on Chinese layout)
        out.append(_check("dep_pymupdf", "ok", f"pypdf {pypdf} (annual-report PDF text); PyMuPDF is optional "
                          "(AGPL-3.0, better on Chinese layout)"))
    else:
        out.append(_check("dep_pymupdf", "warn", "no PDF library: China / India / Taiwan annual-report PDFs cannot be "
                          "read; everything else works", "python3 -m pip install pypdf"))
    return out


def check_home(cfg: Config) -> dict[str, Any]:
    home = Path(cfg.home)
    if not home.exists():
        return _check("home", "fail", f"data folder {home} does not exist yet", "jevscreen init")
    if not home.is_dir():
        return _check("home", "fail", f"{home} is not a folder", None, ask_human=True)
    if not os.access(home, os.W_OK):
        return _check("home", "fail", f"data folder {home} is not writable", None, ask_human=True)
    return _check("home", "ok", f"data folder {home}")


# --------------------------------------------------------------------------------------------------- store + data

def _tables(con) -> set[str]:
    return {r[0] for r in con.execute("SELECT table_name FROM information_schema.tables "
                                      "WHERE table_schema = 'main'").fetchall()}


def data_checks(con, today: dt.date | None = None, min_mcap: float = FIRST_RUN_MIN_MCAP,
                gray_state: str = "unset") -> list[dict[str, Any]]:
    """universe / descriptions / official_text checks on an open (read-only) connection."""
    from . import coverage
    today = today or dt.datetime.now(dt.timezone.utc).date()
    tables = _tables(con)
    needed = {"universe", "securities", "descriptions", "market_daily", "fundamentals_current",
              "fundamentals_annual", "snapshots"}
    if not needed <= tables:
        missing = ", ".join(sorted(needed - tables))
        return [_check("universe", "fail", f"store schema is incomplete (missing {missing})", "jevscreen init"),
                _check("descriptions", "skip", "no store schema yet"),
                _check("official_text", "skip", "no store schema yet")]
    n_all, as_of = con.execute("SELECT count(*), max(market_as_of) FROM universe").fetchone()
    # How to refresh the universe depends on the recorded answer: yes -> the command; no -> nothing (the human
    # declined; do not refresh, do not re-ask); unset/unreadable -> the consent question, never a command.
    if gray_state == "yes":
        gray_fix, gray_ask, gray_extra, gray_note = "jevscreen refresh-universe", False, {}, ""
    elif gray_state == "no":
        gray_fix, gray_ask, gray_extra = None, True, {}
        gray_note = (". The human declined gray-private sources, so do not refresh it; the open data pack has no stock list, so "
                     "no screen can run")
    else:
        gray_fix, gray_ask, gray_extra = None, True, GRAY_ASK
        gray_note = ". Refreshing it needs the human's answer to human_question first"
    if not n_all:
        uni = _check("universe", "fail", "universe is empty. Today it is filled by `jevscreen refresh-universe`, "
                     "which reads the gray-private TradingView scanner (needs recorded consent; about a minute)"
                     + gray_note, gray_fix, gray_ask, companies=0, market_as_of=None, age_days=None, **gray_extra)
        return [uni, _check("descriptions", "skip", "universe is empty"),
                _check("official_text", "skip", "universe is empty")]
    as_of_date = as_of if isinstance(as_of, dt.date) else None
    if isinstance(as_of, dt.datetime):
        as_of_date = as_of.date()
    age = (today - as_of_date).days if as_of_date else None
    base = f"{n_all} companies, market data as of {as_of_date or 'unknown'}"
    if age is not None and age <= FRESH_DAYS:
        uni = _check("universe", "ok", f"{base} ({age} d old)", companies=n_all,
                     market_as_of=str(as_of_date), age_days=age)
    else:
        how = "unknown age" if age is None else f"{age} d old" + (", market caps may be far off" if age > STALE_DAYS
                                                                 else "")
        uni = _check("universe", "warn", f"{base} ({how}); screening still works on the old data{gray_note}",
                     gray_fix, gray_ask, companies=n_all, market_as_of=str(as_of_date) if as_of_date else None,
                     age_days=age, **gray_extra)
    docs, ids = coverage._docs_relation(con), coverage._ids_relation(con)
    sql = coverage._COMPANY_SQL.format(docs=docs, ids=ids)
    big, with_desc, with_doc = con.execute(
        f"SELECT count(*), count(*) FILTER (WHERE len(desc_sources) > 0), "
        f"count(*) FILTER (WHERE len(official_sources) > 0) FROM ({sql}) c WHERE market_cap_usd >= ?",
        [min_mcap]).fetchone()
    label = f"companies with market cap >= ${min_mcap / 1e9:g}B"
    share = with_desc / big if big else 0.0
    desc_detail = (f"{_pct(with_desc, big)} {label} have a business description (layer 1 reads it; the rest are "
                   "reported as gaps, not screened)")
    if big and share < DESCRIPTION_OK_SHARE:
        desc_detail += (f". A warn under {DESCRIPTION_OK_SHARE:.0%} is expected after quickstart (the bulk profile "
                        f"file covers about 60-70%); the largest gap is usually China: `{CN_FILL}` (long, gray "
                        "source, ask the human first)")
    desc = (_check("descriptions", "ok", desc_detail, companies=big, with_description=with_desc)
            if big and share >= DESCRIPTION_OK_SHARE else
            _check("descriptions", "warn", desc_detail, "jevscreen coverage", companies=big,
                   with_description=with_desc, gap_fill_command=CN_FILL))
    doc_detail = (f"{_pct(with_doc, big)} {label} have official annual-report text (layer 2 evidence; without it "
                  "layer 2 re-reads the description, a weaker check)")
    off = (_check("official_text", "ok", doc_detail, companies=big, with_official_text=with_doc) if with_doc else
           _check("official_text", "warn", doc_detail, "jevscreen sync-sec --limit 200", companies=big,
                  with_official_text=with_doc))
    return [uni, desc, off]


def cooldown_checks(cfg: Config, con=None, hours: float = COOLDOWN_HOURS) -> dict[str, Any]:
    """Active 24 h cooldowns after a blocked run: marker files plus the runs journal when a connection is given."""
    from . import guard
    active: dict[str, str] = {}
    folder = Path(cfg.home) / "cooldown"
    commands = {p.stem for p in folder.glob("*.json")} if folder.is_dir() else set()
    try:
        from .cli import RUN_COMMANDS, recent_block
        commands |= set(RUN_COMMANDS)
    except Exception:  # noqa: BLE001 - doctor must not die on a CLI import problem
        recent_block = None
    for cmd in sorted(commands):
        hit = guard.recent_block_marker(cfg, cmd, hours)
        if hit is None and con is not None and recent_block is not None:
            try:
                hit = recent_block(con, cmd, hours)
            except Exception:  # noqa: BLE001 - no runs table on a fresh store
                hit = None
        if hit is not None:
            at = hit["blocked_at"]
            active[cmd] = at.isoformat() if hasattr(at, "isoformat") else str(at)
    if not active:
        return _check("cooldown", "ok", "no command is in a block cooldown", cooldowns={})
    listing = ", ".join(f"{c} (blocked at {t} UTC)" for c, t in active.items())
    return _check("cooldown", "warn", f"in a {hours:g} h cooldown after a provider block: {listing}. Wait it out; "
                  "--after-block needs the human's explicit yes", None, ask_human=True, cooldowns=active)


# --------------------------------------------------------------------------------------------------- keys, consent

def key_checks(cfg: Config) -> list[dict[str, Any]]:
    out = []
    jev_names = set(keys.JEV_KEYS)
    for name, spec in keys.KEYS.items():
        p = keys.presence(cfg, name)
        cid = "key_" + name.replace("-", "_")
        fix = f"jevscreen keys set {name}"
        recorded = bool(p.get("recorded"))          # --from-file: the human's own file, its path is never printed
        shown = "the recorded key file" if recorded else f"file {p['path']}"
        where = {"env": f"environment variable {spec.env}", "file": shown}.get(p["source"], "")
        extra = {"configured": p["configured"], "source": p["source"]}
        if p["source"] != "none" and not p["configured"]:
            blank = (f"environment variable {spec.env} is blank" if p["source"] == "env"
                     else "the recorded key file is empty" if recorded else f"{p['path']} is empty")
            out.append(_check(cid, "warn",
                              f"{name} key: {blank}, so the key is read as missing (later sources are not tried)",
                              fix, ask_human=True, **extra))
        elif p["configured"]:
            if p["mode_ok"] is False:
                out.append(_check(cid, "warn", f"{name} key is set ({where}) but other users can read the file",
                                  "chmod 600 <the file you keep the key in>" if recorded
                                  else f"chmod 600 {shlex.quote(str(p['path']))}", **extra))
            else:
                out.append(_check(cid, "ok", f"{name} key is set ({where}); value not shown", **extra))
        elif name in jev_names:
            out.append(_check(cid, "skip", f"{name} key not set: one Jev key is enough (typesafe, openrouter or "
                              "vercel; see the jev_provider check)", fix, **extra))
        elif name == "sec-email":
            out.append(_check(cid, "warn", "no SEC User-Agent (name + email): needed only for sync-sec (US annual "
                              "reports); it is sent to sec.gov, so ask the human", fix, ask_human=True, **extra))
        else:
            out.append(_check(cid, "skip", f"{name} key not set: off by default, needed only for {spec.used_by} "
                              f"({spec.purpose})", fix, ask_human=True, **extra))
    return out


def jev_provider_check(cfg: Config) -> dict[str, Any]:
    """Which Jev provider paid calls use, and why. Fails when no Jev key is configured (or the chosen provider has
    none, or JEVSCREEN_JEV_PROVIDER is unknown): real screens need one; dry runs do not."""
    from . import jev, quickstart
    try:
        prov, why = jev.resolve_provider(cfg)
    except jev.ProviderError as e:
        return _check("jev_provider", "fail", f"{e}. Unset it or fix it", None, ask_human=False, provider=None)
    configured = jev.configured_providers(cfg)
    extra = {"provider": prov.name, "label": prov.label, "model": prov.model, "pinned": prov.pinned,
             "shown_as": jev.provider_title(prov, "en"), "shown_as_zh": jev.provider_title(prov, "zh"),
             "endpoint": prov.endpoint, "reason": why, "configured": configured}
    if why == "default":
        return _check("jev_provider", "fail", "no Jev key: real screens need one (dry runs do not). Jev is sold at "
                      "the same price by TypeSafe (official API), OpenRouter and Vercel AI Gateway: ask the human "
                      "human_question, then run the matching key_commands entry (a hidden input box)", None,
                      ask_human=True, human_question=quickstart.account_question("en"),
                      human_question_zh=quickstart.account_question("zh"),
                      key_commands={n: f"jevscreen keys set {n} --dialog" for n in jev.PROVIDER_ORDER}, **extra)
    if prov.name not in configured:
        chose = (f"{jev.PROVIDER_ENV}={prov.name}" if why == "explicit" else
                 f"{prov.label} was chosen (`jevscreen keys use {prov.name}`)")
        other = ("" if why == "explicit" or not configured else
                 f"; or go back to {', '.join(configured)} with `jevscreen keys use {configured[0]}`")
        return _check("jev_provider", "fail", f"{chose} but no {prov.label} key is set{other}",
                      f"jevscreen keys set {prov.name}", ask_human=True, **extra)
    pin = ("model pinned to one version" if prov.pinned else
           f"{jev.VERSION_NOT_PINNED['en']}: the model id names no version, so Vercel decides which Jev version "
           "answers")
    others = [n for n in configured if n != prov.name]
    how = (f"chosen by {jev.PROVIDER_ENV}" if why == "explicit" else
           "the only Jev key set" if not others else
           (f"the human's choice (the last Jev key set, or `jevscreen keys use`)" if why == "saved" else
            f"first of {', '.join(configured)}") + f"; `jevscreen keys use {others[0]}` switches")
    return _check("jev_provider", "ok", f"Jev via {jev.provider_title(prov, 'en')}, model {prov.model} ({pin}); {how}. "
                  "Answers are cached per provider: switching provider reads again and pays again", **extra)


# Who receives the text the consent statement says is "sent to Jev": the owner's statement is kept word for word, and
# this line, shown right after it, names the active provider (and the company in between, if any).
RECIPIENT = {
    "en": {None: "(Jev is reached through the account you choose: TypeSafe's official API, OpenRouter or Vercel AI "
                 "Gateway. With OpenRouter or Vercel, that company and the host that serves Jev behind it also "
                 "receive the text.)",
           "typesafe": "(Jev is reached through TypeSafe's official API: the text goes to TypeSafe.)",
           "openrouter": "(Jev is reached through OpenRouter: the text goes to OpenRouter and to the host that serves "
                         "Jev behind it.)",
           "vercel": "(Jev is reached through Vercel AI Gateway: the text goes to Vercel and to the host Vercel picks "
                     "to serve Jev.)"},
    "zh": {None: "（Jev 通过你选的那一家调用：TypeSafe 官方、OpenRouter 或 Vercel AI Gateway。用 OpenRouter 或 Vercel 时，"
                 "内容也会经过这家公司和它背后提供 Jev 的服务商。）",
           "typesafe": "（Jev 通过 TypeSafe 官方调用：内容发给 TypeSafe。）",
           "openrouter": "（Jev 通过 OpenRouter 调用：内容会发给 OpenRouter 和它背后提供 Jev 的服务商。）",
           "vercel": "（Jev 通过 Vercel AI Gateway 调用：内容会发给 Vercel 和 Vercel 选定的提供 Jev 的服务商。）"},
}


def recipient_note(cfg: Config, lang: str = "en") -> str:
    """One line naming who receives the profiles and excerpts sent to Jev, for the active provider (no key yet or an
    unknown JEVSCREEN_JEV_PROVIDER: the three choices)."""
    from . import jev
    try:
        prov, why = jev.resolve_provider(cfg)
        name = None if why == "default" else prov.name
    except jev.ProviderError:
        name = None
    return RECIPIENT["zh" if lang == "zh" else "en"][name]


def consent_check(cfg: Config) -> dict[str, Any]:
    c = consent.get(cfg, consent.GRAY_SOURCES)
    if c["state"] == "yes":
        return _check("consent_gray_sources", "ok", f"gray-private sources allowed for personal use (recorded "
                      f"{c['recorded_at']})", state="yes", recorded_at=c["recorded_at"])
    if c["state"] == "no":
        return _check("consent_gray_sources", "fail", f"gray-private sources declined (recorded {c['recorded_at']}). "
                      "The universe and descriptions come from them (the open data pack has no stock list), so do not screen "
                      "or fetch. Do not ask again; only if the human says they changed their mind, ask the "
                      "AGENTS.md step 5 question", None,
                      ask_human=True, state="no", recorded_at=c["recorded_at"])
    if c["state"] == "unreadable":
        return _check("consent_gray_sources", "warn", f"consent file is unreadable ({c['problem']}): treated as no. "
                      "Ask the human human_question and record their answer", None, ask_human=True,
                      state="unreadable", recorded_at=None, **GRAY_ASK, **_recipient(cfg))
    return _check("consent_gray_sources", "warn", "no recorded answer on gray-private sources (TradingView, "
                  "FinanceDatabase/Yahoo text; personal use only): treated as no. Ask the human human_question and "
                  "record their answer", None, ask_human=True, state="unset", recorded_at=None, **GRAY_ASK,
                  **_recipient(cfg))


def _recipient(cfg: Config) -> dict[str, Any]:
    return {"recipient_note": recipient_note(cfg, "en"), "recipient_note_zh": recipient_note(cfg, "zh"),
            "answer_words": consent.ANSWER_WORDS}


# --------------------------------------------------------------------------------------------------- Jev (optional)

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never follow a redirect: urllib would resend the Authorization header to the new location."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        return None


def check_jev(cfg: Config, opener: Callable[..., Any] | None = None) -> dict[str, Any]:
    """One free GET to the active provider (no model call): does the key work, and what is left to spend when the
    provider says. OpenRouter: the key's own spending limit (the account balance is not in that answer, so a key with
    no limit on an empty account still reads ok; the first paid call then stops with HTTP 402, jev.py). Vercel: the
    team's credit balance. TypeSafe: the models the key may use (no balance is published)."""
    from . import jev
    try:
        prov = jev.active_provider(cfg)
    except jev.ProviderError as e:
        return _check("jev", "fail", str(e), None)
    name, label = prov.name, jev.provider_title(prov, "en")    # Vercel: '... (version cannot be pinned)'
    fix = f"jevscreen keys set {name}"
    key = cfg.jev_key(name)
    if not key:
        return _check("jev", "fail", f"no {label} key configured", fix, ask_human=True, provider=name)
    if any(ord(c) < 33 or ord(c) > 126 for c in key) or len(key) > 4096:
        return _check("jev", "fail", f"{label} key is malformed (spaces or non-ASCII)", fix, ask_human=True,
                      provider=name)
    req = urllib.request.Request(JEV_CHECK_URLS[name], method="GET", headers={
        "Authorization": "Bearer " + key, "Accept": "application/json", "User-Agent": cfg.user_agent})
    open_ = opener or urllib.request.build_opener(_NoRedirect()).open
    try:
        with open_(req, timeout=JEV_CHECK_TIMEOUT_S) as resp:
            body = resp.read(65536)
    except urllib.error.HTTPError as e:
        code = e.code
        if 300 <= code < 400:
            return _check("jev", "warn", f"{label} answered with a redirect (HTTP {code}); not followed, so the "
                          "key was not sent anywhere else", None, http_status=code, provider=name)
        if code in (401, 403):
            return _check("jev", "fail", f"{label} rejected the key (HTTP {code})", fix, ask_human=True,
                          http_status=code, provider=name)
        if code == 402:
            return _check("jev", "fail", f"{label} answered HTTP 402 (payment required): no credit left. The human "
                          f"must add credit at {prov.credits_url}; waiting does not help", None,
                          ask_human=True, http_status=code, provider=name)
        return _check("jev", "warn", f"{label} answered HTTP {code}; try again later", None, http_status=code,
                      provider=name)
    except Exception as e:  # noqa: BLE001 - network trouble is a warning, never a crash
        text = redact(f"{type(e).__name__}: {e}", [key], KEY_PLACEHOLDER)[:200]
        return _check("jev", "warn", f"{label} not reachable ({text})", None, provider=name)
    try:
        parsed = json.loads(body)
    except ValueError:
        parsed = None
    if name == "vercel":
        bal = parsed.get("balance") if isinstance(parsed, dict) else None
        try:
            balance = float(bal) if bal is not None else None
        except (TypeError, ValueError):
            balance = None
        extra = {"http_status": 200, "provider": name, "balance_usd": balance}
        if balance is not None and balance < FIRST_RUN_BUDGET:
            return _check("jev", "warn", f"key works but the AI Gateway balance is ${balance:.2f} (first run needs "
                          f"up to ${FIRST_RUN_BUDGET:g}); add credit at {prov.credits_url}", None, ask_human=True,
                          **extra)
        left = "balance not reported" if balance is None else f"${balance:.2f} AI Gateway credit"
        return _check("jev", "ok", f"{label} key accepted ({left}); no paid call was made", **extra)
    if name == "typesafe":
        listed = prov.model in (body.decode("utf-8", "replace") if isinstance(body, (bytes, bytearray)) else "")
        extra = {"http_status": 200, "provider": name, "model_listed": listed}
        what = (f"{prov.model} is listed" if listed else f"{prov.model} was not found in the model list; the first "
                "paid call will tell")
        return _check("jev", "ok", f"{label} key accepted ({what}; TypeSafe publishes no balance); no paid call was "
                      "made", **extra)
    data = parsed.get("data") if isinstance(parsed, dict) else None
    data = data if isinstance(data, dict) else {}
    remaining = data.get("limit_remaining")
    usage = data.get("usage")
    extra = {"http_status": 200, "limit_remaining": remaining, "usage_usd": usage,
             "is_free_tier": data.get("is_free_tier"), "provider": name}
    if isinstance(remaining, (int, float)) and remaining < FIRST_RUN_BUDGET:
        return _check("jev", "warn", f"key works but only ${remaining:.2f} of its limit is left (first run needs up "
                      f"to ${FIRST_RUN_BUDGET:g})", None, ask_human=True, **extra)
    left = ("no spending limit set on the key" if remaining is None else f"${remaining:.2f} left on its limit")
    left += "; the account balance is not checked"
    return _check("jev", "ok", f"OpenRouter key accepted ({left}); no paid call was made", **extra)


# --------------------------------------------------------------------------------------------------- run

@contextlib.contextmanager
def _read_only_store(cfg: Config, wait_s: float = STORE_WAIT_S, poll_s: float = 0.2):
    """Open the existing store read-only without creating anything (store.session would mkdir the data folders).
    Retries on another process's lock until wait_s, then raises store.StoreLocked."""
    import duckdb

    from .store import StoreLocked
    deadline = time.monotonic() + wait_s
    while True:
        try:
            con = duckdb.connect(str(cfg.db_path), read_only=True)
            break
        except duckdb.IOException as e:
            if "lock" not in str(e).lower():
                raise
            if time.monotonic() >= deadline:
                raise StoreLocked(str(e)) from e
            time.sleep(poll_s)
    try:
        yield con
    finally:
        con.close()


def run(cfg: Config, *, check_jev_flag: bool = False, today: dt.date | None = None,
        importer: Callable[[str], str | None] | None = None,
        jev_opener: Callable[..., Any] | None = None) -> dict[str, Any]:
    checks: list[dict[str, Any]] = [check_python()]
    deps = check_dependencies(importer or _import_version)
    checks += deps
    checks.append(check_home(cfg))
    gray = consent_check(cfg)
    duck_ok = deps[0]["status"] == "ok"
    con_checks: list[dict[str, Any]] = []
    cool = None
    if not duck_ok:
        checks.append(_check("store", "skip", "duckdb is not installed"))
    elif not cfg.db_path.exists():
        checks.append(_check("store", "fail", f"no store at {cfg.db_path}", "jevscreen init"))
    else:
        from . import store
        try:
            with _read_only_store(cfg, wait_s=STORE_WAIT_S) as con:
                checks.append(_check("store", "ok", f"store {cfg.db_path}"))
                con_checks = data_checks(con, today=today, gray_state=gray.get("state", "unset"))
                cool = cooldown_checks(cfg, con)
        except store.StoreLocked:
            checks.append(_check("store", "warn", "store is locked by another jevscreen process (a sync or crawl "
                                 "is writing); data checks skipped. Run doctor again when it pauses", None))
        except Exception as e:  # noqa: BLE001 - a corrupt or foreign file must not crash doctor
            checks.append(_check("store", "fail", f"store cannot be opened: {type(e).__name__}: {str(e)[:200]}",
                                 None, ask_human=True))
    if not con_checks:
        con_checks = [_check(c, "skip", "store not available") for c in ("universe", "descriptions", "official_text")]
    checks += con_checks
    checks += key_checks(cfg)
    checks.append(jev_provider_check(cfg))
    from . import ondemand                  # on_demand_docs (PDF reader + SEC email), mops_annual (skip)
    checks += ondemand.doctor_checks(cfg)
    checks.append(gray)
    checks.append(cool or cooldown_checks(cfg))
    checks.append(check_jev(cfg, jev_opener) if check_jev_flag else
                  _check("jev", "skip", "not checked (add --check-jev: one free request to the Jev provider, no paid "
                         "call)",
                         "jevscreen doctor --check-jev"))
    failed = [c for c in checks if c["status"] == "fail"]
    ok = not failed
    next_cmd = next((c["fix_command"] for c in failed if c["fix_command"]), None)
    if gray.get("state") == "no":
        next_cmd = None   # the human declined: nothing to run, and never a nudge back to the consent question
    if ok:
        next_cmd = FIRST_SCREEN
    counts = {s: sum(1 for c in checks if c["status"] == s) for s in STATUSES}
    ask = [c["id"] for c in checks if c["ask_human"] and c["status"] in ("fail", "warn")]
    return {"command": "doctor", "ok": ok, "ready_for": "screen" if ok else None, "checks": checks,
            "next_command": next_cmd, "ask_human": ask, "summary": counts, "quickstart_command": QUICKSTART,
            "first_run_defaults": {"min_mcap_usd": FIRST_RUN_MIN_MCAP, "budget_usd": FIRST_RUN_BUDGET,
                                   "dry_run_first": True}}


def format_text(result: dict[str, Any]) -> str:
    lines = []
    for c in result["checks"]:
        mark = {"ok": "ok  ", "warn": "WARN", "fail": "FAIL", "skip": "skip"}[c["status"]]
        lines.append(f"[{mark}] {c['id']}: {c['detail']}")
        if c["status"] in ("warn", "fail"):
            # fields the detail points to (a question for the human, ready commands): JSON users get them as fields
            for field, head in (("human_question", "ask the human"), ("recipient_note", "then say"),
                                ("human_question_zh", "ask the human (zh)"), ("recipient_note_zh", "then say (zh)")):
                if c.get(field):
                    lines.append(f"       {head}: {c[field]}")
            for cmd in c.get("record_answer_commands") or []:
                lines.append(f"       record their answer: {cmd}")
            for name, cmd in (c.get("key_commands") or {}).items():
                lines.append(f"       {name} key: {cmd}")
        if c["status"] in ("warn", "fail") and c["fix_command"]:
            who = " (ask the human first)" if c["ask_human"] else ""
            lines.append(f"       fix{who}: {c['fix_command']}")
    s = result["summary"]
    lines.append("")
    lines.append(("READY" if result["ok"] else "NOT READY") +
                 f": {s['ok']} ok, {s['warn']} warn, {s['fail']} fail, {s['skip']} skipped")
    if result["next_command"]:
        lines.append(f"next: {result['next_command']}")
    return "\n".join(lines)
