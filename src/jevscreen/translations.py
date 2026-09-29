"""Content translation by the user's OWN AI agent (no extra model provider, no paid call).

A result page is one language (jevscreen.l10n). What the data holds in another language (an English profile on a
Chinese page, a Chinese annual-report excerpt on an English page, a company without an official Chinese name) is
exported for the agent that runs jev-screen, translated by it, and imported back:

    jevscreen page RUN --export-strings FILE --lang zh|en --json   -> FILE: a JSON list (<= BATCH_MAX items)
    (the agent fills each item's "translation")
    jevscreen page RUN --import-translations FILE --lang zh|en --json -> stored, the page rebuilt

Item: {id, kind: name|description|excerpt|reason, text, source_lang, target_lang, sha, context, translation: null}.
The agent may set "keep_original": true on a text that has no translation (a product code, a brand); a company name
may stay in Latin letters. Such a text is stored as itself: it is no longer pending, the page shows the original.
Translations live in the store table `translations`, keyed by (sha, target_lang), provenance 'agent translation':
reused by every later run and idea whose page shows the same text. The original text is never changed (it stays
the fact); the page shows the translation first with an 'AI 翻译 / AI translation' tag and a 'show original' toggle,
and an item without a translation shows the original marked 'not translated'.
"""
from __future__ import annotations

import datetime as dt
import json
import re
from pathlib import Path
from typing import Any, Iterator

from . import l10n

FORMAT = "jevscreen.translations/1"
PROVENANCE = "agent translation"
KINDS = ("name", "description", "excerpt", "reason")
BATCH_MAX = 60
TOP = 10                 # the first rows go first in a batch (the ones the human reads)
CARDS = 9                # the priority of the card texts: not on the page (a CLI tool for the user's AI), so never
                         # exported nor pending; a translation already stored still fills them (the `cards` printout)
MAX_CHARS = 4000

INSTRUCTIONS_EN = ("Translate each item's \"text\" into {target} and write it into its \"translation\" field; keep "
                   "every other field as it is. Translate faithfully: no summary, no added facts, keep numbers, "
                   "units, product names and tickers. kind name: the company's usual {target} name, short (no legal "
                   "suffix such as Co., Ltd. / 股份有限公司); kind excerpt: a verbatim filing quote, translate every "
                   "sentence. A name with no usual {target} name may stay as it is (write it unchanged or its "
                   "short Latin form); for any other text that has no translation set \"keep_original\": true. "
                   "Leave \"translation\" null only when you are unsure (it comes back in the next export). Then "
                   "run the import command.")
TARGET_WORDS = {"zh": "Simplified Chinese", "en": "English"}


def _slots(data: dict[str, Any]) -> Iterator[tuple[int, str, dict[str, Any], str, dict[str, Any]]]:
    """(priority, kind, holder, field, context) of every text on the page that may need a translation, in the order
    a batch takes them: the top rows, the other rows, the names of the lists, then the cards (priority CARDS)."""
    lang = data.get("lang") or "zh"
    rows = data.get("rows") or []

    def row_slots(r: dict[str, Any], prio: int):
        ctx = {"ticker": r.get("ticker"), "country": r.get("country")}
        if not r.get("name_zh"):
            yield prio, "name", r, "name", ctx
        yield prio, "description", r, "one_line", ctx
        if r.get("one_line_profile"):           # the profile's line beside the annual report's (also the cards')
            yield prio + 1, "description", r, "one_line_profile", ctx
        if isinstance(r.get("quote"), dict):
            yield prio, "excerpt", r["quote"], "text", ctx
    for r in rows[:TOP]:
        yield from row_slots(r, 0)
    for r in rows[TOP:]:
        yield from row_slots(r, 2)
    for part in ("unverified", "excluded"):
        for u in data.get(part) or []:
            if not u.get("name_zh"):
                yield 3, "name", u, "name", {"ticker": u.get("ticker"), "country": u.get("country")}
    for c in data.get("cards") or []:
        ctx = {"ticker": c.get("ticker")}
        if not c.get("name_zh"):
            yield CARDS, "name", c, "name", ctx
        yield CARDS, "description", c, "what", ctx
        if lang == "en" and not c.get("why_en"):
            yield CARDS, "reason", c, "why_zh", ctx
        if isinstance(c.get("quote"), dict):
            yield CARDS, "excerpt", c["quote"], "text", ctx


def _needs(kind: str, holder: dict[str, Any], field: str, lang: str) -> str | None:
    """The text of a slot when it needs a translation into `lang`, else None. A name only on a Chinese page (an
    English page always shows the English name)."""
    text = holder.get(field)
    if not isinstance(text, str) or not text.strip():
        return None
    if kind == "name" and lang == "en":
        return None
    return text if l10n.is_foreign(text, lang) else None


