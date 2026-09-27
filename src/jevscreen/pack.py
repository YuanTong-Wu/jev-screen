"""Open data pack: build a redistributable subset of the local store, publish it, pull it back into a store.

What may go in (see docs/OPEN_PACK.md): only sources listed in PACK_SOURCES. A source is listed only when
(a) provenance.SOURCES registers it, (b) its tier is not gray-private, and (c) it is official-open, or an explicit
allowlist entry names the exact redistribution grant (EDINET content: Public Data License 1.0 with attribution).
Everything else (TradingView, FinanceDatabase/Yahoo, CNINFO, DART, SEC 10-K/20-F verbatim text, Jev labels) never
enters a pack; build() re-checks this for every file it writes (assert_redistributable).

Pack format (FORMAT, FORMAT_VERSION): a directory / GitHub Release with
- manifest.json: format, format_version, created_at, tag, sources {source_id: licence, licence_url, attribution,
  tier, allow_reason, fetched_at}, files [{name, source_id, table, rows, bytes, sha256}], excluded {source_id: why}.
- one gzip'd JSON-lines file per table (deterministic gzip: mtime 0, so identical content gives identical bytes):
  sec_tickers.jsonl.gz (SEC company_tickers_exchange: cik, ticker, name, exchange),
  edinet_codes.jsonl.gz (EDINET code list: edinet_code, sec_code, listed, name, name_en, filer_type, jcn; only
  filers with a 証券コード, never an individual (個人) filer),
  edinet_business.jsonl.gz (有価証券報告書 事業の内容 only, newest valid filing per EDINET code, no filer the code
  list marks delisted; the locally appended 経営方針… block is cut off before export).
- ATTRIBUTION.txt (also listed in the manifest with its sha256).

pull(): GitHub Releases public API (no token), newest release whose tag starts with 'pack-' (or an exact tag),
manifest first, every file verified (size + sha256, and the asset digest GitHub reports when present) before
anything touches the store; files are kept verbatim under <home>/raw/open_pack/<tag>/. Import is idempotent:
- one transaction; one snapshot per file, source_id = the file's original source, kind 'pack:<table>',
  note 'pack-imported ...';
- documents rows of the pack use extractor PACK_EXTRACTOR, so the user's own sync-edinet (which compares the
  extractor) re-extracts them with the richer local extractor; a document the user's own sync wrote is never
  overwritten; a pack document is rewritten only when its text or its resolved line changed;
- documents.cik = EDINET code, so layer 2 finds the text through the 'edinet_code' identifier of any line;
- identifiers (sec_cik, edinet_code) are only added for lines that have none (never overwrite a local mapping);
- descriptions are only written where none exists or the existing one came from a pack.
A manifest whose sha256 was already imported is skipped without downloading the data files (force=True re-imports);
such a pull still runs relink(): a local step that links pack rows to lines added since (universe built later).
HTTP 403/429 or a challenge stops the pull at once (no retry).

fetch_open(): the CI helper for the daily pack job: SEC ticker list (needs the SEC User-Agent; skipped cleanly
without it) and the key-free EDINET code list; with seed_lines=True, and only in a store without any TradingView
data, it adds one active primary line per listed EDINET filer ('TSE:<code>') so sync-edinet has lines to map.
"""
from __future__ import annotations

import datetime as dt
import gzip
import hashlib
import io
import json
import os
import re
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Mapping, Sequence
from urllib.parse import quote, urlparse

from . import provenance, store
from .config import Config
from .provenance import LicenseTier

FORMAT = "jevscreen-open-pack"
FORMAT_VERSION = 1
MANIFEST = "manifest.json"
ATTRIBUTION = "ATTRIBUTION.txt"
PACK_EXTRACTOR = "open-pack-v1"
SNAPSHOT_KIND_PREFIX = "pack:"
TAG_PREFIX = "pack-"
# Owner's GitHub repository that publishes the daily pack ('owner/name'). Env JEVSCREEN_PACK_REPO or --repo win.
DEFAULT_PACK_REPO = "YuanTong-Wu/jev-screen"
GITHUB_API = "https://api.github.com"
MAX_REDIRECTS = 5
MAX_MANIFEST_BYTES = 1 * 1024 * 1024
MAX_FILE_BYTES = 1024 * 1024 * 1024
MAX_PACK_BYTES = 2 * 1024 * 1024 * 1024
MAX_TEXT_CHARS = 300_000
MIN_BUSINESS_CHARS = 80     # edinet.MIN_SECTION_CHARS: the same floor the adapter applies to 事業の内容
MIN_INTERVAL_S = 1.0
# Public EDINET document viewer (no key). The pack never ships the key-gated API URL that sync-edinet records.
EDINET_VIEWER_URL = "https://disclosure2.edinet-fsa.go.jp/WZEK0040.aspx?{doc_id},,"
EDINET_URL_PREFIXES = ("https://api.edinet-fsa.go.jp/", "https://disclosure2.edinet-fsa.go.jp/",
                       "https://disclosure2dl.edinet-fsa.go.jp/")
WRITE_WAIT_S = 600.0
COMMAND_PULL = "pack pull"
COMMAND_FETCH = "pack fetch-open"
COOLDOWN_HOURS = 24

