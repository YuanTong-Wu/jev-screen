"""Recorded consent for choices a human must make, starting with gray-private sources.

Rules:
- Consent lives in <home>/consent.json (git-ignored with data/). Each topic keeps its current value ('yes' | 'no'),
  when it was recorded (UTC ISO-8601), how ('cli'), whether stdin was a terminal, and the statement the human agreed
  to. Every change is appended to 'history', so a later 'no' never erases an earlier 'yes' from the record.
- No consent recorded means NO: require_gray_sources() refuses a gray-private (or unknown) source unless the current
  value of 'gray-sources' is 'yes'. official-* sources never need it.
- An unreadable or malformed file also means NO (fail closed); doctor reports it.
- Only a human decides: an AI agent may run `jevscreen consent set gray-sources yes|no` only to record an answer the
  human gave in words (see AGENTS.md).
"""
from __future__ import annotations

import contextlib
import datetime as dt
import json
import os
import sys
from pathlib import Path
from typing import Any

from . import provenance
from .config import Config

CONSENT_FILE = "consent.json"
GRAY_SOURCES = "gray-sources"
TOPICS: dict[str, str] = {
    GRAY_SOURCES: ("I understand that gray-private sources (TradingView scanner and symbol pages, FinanceDatabase / "
                   "Yahoo summaries) are public but terms-restricted. I use them for personal research on this "
                   "machine only and will not publish or share the data or anything derived from it."),
    # on-demand annual reports from MOPS / doc.twse.com.tw (robots.txt disallows crawlers): off unless the human says
    # yes (jevscreen.ondemand; at most 8 companies per run, >= 1.5 s between requests)
    "mops-annual": ("I want Taiwan annual reports fetched from the exchange's MOPS site during a screen (at most 8 "
                    "companies per run, slowly), although its robots.txt discourages automated downloads. Personal "
                    "use only."),
    # whether screens may ask for an SEC contact email (jevscreen.ondemand questions[] sec_email): 'no' stops the
    # question; `keys set sec-email` stays available any time
    "sec-email-ask": ("Ask me to set an SEC contact email when US companies in a screen could be read from their "
                      "annual reports with it."),
}
VALUES = ("yes", "no")
# Replies that clearly mean yes or no (compared after trimming spaces and end punctuation, case-insensitive). The
# statement offers 可以 / 不要 and yes / no, but a beginner answers 同意, 好 or OK just as clearly; anything else, or a
# reply with a condition, is not an answer: ask again.
ANSWER_WORDS: dict[str, tuple[str, ...]] = {
    "yes": ("yes", "y", "ok", "okay", "sure", "agree", "i agree",
            "可以", "可以的", "同意", "我同意", "好", "好的", "行", "没问题"),
    "no": ("no", "n", "nope", "i don't agree", "不要", "不可以", "不同意", "我不同意", "不行", "算了"),
}
LANGS = ("en", "zh")
# The versioned consent statement: this exact text is shown to the human (quickstart, doctor, AGENTS.md step 5) and
# recorded with the answer. Version 2 added that profile text and annual-report excerpts are sent to the AI service.
# Version 3 says plainly what the terms restrict (TradingView forbids automated use of its data; the Yahoo text's
# terms restrict reuse) and that annual reports from official sites are personal use too. Version 4 says the same in
# a casual tone (the owner's wording): gray area, their terms do not allow automated bulk use, occasional rate limits or
# short blocks, the data stays on this computer, and profiles / report excerpts are sent to Jev (whichever provider
# serves it: TypeSafe, OpenRouter or Vercel AI Gateway). An older 'yes' stays valid for the plain commands; quickstart
# asks once more for the current version.
STATEMENT_VERSION = 4
STATEMENTS: dict[str, dict[str, Any]] = {
    GRAY_SOURCES: {
        "version": STATEMENT_VERSION,
        "en": ("Quick heads-up: the stock list and company profiles come from TradingView and Yahoo. That's a gray "
               "area — their terms don't actually allow automated bulk use, and you may occasionally get rate-limited "
               "or briefly blocked. So this data stays on your computer for your own research; don't share it. Annual "
               "reports downloaded from official sites (SEC, CNINFO, BSE, ...) are the same: for your own use. Company "
               "profiles and report excerpts are sent to Jev (the AI service) to read. OK? (yes / no)"),
        "zh": ("先说一声：股票清单和公司简介来自 TradingView 和 Yahoo，属于灰色用法——人家条款其实不让程序批量拿数据，偶尔可能被"
               "限流或暂时封一下。所以这些数据只留在你电脑上自己研究用，别外传。筛选时从官网（SEC、巨潮、BSE 等）下的年报也一样，"
               "自己用。另外，公司简介和年报摘录会发给 Jev（AI 服务）读一下。可以吗？（可以 / 不要）"),
    },
}