def needed(data: dict[str, Any]) -> list[str]:
    """The shas of every text of the page that needs a translation (for the store lookup)."""
    lang = data.get("lang") or "zh"
    out: list[str] = []
    for _p, kind, holder, field, _ctx in _slots(data):
        t = _needs(kind, holder, field, lang)
        if t is not None:
            s = l10n.text_sha(t)
            if s not in out:
                out.append(s)
    return out


def apply(data: dict[str, Any], tr: dict[str, str] | None) -> dict[str, Any]:
    """Mark every foreign text of the page (<field>_x: true) and attach its translation (<field>_tr) from `tr`
    (sha -> text); data['translation'] = {lang, foreign, translated, pending}. Returns data (changed in place)."""
    lang = data.get("lang") or "zh"
    tr = tr or {}
    seen: dict[str, str] = {}
    for prio, kind, holder, field, _ctx in _slots(data):
        t = _needs(kind, holder, field, lang)
        if t is None:
            continue
        s = l10n.text_sha(t)
        got = tr.get(s)
        kept = bool(got) and got == l10n.norm_text(t)     # the agent kept the original (no translation exists)
        holder[f"{field}_x"] = True
        holder[f"{field}_tr"] = None if kept else got
        if kept:
            holder[f"{field}_kept"] = True
        else:
            holder.pop(f"{field}_kept", None)
        if prio != CARDS:                                 # only what the page shows counts
            seen[s] = "kept" if kept else ("translated" if got else "pending")
    vals = list(seen.values())
    data["translation"] = {"lang": lang, "foreign": len(seen), "translated": vals.count("translated"),
                           "kept": vals.count("kept"), "pending": vals.count("pending")}
    return data


def pending_items(data: dict[str, Any]) -> list[dict[str, Any]]:
    """Every text of the page still without a translation, as export items, most important first (one item per
    distinct text)."""
    lang = data.get("lang") or "zh"
    items: dict[str, dict[str, Any]] = {}
    order: list[tuple[int, int, str]] = []
    for i, (prio, kind, holder, field, ctx) in enumerate(_slots(data)):
        t = _needs(kind, holder, field, lang)
        if prio == CARDS or t is None or holder.get(f"{field}_tr") or holder.get(f"{field}_kept"):
            continue
        s = l10n.text_sha(t)
        if s in items:
            continue
        items[s] = {"id": f"{kind[0]}-{s[:12]}", "kind": kind, "text": l10n.norm_text(t),
                    "source_lang": l10n.text_lang(t), "target_lang": lang, "sha": s,
                    "context": " · ".join(str(v) for v in ctx.values() if v) or None, "translation": None}
        order.append((prio, i, s))
    return [items[s] for _p, _i, s in sorted(order)]


def carried(data: dict[str, Any]) -> dict[str, str]:
    """sha -> translation (or the kept original) of every text an earlier page showed: what a page built while the
    store is busy takes over, so it never falls back to untranslated texts."""
    lang = data.get("lang") or "zh"
    out: dict[str, str] = {}
    for _p, kind, holder, field, _ctx in _slots(data):
        t = _needs(kind, holder, field, lang)
        if t is None:
            continue
        if holder.get(f"{field}_tr"):
            out[l10n.text_sha(t)] = str(holder[f"{field}_tr"])
        elif holder.get(f"{field}_kept"):
            out[l10n.text_sha(t)] = l10n.norm_text(t)
    return out


# ---------------------------------------------------------------------------------------------- store

def lookup(con, shas: list[str], lang: str) -> dict[str, str]:
    """sha -> translation into `lang` for the given shas ({} when the table is missing: an old read-only store)."""
    if not shas:
        return {}
    try:
        return {s: t for s, t in con.execute(
            "SELECT sha, text FROM translations WHERE target_lang = ? AND list_contains(?::VARCHAR[], sha)",
            [lang, sorted(set(shas))]).fetchall() if t}
    except Exception:  # noqa: BLE001 - no table yet (a store no writer opened since the upgrade)
        return {}


def count(con) -> dict[str, int]:
    try:
        return dict(con.execute("SELECT target_lang, count(*) FROM translations GROUP BY 1").fetchall())
    except Exception:  # noqa: BLE001
        return {}


# ---------------------------------------------------------------------------------------------- import

def read_file(path: str | Path) -> list[dict[str, Any]]:
    """The items of an import file: a JSON list, or {"items": [...]}. ValueError with a plain message otherwise."""
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except OSError as e:
        raise ValueError(f"cannot read {path} ({type(e).__name__})") from None
    except ValueError as e:
        raise ValueError(f"{path} is not valid JSON ({str(e)[:120]})") from None
    if isinstance(raw, dict) and isinstance(raw.get("items"), list):
        raw = raw["items"]
    if not isinstance(raw, list):
        raise ValueError(f"{path} must hold a JSON list of items (as --export-strings wrote it)")
    return [x for x in raw if isinstance(x, dict)]