_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$")
_TAG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_SHA_RE = re.compile(r"^[0-9a-f]{64}$")
_EDINET_CODE_RE = re.compile(r"^[A-Z][0-9]{5}$")   # E: filers, G: funds
_EDINET_DOC_RE = re.compile(r"^S[0-9A-Z]{7}$")
_SEC_CODE_RE = re.compile(r"^[0-9A-Z]{5}$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


class PackError(RuntimeError):
    """The pack is malformed, fails verification, or the release cannot be found. Nothing was imported."""


class PackBlocked(RuntimeError):
    """GitHub answered 403/429 or a challenge: the pull stopped at once (no retry)."""

    def __init__(self, url: str, status: Any, reason: str):
        super().__init__(f"blocked: {reason} (HTTP {status}) at {url}")
        self.url, self.status, self.reason = url, status, reason


# ------------------------------------------------------------------------------------------------ what may ship


@dataclass(frozen=True)
class PackSource:
    source_id: str
    tables: tuple[str, ...]
    licence: str
    licence_url: str
    attribution: str
    allow_reason: str       # why this source may be redistributed (quote the grant; explicit for non-open tiers)
    caveat: str = ""        # open licence questions, shipped in ATTRIBUTION.txt / release notes and the manifest


PACK_SOURCES: dict[str, PackSource] = {
    "sec_tickers": PackSource(
        "sec_tickers", ("sec_tickers",),
        "Public information compiled by the U.S. SEC; may be copied or further distributed without permission",
        "https://www.sec.gov/about/privacy-information#dissemination",
        "Source: U.S. Securities and Exchange Commission, EDGAR company_tickers_exchange.json "
        "(https://www.sec.gov/files/company_tickers_exchange.json). Redistributed unchanged in content "
        "(re-encoded as JSON lines). The SEC does not endorse jev-screen.",
        "official-open tier: SEC-compiled identifier list (CIK, ticker, name, exchange)."),
    "edinet_yuho": PackSource(
        "edinet_yuho", ("edinet_codes", "edinet_business"),
        "Public Data License 1.0 (公共データ利用規約 第1.0版), compatible with CC BY 4.0",
        "https://www.digital.go.jp/resources/open_data/public_data_license_v1.0",
        "出典：EDINET（金融庁 電子開示システム, https://disclosure2.edinet-fsa.go.jp/）の EDINETコードリスト及び"
        "有価証券報告書「事業の内容」を jev-screen が加工して作成（XBRL→CSV の該当テキストブロックを抽出し、"
        "空白・改行を整形）。\n"
        "Source: EDINET (Financial Services Agency of Japan, https://disclosure2.edinet-fsa.go.jp/): EDINET code "
        "list and the 'Description of Business' (事業の内容) section of annual securities reports, extracted and "
        "whitespace-normalised by jev-screen. Edited by jev-screen; the FSA does not endorse jev-screen.",
        "explicit allowlist: EDINET site content is offered under PDL 1.0 (attribution + edit notice). Only the "
        "code list and 事業の内容 ship; the 経営方針 block and every other section stay local.",
        "注意：「事業の内容」の文章は各提出会社が作成したものです。公共データ利用規約 第1.0版は第三者が権利を有する"
        "コンテンツには適用されず、提出会社の文章がこの許諾の対象になるかは未確認です。転載・再配布の前にご自身で"
        "確認してください。\n"
        "Caveat: the 事業の内容 text is written by each filing company. PDL 1.0 does not apply to content in which a "
        "third party holds rights, and whether issuer-written text falls under the grant is not confirmed. Check "
        "before republishing or redistributing the text."),
}

# Registered sources that never ship, with the reason written into every manifest.
EXCLUDED_REASONS: dict[str, str] = {
    "tradingview_scanner": "gray-private (TradingView terms): personal use only",
    "tradingview_profile": "gray-private (TradingView terms): personal use only",
    "financedatabase_local": "gray-private (Yahoo-originated summaries): personal use only",
    "cninfo_annual_report": "official-private (exchange rights over disclosed documents): personal use only",
    "dart_business_report": "official-private (no explicit redistribution grant): personal use only",
    "mops_annual_report": "official-private (TWSE-served issuer reports, no redistribution grant): personal use only",
    "mops_basic": "official-private (MOPS basic data, no open-data licence for this field): personal use only",
    "bse_annual_report": "official-private (exchange-hosted issuer filings, no redistribution grant): personal use only",
    "sec_filing_text": "issuer-authored 10-K/20-F text (issuer copyright): kept local, never shipped",
}

# table -> (file name, source_id)
TABLES: dict[str, tuple[str, str]] = {
    "sec_tickers": ("sec_tickers.jsonl.gz", "sec_tickers"),
    "edinet_codes": ("edinet_codes.jsonl.gz", "edinet_yuho"),
    "edinet_business": ("edinet_business.jsonl.gz", "edinet_yuho"),
}


def assert_redistributable(source_id: str) -> PackSource:
    """The PackSource for `source_id`, or PackError. Enforces: allowlisted, registered in provenance, never
    gray-private, and a non-open tier only with an explicit allowlist reason."""
    ps = PACK_SOURCES.get(source_id)
    if ps is None:
        raise PackError(f"source {source_id!r} is not allowlisted for the open pack")
    src = provenance.SOURCES.get(source_id)
    if src is None:
        raise PackError(f"source {source_id!r} is not registered in provenance.SOURCES")
    if src.tier == LicenseTier.GRAY_PRIVATE:
        raise PackError(f"source {source_id!r} is gray-private and can never ship")
    if src.tier != LicenseTier.OFFICIAL_OPEN and not ps.allow_reason.startswith("explicit allowlist"):
        raise PackError(f"source {source_id!r} is {src.tier.value} without an explicit allowlist grant")
    return ps


# ------------------------------------------------------------------------------------------------ helpers


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None, microsecond=0)


def _iso(v: Any) -> Any:
    if isinstance(v, (dt.date, dt.datetime)):
        return v.isoformat()
    return v


def encode_jsonl_gz(rows: Iterable[Mapping[str, Any]]) -> tuple[bytes, int]:
    """Deterministic gzip'd JSON lines (sorted keys, mtime 0): same rows -> same bytes -> same sha256."""
    buf = io.BytesIO()
    n = 0
    with gzip.GzipFile(filename="", mode="wb", fileobj=buf, mtime=0, compresslevel=9) as gz:
        for r in rows:
            gz.write(json.dumps({k: _iso(v) for k, v in r.items()}, ensure_ascii=False, sort_keys=True,
                                separators=(",", ":")).encode("utf-8") + b"\n")
            n += 1
    return buf.getvalue(), n


def decode_jsonl_gz(data: bytes, *, max_rows: int = 5_000_000) -> Iterator[dict]:
    with gzip.GzipFile(fileobj=io.BytesIO(data), mode="rb") as gz:
        for i, line in enumerate(gz):
            if i >= max_rows:
                raise PackError("too many rows")
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            if not isinstance(row, dict):
                raise PackError("row is not an object")
            yield row


def split_business_block(text: str) -> str:
    """事業の内容 only: cut the locally appended 経営方針… block (edinet.POLICY_HEADING line) and what follows."""
    from .sources.edinet import POLICY_HEADING
    i = text.find(POLICY_HEADING)
    if i >= 0:
        text = text[:i]
    return text.strip()


def _sec_code_to_symbol(sec_code: str) -> str:
    """EDINET 5-char 証券コード -> TSE local code: '72030' -> '7203'; a code not ending in 0 stays as is."""
    return sec_code[:4] if sec_code.endswith("0") else sec_code


# ------------------------------------------------------------------------------------------------ build


def _newest_raw(con, source_id: str, kinds: Sequence[str]) -> tuple[str, dict] | None:
    """(raw_path, meta) of the newest ok snapshot of `kinds` whose raw file still exists."""
    ph = ",".join("?" for _ in kinds)
    rows = con.execute(
        f"SELECT raw_path, raw_sha256, fetched_at, kind, snapshot_id FROM snapshots WHERE source_id = ? "
        f"AND kind IN ({ph}) AND raw_path IS NOT NULL AND coalesce(status, 'ok') = 'ok' ORDER BY fetched_at DESC",
        [source_id, *kinds]).fetchall()
    for path, digest, at, kind, snap in rows:
        if Path(path).exists():
            return path, {"raw_sha256": digest, "fetched_at": at, "kind": kind, "snapshot_id": snap}
    return None


def _read_ticker_rows(path: str) -> list[dict]:
    from .sources.sec_edgar import parse_tickers
    data = Path(path).read_bytes()
    if path.endswith(".jsonl.gz"):
        return parse_tickers(list(decode_jsonl_gz(data)))
    return parse_tickers(data)


def _read_codelist_rows(path: str) -> list[dict]:
    from .sources.edinet import parse_codelist
    data = Path(path).read_bytes()
    if path.endswith(".jsonl.gz"):
        return [c for c in map(_clean_codelist_row, decode_jsonl_gz(data)) if c is not None]
    return parse_codelist(data)


def _is_individual(r: Mapping[str, Any]) -> bool:
    """EDINET filer types 個人(組合発行者を除く) / 個人(非居住者)(…): private persons (mostly large-shareholding filers)."""
    return "個人" in str(r.get("filer_type") or "")


def publishable_codelist_rows(rows: Iterable[Mapping[str, Any]]) -> list[dict]:
    """Code-list rows that may ship or be imported: never an individual filer (personal information under APPI,
    and nothing maps to one), and only filers with a 証券コード (the only rows a line can map to)."""
    return [dict(r) for r in rows if r.get("sec_code") and not _is_individual(r)]