class ConsentRequired(PermissionError):
    """A gray-private source was about to be used without a recorded 'yes' for gray-sources."""

    def __init__(self, source_id: str, state: str) -> None:
        self.source_id = source_id
        self.state = state
        super().__init__(f"source {source_id!r} is gray-private and consent for gray-sources is '{state}'; ask the "
                         f"human, then record the answer: jevscreen consent set gray-sources yes|no")


def consent_path(cfg: Config) -> Path:
    return Path(cfg.home) / CONSENT_FILE


def _now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def load(cfg: Config) -> tuple[dict[str, Any], str | None]:
    """(data, problem). A missing file is ({}, None); an unreadable or malformed one is ({}, reason)."""
    path = consent_path(cfg)
    if not path.exists():
        return {}, None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        return {}, f"unreadable {path.name}: {type(e).__name__}"
    if not isinstance(data, dict):
        return {}, f"malformed {path.name}: not a JSON object"
    return data, None


def get(cfg: Config, topic: str = GRAY_SOURCES) -> dict[str, Any]:
    """{'topic', 'state': 'yes' | 'no' | 'unset' | 'unreadable', 'recorded_at', 'problem', 'statement_version'}.
    statement_version is 1 for an answer recorded before versioned statements existed."""
    data, problem = load(cfg)
    if problem:
        return {"topic": topic, "state": "unreadable", "recorded_at": None, "problem": problem,
                "statement_version": None}
    rec = (data.get("topics") or {}).get(topic)
    if not isinstance(rec, dict) or rec.get("value") not in VALUES:
        return {"topic": topic, "state": "unset", "recorded_at": None, "problem": None, "statement_version": None}
    ver = rec.get("statement_version")
    return {"topic": topic, "state": rec["value"], "recorded_at": rec.get("recorded_at"), "problem": None,
            "statement_version": ver if isinstance(ver, int) and not isinstance(ver, bool) else 1}


def record(cfg: Config, topic: str, value: str, *, via: str = "cli", lang: str = "en") -> dict[str, Any]:
    """Record the human's answer (atomic replace). Raises ValueError for an unknown topic, value or language.
    The record holds the statement version, the language and the exact text shown (`statement`), plus the English
    text (`statement_en`)."""
    if lang not in LANGS:
        raise ValueError(f"consent language must be one of {', '.join(LANGS)}, not {lang!r}")
    if topic not in TOPICS:
        raise ValueError(f"unknown consent topic {topic!r}; expected one of: {', '.join(TOPICS)}")
    value = str(value).strip().lower()
    if value not in VALUES:
        raise ValueError(f"consent value must be 'yes' or 'no', not {value!r}")
    data, problem = load(cfg)
    if problem:   # keep the unreadable file for inspection (never overwrite an earlier copy); start a fresh record
        stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        with contextlib.suppress(OSError):
            os.replace(consent_path(cfg), consent_path(cfg).with_name(f"{CONSENT_FILE}.bad-{stamp}"))
        data = {}
    try:
        interactive = bool(sys.stdin and sys.stdin.isatty())
    except (AttributeError, ValueError):
        interactive = False
    st = STATEMENTS.get(topic)
    entry = {"value": value, "recorded_at": _now_iso(), "via": via, "interactive": interactive,
             "statement": st[lang] if st else TOPICS[topic], "statement_en": st["en"] if st else TOPICS[topic],
             "statement_version": st["version"] if st else 1, "lang": lang}
    if topic == GRAY_SOURCES:        # who receives the text "sent to Jev" at the time of the answer (active provider)
        from . import doctor
        entry.update(recipient_note=doctor.recipient_note(cfg, lang), recipient_note_en=doctor.recipient_note(cfg))
    data["version"] = 1
    data.setdefault("topics", {})[topic] = entry
    data.setdefault("history", []).append({"topic": topic, **{k: entry[k] for k in ("value", "recorded_at", "via",
                                                                                   "interactive", "lang",
                                                                                   "statement_version")}})
    home = Path(cfg.home)
    home.mkdir(parents=True, exist_ok=True)
    path = consent_path(cfg)
    tmp = path.with_suffix(f".{os.getpid()}.tmp")
    try:
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise
    return {"topic": topic, **entry, "path": str(path)}


def gray_sources_allowed(cfg: Config) -> bool:
    return get(cfg, GRAY_SOURCES)["state"] == "yes"


def needs_consent(source_id: str) -> bool:
    """True for gray-private sources and for source ids provenance does not know (fail closed)."""
    src = provenance.SOURCES.get(source_id)
    return src is None or src.tier == provenance.LicenseTier.GRAY_PRIVATE


def require_gray_sources(cfg: Config, source_id: str) -> None:
    """Call before a gray-private adapter sends its first request. Raises ConsentRequired unless allowed."""
    if not needs_consent(source_id):
        return
    state = get(cfg, GRAY_SOURCES)["state"]
    if state != "yes":
        raise ConsentRequired(source_id, state)