def check_item(item: dict[str, Any], default_lang: str | None = None) -> tuple[dict[str, Any] | None, str | None]:
    """(row to store, None) | (None, problem) | (None, None) for an item left untranslated (skipped).

    A text that has no translation is resolved with "keep_original": true (stored as the text itself, so it is no
    longer pending and the page shows the original). A company name may stay in Latin letters (a brand without a
    usual Chinese name: "Zscaler", or the name as it is)."""
    t = item.get("translation")
    keep = item.get("keep_original") is True
    if not keep and (t is None or (isinstance(t, str) and not t.strip())):
        return None, None
    if not keep and not isinstance(t, str):
        return None, "translation is not a string"
    lang = item.get("target_lang") or default_lang
    if lang not in l10n.LANGS:
        return None, "target_lang must be zh or en"
    text = item.get("text")
    sha = str(item.get("sha") or "").strip().lower()
    if isinstance(text, str) and text.strip():
        if sha and l10n.text_sha(text) != sha:
            return None, "text does not match sha (keep text and sha as exported)"
        sha = sha or l10n.text_sha(text)
    if not re.fullmatch(r"[0-9a-f]{64}", sha):
        return None, "missing sha"
    kind = item.get("kind") if item.get("kind") in KINDS else "excerpt"
    src = item.get("source_lang") if isinstance(item.get("source_lang"), str) else l10n.text_lang(text)
    if keep:
        if not isinstance(text, str) or not text.strip():
            return None, "keep_original needs the exported text"
        return {"sha": sha, "target_lang": lang, "kind": kind, "source_lang": src, "text": l10n.norm_text(text),
                "kept": True}, None
    t = l10n.norm_text(t)
    same = isinstance(text, str) and l10n.norm_text(text) == t
    if kind == "name" and same:          # the name kept as it is
        return {"sha": sha, "target_lang": lang, "kind": kind, "source_lang": src, "text": t, "kept": True}, None
    if any(ord(c) < 32 for c in t) or "<" in t and re.search(r"</?[a-zA-Z][^>]*>", t):
        return None, "control characters or markup in the translation"
    limit = min(MAX_CHARS, max(400, 3 * len(str(text or "")) + 80))
    if len(t) > limit:
        return None, f"translation too long ({len(t)} > {limit} characters)"
    if same:
        return None, ("translation is the same as the text (set \"keep_original\": true when it has no "
                      "translation)")
    latin_name = kind == "name" and l10n.text_lang(t) in ("en", None)     # a brand name in Latin letters
    if l10n.text_lang(t) != lang and not latin_name:
        return None, f"translation is not in {TARGET_WORDS[lang]}"
    return {"sha": sha, "target_lang": lang, "kind": kind, "source_lang": src, "text": t}, None


def import_items(cfg, con, items: list[dict[str, Any]], *, default_lang: str | None = None) -> dict[str, Any]:
    """Validate and store the translated items (a re-import replaces the earlier translation of that text, never
    the original). Returns {imported, skipped, rejected: [{id, problem}], by_lang}."""
    from . import ops, store
    rows: dict[tuple[str, str], dict[str, Any]] = {}
    rejected: list[dict[str, Any]] = []
    skipped = 0
    for it in items:
        row, problem = check_item(it, default_lang)
        if problem:
            rejected.append({"id": it.get("id"), "problem": problem})
            continue
        if row is None:
            skipped += 1
            continue
        if ops.scan_secrets(cfg, row["text"]):
            rejected.append({"id": it.get("id"), "problem": "the translation contains a configured secret"})
            continue
        rows[(row["sha"], row["target_lang"])] = row
    at = store.now_utc()
    if rows:
        store.upsert_many(con, "translations", COLUMNS,
                          [[r["sha"], r["target_lang"], r["kind"], r["source_lang"], r["text"], PROVENANCE, at]
                           for r in rows.values()])
    by_lang: dict[str, int] = {}
    for (_s, lang) in rows:
        by_lang[lang] = by_lang.get(lang, 0) + 1
    # the one target language of the file (what the page is rebuilt in when the import names none)
    targets = {it.get("target_lang") for it in items if it.get("target_lang") in l10n.LANGS}
    return {"imported": len(rows), "skipped": skipped, "rejected": rejected, "by_lang": by_lang,
            "lang": targets.pop() if len(targets) == 1 else default_lang}


COLUMNS = ("sha", "target_lang", "kind", "source_lang", "text", "provenance", "created_at")


def export_path(cfg, run_id: str, lang: str) -> Path:
    """The default file of an export: <home>/translate/<run_id>-<lang>.json."""
    return Path(cfg.home) / "translate" / f"{run_id}-{lang}.json"


def write_export(path: str | Path, items: list[dict[str, Any]]) -> Path:
    p = Path(path).expanduser()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(items, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(p)
    return p


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