def _collect(con) -> dict[str, Any]:
    """Everything build() needs from the store, in one short read-only session (files are read afterwards)."""
    out: dict[str, Any] = {}
    out["tickers"] = _newest_raw(con, "sec_tickers", ("company_tickers_exchange", SNAPSHOT_KIND_PREFIX + "sec_tickers"))
    out["codelist"] = _newest_raw(con, "edinet_yuho", ("edinet_codelist", SNAPSHOT_KIND_PREFIX + "edinet_codes"))
    cols = ["doc_id", "accession", "form", "filing_date", "report_date", "url", "text_path", "text_sha256",
            "fetched_at", "extractor"]
    out["edinet_docs"] = [dict(zip(cols, r)) for r in con.execute(
        f"SELECT {', '.join(cols)} FROM documents WHERE source_id = 'edinet_yuho' AND text_path IS NOT NULL "
        "ORDER BY doc_id").fetchall()]
    return out


def _edinet_business_rows(docs: Sequence[Mapping[str, Any]], codes: Mapping[str, Mapping[str, Any]],
                          stats: dict) -> list[dict]:
    """Newest valid 事業の内容 per EDINET code: candidates are checked newest first (the text file must exist,
    match its recorded sha256 and be long enough), so a broken newest filing falls back to an older valid one.
    A filer the code list marks as delisted (listed = False) does not ship."""
    by_code: dict[str, list[dict]] = {}
    for d in docs:
        parts = str(d["doc_id"]).split(":")
        if len(parts) != 4 or parts[0] != "edinet_yuho" or not _EDINET_CODE_RE.match(parts[1]) \
                or not _EDINET_DOC_RE.match(parts[2]):
            stats["skipped_bad_id"] += 1
            continue
        code, doc = parts[1], parts[2]
        by_code.setdefault(code, []).append({**d, "_doc": doc, "_key": (str(_iso(d.get("filing_date")) or ""), doc)})
    rows = []
    for code in sorted(by_code):
        entry = codes.get(code) or {}
        if entry.get("listed") is False:
            stats["skipped_delisted"] += 1
            continue
        for d in sorted(by_code[code], key=lambda c: c["_key"], reverse=True):
            try:
                raw = Path(d["text_path"]).read_bytes()
            except (OSError, TypeError):
                stats["skipped_missing_file"] += 1
                continue
            if d.get("text_sha256") and _sha256(raw) != d["text_sha256"]:
                stats["skipped_sha_mismatch"] += 1
                continue
            text = split_business_block(raw.decode("utf-8", errors="replace"))
            if len(text) < MIN_BUSINESS_CHARS:
                stats["skipped_too_short"] += 1
                continue
            text = text[:MAX_TEXT_CHARS]
            rows.append({"edinet_code": code, "sec_code": entry.get("sec_code"), "filer_name": entry.get("name"),
                         "doc_id": d["_doc"], "form": d.get("form"), "filing_date": _iso(d.get("filing_date")),
                         "period_end": _iso(d.get("report_date")),
                         "source_url": EDINET_VIEWER_URL.format(doc_id=d["_doc"]), "lang": "ja",
                         "text": text, "text_sha256": _sha256(text.encode("utf-8")), "text_chars": len(text)})
            break
    return rows


def _attribution_text(manifest: Mapping[str, Any]) -> str:
    lines = [f"jev-screen open data pack {manifest.get('tag') or ''}".strip(),
             f"created_at: {manifest['created_at']}", "",
             "This pack contains only data whose licence allows redistribution. Attribution per source:", ""]
    for sid, s in manifest["sources"].items():
        lines += [f"[{sid}] {s['licence']}", f"  licence: {s['licence_url']}", f"  {s['attribution']}"]
        if s.get("caveat"):
            lines += [f"  {s['caveat']}"]
        lines += [""]
    lines += ["Not included (personal use only, fetched on each user's own machine):"]
    lines += [f"  - {sid}: {why}" for sid, why in manifest["excluded"].items()]
    return "\n".join(lines) + "\n"


def _scan_for_secrets(files: Mapping[str, bytes], secrets: Sequence[str | None]) -> None:
    """Abort the build if any secret (e.g. the SEC User-Agent, API keys) appears in any pack file."""
    needles = [s.encode("utf-8") for s in secrets if s and len(s) >= 6]
    for name, data in files.items():
        plain = gzip.decompress(data) if name.endswith(".gz") else data
        for n in needles:
            if n in plain:
                raise PackError(f"refusing to write the pack: a configured secret appears in {name}")


def default_secrets(cfg: Config) -> list[str | None]:
    """Secrets the build scans its output for (read at call time, never printed): the SEC User-Agent and any
    e-mail address inside it, and the API keys."""
    ua = cfg.sec_user_agent()
    emails = [t.strip("<>()[],;") for t in (ua or "").split() if "@" in t]
    return [ua, *emails, cfg.edinet_api_key(), cfg.opendart_api_key(), cfg.openrouter_key()]


def build(cfg: Config, out_dir: Path | str, *, tag: str | None = None, now: dt.datetime | None = None,
          secrets: Sequence[str | None] | None = None, wait_s: float = 120.0) -> dict:
    """Write the pack for the local store into out_dir (created). Returns the manifest plus a 'summary'."""
    now = now or _utcnow()
    tag = tag or f"{TAG_PREFIX}{now.date().isoformat()}"
    if not _TAG_RE.match(tag):
        raise PackError(f"bad tag {tag!r}")
    with store.session(cfg, read_only=True, wait_s=wait_s) as con:
        got = _collect(con)
    stats = {"skipped_bad_id": 0, "skipped_missing_file": 0, "skipped_sha_mismatch": 0, "skipped_too_short": 0,
             "skipped_delisted": 0, "codelist_rows_dropped": 0}
    tables: dict[str, list[dict]] = {}
    fetched: dict[str, Any] = {}
    if got["tickers"]:
        path, meta = got["tickers"]
        tables["sec_tickers"] = sorted(_read_ticker_rows(path), key=lambda r: (r["cik"], r["ticker"]))
        fetched["sec_tickers"] = _iso(meta["fetched_at"])
    codes: dict[str, dict] = {}
    if got["codelist"]:
        path, meta = got["codelist"]
        all_rows = _read_codelist_rows(path)
        code_rows = sorted(publishable_codelist_rows(all_rows), key=lambda r: r["edinet_code"])
        stats["codelist_rows_dropped"] = len(all_rows) - len(code_rows)
        tables["edinet_codes"] = code_rows
        codes = {r["edinet_code"]: r for r in all_rows if not _is_individual(r)}
        fetched["edinet_yuho"] = _iso(meta["fetched_at"])
    business = _edinet_business_rows(got["edinet_docs"], codes, stats)
    if business:
        tables["edinet_business"] = business
        docs_at = max((_iso(d.get("fetched_at")) or "" for d in got["edinet_docs"]), default="")
        fetched["edinet_yuho"] = max(fetched.get("edinet_yuho") or "", docs_at) or None

    payloads: dict[str, bytes] = {}
    files: list[dict] = []
    sources: dict[str, dict] = {}
    for table, rows in tables.items():
        name, source_id = TABLES[table]
        ps = assert_redistributable(source_id)
        data, n = encode_jsonl_gz(rows)
        payloads[name] = data
        files.append({"name": name, "table": table, "source_id": source_id, "rows": n, "bytes": len(data),
                      "sha256": _sha256(data)})
        sources[source_id] = {"name": provenance.SOURCES[source_id].name,
                              "tier": provenance.SOURCES[source_id].tier.value, "licence": ps.licence,
                              "licence_url": ps.licence_url, "attribution": ps.attribution,
                              "allow_reason": ps.allow_reason, "fetched_at": fetched.get(source_id),
                              **({"caveat": ps.caveat} if ps.caveat else {})}
    manifest: dict[str, Any] = {
        "format": FORMAT, "format_version": FORMAT_VERSION, "created_at": now.isoformat() + "Z", "tag": tag,
        "generator": "jevscreen.pack", "sources": sources, "files": files,
        "excluded": {sid: EXCLUDED_REASONS.get(sid, "not allowlisted for the open pack")
                     for sid in provenance.SOURCES if sid not in PACK_SOURCES},
    }
    attribution = _attribution_text(manifest).encode("utf-8")
    payloads[ATTRIBUTION] = attribution
    files.append({"name": ATTRIBUTION, "table": None, "source_id": None, "rows": None, "bytes": len(attribution),
                  "sha256": _sha256(attribution)})
    mbytes = (json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    _scan_for_secrets({**payloads, MANIFEST: mbytes}, default_secrets(cfg) if secrets is None else secrets)

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / MANIFEST).unlink(missing_ok=True)   # an interrupted rebuild leaves no manifest, never a stale one
    for name, _source in TABLES.values():     # a rebuild into the same directory must not leave stale tables
        if name not in payloads:
            (out / name).unlink(missing_ok=True)
    for name, data in payloads.items():
        _atomic_write(out / name, data)
    _atomic_write(out / MANIFEST, mbytes)
    summary = {"out_dir": str(out), "tag": tag, "files": len(files) + 1,
               "total_bytes": sum(f["bytes"] for f in files) + len(mbytes),
               "rows": {f["table"]: f["rows"] for f in files if f["table"]}, **stats}
    return {**manifest, "summary": summary}


