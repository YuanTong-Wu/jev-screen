"""A stand-in adapter for tests/test_ondemand.py: plays any on-demand source inside a REAL child process
(`python -m jevscreen.ondemand child`, JEVSCREEN_TESTING=1 + JEVSCREEN_ONDEMAND_MODULES). No network.

Behaviour per source key from <home>/fake_adapter.json, e.g. {"sec": {"ok": ["NYSE:ACME"], "delay_s": 0.2,
"status": {"NYSE:X": "no_annual_filing"}, "hang": false, "ignore_term": false, "result_status": "ok"}}. It writes
identifiers / crawl_state / documents (a synthetic text file) in short sessions like a real adapter and calls
on_company once per company. Text is synthetic and names only invented issuers.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

from jevscreen import ondemand, store

EXTRACTOR_VERSION = "fake-v1"
TEXT = ("Item 1. Business\n\n{name} designs, builds and sells humanoid robots for warehouse picking. Our humanoid "
        "robots are deployed at customer sites under multi-year service contracts and are our fastest growing "
        "product line.\n\nWe also sell spare parts and maintenance subscriptions to the same customers.\n")


def _key() -> str:
    argv = sys.argv
    return argv[argv.index("--source") + 1] if "--source" in argv else "sec"


def sync(cfg, client=None, *, codes=None, refresh=False, only_universe=True, progress_every=0, on_company=None,
         **kw):
    key = _key()
    src = ondemand.SOURCES[key]
    spec = {}
    try:
        spec = json.loads((Path(cfg.home) / "fake_adapter.json").read_text(encoding="utf-8")).get(key) or {}
    except (OSError, ValueError):
        pass
    delay = float(spec.get("delay_s") or 0.0)
    ok = set(spec.get("ok") or [])
    statuses = spec.get("status") or {}
    unmapped = set(spec.get("unmapped") or [])
    requests = 0
    try:
        for sid in codes or []:
            time.sleep(delay)
            requests += 2
            if sid in unmapped:
                continue
            status = statuses.get(sid) or ("ok" if sid in ok else "no_annual_filing")
            now = store.now_utc()
            with store.session(cfg) as con:
                ck = (con.execute("SELECT company_key, name FROM securities WHERE security_id = ?", [sid]).fetchone()
                      or (None, sid))
                con.execute("INSERT OR REPLACE INTO identifiers VALUES (?, ?, ?, 'fake', NULL)",
                            [sid, src.id_type, f"id-{sid}"])
                con.execute("INSERT OR REPLACE INTO crawl_state VALUES (?, ?, ?, 200, 1, ?, ?)",
                            [src.source_id, sid, status, now, None if status == "ok" else status])
                if status == "ok":
                    path = Path(cfg.home) / "docs" / "fake" / f"{sid.replace(':', '_')}.txt"
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(TEXT.format(name=ck[1] or sid), encoding="utf-8")
                    con.execute("INSERT OR REPLACE INTO documents (doc_id, security_id, company_key, source_id, cik, "
                                "form, section, accession, filing_date, url, text_path, extractor, fetched_at) "
                                "VALUES (?, ?, ?, ?, ?, '10-K', 'item1', 'acc-1', ?, 'https://example.invalid/doc', ?, "
                                "?, ?)", [f"{src.source_id}:{sid}:acc-1", sid, ck[0], src.source_id, f"id-{sid}",
                                         now.date(), str(path), f"{EXTRACTOR_VERSION}/fake", now])
            if on_company is not None:
                on_company([sid], status, None if status == "ok" else status)
        while spec.get("hang"):
            try:
                time.sleep(0.05)
            except KeyboardInterrupt:
                if not spec.get("ignore_term"):
                    raise
    except KeyboardInterrupt:
        return {"status": "interrupted", "stopped_reason": "interrupted", "requests": requests}
    return {"status": spec.get("result_status") or "ok", "requests": requests, "bytes_downloaded": 1000 * requests}