def _atomic_write(path: Path, data: bytes) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


# ------------------------------------------------------------------------------------------------ validation


def validate_manifest(manifest: Any) -> dict:
    """Structural and licence checks on a downloaded manifest; returns it or raises PackError."""
    if not isinstance(manifest, dict):
        raise PackError("manifest is not an object")
    if manifest.get("format") != FORMAT:
        raise PackError(f"not a jev-screen open pack (format={manifest.get('format')!r})")
    version = manifest.get("format_version")
    if not isinstance(version, int) or version < 1:
        raise PackError("bad format_version")
    if version > FORMAT_VERSION:
        raise PackError(f"pack format {version} is newer than this jevscreen supports ({FORMAT_VERSION}): upgrade")
    files = manifest.get("files")
    if not isinstance(files, list) or not files:
        raise PackError("manifest lists no files")
    if sum(f.get("bytes") or 0 for f in files if isinstance(f, dict) and isinstance(f.get("bytes"), int)) \
            > MAX_PACK_BYTES:
        raise PackError("pack is larger than MAX_PACK_BYTES")
    seen = set()
    for f in files:
        if not isinstance(f, dict):
            raise PackError("bad file entry")
        name, sha, size = f.get("name"), f.get("sha256"), f.get("bytes")
        if not isinstance(name, str) or not _NAME_RE.match(name) or name == MANIFEST or name in seen:
            raise PackError(f"bad file name {name!r}")
        seen.add(name)
        if not isinstance(sha, str) or not _SHA_RE.match(sha):
            raise PackError(f"bad sha256 for {name}")
        if not isinstance(size, int) or size < 0 or size > MAX_FILE_BYTES:
            raise PackError(f"bad size for {name}")
        table = f.get("table")
        if table is None:
            if name != ATTRIBUTION or f.get("source_id") is not None:
                raise PackError(f"file {name!r} has no table")
            continue
        if table not in TABLES or TABLES[table][0] != name or TABLES[table][1] != f.get("source_id"):
            raise PackError(f"unknown table {table!r} / file {name!r}")
        assert_redistributable(f["source_id"])
    return manifest


def _clean_codelist_row(r: Mapping[str, Any]) -> dict | None:
    code = str(r.get("edinet_code") or "").upper()
    if not _EDINET_CODE_RE.match(code):
        return None
    sc = r.get("sec_code")
    sc = str(sc).upper() if sc else None
    if sc is not None and not _SEC_CODE_RE.match(sc):
        sc = None
    listed = r.get("listed")
    return {"edinet_code": code, "sec_code": sc, "listed": listed if isinstance(listed, bool) else None,
            **{k: (str(r[k])[:500] if r.get(k) is not None else None)
               for k in ("name", "name_en", "filer_type", "jcn")}}


def _clean_business_row(r: Mapping[str, Any]) -> dict | None:
    code, doc = str(r.get("edinet_code") or "").upper(), str(r.get("doc_id") or "").upper()
    text = r.get("text")
    if not _EDINET_CODE_RE.match(code) or not _EDINET_DOC_RE.match(doc) or not isinstance(text, str):
        return None
    text = text[:MAX_TEXT_CHARS]
    if len(text.strip()) < MIN_BUSINESS_CHARS:
        return None

    def date(v: Any) -> dt.date | None:
        return dt.date.fromisoformat(v) if isinstance(v, str) and _DATE_RE.match(v) else None
    url = r.get("source_url")
    if not (isinstance(url, str) and url.startswith(EDINET_URL_PREFIXES) and len(url) < 500):
        url = None
    elif url.startswith("https://api.edinet-fsa.go.jp/"):      # older packs: the key-gated API URL
        url = EDINET_VIEWER_URL.format(doc_id=doc)
    return {"edinet_code": code, "doc_id": doc, "text": text, "form": str(r.get("form") or "")[:50] or None,
            "filing_date": date(r.get("filing_date")), "period_end": date(r.get("period_end")), "url": url}


# ------------------------------------------------------------------------------------------------ pull (network)


class GitHubFetcher:
    """GET over the polite http.Client (1 s per host, no silent redirects). Follows at most MAX_REDIRECTS https
    redirects, and only to GitHub hosts. 403/429/challenge -> PackBlocked (the client halts; no retry)."""

    def __init__(self, cfg: Config, client: Any = None):
        if client is None:
            from .http import Client
            client = Client(user_agent=cfg.user_agent, min_interval_s=max(cfg.min_interval_s, MIN_INTERVAL_S),
                            timeout_s=cfg.timeout_s)
        self.client = client
        self.requests = 0

    @staticmethod
    def allowed(url: str) -> bool:
        u = urlparse(url)
        host = (u.hostname or "").lower()
        return u.scheme == "https" and (host in ("github.com", "api.github.com")
                                        or host.endswith(".githubusercontent.com"))

    def get(self, url: str, *, accept: str = "application/octet-stream", max_bytes: int = MAX_FILE_BYTES) -> bytes:
        from .http import Blocked
        for _ in range(MAX_REDIRECTS + 1):
            if not self.allowed(url):
                raise PackError(f"refusing to fetch non-GitHub URL {url}")
            try:
                self.requests += 1
                resp = self.client.request("GET", url, headers={"Accept": accept}, max_bytes=max_bytes + 1,
                                           deadline_s=1800.0)
            except Blocked as e:
                raise PackBlocked(url, e.status, e.reason) from None
            if resp.status in (301, 302, 303, 307, 308):
                loc = {k.lower(): v for k, v in resp.headers.items()}.get("location")
                if not loc:
                    raise PackError(f"redirect without Location from {url}")
                url = loc
                continue
            if resp.status == 404:
                raise PackError(f"not found: {url}")
            if resp.status != 200:
                raise PackError(f"HTTP {resp.status} from {url}")
            if getattr(resp, "truncated", False) or len(resp.body) > max_bytes:
                raise PackError(f"response too large from {url}")
            return resp.body
        raise PackError(f"too many redirects from {url}")

    def json(self, url: str) -> Any:
        return json.loads(self.get(url, accept="application/vnd.github+json", max_bytes=8 * 1024 * 1024))


def resolve_repo(repo: str | None) -> str:
    repo = (repo or os.environ.get("JEVSCREEN_PACK_REPO") or DEFAULT_PACK_REPO).strip()
    if not repo:
        raise PackError("no pack repository configured: pass --repo OWNER/NAME or set JEVSCREEN_PACK_REPO")
    if not _REPO_RE.match(repo):
        raise PackError(f"bad repository name {repo!r} (expected OWNER/NAME)")
    return repo


def find_release(fetch: GitHubFetcher, repo: str, tag: str = "latest") -> dict:
    """The newest non-draft, non-prerelease release tagged 'pack-*' (tag 'latest'), or the release with `tag`."""
    if tag and tag != "latest":
        if not _TAG_RE.match(tag):
            raise PackError(f"bad tag {tag!r}")
        return fetch.json(f"{GITHUB_API}/repos/{repo}/releases/tags/{quote(tag)}")
    releases = fetch.json(f"{GITHUB_API}/repos/{repo}/releases?per_page=100")
    packs = [r for r in releases if isinstance(r, dict) and not r.get("draft") and not r.get("prerelease")
             and str(r.get("tag_name") or "").startswith(TAG_PREFIX)]
    if not packs:
        raise PackError(f"no '{TAG_PREFIX}*' release found in {repo}")
    from .pack_ci import tag_key         # pack-DATE < pack-DATE.2 < pack-DATE.10 (a second run on one day)
    return max(packs, key=lambda r: (tag_key(str(r.get("tag_name"))), str(r.get("tag_name")),
                                     str(r.get("published_at") or "")))


def _asset(release: Mapping[str, Any], name: str) -> dict:
    for a in release.get("assets") or []:
        if isinstance(a, dict) and a.get("name") == name:
            return a
    raise PackError(f"release {release.get('tag_name')} has no asset {name}")


def _verify(name: str, data: bytes, sha: str, size: int | None, asset: Mapping[str, Any] | None = None) -> None:
    if size is not None and len(data) != size:
        raise PackError(f"{name}: size {len(data)} != manifest {size}")
    got = _sha256(data)
    if got != sha:
        raise PackError(f"{name}: sha256 mismatch (manifest {sha[:12]}..., got {got[:12]}...)")
    digest = str((asset or {}).get("digest") or "")
    if digest.startswith("sha256:") and digest[7:] != got:
        raise PackError(f"{name}: sha256 differs from GitHub's asset digest")


def pull(cfg: Config, *, tag: str = "latest", repo: str | None = None, fetcher: GitHubFetcher | None = None,
         force: bool = False, after_block: bool = False) -> dict:
    """Download, verify and import a pack. Returns a summary with 'status' ok | already_imported | blocked | error.
    Raises nothing for network/verification failures: they come back as status 'error'/'blocked' with a note,
    and nothing is imported unless every file verified."""
    started = store.now_utc()
    summary: dict[str, Any] = {"command": COMMAND_PULL, "status": "ok", "tag": None, "requests": 0, "imported": {}}
    refused = _cooldown(cfg, COMMAND_PULL, after_block)
    if refused:
        return {**summary, **refused}
    fetch = fetcher or GitHubFetcher(cfg)
    try:
        repo = resolve_repo(repo)
        summary["repo"] = repo
        release = find_release(fetch, repo, tag)
        rtag = str(release.get("tag_name") or "")
        if not _TAG_RE.match(rtag):
            raise PackError(f"bad release tag {rtag!r}")
        summary["tag"] = rtag
        m_asset = _asset(release, MANIFEST)
        m_bytes = fetch.get(str(m_asset.get("browser_download_url")), max_bytes=MAX_MANIFEST_BYTES)
        digest = str(m_asset.get("digest") or "")
        m_sha = _sha256(m_bytes)
        if digest.startswith("sha256:") and digest[7:] != m_sha:
            raise PackError("manifest.json differs from GitHub's asset digest")
        manifest = validate_manifest(json.loads(m_bytes))
        summary["manifest_sha256"] = m_sha
        if not force and _manifest_imported(cfg, m_sha):
            summary["status"] = "already_imported"
            summary["requests"] = fetch.requests
            # cheap and local: lines added since the import (e.g. the universe was built after the first pull)
            summary["relinked"] = relink(cfg)
            _journal(cfg, started, summary)
            return summary
        blobs: dict[str, bytes] = {}
        for f in manifest["files"]:
            a = _asset(release, f["name"])
            data = fetch.get(str(a.get("browser_download_url")), max_bytes=min(f["bytes"], MAX_FILE_BYTES))
            _verify(f["name"], data, f["sha256"], f["bytes"], a)
            blobs[f["name"]] = data
    except PackBlocked as e:
        from . import guard
        guard.mark_blocked(cfg, COMMAND_PULL, url=e.url, status=e.status, reason=e.reason)
        summary.update(status="blocked", note=str(e), requests=fetch.requests)
        _journal(cfg, started, summary)
        return summary
    except (PackError, ValueError, OSError) as e:
        summary.update(status="error", note=f"{type(e).__name__}: {e}"[:500], requests=fetch.requests)
        _journal(cfg, started, summary)
        return summary
    summary["requests"] = fetch.requests
    from . import guard
    guard.clear_blocked(cfg, COMMAND_PULL)
    folder = cfg.raw_dir / "open_pack" / f"{rtag}-{m_sha[:12]}"   # a re-released tag never overwrites cited files
    folder.mkdir(parents=True, exist_ok=True)
    _atomic_write(folder / MANIFEST, m_bytes)
    for name, data in blobs.items():
        _atomic_write(folder / name, data)
    try:
        summary["imported"] = import_pack(cfg, folder, manifest=manifest, manifest_sha256=m_sha, repo=repo)
    except (PackError, ValueError, OSError, EOFError) as e:     # e.g. a corrupt gzip body that matched its sha256
        summary.update(status="error", note=f"import: {type(e).__name__}: {e}"[:500])
    _journal(cfg, started, summary)
    return summary


def _cooldown(cfg: Config, command: str, after_block: bool) -> dict | None:
    """A block less than COOLDOWN_HOURS ago refuses the run before any request (unless after_block)."""
    from . import guard
    marker = None if after_block else guard.recent_block_marker(cfg, command, hours=COOLDOWN_HOURS)
    if marker is None:
        return None
    return {"status": "blocked", "note": f"cooldown: {command} was blocked at {marker.get('blocked_at')} "
                                         f"(HTTP {marker.get('status')}); retry after {COOLDOWN_HOURS} h or pass "
                                         "--after-block"}


def _manifest_imported(cfg: Config, m_sha: str) -> bool:
    if not cfg.db_path.exists():
        return False
    with store.session(cfg, wait_s=WRITE_WAIT_S) as con:
        return bool(con.execute("SELECT count(*) FROM snapshots WHERE kind = ? AND raw_sha256 = ?",
                                [SNAPSHOT_KIND_PREFIX + "manifest", m_sha]).fetchone()[0])


def _journal(cfg: Config, started: dt.datetime, summary: Mapping[str, Any]) -> None:
    status = {"already_imported": "ok"}.get(summary["status"], summary["status"])
    note = json.dumps({k: summary.get(k) for k in ("tag", "manifest_sha256", "imported", "note") if summary.get(k)},
                      ensure_ascii=False, sort_keys=True, default=str)[:2000]
    try:
        with store.session(cfg, wait_s=WRITE_WAIT_S) as con:
            con.execute("INSERT INTO runs VALUES (?,?,?,?,?,?,?)",
                        [f"run:{uuid.uuid4().hex[:12]}", summary.get("command", COMMAND_PULL), started,
                         store.now_utc(), status, summary.get("requests"), note])
    except store.StoreLocked:
        pass


# ------------------------------------------------------------------------------------------------ import


def import_pack(cfg: Config, folder: Path | str, *, manifest: Mapping[str, Any] | None = None,
                manifest_sha256: str | None = None, repo: str | None = None) -> dict:
    """Import a verified pack directory into the store (idempotent). Re-verifies every file against the manifest."""
    folder = Path(folder)
    m_bytes = (folder / MANIFEST).read_bytes()
    manifest_sha256 = manifest_sha256 or _sha256(m_bytes)
    manifest = validate_manifest(manifest if manifest is not None else json.loads(m_bytes))
    tag = str(manifest.get("tag") or folder.name)
    tables: dict[str, tuple[dict, bytes]] = {}
    for f in manifest["files"]:
        data = (folder / f["name"]).read_bytes()
        _verify(f["name"], data, f["sha256"], f["bytes"])
        if f.get("table"):
            tables[f["table"]] = (f, data)

    tickers = _read_ticker_rows(str(folder / TABLES["sec_tickers"][0])) if "sec_tickers" in tables else None
    business, rejected, codelist_dropped = [], 0, 0
    codelist = None
    if "edinet_codes" in tables:
        codelist = []
        for r in decode_jsonl_gz(tables["edinet_codes"][1]):
            c = _clean_codelist_row(r)
            if c is None:
                rejected += 1
            else:
                codelist.append(c)
        kept = publishable_codelist_rows(codelist)
        codelist_dropped = len(codelist) - len(kept)
        codelist = kept
    if "edinet_business" in tables:
        for r in decode_jsonl_gz(tables["edinet_business"][1]):
            c = _clean_business_row(r)
            if c is None:
                rejected += 1
            else:
                business.append(c)

    from .sources import edinet
    now = store.now_utc()
    counts: dict[str, Any] = {"rows_rejected": rejected, "codelist_rows_dropped": codelist_dropped}
    request = {"repo": repo, "tag": tag, "manifest_sha256": manifest_sha256}
    # Where each text goes (written by _import_business only for rows that change; idempotent by content).
    texts: dict[str, tuple[Path, str]] = {}
    for b in business:
        tp = edinet.text_path_for(cfg, b["edinet_code"], b["doc_id"])
        texts[b["doc_id"]] = (tp, _sha256(b["text"].encode("utf-8")))

    # Text files are staged next to their target and renamed only after COMMIT: a rolled-back import never leaves a
    # file whose content disagrees with its committed documents row.
    staged: list[tuple[Path, Path]] = []
    with store.session(cfg, wait_s=WRITE_WAIT_S) as con:
        con.execute("BEGIN TRANSACTION")     # all or nothing: a failed import leaves no pack snapshot behind
        try:
            counts.update(_import_rows(con, cfg, tables, tickers, codelist, business, texts, request, tag, now,
                                       folder, m_bytes, manifest, manifest_sha256, staged))
            con.execute("COMMIT")
        except BaseException:
            con.execute("ROLLBACK")
            for tmp, _target in staged:
                tmp.unlink(missing_ok=True)
            raise
    for tmp, target in staged:
        os.replace(tmp, target)
    return counts


def _import_rows(con, cfg: Config, tables: Mapping[str, tuple[dict, bytes]], tickers: list | None,
                 codelist: list | None, business: list, texts: Mapping[str, tuple[Path, str]], request: dict,
                 tag: str, now: dt.datetime, folder: Path, m_bytes: bytes, manifest: Mapping[str, Any],
                 manifest_sha256: str, staged: list) -> dict:
    from .sources import edinet, sec_edgar
    counts: dict[str, Any] = {}
    snaps: dict[str, str] = {}
    for table, (f, data) in tables.items():
        snaps[table] = store.record_snapshot(
            con, source_id=f["source_id"], kind=SNAPSHOT_KIND_PREFIX + table, request={**request,
                                                                                      "file": f["name"]},
            raw_path=str(folder / f["name"]), raw_sha256=f["sha256"], raw_bytes=f["bytes"], rows=f.get("rows"),
            duration_s=None, note=f"pack-imported tag={tag}", fetched_at=now)
    if tables:
        store.record_snapshot(
            con, source_id=next(iter(tables.values()))[0]["source_id"], kind=SNAPSHOT_KIND_PREFIX + "manifest",
            request=request, raw_path=str(folder / MANIFEST), raw_sha256=manifest_sha256,
            raw_bytes=len(m_bytes), rows=len(manifest["files"]), duration_s=None,
            note=f"pack-imported tag={tag}", fetched_at=now)
    if tickers is not None:
        counts["sec_cik_identifiers_added"] = _add_identifiers(
            con, "sec_cik", sec_edgar.US_VENUES,
            lambda lines: [(s, str(c), m) for s, c, m in sec_edgar.map_securities_to_cik(lines, tickers)],
            snaps["sec_tickers"])
        counts["sec_tickers"] = len(tickers)
    if codelist is not None:
        counts["edinet_code_identifiers_added"] = _add_identifiers(
            con, "edinet_code", edinet.JP_VENUES,
            lambda lines: edinet.map_securities_to_edinet(lines, codelist), snaps["edinet_codes"])
        counts["edinet_codes"] = len(codelist)
    if business:
        counts.update(_import_business(con, cfg, business, texts, snaps["edinet_business"], tag, now, staged))
    return counts


def _add_identifiers(con, id_type: str, venues: Iterable[str], mapper: Callable[[list], list],
                     snapshot_id: str) -> int:
    venues = sorted(venues)
    ph = ",".join("?" for _ in venues)
    lines = [dict(zip(("security_id", "exchange", "symbol"), r)) for r in con.execute(
        f"SELECT s.security_id, s.exchange, s.symbol FROM securities s WHERE s.active AND upper(s.exchange) IN ({ph}) "
        "AND NOT EXISTS (SELECT 1 FROM identifiers i WHERE i.security_id = s.security_id AND i.id_type = ?)",
        [*venues, id_type]).fetchall()]
    if not lines:
        return 0
    rows = [[sid, id_type, str(native), method, snapshot_id] for sid, native, method in mapper(lines)]
    return store.upsert_many(con, "identifiers", ["security_id", "id_type", "id_value", "method", "snapshot_id"], rows)


_DOC_COLS = ["doc_id", "security_id", "company_key", "source_id", "cik", "form", "section", "accession",
             "filing_date", "report_date", "url", "raw_sha256", "raw_bytes", "text_path", "text_sha256", "text_chars",
             "extractor", "extract_note", "fetched_at", "snapshot_id"]
_DESC_COLS = ["security_id", "source_id", "company_key", "text", "text_sha256", "lang", "source_url", "fetched_at",
              "match_method", "match_score", "snapshot_id"]


def _edinet_line_of(con) -> dict[str, tuple[str, str]]:
    """EDINET code -> its primary line (universe first, then any active line), through the edinet_code identifier."""
    line_of: dict[str, tuple[str, str]] = {}
    for code, sid, ck, in_u in con.execute(
            "SELECT i.id_value, s.security_id, s.company_key, (u.security_id IS NOT NULL) AS in_u "
            "FROM identifiers i JOIN securities s USING (security_id) LEFT JOIN universe u USING (security_id) "
            "WHERE i.id_type = 'edinet_code' ORDER BY in_u DESC, u.market_cap_usd DESC NULLS LAST, s.security_id"
    ).fetchall():
        line_of.setdefault(code, (sid, ck))
    return line_of


def _edinet_desc_from_pack(con) -> dict[str, bool]:
    """security_id -> whether its EDINET description came from a pack (a local one is never replaced)."""
    return {r[0]: r[1] for r in con.execute(
        "SELECT d.security_id, coalesce(n.kind LIKE 'pack:%', false) FROM descriptions d "
        "LEFT JOIN snapshots n USING (snapshot_id) WHERE d.source_id = 'edinet_yuho'").fetchall()}


def relink(cfg: Config) -> dict:
    """Local, no network: link what earlier pack imports could not, because the lines did not exist yet (a pull
    before the universe was built). From the newest imported pack files kept under raw/open_pack/: add missing
    sec_cik / edinet_code identifiers (never replacing one), set the line of pack documents that have none, and
    write their short description where none exists or the existing one came from a pack. One transaction."""
    if not cfg.db_path.exists():
        return {}
    with store.session(cfg, wait_s=WRITE_WAIT_S) as con:
        con.execute("BEGIN TRANSACTION")
        try:
            counts = _relink(con)
            con.execute("COMMIT")
        except BaseException:
            con.execute("ROLLBACK")
            raise
    return counts


def _relink(con) -> dict:
    from .sources import edinet, sec_edgar
    counts = {"sec_cik_identifiers_added": 0, "edinet_code_identifiers_added": 0, "edinet_documents_linked": 0,
              "edinet_descriptions_written": 0}
    tick = _newest_raw(con, "sec_tickers", (SNAPSHOT_KIND_PREFIX + "sec_tickers",))
    if tick:
        tickers = _read_ticker_rows(tick[0])
        counts["sec_cik_identifiers_added"] = _add_identifiers(
            con, "sec_cik", sec_edgar.US_VENUES,
            lambda lines: [(s, str(c), m) for s, c, m in sec_edgar.map_securities_to_cik(lines, tickers)],
            tick[1]["snapshot_id"])
    cl = _newest_raw(con, "edinet_yuho", (SNAPSHOT_KIND_PREFIX + "edinet_codes",))
    if cl:
        codelist = publishable_codelist_rows(_read_codelist_rows(cl[0]))
        counts["edinet_code_identifiers_added"] = _add_identifiers(
            con, "edinet_code", edinet.JP_VENUES, lambda lines: edinet.map_securities_to_edinet(lines, codelist),
            cl[1]["snapshot_id"])
    docs = con.execute(
        "SELECT doc_id, cik, url, text_path, text_sha256, snapshot_id FROM documents WHERE source_id = 'edinet_yuho' "
        "AND extractor = ? AND security_id IS NULL AND cik IS NOT NULL ORDER BY filing_date DESC NULLS LAST, doc_id",
        [PACK_EXTRACTOR]).fetchall()
    if not docs:
        return counts
    line_of = _edinet_line_of(con)
    desc_from_pack = _edinet_desc_from_pack(con)
    now = store.now_utc()
    desc_rows, described = [], set()
    for doc_id, code, url, tp, text_sha, snap in docs:
        if code not in line_of:
            continue
        sid, ck = line_of[code]
        con.execute("UPDATE documents SET security_id = ?, company_key = ? WHERE doc_id = ?", [sid, ck, doc_id])
        counts["edinet_documents_linked"] += 1
        if sid in described or not desc_from_pack.get(sid, True):
            continue
        try:
            raw = Path(tp).read_bytes()
        except (OSError, TypeError):
            continue
        if text_sha and _sha256(raw) != text_sha:
            continue
        short = edinet.short_description_cjk(raw.decode("utf-8", errors="replace"))
        if short:
            described.add(sid)
            desc_rows.append([sid, "edinet_yuho", ck, short, _sha256(short.encode("utf-8")), "ja", url, now,
                              "pack:edinet_code", None, snap])
    counts["edinet_descriptions_written"] = store.upsert_many(con, "descriptions", _DESC_COLS, desc_rows)
    return counts


def _import_business(con, cfg: Config, business: Sequence[dict], texts: Mapping[str, tuple[Path, str]],
                     snapshot_id: str, tag: str, now: dt.datetime, staged: list) -> dict:
    """Upsert pack documents and descriptions. Text files are only staged (written to a temp name, appended to
    `staged` as (temp, target)); import_pack renames them after COMMIT."""
    from .sources import edinet
    counts = {"edinet_documents_added": 0, "edinet_documents_updated": 0, "edinet_documents_kept_local": 0,
              "edinet_documents_unchanged": 0, "edinet_documents_unmapped": 0, "edinet_descriptions_written": 0}
    line_of = _edinet_line_of(con)
    existing = {r[0]: r[1:] for r in con.execute(
        "SELECT doc_id, extractor, text_sha256, security_id FROM documents WHERE source_id = 'edinet_yuho'").fetchall()}
    desc_from_pack = _edinet_desc_from_pack(con)
    doc_rows, desc_rows = [], []
    for b in business:
        code, doc = b["edinet_code"], b["doc_id"]
        doc_id = f"edinet_yuho:{code}:{doc}:business"
        sid, ck = line_of.get(code, (None, None))
        tp, text_sha = texts[doc]
        old = existing.get(doc_id)
        if old is not None and not str(old[0] or "").startswith("open-pack"):
            counts["edinet_documents_kept_local"] += 1
            continue
        if old is not None and old[1] == text_sha and old[2] == sid and tp.exists():
            counts["edinet_documents_unchanged"] += 1
            continue
        tp.parent.mkdir(parents=True, exist_ok=True)
        data = b["text"].encode("utf-8")
        if not tp.exists() or _sha256(tp.read_bytes()) != text_sha:
            tmp = tp.with_name(f"{tp.name}.{uuid.uuid4().hex[:8]}.tmp")
            staged.append((tmp, tp))      # listed first, so a failed write is cleaned up on ROLLBACK too
            tmp.write_bytes(data)
        counts["edinet_documents_updated" if old is not None else "edinet_documents_added"] += 1
        if sid is None:
            counts["edinet_documents_unmapped"] += 1
        doc_rows.append({"doc_id": doc_id, "security_id": sid, "company_key": ck, "source_id": "edinet_yuho",
                         "cik": code, "form": b["form"], "section": edinet.SECTION, "accession": doc,
                         "filing_date": b["filing_date"], "report_date": b["period_end"], "url": b["url"],
                         "raw_sha256": None, "raw_bytes": None, "text_path": str(tp), "text_sha256": text_sha,
                         "text_chars": len(b["text"]), "extractor": PACK_EXTRACTOR,
                         "extract_note": f"pack:{tag};blocks:business", "fetched_at": now,
                         "snapshot_id": snapshot_id})
        if sid is not None and desc_from_pack.get(sid, True):
            short = edinet.short_description_cjk(b["text"])
            if short:
                desc_rows.append([sid, "edinet_yuho", ck, short, _sha256(short.encode("utf-8")), "ja", b["url"], now,
                                  "pack:edinet_code", None, snapshot_id])
    store.upsert_many(con, "documents", _DOC_COLS, [[d[c] for c in _DOC_COLS] for d in doc_rows])
    counts["edinet_descriptions_written"] = store.upsert_many(con, "descriptions", _DESC_COLS, desc_rows)
    return counts


# ------------------------------------------------------------------------------------------------ CI: open syncs


def _has_tradingview_data(con) -> bool:
    return bool(con.execute("SELECT count(*) FROM snapshots WHERE source_id LIKE 'tradingview%'").fetchone()[0])


def seed_jp_lines(con, codelist: Sequence[Mapping[str, Any]], snapshot_id: str) -> int:
    """One active primary common-stock line 'TSE:<code>' per listed EDINET filer, for a CI store WITHOUT any
    TradingView data (refused otherwise, so a user's universe is never touched). Lines carry no market data."""
    if _has_tradingview_data(con):
        raise PackError("refusing to seed lines into a store that holds TradingView data")
    rows, seen = [], set()
    for e in codelist:
        sc = e.get("sec_code")
        if not sc or e.get("listed") is False or not _SEC_CODE_RE.match(str(sc)):
            continue
        sym = _sec_code_to_symbol(str(sc))
        sid = f"TSE:{sym}"
        if sid in seen:
            continue
        seen.add(sid)
        rows.append([sid, "TSE", sym, e.get("name"), None, "Japan", "stock", "common", True, "JPY", "JPY", None, None,
                     f"edinet:{e['edinet_code']}", snapshot_id, snapshot_id, store.now_utc(), True])
    cols = ["security_id", "exchange", "symbol", "name", "isin", "country", "tv_type", "tv_subtype", "is_primary",
            "price_currency", "fundamental_currency", "sector", "industry", "company_key", "first_seen_snapshot",
            "last_seen_snapshot", "last_seen_at", "active"]
    n = store.upsert_many(con, "securities", cols, rows)
    # filers that left the code list (or were delisted) are deactivated, so sync-edinet stops queueing them
    con.execute("UPDATE securities SET active = false WHERE company_key LIKE 'edinet:%' AND active "
                "AND NOT list_contains(?::VARCHAR[], security_id)", [sorted(seen)])
    return n


def fetch_open(cfg: Config, *, seed_lines: bool = False, sec_client: Any = None, edinet_client: Any = None,
               after_block: bool = False) -> dict:
    """CI helper: SEC ticker list (skipped without an SEC User-Agent) and the key-free EDINET code list, each one
    request, saved raw + snapshot. Returns {'status', 'sec_tickers': ..., 'edinet_codelist': ...}.
    The first 403/429/challenge stops everything (cooldown marker, status 'blocked')."""
    from . import guard
    from .http import Blocked, Client
    from .sources import edinet, sec_edgar
    started = store.now_utc()
    summary: dict[str, Any] = {"command": COMMAND_FETCH, "status": "ok", "requests": 0}
    refused = _cooldown(cfg, COMMAND_FETCH, after_block)
    if refused:
        return {**summary, **refused}
    ua: str | None = None
    try:
        ua = cfg.sec_user_agent() if sec_client is None else "injected"
        if not ua:
            summary["sec_tickers"] = "skipped: no SEC User-Agent (set JEVSCREEN_SEC_USER_AGENT)"
        else:
            client = sec_client or Client(user_agent=ua, min_interval_s=sec_edgar.DEFAULT_MIN_INTERVAL_S,
                                          timeout_s=cfg.timeout_s)
            resp = None
            with guard.budget_lock(cfg, sec_edgar.RATE_KEY, reentrant=True):
                t0 = time.monotonic()
                summary["requests"] += 1
                try:
                    resp = client.request("GET", sec_edgar.TICKERS_URL, rate_key=sec_edgar.RATE_KEY,
                                          headers={"Accept": "application/json"})
                except (OSError, ValueError) as e:   # network trouble on SEC must not cost the EDINET step
                    summary["sec_tickers"] = f"error: {type(e).__name__}"
                    summary["status"] = "error"
            try:
                rows = sec_edgar.parse_tickers(resp.body) if resp is not None and resp.status == 200 else []
            except ValueError:
                rows = []
            if resp is None:
                pass
            elif not rows:
                summary["sec_tickers"] = f"error: http_{resp.status} rows=0"
                summary["status"] = "error"
            else:
                path, digest = store.save_raw(cfg, "sec_tickers", "company_tickers_exchange", resp.body)
                with store.session(cfg, wait_s=WRITE_WAIT_S) as con:
                    store.record_snapshot(con, source_id="sec_tickers", kind="company_tickers_exchange",
                                          request={"url": sec_edgar.TICKERS_URL, "headers": {"User-Agent":
                                                                                              "<sec-user-agent>"}},
                                          raw_path=path, raw_sha256=digest, raw_bytes=len(resp.body), rows=len(rows),
                                          duration_s=round(time.monotonic() - t0, 3), note="pack fetch-open")
                summary["sec_tickers"] = {"rows": len(rows)}
        client = edinet_client or Client(user_agent=cfg.user_agent,
                                         min_interval_s=max(cfg.min_interval_s, edinet.DEFAULT_MIN_INTERVAL_S),
                                         timeout_s=cfg.timeout_s)
        with guard.budget_lock(cfg, edinet.RATE_KEY, reentrant=True):
            t0 = time.monotonic()
            summary["requests"] += 1
            resp = client.request("GET", edinet.CODELIST_URL, rate_key=edinet.RATE_KEY,
                                  max_bytes=edinet.MAX_CODELIST_BYTES, deadline_s=edinet.CODELIST_DEADLINE_S)
        entries = edinet.parse_codelist(resp.body) if resp.status == 200 else []
        if not entries:
            summary["edinet_codelist"] = f"error: http_{resp.status} rows=0"
            summary["status"] = "error"
        else:
            path, digest = store.save_raw(cfg, "edinet_yuho", "edinet_codelist", resp.body, suffix="zip")
            with store.session(cfg, wait_s=WRITE_WAIT_S) as con:
                snap = store.record_snapshot(con, source_id="edinet_yuho", kind="edinet_codelist",
                                             request={"url": edinet.CODELIST_URL}, raw_path=path, raw_sha256=digest,
                                             raw_bytes=len(resp.body), rows=len(entries),
                                             duration_s=round(time.monotonic() - t0, 3), note="pack fetch-open")
                summary["edinet_codelist"] = {"rows": len(entries)}
                if seed_lines:
                    summary["edinet_codelist"]["lines_seeded"] = seed_jp_lines(con, entries, snap)
    except Blocked as e:
        guard.mark_blocked(cfg, COMMAND_FETCH, url=e.url, status=e.status, reason=e.reason)
        summary.update(status="blocked", note=f"blocked: {e.reason} (HTTP {e.status}) at {e.url}")
    except (PackError, ValueError, OSError) as e:
        summary.update(status="error", note=f"{type(e).__name__}: {e}"[:500])
    if summary["status"] == "ok":
        guard.clear_blocked(cfg, COMMAND_FETCH)
    if summary.get("note") and ua and ua != "injected":
        from .config import redact
        summary["note"] = redact(summary["note"], [ua])
    _journal(cfg, started, summary)
    return summary
