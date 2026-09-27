"""jevscreen command line.

Rules (see docs/DATA_RULES.md):
- Every networked run goes through one polite http.Client: refresh-universe at most 1 request/second;
  crawl-descriptions defaults to and is capped at CRAWL_MAX_RATE = 4.4 requests/second (measured ceiling; a faster
  --rate is clamped with a warning); sync-sec uses the SEC fair-access interval. crawl-descriptions --workers N
  (default 3, clamped to 1..4) overlaps page downloads; the shared thread-safe limiter keeps request starts at least
  1/rate apart and at most 4 in any rolling second, so more workers approach but never exceed --rate (sustained
  <= 4.0/s at the default: a margin under the 4.4/s ceiling for network jitter).
- http.Blocked (403/429/challenge) stops the whole run and exits with code 2; never retry or work around it.
  crawl-descriptions also prints one line 'BLOCKED url=... status=... reason=...' as the last stdout line. Once the
  adapter reports a block (on_blocked callback, before any database wait), SIGTERM is ignored and any later failure
  (final flush StoreLocked/error, journal wait, Ctrl-C) still ends with that BLOCKED line and exit 2.
- SIGTERM (a background job started with '&' ignores SIGINT) is converted to the same graceful path as Ctrl-C for
  crawl-descriptions and sync-sec: the adapter flushes, the run is journaled 'interrupted', exit 130.
- On start (after taking the rate-budget lock, which proves no other live process of the command exists), runs rows
  of the same command still 'running' are marked 'abandoned' (a process killed without cleanup).
- Each networked/import command is journaled in `runs` (ok | blocked | error | interrupted), unless the adapter
  journaled itself.
- Cooldown: if the latest run of a networked command was 'blocked' less than COOLDOWN_HOURS ago, the command refuses
  to make any request (exit 2) unless --after-block is given. The block is also recorded in a marker file
  (guard.mark_blocked, written by the adapter the moment it sees Blocked and by the CLI) that needs no DuckDB lock, so
  the cooldown holds even when the final journal write failed; a clean ('ok') run clears the marker.
- One process per rate budget: networked commands hold guard.budget_lock (sec.gov, www.tradingview.com,
  scanner.tradingview.com) for the whole run; a second copy exits 4 without making a request.
- Exit status follows the adapter's result: ok/partial 0, stopped_errors/store_locked/error 1, blocked 2,
  interrupted 130.
- Source adapters are imported lazily so the CLI still loads when one of them is missing.
- Short sessions (DuckDB allows one writer process and then refuses even read-only connections from others):
  read-only commands (coverage, status) open store.session(read_only=True, wait_s=120); refresh-universe and
  import-fd hold one store.session(wait_s=600) for their minute-long run; long crawls (crawl-descriptions, sync-sec)
  never hold a connection: the adapter opens a short session per batch and the CLI opens its own only to check the
  cooldown and to journal. A lock that outlasts the wait prints a clear message and exits 3.
- The SEC User-Agent (cfg.sec_user_agent()) is never printed, logged or journaled: any occurrence in output, error
  text or run notes is replaced by '<sec-user-agent>'.
- sync-cninfo / sync-edinet / sync-dart follow the sync-sec conventions (lazy adapter import, rate-budget lock
  'cninfo' / 'edinet' / 'dart', cooldown per command, SIGTERM -> interrupted 130, blocked 2, busy 4). sync-cninfo
  runs the fast mode by default (bulk listing at 1 req/s, PDF workers --workers N, default 3, 1..4, on the separate
  'cninfo-static' limiter, --since DATE for the listing window; --codes or a queue <= 400 skips the listing);
  --per-company is the old sequential path. EDINET and DART
  need the user's own free API key (cfg.edinet_api_key() / cfg.opendart_api_key()); a missing key is refused before
  any request, and the key is replaced by '<edinet-api-key>' / '<opendart-api-key>' in every output, journal note and
  cooldown marker (keys can travel in request URLs).
- sync-bse (India, BSE annual report PDFs -> MD&A / company overview pages) follows the same conventions (rate
  budget 'bse', >= 1 s between request starts per host, --workers N PDF threads, default 2, 1..3; no key).
- keywords "<idea>" prints the local model's English idea and keywords per language (jevscreen.keywords, offline).
- Calibration (calib.py, docs/DATA_RULES.md "Calibration (sieve)"): screen loads the idea's sieve (--sieve
  auto|none|PATH), re-reads boundary items (--reads K), can start from an earlier run (--from-run: its params are the
  defaults for every option not given, its L1 answers are loaded, L1 $0) and ends by writing and printing the
  calibration cards (--cards N). `cards` rebuilds / reprints a run's deck (free, read-only store session); `answer`
  records answers into the sieve, tries the rules they point to and screens again from the deck's run within
  --apply-budget (exit 0, 1 bad answers / deck / stale sieve, 4 Jev busy, 5 over budget, 6 Jev unavailable, 7 every
  rule rejected; the answers stay saved in every case after parsing); `sieve check` / `sieve show` are free.
"""
from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import importlib
import json
import math
import signal
import sys
import threading
import uuid
from pathlib import Path
from typing import Any, Callable, Sequence

from . import agent_cli, ondemand_cli, quickstart_cli
from . import why_cli

EXIT_OK, EXIT_ERROR, EXIT_BLOCKED, EXIT_LOCKED, EXIT_BUSY, EXIT_INTERRUPTED = 0, 1, 2, 3, 4, 130
EXIT_BUDGET, EXIT_JEV_UNAVAILABLE = 5, 6       # screen / answer only
EXIT_RULES_REJECTED = 7                         # answer: every proposed rule was rejected (pins, keywords applied)
SCREEN_READS_DEFAULT = 3       # screen --reads (screen.L2_READS_DEFAULT; screen is imported lazily)
RANK_CHOICES = ("label", "ev")  # screen --rank (screen.RANKS)
CARDS_DEFAULT = 8              # screen --cards / cards --max (calib.CARDS_MAX)
APPLY_BUDGET_DEFAULT = 0.05    # answer --apply-budget: rule trials + the new screen together (USD)
# adapter result status -> exit code (unknown / missing status counts as 'ok')
RESULT_EXIT = {"ok": EXIT_OK, "partial": EXIT_OK, "blocked": EXIT_BLOCKED, "stopped_errors": EXIT_ERROR,
               "store_locked": EXIT_ERROR, "error": EXIT_ERROR, "interrupted": EXIT_INTERRUPTED}
MAX_RATE = 1.0  # requests per second (refresh-universe)
CRAWL_MAX_RATE = 4.4  # crawl-descriptions: measured ceiling, default and hard maximum (req/s)
CRAWL_DEFAULT_WORKERS, CRAWL_MAX_WORKERS = 3, 4   # crawl-descriptions fetch threads (the limiter still caps the rate)
CRAWL_MAX_STARTS_PER_S = 4   # crawl-descriptions: never more than 4 request starts in any rolling second
CNINFO_DEFAULT_WORKERS, CNINFO_MAX_WORKERS = 3, 4   # sync-cninfo fast mode PDF workers ('cninfo-static' limiter)
CRAWL_TIMEOUT_S = 15.0       # crawl-descriptions: per socket operation (pages take ~0.5 s); bounds the SIGTERM drain
COOLDOWN_HOURS = 24
READ_WAIT_S = 120.0    # read-only commands wait this long for another process's lock
WRITE_WAIT_S = 600.0   # write sessions wait this long
SEC_MIN_INTERVAL_S = 0.15   # SEC fair access: <= 10 req/s total; we stay near 6.7
SEC_DEFAULT_FORMS = "10-K,20-F"
REDACTED_SEC_UA = "<sec-user-agent>"

# runs.command values written for each networked CLI command (adapter-journaled name, CLI fallback name).
RUN_COMMANDS: dict[str, tuple[str, ...]] = {
    "refresh-universe": ("tradingview_scanner.refresh_universe", "refresh-universe"),
    "crawl-descriptions": ("crawl tradingview_profile", "crawl-descriptions"),
    "sync-sec": ("sync-sec", "sync sec_edgar", "sec_edgar.sync", "sync sec_filing_text"),
    "sync-cninfo": ("sync-cninfo", "sync cninfo", "cninfo.sync", "sync cninfo_annual_report"),
    "sync-edinet": ("sync-edinet", "sync edinet", "edinet.sync", "sync edinet_yuho"),
    "sync-dart": ("sync-dart", "sync dart", "dart.sync", "sync dart_business_report"),
    "sync-bse": ("sync-bse",),
}

SCANNER = "jevscreen.sources.tradingview_scanner"
FD_LOCAL = "jevscreen.sources.financedatabase_local"
PROFILES = "jevscreen.sources.tradingview_profiles"
SEC_EDGAR = "jevscreen.sources.sec_edgar"
CNINFO = "jevscreen.sources.cninfo"
EDINET = "jevscreen.sources.edinet"
DART = "jevscreen.sources.dart"
BSE = "jevscreen.sources.bse"
BSE_DEFAULT_WORKERS, BSE_MAX_WORKERS = 2, 3   # sync-bse PDF threads (www.bseindia.com stays >= 1 s between starts)
KEYWORDS = "jevscreen.keywords"

# Minimum seconds between request starts per official-document command (the adapter may only raise it): CNINFO and
# EDINET 1 req/s (EDINET's terms forbid heavy short-interval access); DART 2 req/s, far below its ~20,000/day quota.
OFFICIAL_MIN_INTERVAL_S = {"sync-cninfo": 1.0, "sync-edinet": 1.0, "sync-dart": 0.5, "sync-bse": 1.0}
# Rate budgets of the official-document commands (guard.RATE_BUDGETS wins when it names them too).
CLI_RATE_BUDGETS = {"sync-cninfo": "cninfo", "sync-edinet": "edinet", "sync-dart": "dart", "sync-bse": "bse"}
# command -> (adapter module, Config key method or None, placeholder, env variable, key file, where to register)
OFFICIAL_SYNC: dict[str, tuple[str, str | None, str, str, str, str]] = {
    "sync-cninfo": (CNINFO, None, "", "", "", ""),
    "sync-edinet": (EDINET, "edinet_api_key", "<edinet-api-key>", "JEVSCREEN_EDINET_API_KEY", "edinet_api_key",
                    "https://api.edinet-fsa.go.jp/api/auth/index.aspx?mode=1"),
    "sync-dart": (DART, "opendart_api_key", "<opendart-api-key>", "JEVSCREEN_OPENDART_API_KEY", "opendart_api_key",
                  "https://opendart.fss.or.kr/uss/umt/EgovMberInsertView.do"),
    "sync-bse": (BSE, None, "", "", "", ""),
}

# sync-mops (Taiwan, TWSE / TPEx annual reports from MOPS / doc.twse.com.tw): registered here, apart from the literals
# above. No API key. Rate budget 'mops'; >= 1.5 s between request starts (the adapter raises it to 2 s in mode basic).
MOPS = "jevscreen.sources.mops"
RUN_COMMANDS["sync-mops"] = ("sync-mops", "sync mops", "mops.sync", "sync mops_annual_report")
OFFICIAL_MIN_INTERVAL_S["sync-mops"] = 1.5
CLI_RATE_BUDGETS["sync-mops"] = "mops"
OFFICIAL_SYNC["sync-mops"] = (MOPS, None, "", "", "", "")
# quickstart: the FinanceDatabase bulk download (equities.bz2) is journaled and cooled down like any networked command
RUN_COMMANDS["fetch-fd"] = ("financedatabase_local.download", "fetch-fd")
CLI_RATE_BUDGETS["fetch-fd"] = "fetch-fd"


def add_sync_mops_parser(sub: Any) -> None:
    """The sync-mops subcommand (same options as sync-dart plus --mode and --full-pass)."""
    so = sub.add_parser("sync-mops", help="fetch Taiwan business text: the MOPS 主要經營業務 field (default), or with "
                        "--mode annual the 營運概況/業務內容 section of annual reports (股東會年報) from TWSE's "
                        "e-document server (needs --codes or --full-pass)")
    so.add_argument("--limit", type=positive_int, default=None, metavar="N", help="at most N companies this run")
    so.add_argument("--min-mcap", type=nonneg_float, default=None, metavar="USD",
                    help="only companies with at least this market cap in USD (default: all)")
    so.add_argument("--codes", type=parse_list, default=None, metavar="A,B",
                    help="only these companies (4-digit codes, e.g. 2330,6223)")
    so.add_argument("--refresh", action="store_true", help="re-fetch companies that already have a document")
    so.add_argument("--mode", choices=("annual", "basic"), default="basic",
                    help="basic (default): the MOPS basic-data 主要經營業務 text, 1 request per company; annual: "
                         "annual report business section, 3 requests per company on doc.twse.com.tw (robots.txt "
                         "disallows crawlers: needs --codes, or --full-pass for all companies)")
    so.add_argument("--full-pass", action="store_true",
                    help="with --mode annual and no --codes: really crawl every Taiwan company (~953, ~4.5 GB, "
                         "2.5-6 h) on doc.twse.com.tw; meant as one slow pass per year")
    so.add_argument("--after-block", action="store_true",
                    help=f"run even though the last run was blocked less than {COOLDOWN_HOURS} h ago")


def _mops_annual_refusal(args) -> bool:
    """True (message printed, nothing run) for `sync-mops --mode annual` without --codes and without --full-pass: the
    annual reports live on doc.twse.com.tw, whose robots.txt disallows crawlers, so no bulk crawl by default."""
    if getattr(args, "mode", "basic") != "annual" or getattr(args, "codes", None) or getattr(args, "full_pass", False):
        return False
    _emit({"command": "sync-mops", "status": "error", "error": "annual_needs_codes_or_full_pass"})
    print("error: sync-mops: --mode annual downloads each company's annual report from doc.twse.com.tw, whose "
          "robots.txt disallows crawlers; all Taiwan companies are ~953 companies, ~4.5 GB and 2.5-6 h. Name the "
          "companies you need with --codes 2330,6223, or add --full-pass to run the full yearly pass on purpose. "
          "The default --mode basic uses the MOPS API instead.", file=sys.stderr)
    return True


def cmd_sync_mops(args, cfg) -> int:
    """sync-mops: cmd_sync_official, after refusing a bulk `--mode annual` without --codes / --full-pass."""
    if _mops_annual_refusal(args):
        return EXIT_ERROR
    return cmd_sync_official(args, cfg)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="jevscreen", description="jev-screen: local data layer for Jev screening "
                                "(gray-private data is never redistributed; profile text and short excerpts are sent "
                                "only to the AI service to be read).")
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("init", help="create the DuckDB store and register sources")
    ru = sub.add_parser("refresh-universe", help="pull the global primary-stock + DR universe from the scanner")
    ru.add_argument("--with-history", action="store_true", help="also pull *_fy_h history columns (local currency)")
    ru.add_argument("--history-batch", type=int, default=2000, metavar="N", help="symbols per history request")
    ru.add_argument("--after-block", action="store_true",
                    help=f"run even though the last run was blocked less than {COOLDOWN_HOURS} h ago")
    fd = sub.add_parser("import-fd", help="import descriptions from a local FinanceDatabase DuckDB (read-only)")
    fd.add_argument("--fd-duckdb", type=Path, default=None, metavar="PATH")
    cd = sub.add_parser("crawl-descriptions", help="crawl TradingView symbol pages for business descriptions")
    cd.add_argument("--limit", type=int, default=None, metavar="N")
    cd.add_argument("--rate", type=float, default=CRAWL_MAX_RATE, metavar="R",
                    help=f"requests per second (default and maximum {CRAWL_MAX_RATE}; higher values are clamped)")
    cd.add_argument("--workers", type=int, default=CRAWL_DEFAULT_WORKERS, metavar="N",
                    help=f"concurrent fetches (default {CRAWL_DEFAULT_WORKERS}, clamped to 1..{CRAWL_MAX_WORKERS}); "
                         f"request starts stay >= 1/rate apart and <= {CRAWL_MAX_STARTS_PER_S} per rolling second, "
                         f"so this never raises the rate above --rate")
    cd.add_argument("--all", action="store_true", help="re-crawl securities that already have a description")
    cd.add_argument("--countries", type=parse_list, default=None, metavar="CN",
                    help="only these countries / regions (ISO-2 codes, names or regions; e.g. the China gap fill)")
    cd.add_argument("--min-mcap", type=nonneg_float, default=None, metavar="USD",
                    help="only companies with at least this market cap in USD")
    cd.add_argument("--after-block", action="store_true",
                    help=f"run even though the last run was blocked less than {COOLDOWN_HOURS} h ago")
    ss = sub.add_parser("sync-sec", help="map universe lines to SEC CIKs and fetch 10-K Item 1 / 20-F Item 4 text")
    ss.add_argument("--limit", type=int, default=None, metavar="N", help="at most N companies this run")
    ss.add_argument("--refresh", action="store_true", help="re-fetch companies that already have a document")
    ss.add_argument("--forms", type=parse_forms, default=parse_forms(SEC_DEFAULT_FORMS), metavar="10-K,20-F",
                    help=f"comma-separated annual report forms (default {SEC_DEFAULT_FORMS})")
    ss.add_argument("--after-block", action="store_true",
                    help=f"run even though the last run was blocked less than {COOLDOWN_HOURS} h ago")
    for command, help_text in (
            ("sync-cninfo", "fetch A-share annual reports (summary or full) from CNINFO into documents"),
            ("sync-edinet", "fetch Japanese annual securities reports (事業の内容) from EDINET (needs your API key)"),
            ("sync-dart", "fetch Korean business reports (사업의 개요) from OpenDART (needs your API key)")):
        so = sub.add_parser(command, help=help_text)
        so.add_argument("--limit", type=positive_int, default=None, metavar="N", help="at most N companies this run")
        so.add_argument("--min-mcap", type=nonneg_float, default=None, metavar="USD",
                        help="only companies with at least this market cap in USD (default: the adapter's)")
        so.add_argument("--codes", type=parse_list, default=None, metavar="A,B",
                        help="only these companies (exchange codes, e.g. 300386 / 7203 / 005930)")
        so.add_argument("--refresh", action="store_true", help="re-fetch companies that already have a document")
        if command == "sync-cninfo":
            so.add_argument("--kind", choices=("summary", "full"), default="summary",
                            help="annual report summary (default, small PDF) or the full report")
            so.add_argument("--workers", type=int, default=CNINFO_DEFAULT_WORKERS, metavar="N",
                            help=f"fast mode: concurrent PDF downloads on static.cninfo.com.cn (default "
                                 f"{CNINFO_DEFAULT_WORKERS}, clamped to 1..{CNINFO_MAX_WORKERS}); PDF starts stay "
                                 f">= 0.25 s apart and <= 4 per rolling second, queries stay 1/s")
            so.add_argument("--since", type=parse_date, default=None, metavar="YYYY-MM-DD",
                            help="fast mode: start of the bulk announcement listing (default: 13 months ago, never later "
                                 "than 1 January of last year until 30 April; a later date logs a warning)")
            so.add_argument("--per-company", action="store_true",
                            help="old path: one announcement query per company, everything sequential at 1 req/s")
        so.add_argument("--after-block", action="store_true",
                        help=f"run even though the last run was blocked less than {COOLDOWN_HOURS} h ago")
    add_sync_mops_parser(sub)
    add_sync_bse_parser(sub)
    kw = sub.add_parser("keywords", help="translate an idea into English keywords per language with the local model "
                        "(offline, free) and print them as JSON")
    kw.add_argument("idea", help="the investment idea, any language (quote it)")
    kw.add_argument("--refresh", action="store_true", help="ignore the cached result and generate again")
    cv = sub.add_parser("coverage", help="coverage report of the universe (one row per company)")
    cv.add_argument("--json", action="store_true", help="print the report as JSON")
    sub.add_parser("status", help="row counts per table and the last runs")
    sc = sub.add_parser("screen", help="screen an investment idea: local pre-screen, Jev L1 on descriptions, "
                        "Jev L2 on annual-report excerpts, ranked list (paid Jev calls unless --dry-run)")
    sc.add_argument("idea", nargs="?", default=None,
                    help="the investment idea, any language (quote it); may be left out with --from-run / --idea-of")
    sc.add_argument("--min-mcap", type=nonneg_float, default=2e8, action=_Given, metavar="USD",
                    help="minimum market cap in USD (default 2e8); companies with NULL market cap are excluded")
    sc.add_argument("--min-volume", type=nonneg_float, default=None, action=_Given, metavar="N",
                    help="minimum 10-day average volume")
    sc.add_argument("--countries", type=parse_list, default=None, action=_Given, metavar="US,JP",
                    help="ISO-2 codes, TradingView country names or coverage regions, comma-separated; an unknown "
                         "token is an error. Country tokens match the country of incorporation: Cayman/Bermuda-"
                         "incorporated Chinese companies need the region 'Hong Kong' or 'US' (by listing venue)")
    sc.add_argument("--max-out", type=positive_int, default=40, action=_Given, metavar="N",
                    help="rows in the ranked output (default 40)")
    sc.add_argument("--budget", type=nonneg_float, default=3.0, action=_Given, metavar="USD",
                    help="total Jev budget in USD (default 3)")
    sc.add_argument("--l2-max", type=nonneg_int, default=600, action=_Given, metavar="N",
                    help="at most N L1 passes go to L2 (default 600)")
    sc.add_argument("--keywords", type=parse_terms, default=None, action=_Given, metavar="a,b",
                    help="English terms or phrases used to pick excerpts of English annual reports for L2, e.g. "
                         "'AI agent,access management' (default: generated by the local model for a non-English "
                         "idea, else the Latin words of the idea). A non-English idea is still translated for the Jev "
                         "questions; only --no-translate turns translation off")
    for lang, name, example in (("zh", "Chinese (CNINFO)", "人形机器人,减速器"),
                                ("ja", "Japanese (EDINET)", "ヒューマノイド,減速機"),
                                ("ko", "Korean (DART)", "휴머노이드,감속기")):
        sc.add_argument(f"--keywords-{lang}", type=parse_terms, default=None, action=_Given, metavar=example,
                        help=f"terms used to pick excerpts of {name} annual reports (substring match; default: "
                             "generated by the local model, else the idea's own words in that script)")
    sc.add_argument("--no-translate", action=_Given, nargs=0, default=False,
                    help="do not translate a non-English idea with the local model (the idea is used verbatim)")
    sc.add_argument("--retry-uncertain", action="store_true",
                    help="resend items whose earlier Jev send had an unknown outcome (it may have been charged)")
    sc.add_argument("--dry-run", action="store_true",
                    help="build the Jev inputs and estimate requests/cost/time; no paid calls, no store writes")
    sc.add_argument("--reads", type=positive_int, default=None, action=_Given, metavar="K",
                    help=f"L2 reads of the items near the boundary (read-0 P(explicit)+P(partial) in 0.30..0.75), "
                         f"decided on the mean (default {SCREEN_READS_DEFAULT}; 1 = a single read, as before)")
    sc.add_argument("--read-offset", type=nonneg_int, default=0, metavar="N",
                    help="give the boundary items the fresh reads N..N+K-1 instead of read 0 (churn check)")
    sc.add_argument("--cards", type=nonneg_int, default=CARDS_DEFAULT, metavar="N",
                    help=f"write at most N calibration cards (cards.json / cards.md) and print them (default "
                         f"{CARDS_DEFAULT}; 0 = none)")
    sc.add_argument("--sieve", default="auto", metavar="auto|none|PATH",
                    help="calibration file: auto (data/sieves/<idea key>.json when it exists; default), none, or a "
                         "path")
    sc.add_argument("--from-run", default=None, metavar="RUN_ID|OUT_DIR|results.json",
                    help="use that run's parameters as defaults and its stored L1 answers (L1 costs $0, never "
                         "re-read); options given here override. A run id, the run's output directory or its "
                         "results.json")
    sc.add_argument("--l1-new", action="store_true",
                    help="with --from-run: also read (L1, paid) the described companies that run has no L1 answer "
                         "for (e.g. after crawl-descriptions filled profiles); everyone else keeps its answer ($0)")
    sc.add_argument("--idea-en", default=None, metavar="TEXT",
                    help="the English sentence the Jev questions use (instead of the local translation); a "
                         "--from-run inherits its base run's and refuses a different one")
    sc.add_argument("--no-page", action="store_true", help="do not write page.html (the result page)")
    sc.add_argument("--rank", choices=RANK_CHOICES, default=None, action=_Given,
                    help="label (default: the L2 label's weight) or ev (the mean probabilities; measured, not the "
                         "default)")
    why_cli.add_screen_arguments(sc, _Given)   # --shells drop|keep, --idea-of RUN_ID (idea optional)
    ondemand_cli.add_screen_flags(sc)   # --fetch-docs / --fetch-time / --fetch-sources (on-demand annual reports)
    sc.set_defaults(given=frozenset())
    ca = sub.add_parser("cards", help="rebuild or reprint the calibration cards of a screen run from its stored "
                        "results (free)")
    ca.add_argument("target", nargs="?", default="latest", metavar="RUN_ID|OUT_DIR|latest",
                    help="the screen run (default: the newest)")
    ca.add_argument("--max", type=nonneg_int, default=CARDS_DEFAULT, metavar="N",
                    help=f"at most N cards (default {CARDS_DEFAULT})")
    ca.add_argument("--out", type=Path, default=None, metavar="DIR",
                    help="write cards.json / cards.md here instead of the run's output directory")
    ca.add_argument("--json", action="store_true", help="print cards.json instead of the Chinese cards")
    an = sub.add_parser("answer", help="record your answers to the calibration cards ('1要a 2不要c 3?'), try the "
                        "rules they point to and screen again from the same run (L1 $0; about $0.01–0.03)")
    an.add_argument("text", nargs="?", default=None, metavar="ANSWERS",
                    help="e.g. '1要a 2不要c 3?' (要a direct, 要b related; 不要 c-k the reason (h buyer, i holding only, j upstream parts, k hardware to operators); ? unsure; "
                         "其余都要 / 其余不要)")
    an.add_argument("--run", default="latest", metavar="RUN_ID|latest",
                    help="the run whose cards you answer (default: the newest run with cards)")
    an.add_argument("--deck", default=None, metavar="DECK_ID|PATH", help="a deck id (deck-<run_id>-<n>) or cards.json")
    an.add_argument("--file", type=Path, default=None, metavar="answers.json",
                    help='answers as {"deck_id": ..., "answers": {"1": {"v": "yes", "chip": "b"}}}')
    an.add_argument("--no-apply", action="store_true",
                    help="only record the answers and show the free re-ranking (no rule trial, no new screen)")
    an.add_argument("--apply-budget", type=nonneg_float, default=APPLY_BUDGET_DEFAULT, metavar="USD",
                    help=f"most the rule trials and the new screen may cost together (default "
                         f"{APPLY_BUDGET_DEFAULT}); a higher estimate stops before any paid call (exit 5)")
    an.add_argument("--undo", type=positive_int, default=None, metavar="N",
                    help="remove answer N (as `sieve show` numbers them) from the calibration file")
    an.add_argument("--json", action="store_true", help="print a JSON summary instead of the Chinese text")
    an.add_argument("--lang", choices=("zh", "en"), default="zh", help="language of the answer error messages")
    an.add_argument("--verbose", action="store_true", help="also print the details: rule ids, keyword weighting, "
                    "the cost breakdown and the rule trial table")
    sv = sub.add_parser("sieve", help="the calibration file (sieve) of an idea (free)")
    svs = sv.add_subparsers(dest="sieve_command", required=True)
    chk = svs.add_parser("check", help="check a sieve: schema, companies, rule texts, keyword document frequency; "
                         "prints the L2 question it gives")
    chk.add_argument("target", nargs="?", default=None, metavar="PATH|IDEA")
    shw = svs.add_parser("show", help="show the sieve of an idea (answers numbered for answer --undo)")
    shw.add_argument("target", metavar="IDEA|PATH")
    shw.add_argument("--json", action="store_true", help="print the sieve file itself")
    why_cli.add_sieve_subparsers(svs)   # sieve new / set / add / remove / pin / unpin / list; check --json / --run
    agent_cli.add_parsers(sub)   # doctor, keys, consent (agent-first operation)
    why_cli.add_parsers(sub)     # why
    ondemand_cli.add_parsers(sub)   # fetch-docs
    add_pack_parser(sub)
    quickstart_cli.add_parsers(sub)   # quickstart, page
    return p


class _Given(argparse.Action):
    """argparse 'store' (with nargs=0: 'store_true') that also records the option's dest in namespace.given.
    screen --from-run inherits the base run's parameter for every option NOT given on the command line."""

    def __init__(self, option_strings, dest, nargs=None, const=None, **kw):
        if nargs == 0 and const is None:
            const = True
        super().__init__(option_strings, dest, nargs=nargs, const=const, **kw)

    def __call__(self, parser, namespace, values, option_string=None):
        setattr(namespace, self.dest, self.const if self.nargs == 0 else values)
        setattr(namespace, "given", frozenset(getattr(namespace, "given", None) or ()) | {self.dest})


def add_sync_bse_parser(sub) -> None:
    """sync-bse: Indian annual reports from BSE (personal use; official-private)."""
    sb = sub.add_parser("sync-bse", help="fetch Indian annual reports (MD&A / company overview) from BSE into "
                        "documents (personal use)")
    sb.add_argument("--limit", type=positive_int, default=None, metavar="N", help="at most N companies this run")
    sb.add_argument("--min-mcap", type=nonneg_float, default=None, metavar="USD",
                    help="only companies with at least this market cap in USD")
    sb.add_argument("--codes", type=parse_list, default=None, metavar="A,B",
                    help="only these companies (BSE scrip codes like 500325, or symbols like RELIANCE / NSE:TCS)")
    sb.add_argument("--refresh", action="store_true", help="re-fetch companies that already have a document")
    sb.add_argument("--workers", type=int, default=BSE_DEFAULT_WORKERS, metavar="N",
                    help=f"concurrent PDF downloads (default {BSE_DEFAULT_WORKERS}, clamped to 1..{BSE_MAX_WORKERS}); "
                         f"request starts stay >= 1 s apart per host")
    sb.add_argument("--after-block", action="store_true",
                    help=f"run even though the last run was blocked less than {COOLDOWN_HOURS} h ago")


def parse_date(text: str) -> dt.date:
    """'YYYY-MM-DD' -> date (argparse type)."""
    try:
        return dt.date.fromisoformat(str(text).strip())
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected a date YYYY-MM-DD, got {text!r}") from None


def nonneg_float(text: str) -> float:
    """A finite number >= 0 (rejects nan, inf and negatives)."""
    try:
        v = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a number: {text!r}") from None
    if not math.isfinite(v) or v < 0:
        raise argparse.ArgumentTypeError(f"must be a finite number >= 0, got {text!r}")
    return v


def _int_at_least(text: str, low: int) -> int:
    try:
        v = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not an integer: {text!r}") from None
    if v < low:
        raise argparse.ArgumentTypeError(f"must be >= {low}, got {v}")
    return v


def positive_int(text: str) -> int:
    return _int_at_least(text, 1)


def nonneg_int(text: str) -> int:
    return _int_at_least(text, 0)


def parse_list(text: str) -> list[str]:
    """'US, JP' -> ['US', 'JP'] (order kept, empties dropped). Empty -> argparse error."""
    out = [t.strip() for t in text.split(",") if t.strip()]
    if not out:
        raise argparse.ArgumentTypeError("expected a comma-separated list, e.g. US,JP")
    return out


def parse_terms(text: str) -> list[str]:
    """Keyword list: split at ASCII or full-width commas, 、 and semicolons; order kept, empties dropped."""
    import re
    out = [t.strip() for t in re.split(r"[,，、;；]", text) if t.strip()]
    if not out:
        raise argparse.ArgumentTypeError("expected a comma-separated list of terms")
    return out


def parse_forms(text: str) -> tuple[str, ...]:
    """'10-K, 20-F' -> ('10-K', '20-F'); upper-cased, de-duplicated, order kept. Empty -> argparse error."""
    forms = tuple(dict.fromkeys(f.strip().upper() for f in (text or "").split(",") if f.strip()))
    if not forms:
        raise argparse.ArgumentTypeError("--forms needs at least one form, e.g. 10-K,20-F")
    return forms


def clamp_rate(rate: float, maximum: float = CRAWL_MAX_RATE) -> float:
    """Enforce the rate ceiling (crawl-descriptions: 4.4 req/s); warn on stderr when clamping (also for inf).
    NaN or <= 0 raise ValueError (NaN would otherwise switch the limiter off)."""
    rate = float(rate)
    if math.isnan(rate) or rate <= 0:
        raise ValueError("--rate must be a number > 0")
    if rate > maximum:
        print(f"warning: --rate {rate} exceeds {maximum} req/s; clamped to {maximum}", file=sys.stderr)
        return maximum
    return rate


def clamp_workers(workers: Any) -> int:
    """crawl-descriptions --workers: default 3, clamped to 1..4 with a warning on stderr when clamped."""
    if workers is None:
        return CRAWL_DEFAULT_WORKERS
    n = int(workers)
    clamped = max(1, min(n, CRAWL_MAX_WORKERS))
    if clamped != n:
        print(f"warning: --workers {n} outside 1..{CRAWL_MAX_WORKERS}; clamped to {clamped}", file=sys.stderr)
    return clamped


def make_client(cfg, rate: float = MAX_RATE):
    """Shared polite client; min interval is never below the configured one nor below 1/rate."""
    from .http import Client
    return Client(user_agent=cfg.user_agent, min_interval_s=max(cfg.min_interval_s, 1.0 / rate),
                  timeout_s=cfg.timeout_s)


def make_crawl_client(cfg, rate: float = CRAWL_MAX_RATE):
    """Client for crawl-descriptions: min interval exactly 1/rate (4.4 req/s -> ~0.227 s), never faster than
    CRAWL_MAX_RATE, and at most CRAWL_MAX_STARTS_PER_S starts in any rolling second (margin for network jitter).
    JEVSCREEN_MIN_INTERVAL_S (the generic 1 s default) does not apply here; --rate is the knob. Socket timeout is at
    most CRAWL_TIMEOUT_S so a stalled page cannot hold a SIGTERM drain for a minute."""
    from .http import Client
    rate = float(rate)
    rate = min(rate, CRAWL_MAX_RATE) if rate > 0 else CRAWL_MAX_RATE   # NaN / <= 0 fall back to the ceiling
    try:
        timeout = float(cfg.timeout_s)
    except (TypeError, ValueError):
        timeout = CRAWL_TIMEOUT_S
    timeout = min(timeout, CRAWL_TIMEOUT_S) if timeout > 0 else CRAWL_TIMEOUT_S
    return Client(user_agent=cfg.user_agent, min_interval_s=1.0 / rate, timeout_s=timeout,
                  max_per_window=CRAWL_MAX_STARTS_PER_S, window_s=1.0)


def make_sec_client(cfg):
    """Client for www.sec.gov + data.sec.gov (the adapter passes one rate_key for both). None if no User-Agent."""
    from .http import Client
    ua = cfg.sec_user_agent()
    if not ua:
        return None
    return Client(user_agent=ua, min_interval_s=SEC_MIN_INTERVAL_S, timeout_s=cfg.timeout_s)


def _redact(text: str, secrets: Sequence[Any]) -> str:
    """Replace each secret, verbatim or JSON-escaped (a non-ASCII UA under ensure_ascii), by its placeholder.

    A plain string is the SEC User-Agent ('<sec-user-agent>'); a (secret, placeholder) pair names its own
    placeholder (API keys: '<edinet-api-key>', '<opendart-api-key>')."""
    from .config import redact
    plain = [x for x in secrets if not isinstance(x, tuple)]
    text = redact(text, plain, REDACTED_SEC_UA)
    for x in secrets:
        if isinstance(x, tuple) and x[0]:
            text = redact(text, [x[0]], x[1])
    return text


def _emit(obj: Any, secrets: Sequence[str] = ()) -> None:
    print(_redact(json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=True, default=str), secrets))


def _locked(command: str, e: Exception) -> int:
    """StoreLocked: another process holds the DuckDB lock (e.g. a long write). Clear message, exit 3."""
    _emit({"command": command, "status": "locked", "db_error": str(e)[:500],
           "hint": "another jevscreen process holds the database lock; retry when it finishes"})
    print(f"error: {command}: the database is locked by another process (DuckDB allows one writer and then "
          "refuses other connections). Try again when that process finishes or pauses between batches.",
          file=sys.stderr)
    return EXIT_LOCKED


def _read_session(cfg):
    """Read-only session for inspection commands. A missing database is created first (short write session)."""
    from . import store
    if not cfg.db_path.exists():
        with store.session(cfg, wait_s=WRITE_WAIT_S):
            pass
    return store.session(cfg, read_only=True, wait_s=READ_WAIT_S)


def _was_blocked(result: Any) -> bool:
    """Adapters may either raise Blocked or return a summary saying the run stopped because it was blocked."""
    if not isinstance(result, dict):
        return False
    if result.get("blocked"):
        return True
    for key in ("status", "stopped", "stop_reason", "stopped_reason"):
        val = result.get(key)
        if isinstance(val, str) and val.lower().startswith("blocked"):
            return True
    return False


def _result_status(result: Any) -> str:
    """'blocked' when the result says so, else the adapter's own 'status' when it is a known one, else 'ok'."""
    if _was_blocked(result):
        return "blocked"
    val = result.get("status") if isinstance(result, dict) else None
    return val.lower() if isinstance(val, str) and val.lower() in RESULT_EXIT else "ok"


def _mark_block(cfg, command: str, e: Any = None, secrets: Sequence[Any] = ()) -> None:
    from . import guard
    url = getattr(e, "url", None)
    guard.mark_blocked(cfg, command, url=_redact(url, secrets) if url and secrets else url,
                       status=getattr(e, "status", None),
                       reason=getattr(e, "reason", None) or ("result_blocked" if e is None else None))


def _after_run(cfg, command: str, status: str) -> None:
    """Cooldown marker bookkeeping: set it on 'blocked', clear it after a clean 'ok' run."""
    from . import guard
    if status == "blocked":
        if guard.recent_block_marker(cfg, command, COOLDOWN_HOURS) is None:
            _mark_block(cfg, command)
    elif status == "ok":
        guard.clear_blocked(cfg, command)


def _run_count(con) -> int:
    return con.execute("SELECT count(*) FROM runs").fetchone()[0]


def _insert_run(con, command: str, started, status: str, requests: int | None, note: Any,
                secrets: Sequence[str] = ()) -> None:
    from . import store
    text = note if isinstance(note, str) or note is None else json.dumps(note, default=str, sort_keys=True)
    text = _redact(text or "", secrets)[:2000] or None
    con.execute("INSERT INTO runs VALUES (?,?,?,?,?,?,?)",
                [uuid.uuid4().hex, command, started, store.now_utc(), status, requests, text])


def _journal(con, command: str, started, status: str, requests: int | None, note: Any, before: int) -> None:
    """Add a runs row unless the adapter already wrote its own during this command (avoid duplicates)."""
    if _run_count(con) > before:
        return
    _insert_run(con, command, started, status, requests, note)


def _journal_detached(cfg, command: str, started, status: str, requests: int | None, note: Any,
                      secrets: Sequence[str] = ()) -> None:
    """Journal in a short session unless the adapter already wrote a run under one of the command's names since
    `started`. An adapter row left 'running' (no finished_at: Ctrl-C or a crash outside the adapter's own handling)
    is closed with this status instead. A lock that outlasts the wait only costs the journal row (warned on
    stderr), never the exit code."""
    from . import store
    names = RUN_COMMANDS.get(command, (command,))
    try:
        with store.session(cfg, wait_s=READ_WAIT_S) as con:
            rows = con.execute(f"SELECT run_id, status, finished_at FROM runs "
                               f"WHERE command IN ({','.join('?' * len(names))}) AND started_at >= ?",
                               [*names, started]).fetchall()
            if not rows:
                _insert_run(con, command, started, status, requests, note, secrets)
            for run_id, st, finished in rows:
                if st == "running" and finished is None:
                    text = note if isinstance(note, str) or note is None else json.dumps(note, default=str)
                    con.execute("UPDATE runs SET finished_at = ?, status = ?, requests = coalesce(?, requests), "
                                "note = coalesce(note, ?) WHERE run_id = ?",
                                [store.now_utc(), status, requests, _redact(text or "", secrets)[:2000] or None,
                                 run_id])
    except store.StoreLocked:
        print(f"warning: {command}: database locked; run not journaled ({status})", file=sys.stderr)


def recent_block(con, command: str, hours: float = COOLDOWN_HOURS) -> dict[str, Any] | None:
    """The latest run of `command` if it was 'blocked' less than `hours` ago, else None."""
    names = RUN_COMMANDS.get(command, (command,))
    row = con.execute(f"SELECT started_at, status, note FROM runs WHERE command IN ({','.join('?' * len(names))}) "
                      "ORDER BY started_at DESC LIMIT 1", list(names)).fetchone()
    if not row or row[1] != "blocked" or row[0] is None:
        return None
    from . import store
    if store.now_utc() - row[0] >= dt.timedelta(hours=hours):
        return None
    return {"blocked_at": row[0], "note": row[2]}


def _cooldown_refusal(command: str, cfg, override: bool, secrets: Sequence[Any] = ()) -> int | None:
    """Rule: no request within COOLDOWN_HOURS of a blocked run unless explicitly overridden. Returns exit 2 or None.

    The on-disk marker is checked first (it needs no DuckDB lock and survives a failed journal write)."""
    from . import guard
    if override:
        return None
    hit = guard.recent_block_marker(cfg, command, COOLDOWN_HOURS)
    if hit is None:
        with _read_session(cfg) as con:
            hit = recent_block(con, command)
    if hit is None:
        return None
    _emit({"command": command, "status": "cooldown", "blocked_at": hit["blocked_at"], "note": hit["note"],
           "hint": f"last run was blocked less than {COOLDOWN_HOURS} h ago; wait, or pass --after-block"}, secrets)
    print(f"error: {command}: refusing to run within {COOLDOWN_HOURS} h of a blocked run (use --after-block)",
          file=sys.stderr)
    return EXIT_BLOCKED


def _consent_refusal(command: str, cfg, source_id: str) -> int | None:
    """Rule: a gray-private source sends no request (and reads no file) without a recorded 'yes' for gray-sources
    (consent.require_gray_sources). Returns exit 1 with a JSON 'consent_required' line, or None."""
    from . import consent
    try:
        consent.require_gray_sources(cfg, source_id)
    except consent.ConsentRequired as e:
        _emit({"command": command, "status": "consent_required", "source_id": e.source_id, "consent": e.state,
               "hint": "ask the human (AGENTS.md step 5), then record the answer: "
                       "jevscreen consent set gray-sources yes|no"})
        print(f"error: {command}: {e}", file=sys.stderr)
        return EXIT_ERROR
    return None


def _run(command: str, cfg, work: Callable[[Any], Any], client=None) -> int:
    """Open the store, run one adapter call, journal it, print its JSON summary, map Blocked -> exit 2."""
    from . import store
    from .http import Blocked
    with store.session(cfg, wait_s=WRITE_WAIT_S) as con:
        started, before = store.now_utc(), _run_count(con)
        requests = lambda: client.requests_made if client is not None else None  # noqa: E731
        try:
            result = work(con)
        except Blocked as e:
            _mark_block(cfg, command, e)
            _journal(con, command, started, "blocked", requests(), str(e), before)
            _emit({"command": command, "status": "blocked", "reason": e.reason, "http_status": e.status, "url": e.url})
            return EXIT_BLOCKED
        except store.StoreLocked:
            raise
        except Exception as e:  # journal, report, non-zero exit
            _journal(con, command, started, "error", requests(), f"{type(e).__name__}: {e}", before)
            print(f"error: {command}: {type(e).__name__}: {e}", file=sys.stderr)
            return EXIT_ERROR
        except BaseException as e:  # Ctrl-C: journal (unless the adapter did), then propagate
            _journal(con, command, started, "interrupted", requests(), type(e).__name__, before)
            raise
        status = _result_status(result)
        _journal(con, command, started, status, requests(), result, before)
    _after_run(cfg, command, status)
    _emit({"command": command, "status": status, "result": result})
    return RESULT_EXIT[status]


class BlockNotice:
    """Holds the first block an adapter reports through its on_blocked callback (before any database wait).

    From then on SIGTERM is ignored (main thread), so nothing can turn the blocked run into 'interrupted'."""

    def __init__(self) -> None:
        self.url = self.status = self.reason = None
        self.seen = False

    def __call__(self, e: Any) -> None:
        if not self.seen:
            self.url, self.status, self.reason = getattr(e, "url", None), getattr(e, "status", None), \
                getattr(e, "reason", None)
            self.seen = True
        if threading.current_thread() is threading.main_thread():
            with contextlib.suppress(ValueError, OSError):
                signal.signal(signal.SIGTERM, signal.SIG_IGN)


def _run_detached(command: str, cfg, work: Callable[..., Any], client=None, secrets: Sequence[str] = (),
                  blocked_notice: bool = False, notice: BlockNotice | None = None) -> int:
    """Like _run, but the adapter manages its own short store sessions: no connection is held while it works.

    Journaling (only when the adapter did not journal itself) uses a separate short session afterwards.
    With `notice` (fed by the adapter's on_blocked callback), a block always ends with the BLOCKED line (when
    blocked_notice) and exit 2, even if the adapter's final write, the journal or a Ctrl-C fails afterwards.
    """
    from . import store
    from .http import Blocked
    started = store.now_utc()
    requests = lambda: client.requests_made if client is not None else None  # noqa: E731

    def blocked_exit(url: Any, status: Any, reason: Any, note: Any, result: Any = None) -> int:
        # every step is best effort: the BLOCKED line and exit 2 must survive a lock, an error or a Ctrl-C
        for step in (lambda: _after_run(cfg, command, "blocked"),
                     lambda: _journal_detached(cfg, command, started, "blocked", requests(), note, secrets),
                     lambda: _emit({"command": command, "status": "blocked", "reason": reason, "http_status": status,
                                    "url": url, **({"result": result} if result is not None else {})}, secrets)):
            try:
                step()
            except (Exception, KeyboardInterrupt) as e:
                with contextlib.suppress(Exception):
                    print(_redact(f"warning: {command}: after the block: {type(e).__name__}: {e}", secrets)[:500],
                          file=sys.stderr)
        if blocked_notice:
            print(_redact(blocked_line(url, status, reason), secrets), flush=True)
        return EXIT_BLOCKED

    def from_notice(e: BaseException) -> int:
        return blocked_exit(notice.url, notice.status, notice.reason,
                            f"blocked: {notice.reason} (status={notice.status}) at {notice.url}; "
                            f"then {type(e).__name__}: {e}")

    try:
        result = work()
    except Blocked as e:
        _mark_block(cfg, command, e, secrets)
        return blocked_exit(e.url, e.status, e.reason, str(e))
    except store.StoreLocked as e:
        if notice is not None and notice.seen:
            return from_notice(e)
        return _locked(command, e)
    except Exception as e:
        if notice is not None and notice.seen:
            return from_notice(e)
        _journal_detached(cfg, command, started, "error", requests(), f"{type(e).__name__}: {e}", secrets)
        print(_redact(f"error: {command}: {type(e).__name__}: {e}", secrets), file=sys.stderr)
        return EXIT_ERROR
    except BaseException as e:  # Ctrl-C: journal (unless the adapter did), then propagate
        if notice is not None and notice.seen and isinstance(e, KeyboardInterrupt):
            return from_notice(e)
        _journal_detached(cfg, command, started, "interrupted", requests(), type(e).__name__, secrets)
        raise
    status = _result_status(result)
    if status == "blocked" or (notice is not None and notice.seen):
        r = result if isinstance(result, dict) else {}
        if notice is not None and notice.seen:
            url, st, reason = notice.url, notice.status, notice.reason
        else:
            url = r.get("blocked_url") or r.get("blocked_at")
            st = r.get("blocked_status")
            reason = r.get("blocked_reason") or r.get("stopped_reason") or r.get("stopped")
        return blocked_exit(url, st, reason, result, result)
    _after_run(cfg, command, status)
    _journal_detached(cfg, command, started, status, requests(), result, secrets)
    _emit({"command": command, "status": status, "result": result}, secrets)
    return RESULT_EXIT[status]


class Terminated(KeyboardInterrupt):
    """SIGTERM converted to the Ctrl-C path, so adapters flush and journal 'interrupted' exactly as on Ctrl-C."""


@contextlib.contextmanager
def sigterm_as_interrupt():
    """While active, the first SIGTERM raises Terminated (a KeyboardInterrupt) in the main thread; later SIGTERMs
    are ignored so the graceful flush can finish (SIGKILL still works). The previous handler is restored after."""
    if threading.current_thread() is not threading.main_thread():
        yield
        return

    def handler(signum, frame):
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        raise Terminated("SIGTERM")

    previous = signal.signal(signal.SIGTERM, handler)
    try:
        yield
    finally:
        signal.signal(signal.SIGTERM, previous)


def _interrupted_exit(command: str, e: BaseException) -> int:
    _emit({"command": command, "status": "interrupted", "signal": "SIGTERM" if isinstance(e, Terminated) else "SIGINT"})
    return EXIT_INTERRUPTED


def mark_abandoned(cfg, command: str) -> int:
    """Mark runs of `command` still 'running' as 'abandoned'. Call only while holding the command's budget lock:
    then no live process of this command can own such a row. Returns the number of rows changed (0 if locked)."""
    from . import store
    names = RUN_COMMANDS.get(command, (command,))
    try:
        with store.session(cfg, wait_s=READ_WAIT_S) as con:
            ph = ",".join("?" * len(names))
            n = con.execute(f"SELECT count(*) FROM runs WHERE command IN ({ph}) AND status = 'running'",
                            list(names)).fetchone()[0]
            if n:
                con.execute(f"UPDATE runs SET status = 'abandoned', finished_at = coalesce(finished_at, ?), "
                            f"note = coalesce(note, 'abandoned: process gone (no live budget lock)') "
                            f"WHERE command IN ({ph}) AND status = 'running'", [store.now_utc(), *names])
                print(f"warning: {command}: marked {n} earlier run(s) left 'running' as 'abandoned'",
                      file=sys.stderr)
            return n
    except store.StoreLocked:
        print(f"warning: {command}: database locked; earlier 'running' rows not checked", file=sys.stderr)
        return 0


def blocked_line(url: Any, status: Any, reason: Any) -> str:
    return f"BLOCKED url={url} status={status} reason={reason}"


def _with_budget(command: str, cfg, fn: Callable[[], int]) -> int:
    """Run fn while holding the command's rate-budget lock; another process holding it -> exit 4, no request."""
    import contextlib
    from . import guard
    budget = guard.RATE_BUDGETS.get(command) or CLI_RATE_BUDGETS.get(command, command)
    with contextlib.ExitStack() as stack:
        try:
            stack.enter_context(guard.budget_lock(cfg, budget))
        except guard.Busy as e:
            _emit({"command": command, "status": "busy", "budget": budget,
                   "hint": "another jevscreen process is already using this host's rate budget; wait for it"})
            print(f"error: {command}: {e}", file=sys.stderr)
            return EXIT_BUSY
        return fn()


def cmd_init(args, cfg) -> int:
    from . import store
    with store.session(cfg, wait_s=WRITE_WAIT_S) as con:
        tables = [r[0] for r in con.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = 'main' ORDER BY 1").fetchall()]
    _emit({"command": "init", "status": "ok", "db": str(cfg.db_path), "tables": tables})
    return EXIT_OK


def cmd_refresh_universe(args, cfg) -> int:
    refused = _consent_refusal("refresh-universe", cfg, "tradingview_scanner")
    if refused is not None:
        return refused
    def go() -> int:
        refused = _cooldown_refusal("refresh-universe", cfg, getattr(args, "after_block", False))
        if refused is not None:
            return refused
        client = make_client(cfg)
        def work(con):
            mod = importlib.import_module(SCANNER)
            return mod.refresh_universe(cfg, con, client, with_history=args.with_history,
                                        history_batch=args.history_batch)
        return _run("refresh-universe", cfg, work, client)
    return _with_budget("refresh-universe", cfg, go)


def cmd_import_fd(args, cfg) -> int:
    refused = _consent_refusal("import-fd", cfg, "financedatabase_local")
    if refused is not None:
        return refused
    def work(con):
        mod = importlib.import_module(FD_LOCAL)
        return mod.import_descriptions(cfg, con, fd_duckdb=args.fd_duckdb)
    return _run("import-fd", cfg, work)


def cmd_crawl_descriptions(args, cfg) -> int:
    refused = _consent_refusal("crawl-descriptions", cfg, "tradingview_profile")
    if refused is not None:
        return refused
    try:
        rate = clamp_rate(args.rate, CRAWL_MAX_RATE)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return EXIT_ERROR
    workers = clamp_workers(getattr(args, "workers", None))
    def go() -> int:
        refused = _cooldown_refusal("crawl-descriptions", cfg, getattr(args, "after_block", False))
        if refused is not None:
            return refused
        mark_abandoned(cfg, "crawl-descriptions")
        client = make_crawl_client(cfg, rate)
        notice = BlockNotice()
        def work():
            mod = importlib.import_module(PROFILES)
            extra = {k: v for k, v in (("countries", getattr(args, "countries", None)),
                                       ("min_mcap_usd", getattr(args, "min_mcap", None))) if v is not None}
            return mod.crawl(cfg, client, limit=args.limit, only_missing=not args.all, on_blocked=notice,
                             workers=workers, after_block=bool(getattr(args, "after_block", False)), **extra)
        try:
            with sigterm_as_interrupt():
                return _run_detached("crawl-descriptions", cfg, work, client, blocked_notice=True, notice=notice)
        except Terminated as e:
            return _interrupted_exit("crawl-descriptions", e)
    return _with_budget("crawl-descriptions", cfg, go)


def cmd_sync_sec(args, cfg) -> int:
    client = make_sec_client(cfg)
    if client is None:
        print("error: sync-sec: no SEC User-Agent configured. Put '<name> <email>' in "
              f"{cfg.home / 'sec_user_agent'} (git-ignored) or set JEVSCREEN_SEC_USER_AGENT.", file=sys.stderr)
        return EXIT_ERROR
    def go() -> int:
        refused = _cooldown_refusal("sync-sec", cfg, getattr(args, "after_block", False))
        if refused is not None:
            return refused
        mark_abandoned(cfg, "sync-sec")
        def work():
            mod = importlib.import_module(SEC_EDGAR)
            return mod.sync(cfg, client, limit=args.limit, forms=args.forms, refresh=args.refresh)
        try:
            with sigterm_as_interrupt():
                return _run_detached("sync-sec", cfg, work, client, secrets=(client.user_agent,))
        except Terminated as e:
            return _interrupted_exit("sync-sec", e)
    return _with_budget("sync-sec", cfg, go)


def _official_key(command: str, cfg) -> tuple[bool, str | None]:
    """(ok, key) for an official-document command. Commands without a key -> (True, None). A missing key prints
    where to register and where to put it (never any key value) -> (False, None)."""
    _module, method, _placeholder, env, filename, register = OFFICIAL_SYNC[command]
    if method is None:
        return True, None
    key = getattr(cfg, method)()
    if key:
        return True, key
    print(f"error: {command}: no API key configured. Register (free) at {register} and put the key in "
          f"{cfg.home / filename} (git-ignored; never commit it) or set {env}.", file=sys.stderr)
    return False, None


def cmd_sync_official(args, cfg) -> int:
    """sync-cninfo / sync-edinet / sync-dart: same conventions as sync-sec (budget lock, cooldown, SIGTERM, exit
    codes). The adapter builds its own requests on the polite client passed here and manages its short sessions."""
    command = args.command
    module, _method, placeholder, *_ = OFFICIAL_SYNC[command]
    ok, key = _official_key(command, cfg)
    if not ok:
        return EXIT_ERROR
    secrets: tuple = ((key, placeholder),) if key else ()

    def go() -> int:
        refused = _cooldown_refusal(command, cfg, getattr(args, "after_block", False), secrets)
        if refused is not None:
            return refused
        mark_abandoned(cfg, command)
        from .http import Client
        client = Client(user_agent=cfg.user_agent, min_interval_s=OFFICIAL_MIN_INTERVAL_S[command],
                        timeout_s=cfg.timeout_s)
        kwargs: dict[str, Any] = {"limit": args.limit, "codes": args.codes, "refresh": args.refresh,
                                  "min_mcap_usd": args.min_mcap}
        optional: dict[str, Any] = {}
        if command == "sync-cninfo":
            kwargs["kind"] = args.kind
            # fast mode (default): bulk listing on 'cninfo' + PDF workers on 'cninfo-static'
            workers = getattr(args, "workers", None)
            workers = CNINFO_DEFAULT_WORKERS if workers is None else max(1, min(int(workers), CNINFO_MAX_WORKERS))
            optional.update(workers=workers, since=getattr(args, "since", None),
                            per_company=bool(getattr(args, "per_company", False)))
        elif command == "sync-bse":
            workers = getattr(args, "workers", None)
            optional["workers"] = BSE_DEFAULT_WORKERS if workers is None else max(1, min(int(workers), BSE_MAX_WORKERS))

        if command == "sync-mops":
            optional.update(mode=getattr(args, "mode", "basic"), full_pass=bool(getattr(args, "full_pass", False)))
        notice = BlockNotice()
        used: dict[str, Any] = {}

        def work():
            mod = importlib.import_module(module)
            if _accepts(mod.sync, "on_blocked"):     # hear about a block before any database wait (cninfo)
                kwargs["on_blocked"] = notice
                used["notice"] = notice
            for name, value in optional.items():   # only to an adapter that takes them
                if _accepts(mod.sync, name):
                    kwargs[name] = value
            return mod.sync(cfg, client, **kwargs)
        try:
            with sigterm_as_interrupt():
                return _run_detached(command, cfg, work, client, secrets=secrets, notice=_LazyNotice(used))
        except Terminated as e:
            return _interrupted_exit(command, e)
    return _with_budget(command, cfg, go)


def _accepts(fn: Any, name: str) -> bool:
    """True when fn names `name` as a parameter (a **kwargs catch-all does not count)."""
    import inspect
    try:
        p = inspect.signature(fn).parameters.get(name)
    except (TypeError, ValueError):
        return False
    return p is not None and p.kind in (p.POSITIONAL_OR_KEYWORD, p.KEYWORD_ONLY)


class _LazyNotice:
    """The BlockNotice handed to the adapter, or an unseen stand-in when the adapter takes no on_blocked callback."""

    def __init__(self, used: dict[str, Any]) -> None:
        self._used = used

    def __getattr__(self, name: str) -> Any:
        n = self._used.get("notice")
        if n is None:
            if name == "seen":
                return False
            if name in ("url", "status", "reason"):
                return None
            raise AttributeError(name)
        return getattr(n, name)


def cmd_keywords(args, cfg) -> int:
    """Print the local model's translation of an idea (idea_en, keywords per language). Exit 1 when unavailable."""
    try:
        mod = importlib.import_module(KEYWORDS)
        result = mod.generate(cfg, args.idea, refresh=args.refresh)
    except KeyboardInterrupt:
        return EXIT_INTERRUPTED
    except Exception as e:  # noqa: BLE001 - KeywordsUnavailable, ImportError or a local-model failure
        _emit({"command": "keywords", "status": "unavailable", "error": f"{type(e).__name__}: {str(e)[:500]}"})
        print(f"error: keywords: {type(e).__name__}: {str(e)[:300]}", file=sys.stderr)
        return EXIT_ERROR
    _emit({"command": "keywords", "status": "ok", "idea": args.idea, **(result if isinstance(result, dict) else {})})
    return EXIT_OK


def cmd_coverage(args, cfg) -> int:
    from . import coverage
    with _read_session(cfg) as con:
        rep = coverage.report(con)
    if args.json:
        _emit(rep)
    else:
        print(coverage.format_report(rep))
    return EXIT_OK


def cmd_status(args, cfg) -> int:
    with _read_session(cfg) as con:
        tables = [r[0] for r in con.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'main' AND table_type = 'BASE TABLE' ORDER BY 1").fetchall()]
        counts = {t: con.execute(f'SELECT count(*) FROM "{t}"').fetchone()[0] for t in tables}
        counts["universe (view)"] = con.execute("SELECT count(*) FROM universe").fetchone()[0]
        runs = [dict(zip(("command", "started_at", "finished_at", "status", "requests", "note"), r))
                for r in con.execute("SELECT command, started_at, finished_at, status, requests, note FROM runs "
                                     "ORDER BY started_at DESC LIMIT 10").fetchall()]
        documents = identifiers = None   # None: table absent (store created before it existed, opened read-only)
        if "documents" in tables:
            documents = {
                "rows": counts["documents"],
                "with_text": con.execute("SELECT count(*) FROM documents WHERE text_path IS NOT NULL").fetchone()[0],
                "companies": con.execute("SELECT count(DISTINCT company_key) FROM documents "
                                         "WHERE text_path IS NOT NULL").fetchone()[0],
                "by_source_form": {f"{s}/{f}": n for s, f, n in con.execute(
                    "SELECT coalesce(source_id, '?'), coalesce(form, '?'), count(*) FROM documents "
                    "GROUP BY 1, 2 ORDER BY 1, 2").fetchall()},
                "companies_with_text_by_source": dict(con.execute(
                    "SELECT coalesce(source_id, '?'), count(DISTINCT coalesce(company_key, security_id, cik, doc_id)) "
                    "FROM documents WHERE text_path IS NOT NULL GROUP BY 1 ORDER BY 1").fetchall()),
            }
        if "identifiers" in tables:
            identifiers = {
                "rows": counts["identifiers"],
                "by_type": dict(con.execute("SELECT coalesce(id_type, '?'), count(*) FROM identifiers "
                                            "GROUP BY 1 ORDER BY 1").fetchall()),
            }
    for r in runs:
        if r["note"] and len(r["note"]) > 200:
            r["note"] = r["note"][:200] + "..."
    _emit({"command": "status", "db": str(cfg.db_path), "rows": counts, "documents": documents,
           "identifiers": identifiers, "last_runs": runs})
    return EXIT_OK


SCREEN_EXIT = {"ok": EXIT_OK, "partial": EXIT_OK, "dry_run": EXIT_OK, "budget_exhausted": EXIT_BUDGET,
               "jev_unavailable": EXIT_JEV_UNAVAILABLE, "jev_busy": EXIT_BUSY}


def _screen_arg(args, dest: str, value: Any, unset: Any) -> Any:
    """The value screen() gets for an inheritable option: `unset` (screen.UNSET: the base run's value with
    --from-run, else screen.SCREEN_DEFAULTS, which equal the parser defaults) when the option was not given."""
    given = getattr(args, "given", None)
    if given is None:            # a hand-built Namespace: every value counts as given
        return unset if value is None and dest in ("reads", "rank") else value
    return value if dest in given else unset


def _jev_exit(e: BaseException) -> int | None:
    """The exit code of a Jev error that escaped screen() / a trial client (None: not a Jev error)."""
    from . import screen
    if screen._is_error(e, "JevBusy"):
        return EXIT_BUSY
    if screen._is_error(e, "JevUnavailable"):
        return EXIT_JEV_UNAVAILABLE
    if screen._is_error(e, "BudgetExceeded"):
        return EXIT_BUDGET
    return None


def cmd_screen(args, cfg) -> int:
    """Exit 0 ok / partial / dry run, 1 bad parameter (e.g. unknown country, unknown --from-run, invalid --sieve),
    3 database locked, 4 another process is using Jev, 5 budget exhausted before L1 finished (partial report still
    written), 6 Jev unavailable (missing key, 401/402/403), 130 interrupted. The OpenRouter key is never printed.
    Options not given on the command line come from the --from-run base run (else the defaults). At the end the
    calibration cards (cards.json / cards.md, free) are written into the output directory and printed, unless
    --cards 0 or a dry run."""
    from . import report, screen
    u = screen.UNSET
    try:
        idea = why_cli.screen_idea(cfg, args)
        result = screen.screen(
            cfg, idea, min_mcap_usd=_screen_arg(args, "min_mcap", args.min_mcap, u),
            min_avg_volume=_screen_arg(args, "min_volume", args.min_volume, u),
            countries=_screen_arg(args, "countries", args.countries, u),
            max_out=_screen_arg(args, "max_out", args.max_out, u), budget_usd=_screen_arg(args, "budget", args.budget, u),
            l2_max=_screen_arg(args, "l2_max", args.l2_max, u), keywords=_screen_arg(args, "keywords", args.keywords, u),
            dry_run=args.dry_run, retry_uncertain=args.retry_uncertain,
            keywords_zh=_screen_arg(args, "keywords_zh", args.keywords_zh, u),
            keywords_ja=_screen_arg(args, "keywords_ja", args.keywords_ja, u),
            keywords_ko=_screen_arg(args, "keywords_ko", args.keywords_ko, u),
            translate=_screen_arg(args, "no_translate", not args.no_translate, u),
            reads=_screen_arg(args, "reads", getattr(args, "reads", None), u),
            read_offset=getattr(args, "read_offset", 0), from_run=getattr(args, "from_run", None),
            l1_new=bool(getattr(args, "l1_new", False)), sieve=getattr(args, "sieve", "auto"), rank=_screen_arg(args, "rank", getattr(args, "rank", None), u),
            shells=_screen_arg(args, "shells", getattr(args, "shells", None), u),
            **({"idea_en": args.idea_en} if getattr(args, "idea_en", None) else {}))
    except ValueError as e:
        print(f"error: screen: {e}", file=sys.stderr)
        return EXIT_ERROR
    except KeyboardInterrupt:
        print("error: screen: interrupted (paid answers so far are in the ledger; a rerun reuses them)",
              file=sys.stderr)
        return EXIT_INTERRUPTED
    except Exception as e:  # noqa: BLE001 - only Jev errors that escaped screen() are mapped here
        code = _jev_exit(e)
        if code is None:
            raise
        print({EXIT_BUSY: "error: screen: another process is using Jev; try again when it finishes",
               EXIT_JEV_UNAVAILABLE: f"error: screen: Jev unavailable: {type(e).__name__}",
               EXIT_BUDGET: "error: screen: budget exhausted"}[code], file=sys.stderr)
        return code
    text = report.format_console(result)
    print(ondemand_cli.dry_run_text(args, cfg, result, text) if args.dry_run else text, flush=True)
    phase1_status = result["status"]
    if not args.dry_run and ondemand_cli.fetch_mode(args) == "auto":
        try:     # phase 2: fetch missing annual reports, update the same report (never changes the exit code)
            result = ondemand_cli.fetch_phase(args, cfg, result)
        except KeyboardInterrupt:
            print("error: screen: annual-report fetch interrupted; the first report is kept", file=sys.stderr)
            return EXIT_INTERRUPTED
    n_cards = getattr(args, "cards", 0)
    if not args.dry_run and n_cards and result["status"] in ("ok", "partial"):
        deck, path = _write_run_cards(cfg, result, n_cards)
        if deck is not None:
            print()
            print(_deck_text(deck, path))
        if not getattr(args, "no_page", False):
            quickstart_cli.write_after_cards(cfg, result, deck)
    return SCREEN_EXIT.get(phase1_status, EXIT_ERROR)


# ---------------------------------------------------------------------------------------------------------------
# Calibration: cards / answer / sieve (see calib.py and docs/DATA_RULES.md "Calibration (sieve)")

def _load_result(out_dir: Path) -> dict[str, Any]:
    p = Path(out_dir) / "results.json"
    try:
        res = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise ValueError(f"读不了筛选结果 {p}（{type(e).__name__}）") from None
    if not isinstance(res, dict) or not res.get("run_id"):
        raise ValueError(f"{p} 不是 jevscreen screen 的结果")
    if res.get("dry_run"):
        raise ValueError(f"{p} 是 dry run（没有付费结果，也没有卡）")
    return res


def _resolve_run(con, target: str | None, *, need: tuple[str, ...] = ("results.json",)
                 ) -> tuple[str, Path, dict[str, Any]]:
    """(run_id, output directory, results.json) of a screen run: 'latest' (the newest ok / partial run whose output
    directory holds every file in `need`), a run id, or a run's output directory. ValueError (Chinese) otherwise."""
    target = (target or "latest").strip()
    if target == "latest":
        for run_id, od in con.execute("SELECT run_id, output_dir FROM screen_runs WHERE status IN ('ok', 'partial') "
                                      "AND output_dir IS NOT NULL ORDER BY started_at DESC").fetchall():
            d = Path(od)
            if all((d / n).exists() for n in need):
                return run_id, d, _load_result(d)
        raise ValueError("没有找到可用的筛选结果" + ("和校准卡" if "cards.json" in need else "")
                         + "：先运行 jevscreen screen \"<想法>\"")
    p = Path(target).expanduser()
    if p.is_file() and p.name == "results.json":
        p = p.parent
    if p.is_dir():
        res = _load_result(p)
        return res["run_id"], p, res
    row = con.execute("SELECT output_dir FROM screen_runs WHERE run_id = ?", [target]).fetchone()
    if row is None:
        raise ValueError(f"没有这个筛选运行：{target}（运行编号形如 scr-20260926145744-4f806b，或给结果目录）")
    if not row[0]:
        raise ValueError(f"运行 {target} 没有记录结果目录")
    d = Path(row[0])
    return target, d, _load_result(d)


def _run_sieve(cfg, result: dict[str, Any]) -> tuple[dict[str, Any] | None, Path]:
    """(sieve or None, path) of a run: the --sieve PATH it was screened with, else data/sieves/<idea_key>.json."""
    from . import calib
    p = (result.get("params") or {}).get("sieve_path")
    path = Path(p) if p and p != "<inline>" else calib.sieve_path(cfg, result.get("idea") or "")
    return calib.load_sieve(path), path


def _run_inputs(con, result: dict[str, Any], out_dir: Path, pool: list[dict[str, Any]]
                ) -> tuple[dict[str, dict[str, Any]], bool]:
    """(l2 inputs, rebuilt): the run's l2_inputs.jsonl, or for a run made before it existed the inputs rebuilt by
    screen._l2_input from the current documents (calib.rebuild_inputs)."""
    from . import calib
    ins = calib.load_inputs(out_dir)
    if ins:
        return ins, False
    return calib.rebuild_inputs(con, result, [p["company_key"] for p in pool]), True


def _deck_for(cfg, con, result: dict[str, Any], out_dir: Path, max_cards: int
              ) -> tuple[dict[str, Any], bool]:
    """(cards.json deck, inputs rebuilt) of a finished run, from results.json, its stored L2 reads (load_pool),
    l2_inputs.jsonl and the idea's sieve."""
    from . import calib
    sv, _path = _run_sieve(cfg, result)
    pool = calib.load_pool(con, result["run_id"], result.get("params"))
    inputs, rebuilt = _run_inputs(con, result, out_dir, pool)
    return calib.build_deck(result, inputs, sv, max_cards=max_cards, pool=pool), rebuilt


def _deck_text(deck: dict[str, Any], path: Path | None, run_id: str | None = None) -> str:
    from . import calib
    text = calib.render_cards_md(deck).rstrip()
    nums = [c["n"] for c in deck.get("cards") or []]
    if nums:
        example = f"{nums[0]}要a" + (f" {nums[1]}不要c" if len(nums) > 1 else "")    # only card numbers that exist
        text += (f"\n\n校准卡：{path}\n回答（例）：jevscreen answer \"{example}\" --run {run_id or deck.get('run_id')}"
                 "（不答的卡=跳过；--no-apply 只记录不重筛）")
    return text


def _write_run_cards(cfg, result: dict[str, Any], max_cards: int) -> tuple[dict[str, Any] | None, Path | None]:
    """Build and write the deck of a run just screened (free). A failure is a warning, never the exit code."""
    from . import calib, store
    out_dir = Path(result["output_dir"])
    try:
        with store.session(cfg, read_only=True, wait_s=READ_WAIT_S) as con:
            deck, _ = _deck_for(cfg, con, result, out_dir, max_cards)
        _pj, pm = calib.write_deck(deck, out_dir)
    except (store.StoreLocked, ValueError, OSError) as e:
        print(f"warning: calibration cards not written ({type(e).__name__}: {str(e)[:200]})", file=sys.stderr)
        return None, None
    return deck, pm


def cmd_cards(args, cfg) -> int:
    """Rebuild / reprint the calibration cards of a run (free): cards.json + cards.md in the run's output directory
    (or --out). Exit 0, 1 unknown run / unreadable results, 3 database locked."""
    from . import calib
    try:
        with _read_session(cfg) as con:
            run_id, out_dir, result = _resolve_run(con, args.target)
            deck, rebuilt = _deck_for(cfg, con, result, out_dir, args.max)
        dest = Path(args.out) if args.out is not None else out_dir
        _pj, pm = calib.write_deck(deck, dest)
    except (ValueError, OSError) as e:
        print(f"error: cards: {e}", file=sys.stderr)
        return EXIT_ERROR
    if args.out is None:          # the page always carries the current deck_id
        quickstart_cli.write_after_cards(cfg, result, deck)
    if args.json:
        _emit(deck)
        return EXIT_OK
    if rebuilt:
        print(f"（运行 {run_id} 早于 l2_inputs.jsonl：卡片的年报摘录按现在的文档重建，按一次读取计）")
    print(_deck_text(deck, pm, run_id))
    return EXIT_OK


def _resolve_deck(con, args) -> tuple[dict[str, Any], str, Path, dict[str, Any]]:
    """(deck, run_id, output directory, results.json) for answer: --deck (a deck id or cards.json), else the cards
    of --run (default: the newest run with cards). A deck that was replaced by a newer one is refused."""
    import re
    from . import calib
    if args.deck:
        p = Path(args.deck).expanduser()
        if p.exists():
            deck = calib.load_deck(p)
            run_id, out_dir, result = _resolve_run(con, deck.get("run_id"))
        else:
            m = re.fullmatch(r"deck-(.+)-(\d+)", args.deck.strip())
            if not m:
                raise ValueError(f"看不懂卡组 {args.deck}（应为 deck-<运行编号>-<n> 或 cards.json 的路径）")
            run_id, out_dir, result = _resolve_run(con, m.group(1))
            deck = calib.load_deck(out_dir)
            if deck.get("deck_id") != args.deck.strip():
                raise ValueError(f"卡组 {args.deck} 已换成 {deck.get('deck_id')}：请回答新的卡组"
                                 f"（jevscreen cards {run_id}）")
    else:
        latest = (args.run or "latest").strip() == "latest"
        run_id, out_dir, result = _resolve_run(con, args.run, need=("results.json", "cards.json") if latest
                                               else ("results.json",))
        deck = calib.load_deck(out_dir)
    if deck.get("run_id") != result.get("run_id"):
        raise ValueError(f"卡组 {deck.get('deck_id')} 属于运行 {deck.get('run_id')}，不是 {result.get('run_id')}")
    return deck, run_id, out_dir, result


def _newest_descendant(con, run_id: str, idea: str | None) -> str | None:
    """The newest ok / partial run of the idea screened (directly or through later versions) from `run_id`: a profile
    fill, an annual-report update pass or earlier answers made after the deck was dealt. None when there is none."""
    parent: dict[str, str | None] = {}
    info: dict[str, tuple[str, str | None]] = {}
    for rid, pj, started, status in con.execute(
            "SELECT run_id, params_json, started_at, status FROM screen_runs WHERE idea = ?", [idea or ""]).fetchall():
        try:
            params = json.loads(pj or "{}")
        except ValueError:
            params = {}
        parent[rid] = params.get("from_run") if isinstance(params, dict) else None
        info[rid] = (str(started or ""), status)
    best = None
    for rid in parent:
        if rid == run_id or info[rid][1] not in ("ok", "partial"):
            continue
        p, seen = parent.get(rid), {rid}
        while p and p not in seen:
            if p == run_id:
                if best is None or info[rid][0] > info[best][0]:
                    best = rid
                break
            seen.add(p)
            p = parent.get(p)
    return best


def cmd_answer(args, cfg) -> int:
    """Record answers to a calibration deck and apply them (see calib.py):
    1) parse and save the answers into the sieve, print the free re-ranking (stage A: pins);
    2) propose rules (the answers' chips) and keyword changes (noisy seeds, mined terms, peer preview; free);
    3) estimate rule trials + the new screen: over --apply-budget -> stop (stage A kept), exit 5;
    4) save the keyword changes, run the rule trials (adopted rules saved, rejections recorded);
    5) screen again from the deck's run (from_run: L1 $0) with the sieve, print the row diff and the next deck. When
       a newer version was screened from the deck's run meanwhile (a profile fill, an update pass, other answers),
       the answers apply to that newest version instead, so its newly read companies are kept.
    Exit 0 ok, 1 bad answers / deck / stale sieve, 3 database locked, 4 Jev busy, 5 over budget (stage A saved),
    6 Jev unavailable (stage A saved), 7 every proposed rule rejected (pins and keywords applied)."""
    import shlex
    import tempfile
    from . import calib, report, screen
    lines: list[str] = []
    summary: dict[str, Any] = {"command": "answer"}
    verbose = bool(getattr(args, "verbose", False))

    def say(text: str = "") -> None:
        if args.json:
            lines.append(text)
        else:
            print(text, flush=True)

    def detail(text: str = "") -> None:
        """Details for --verbose only (rule ids, keyword weighting, cost breakdown): the plain summary is said."""
        if verbose:
            say(text)

    def done(code: int, status: str) -> int:
        if args.json:
            summary.update(status=status, exit_code=code, text="\n".join(lines))
            _emit(summary)
        return code

    def fail(code: int, status: str, msg: str) -> int:
        print(f"error: answer: {msg}", file=sys.stderr)
        return done(code, status)

    try:
        with _read_session(cfg) as con:
            deck, run_id, out_dir, result = _resolve_deck(con, args)
            pool = calib.load_pool(con, run_id, result.get("params"))
            inputs, _rebuilt = _run_inputs(con, result, out_dir, pool)
            base_id, base_result, base_pool = run_id, result, pool
            newer = _newest_descendant(con, run_id, result.get("idea"))
            if newer is not None and args.undo is None:
                with contextlib.suppress(ValueError, OSError):
                    base_id, _d, base_result = _resolve_run(con, newer)
                    base_pool = calib.load_pool(con, base_id, base_result.get("params"))
        sv, sv_path = _run_sieve(cfg, result)
    except ValueError as e:
        return fail(EXIT_ERROR, "error", str(e))
    idea = result.get("idea") or deck.get("idea") or ""
    sv = sv if sv is not None else calib.new_sieve(idea)
    summary.update(run_id=run_id, deck_id=deck.get("deck_id"), sieve_path=str(sv_path))

    if args.undo is not None:
        try:
            sv2, removed = calib.undo_example(sv, args.undo)
            saved = calib.save_sieve(sv_path, sv2)
        except ValueError as e:          # AnswerError / SieveStale
            return fail(EXIT_ERROR, "error", str(e))
        say(f"已撤销第 {args.undo} 条回答：{removed.get('name') or removed.get('security_id')}"
            f"（{calib.answer_words_zh(removed.get('want'), removed.get('chip'))}）"
            + (f" → {sv_path}" if verbose else ""))
        say(calib.render_diff_zh(result, calib.rerank_result(result, saved, pool=pool), title="撤销后（免费）"))
        summary["undone"] = removed
        return done(EXIT_OK, "undone")

    # 1) parse, save, stage A
    warnings: list[str] = []
    try:
        if args.file is not None:
            try:
                data = json.loads(Path(args.file).read_text(encoding="utf-8"))
            except (OSError, ValueError) as e:
                raise calib.AnswerError(f"读不了 {args.file}（{type(e).__name__}）") from None
            answers = calib.answers_from_file(data, deck, warnings)
        elif args.text and args.text.strip():
            answers = calib.parse_answers(args.text, deck, warnings, lang=getattr(args, "lang", "zh") or "zh")
        else:
            raise calib.AnswerError('没有回答：写成 jevscreen answer "1要a 2不要c"，或用 --file answers.json')
        if not answers:
            raise calib.AnswerError("没有可记录的回答（都跳过了）")
    except calib.AnswerError as e:
        return fail(EXIT_ERROR, "bad_answers", str(e))
    for w in warnings:
        say(f"注意：{w}")
    try:
        sv2, new = calib.record_answers(sv, deck, answers)
        saved = calib.save_sieve(sv_path, sv2)
    except ValueError as e:              # SieveStale (请重新读取) / an invalid sieve
        return fail(EXIT_ERROR, "sieve_stale" if isinstance(e, calib.SieveStale) else "error", str(e))
    with contextlib.suppress(OSError):
        (out_dir / "answers.json").write_text(json.dumps(calib.answers_json(deck, answers), ensure_ascii=False,
                                                         indent=2), encoding="utf-8")
    say(f"已记录 {len(new)} 条回答（同一想法以后自动使用）" + (f" → {sv_path}" if verbose else ""))
    if base_id != run_id:
        say(f"这组卡片来自较早的一版结果（{run_id}）；回答用在最新一版（{base_id}）上重新排序，"
            "后来补进来的公司不会丢。")
        summary["applied_to_run"] = base_id
    stage_a = calib.rerank_result(base_result, saved, pool=base_pool)
    say(calib.render_diff_zh(base_result, stage_a))
    summary.update(answers=[a._asdict() for a in answers],
                   stage_a={"rows": [r["security_id"] for r in stage_a["rows"]],
                            "excluded_by_user": [r["security_id"] for r in stage_a["excluded_by_user"]]})
    apply_cmd = f"jevscreen screen {shlex.quote(idea)} --from-run {base_id} --sieve {shlex.quote(str(sv_path))}"
    if args.no_apply:
        say(f"没有试规则、没有重新筛选（--no-apply）。要应用：{apply_cmd}")
        return done(EXIT_OK, "recorded")

    # 2) candidate rules and keyword changes (free)
    with _read_session(cfg) as con:
        names, tickers = calib.load_names(con)
        props = calib.propose_keywords(cfg, con, result, saved, inputs, pool)
    cands, dropped = calib.candidate_rules(saved, deck, answers, names=names, tickers=tickers,
                                           terms=calib.keyword_terms(result.get("terms_by_lang"), saved))
    if cands:
        detail("候选规则：" + "；".join(f"{c['id']}（{c['from']}）" for c in cands))
        if not verbose:
            say(f"从你的回答里总结出 {len(cands)} 条规则，先在少数公司上试一下，只留下不误伤的")
    for d in dropped:
        detail(f"  不试 {d['id']}：{d['why_zh']}")
    for pr in props:
        detail(calib.keyword_log_zh(pr["lang"], pr["add"] if pr["adopted"] else [],
                                    {**pr["noisy"], "weak": pr["weak"] if pr["adopted"] else []},
                                    removed=pr.get("remove") or []))
        if pr["preview"] is not None:
            detail(calib.render_peer_preview_zh(pr["preview"], pr["lang"]))
    kw_changes = [pr for pr in props if (pr["adopted"] and (pr["add"] or pr["weak"])) or pr.get("remove")]
    summary.update(candidates=[c["id"] for c in cands], dropped=dropped,
                   keywords=[{k: pr.get(k) for k in ("lang", "add", "weak", "remove", "adopted", "why_zh")}
                             for pr in props])

    # 3) estimate: rule trials + the new screen (upper bounds, no cache)
    idea_en = result.get("idea_en")
    base_reads = calib.base_reads_of(result, pool, inputs)
    trial_run = f"{deck.get('deck_id')}-trial"
    try:
        est_client = screen._default_factory(cfg, run_id=trial_run, layer="trial", budget_usd=args.apply_budget,
                                             dry_run=True)
        est_t = calib.estimate_trials(cands, saved, base_reads, est_client, inputs=inputs, idea=idea,
                                      idea_en=idea_en)
        with tempfile.TemporaryDirectory() as tmp:
            dry = screen.screen(cfg, idea, from_run=base_id, sieve=str(sv_path), reads=SCREEN_READS_DEFAULT,
                                budget_usd=args.apply_budget, dry_run=True, out_dir=Path(tmp) / "estimate")
    except ValueError as e:
        return fail(EXIT_ERROR, "error", str(e))
    except Exception as e:  # noqa: BLE001 - Jev errors only
        code = _jev_exit(e)
        if code is None:
            raise
        return fail(code, "jev_unavailable", f"Jev 不可用（{type(e).__name__}）：回答已保存，没有试规则、没有重新筛选")
    est_apply = float((dry.get("dry_run_budget") or {}).get("est_cost_usd") or 0.0)
    est_total = est_t["est_cost_usd"] + est_apply
    extra = float(est_t.get("extra_max_usd") or 0.0)
    l2e = ((dry.get("layers") or {}).get("l2") or {}).get("estimate") or {}
    br = l2e.get("band_reads") or {}
    band_zh = (f"，含边界多读 {br.get('items')} 家 × {br.get('extra_reads')} 次"
               + ("（按上次运行的实际边界）" if br.get("basis") == "run" else "（按约 28% 估）")) if br else ""
    spr = l2e.get("should_pass_reads") or {}
    sp_zh = f"，含应通过公司多读 {spr.get('items')} 家 × {spr.get('extra_reads')} 次" if spr else ""
    extra_zh = f"；边界公司多读和对照读最多再加 ${extra:.4f}" if extra > 0 else ""
    detail(f"预计：规则试验约 ${est_t['est_cost_usd']:.4f}（{est_t['trials']} 次 × {est_t['items']} 家 × 2 读{extra_zh}）"
        f" + 重新筛选最多 ${est_apply:.4f}（L1 沿用 $0，缓存命中的不花钱{band_zh}{sp_zh}）= ${est_total:.4f}"
        + (f"，连同多读最多 ${est_total + extra:.4f}" if extra > 0 else "")
        + f"；上限 --apply-budget ${args.apply_budget:.4f}，到上限就停")
    if not verbose:
        say(f"预计约 ${est_total:.4f}" + (f"，要多读几次时最多 ${est_total + extra:.4f}" if extra > 0 else "")
            + f"（上限 ${args.apply_budget:.4f}，到上限就停）")
    summary["estimate"] = {"trials_usd": est_t["est_cost_usd"], "trials_extra_max_usd": extra,
                           "apply_usd": est_apply, "total_usd": est_total, "total_max_usd": est_total + extra}
    if est_total <= args.apply_budget + 1e-12 < est_total + extra:
        say("  预算只够计划内的试验：边界公司要多读或对照读时钱不够，那条规则记为没试，不会采用")
    if est_total > args.apply_budget + 1e-12:
        again = ("jevscreen answer " + (f"--file {shlex.quote(str(args.file))}" if args.file is not None
                                        else shlex.quote(args.text)) + f" --run {run_id} --apply-budget "
                 f"{math.ceil((est_total + extra) * 100) / 100:.2f}")
        say("超过上限：回答已保存（上面「立即生效」的部分），没有试规则、没有重新筛选。")
        say(f"要继续：{again}；或只应用回答：{apply_cmd}")
        return done(EXIT_BUDGET, "over_budget")

    # 4) keyword changes (free), then the rule trials
    try:
        if kw_changes:
            for pr in kw_changes:
                ok = pr["adopted"]              # a failing learned add is removed even when the proposal is not
                saved = calib.merge_keywords(saved, pr["lang"], add=pr["add"] if ok else [],
                                             weak=pr["weak"] if ok else [],
                                             remove=[x["term"] for x in pr.get("remove") or []],
                                             log=(pr["log"] if ok else []) + (pr.get("remove_log") or []))
            saved = calib.save_sieve(sv_path, saved)
    except ValueError as e:
        return fail(EXIT_ERROR, "sieve_stale" if isinstance(e, calib.SieveStale) else "error", str(e))
    trial = None
    rules_rejected = False
    if cands:
        trial_budget = round(max(0.0, args.apply_budget - est_apply), 6)
        try:
            client = screen._default_factory(cfg, run_id=trial_run, layer="trial", budget_usd=trial_budget,
                                             dry_run=False)
        except Exception as e:  # noqa: BLE001
            code = _jev_exit(e)
            if code is None:
                raise
            return fail(code, "jev_unavailable", f"Jev 不可用（{type(e).__name__}）：回答和关键词已保存，规则没有试")
        trial = calib.trial_rules(cands, saved, base_reads, client, inputs=inputs, idea=idea, idea_en=idea_en,
                                  budget_usd=trial_budget)
        detail(calib.render_trial_zh(trial))
        if not verbose:
            n_ok, rej = len(trial.get("adopted") or []), trial.get("rejected") or []
            whys = list(dict.fromkeys(r.get("why_zh") for r in rej if r.get("why_zh")))
            why_untried = {"budget": "预算不够", "incomplete": "Jev 中途停了", "jev_unavailable": "Jev 中途停了",
                           "jev_busy": "Jev 中途停了"}.get(trial.get("status"), "已达试验次数上限")
            say(f"规则试验：采用 {n_ok} 条" + (f"；没采用 {len(rej)} 条：" + "；".join(whys) if rej else "")
                + (f"；{len(trial['untried'])} 条没试（{why_untried}）" if trial.get("untried") else "")
                + (f"（{trial.get('note_zh') or calib.IN_SAMPLE_NOTE_ZH}）" if n_ok or rej else ""))
        summary["trial"] = {k: trial.get(k) for k in ("status", "cost_usd", "adopted", "rejected", "untried",
                                                      "trials")}
        if trial["adopted"] or trial["rejected"]:
            try:
                saved = calib.save_sieve(sv_path, calib.adopt_rules(saved, trial))
            except ValueError as e:
                return fail(EXIT_ERROR, "sieve_stale" if isinstance(e, calib.SieveStale) else "error", str(e))
        if trial["status"] in ("jev_unavailable", "jev_busy"):
            return fail(EXIT_BUSY if trial["status"] == "jev_busy" else EXIT_JEV_UNAVAILABLE, trial["status"],
                        "Jev 中途不可用：回答和关键词已保存，没有重新筛选")
        rules_rejected = (not trial["adopted"] and bool(trial["rejected"])
                          and {r["id"] for r in trial["rejected"]} >= {c["id"] for c in cands})

    # 5) screen again from the deck's run (L1 loaded, $0)
    spent = float((trial or {}).get("cost_usd") or 0.0)
    try:
        new_res = screen.screen(cfg, idea, from_run=base_id, sieve=str(sv_path), reads=SCREEN_READS_DEFAULT,
                                budget_usd=round(max(0.0, args.apply_budget - spent), 6))
    except ValueError as e:
        return fail(EXIT_ERROR, "error", str(e))
    except KeyboardInterrupt:
        print("error: answer: interrupted (answers saved; paid answers so far are in the ledger)", file=sys.stderr)
        return done(EXIT_INTERRUPTED, "interrupted")
    except Exception as e:  # noqa: BLE001
        code = _jev_exit(e)
        if code is None:
            raise
        return fail(code, "jev_unavailable", f"重新筛选时 Jev 不可用（{type(e).__name__}）：回答已保存")
    l2 = new_res["layers"].get("l2") or {}
    st = new_res.get("status")
    head = {"ok": "重新筛选完成", "partial": "重新筛选只完成了一部分（预算用完或出错，有些公司没有重新核对，下面的变化不完整）",
            "budget_exhausted": "预算用完，重新筛选只做了一部分",
            "jev_unavailable": "重新筛选没有完成：AI 服务（Jev）不可用，回答已保存",
            "jev_busy": "重新筛选没有完成：AI 服务（Jev）正忙，回答已保存，稍后再试"}.get(st, f"重新筛选没有完成（{st}）")
    title = (f"重新筛选 {new_res['run_id']}（{st}；L1 沿用 $0；L2 {report.usd(l2.get('cost_usd'))}，"
             f"{l2.get('requests', 0)} 次请求）" if verbose
             else f"{head}（规则试验和重新筛选共花费 {report.usd(spent + float(new_res.get('cost_usd') or 0.0))}，"
                  f"没有重新下载；新结果编号 {new_res['run_id']}）")
    say(calib.render_diff_zh(base_result, new_res, title=title))
    if st in ("jev_unavailable", "jev_busy"):
        print(f"error: answer: {head}", file=sys.stderr)
    summary["apply"] = {"run_id": new_res["run_id"], "status": new_res["status"], "cost_usd": new_res["cost_usd"],
                        "output_dir": new_res["output_dir"], "rows": [r["security_id"] for r in new_res["rows"]]}
    code = SCREEN_EXIT.get(new_res["status"], EXIT_ERROR)
    if new_res["status"] in ("ok", "partial"):
        deck2, path2 = _write_run_cards(cfg, new_res, CARDS_DEFAULT)
        page_path, _data = quickstart_cli.write_after_cards(cfg, new_res, deck2)
        stable = quickstart_cli.stable_page_for(cfg, new_res) if page_path else None
        if stable is not None:
            say(f"结果页已更新（同一个页面，刷新即可）：{stable.resolve().as_uri()}")
            summary["apply"]["page"] = str(stable)
        elif page_path is not None:
            say(f"更新后的结果页：{page_path}")
            summary["apply"]["page"] = str(page_path)
        if deck2 is not None:
            say(f"下一轮校准卡：{len(deck2['cards'])} 张 → {path2}（jevscreen cards 查看）" if deck2["cards"]
                else calib.EMPTY_DECK_ZH)
    if code == EXIT_OK and rules_rejected:
        say("候选规则都没有采用（回答和关键词照常生效）。")
        code = EXIT_RULES_REJECTED
    return done(code, "rules_rejected" if code == EXIT_RULES_REJECTED else new_res["status"])


def _sieve_target(cfg, target: str) -> tuple[Path, str | None]:
    """(sieve path, idea or None): an existing file, else the idea's data/sieves/<idea_key>.json."""
    from . import calib
    p = Path(target).expanduser()
    if p.is_file():
        return p, None
    return calib.sieve_path(cfg, target), target


def _latest_run_for(con, idea: str) -> tuple[str, Path, dict[str, Any]] | None:
    for run_id, od in con.execute("SELECT run_id, output_dir FROM screen_runs WHERE trim(idea) = ? AND status IN "
                                  "('ok', 'partial') AND output_dir IS NOT NULL ORDER BY started_at DESC",
                                  [idea.strip()]).fetchall():
        with contextlib.suppress(ValueError):
            return run_id, Path(od), _load_result(Path(od))
    return None


def cmd_sieve(args, cfg) -> int:
    return {"check": cmd_sieve_check, "show": cmd_sieve_show, **why_cli.SIEVE_COMMANDS}[args.sieve_command](args, cfg)


def cmd_sieve_check(args, cfg) -> int:
    """Check a sieve (free): the schema; that every example resolves to a company (near matches listed); that the
    rendered rule texts (adopted ones, and every library rule with the sieve's facets) carry no company name,
    ticker of >= 4 characters or 30-character card quote; each keyword's background document frequency and lift
    (a term in >= 5% of a language's filings is flagged weak); then the L2 question the sieve gives.
    Exit 0 when it passes, 1 on any problem (missing file, schema, unresolved company, rule text)."""
    from . import calib, screen
    path, idea_arg = _sieve_target(cfg, args.target)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        print(f"error: sieve check: 没有校准文件 {path}" + (f"（想法：{idea_arg}）" if idea_arg else ""),
              file=sys.stderr)
        return EXIT_ERROR
    except (OSError, ValueError) as e:
        print(f"error: sieve check: {path} 读不了（{type(e).__name__}: {str(e)[:120]}）", file=sys.stderr)
        return EXIT_ERROR
    errs = calib.validate_sieve(raw)
    print(f"校准文件：{path}")
    if errs:
        print("结构：不通过")
        for e in errs:
            print(f"  - {e}")
        return EXIT_ERROR
    sv = calib.load_sieve(path)
    idea = sv.get("idea") or idea_arg or ""
    print(f"想法：{idea}（版本 {sv.get('version')}；{len(sv.get('examples') or [])} 条示例，"
          f"{len(sv.get('rules') or [])} 条规则）")
    print("结构：通过")
    problems: list[str] = []
    facets = calib._facets(sv)
    with _read_session(cfg) as con:
        # companies
        bad = 0
        for i, ex in enumerate(sv.get("examples") or [], 1):
            sid, ck = ex.get("security_id"), ex.get("company_key")
            hit = con.execute("SELECT security_id, company_key FROM securities WHERE security_id = ? "
                              "OR company_key = ? LIMIT 1", [sid, ck]).fetchone()
            if hit is not None:
                continue
            bad += 1
            sym = (sid or ck or "").split(":")[-1]
            near = con.execute("SELECT security_id, name FROM securities WHERE active AND (symbol = ? OR "
                               "lower(name) LIKE ?) ORDER BY security_id LIMIT 5",
                               [sym, f"%{(ex.get('name') or sym).lower()}%"]).fetchall()
            problems.append(f"第 {i} 条示例 {sid or ck} 找不到公司"
                            + (f"；相近：{'、'.join(f'{s}（{n}）' for s, n in near)}" if near else ""))
        print(f"公司：{len(sv.get('examples') or []) - bad}/{len(sv.get('examples') or [])} 条示例对得上")
        # rule texts
        names, tickers = calib.load_names(con)
        run = _latest_run_for(con, idea)
        quotes: list[str] = []
        dirs = {Path(od) for rid in {d.get("run_id") for d in sv.get("decks") or [] if isinstance(d, dict)}
                for (od,) in con.execute("SELECT output_dir FROM screen_runs WHERE run_id = ?", [rid]).fetchall()
                if od}
        if run is not None:
            dirs.add(run[1])
        for d in sorted(dirs):
            with contextlib.suppress(ValueError):
                quotes += [(c.get("quote") or {}).get("text") or "" for c in calib.load_deck(d).get("cards") or []]
        texts = calib.render_rules(sv.get("rules") or [], facets)
        texts += [t for rid in sorted(calib.RULES) for t in calib.render_rules([rid], facets) if t not in texts]
        own_terms = calib.keyword_terms((run[2] if run else {}).get("terms_by_lang"), sv)
        rule_bad = 0
        for rid, label, text in texts:
            probs = calib.validate_rule_text(text, names=names, tickers=tickers, quotes=quotes, terms=own_terms)
            if probs:
                rule_bad += 1
                problems.append(f"规则 {rid}（{label}）：" + "、".join(dict.fromkeys(probs)))
        print(f"规则文字：{len(texts) - rule_bad}/{len(texts)} 句通过（已采用的规则和规则库按 facets 渲染）")
        # keywords: background df and lift
        res = run[2] if run else {}
        tb = res.get("terms_by_lang") or {}
        rel = calib.l1_relevant(con, run[0]) if run else set()
        skw = calib.keywords_of(sv) or {}
        for lang in screen.LANGS:
            src = calib.LANG_SOURCE.get(lang)
            k = skw.get(lang) or {}
            if src is None or (lang == "en" and not k):         # the SEC corpus only for the sieve's own en terms
                continue
            weak = list(k.get("weak") or [])
            terms = list(dict.fromkeys(list(tb.get(lang) or []) + list(k.get("add") or []) + weak))
            if not terms:
                continue
            df = calib.background_df(cfg, con, src, terms)
            n = df.get("n") or 0
            if not n:
                print(f"{calib.LANG_ZH[lang]}关键词：没有{calib.CORPUS_ZH[lang]}，跳过")
                continue
            print(f"{calib.LANG_ZH[lang]}关键词（{screen.source_label(src)} {n} 份年报；lift 相对"
                  + (f"运行 {run[0]} 的 L1 core/adjacent）：" if run else "：没有运行，算不了）："))
            for t in terms:
                c = calib.df_count(df, t) or 0
                share = c / n
                lift = calib.lift_of(df, t, rel) if rel else None
                if t in weak:
                    state = "已降权"
                elif share >= calib.NOISY_DF_SHARE:
                    state = f"应降权（weak：≥{calib.NOISY_DF_SHARE:.0%} 的{calib.CORPUS_ZH[lang]}都有）"
                elif c == 0:
                    state = "年报里没出现"
                else:
                    state = ""
                print(f"  {t}  df {c}（{share:.1%}）lift {'-' if lift is None else f'{lift:.1f}'}"
                      + (f"  {state}" if state else ""))
    q = screen.build_l2_question(idea, (res or {}).get("idea_en"), rules=sv.get("rules") or [], facets=facets)
    print("L2 问题（Jev 收到的）：")
    print(f"  {q.instructions}")
    for label, text in q.criteria.items():
        print(f"  - {label}: {text}")
    if problems:
        print(f"结果：{len(problems)} 个问题")
        for p in problems:
            print(f"  - {p}")
        return EXIT_ERROR
    print("结果：通过")
    return EXIT_OK


def cmd_sieve_show(args, cfg) -> int:
    """Show an idea's sieve (free): facets, target terms, keywords, rules (adopted / rejected), the answers numbered
    for answer --undo, decks. --json prints the file. Exit 1 when there is none or it is invalid."""
    from . import calib
    path, idea_arg = _sieve_target(cfg, args.target)
    try:
        sv = calib.load_sieve(path)
    except ValueError as e:
        print(f"error: sieve show: {e}", file=sys.stderr)
        return EXIT_ERROR
    if sv is None:
        print(f"error: sieve show: 没有校准文件 {path}" + (f"（想法：{idea_arg}）" if idea_arg else ""),
              file=sys.stderr)
        return EXIT_ERROR
    from . import sieve_author
    if args.json:
        _emit({**sv, "examples": sieve_author.strip_author(sv.get("examples"))})    # the file, not the checks
        return EXIT_OK
    out = [f"校准文件：{path}（版本 {sv.get('version')}，更新于 {sv.get('updated_at') or '-'}）",
           f"想法：{sv.get('idea') or '-'}"]
    fz, f = sv.get("facets_zh") or {}, sv.get("facets") or {}
    if f or fz:
        out.append("范围：" + "；".join(f"{k} {fz.get(k) or f.get(k)}" for k in ("category", "target", "mechanism")
                                     if fz.get(k) or f.get(k)))
    for lang, terms in (sv.get("target_terms") or {}).items():
        out.append(f"对象词（{lang}）：{'、'.join(terms)}")
    for lang, k in (sv.get("keywords") or {}).items():
        if isinstance(k, dict):
            out.append(f"{calib.LANG_ZH.get(lang, lang)}关键词：+{' +'.join(k.get('add') or []) or '（无）'}"
                       + (f"；降权 {'、'.join(k['weak'])}" if k.get("weak") else ""))
    for r in sv.get("rules") or []:
        r = {"id": r} if isinstance(r, str) else r
        t = r.get("trial") or {}
        out.append(f"规则：{r.get('id')}" + (f"（一致 {t.get('agree_before')}→{t.get('agree_after')}，"
                                             f"{r.get('adopted_at') or ''}）" if t else ""))
    for r in sv.get("rejected_rules") or []:
        out.append(f"没采用：{r.get('id')} — {r.get('why_zh') or ''}")
    exs = sieve_author.strip_author(sv.get("examples"))
    out.append(f"回答 {len(exs)} 条（answer --undo N 撤销第 N 条）：" if exs else "回答：（无）")
    for i, ex in enumerate(exs, 1):
        who = ex.get("name") or ex.get("security_id") or ex.get("company_key")
        kind = "检查" if ex.get("source") == "sieve" else ("钉选" if ex.get("pin") else "不确定")
        out.append(f"  {i}. {who} {ex.get('security_id') or ''} · {calib.want_zh(ex.get('want'), ex.get('chip'))}"
                   + f" · {kind}"
                   + (f" · {ex.get('deck_id')} 第 {ex.get('card_n')} 张" if ex.get("deck_id") else ""))
    for lst in sieve_author.CHECK_LISTS:
        ents = sv.get(lst) or []
        if ents:
            out.append(f"{sieve_author.LIST_ZH[lst]} {len(ents)} 家（jevscreen sieve remove {lst} 代码 撤掉）：")
            out += [f"  - {(e.get('resolved') or {}).get('name') or e['company']} "
                    f"{(e.get('resolved') or {}).get('security_id') or '（未对上公司）'}" for e in ents]
    if sv.get("history"):
        out.append(f"历史：{len(sv['history'])} 条（被新回答取代或撤销）")
    for d in sv.get("decks") or []:
        out.append(f"卡组：{d.get('deck_id')}（{d.get('answered')}/{d.get('cards')} 张已答）")
    print("\n".join(out))
    return EXIT_OK


COMMANDS: dict[str, Callable[[argparse.Namespace, Any], int]] = {
    "init": cmd_init,
    "refresh-universe": cmd_refresh_universe,
    "import-fd": cmd_import_fd,
    "crawl-descriptions": cmd_crawl_descriptions,
    "sync-sec": cmd_sync_sec,
    "sync-mops": cmd_sync_mops,             # Taiwan; defined with the other sync-mops helpers above
    "sync-cninfo": cmd_sync_official,
    "sync-edinet": cmd_sync_official,
    "sync-dart": cmd_sync_official,
    "sync-bse": cmd_sync_official,
    "keywords": cmd_keywords,
    "coverage": cmd_coverage,
    "status": cmd_status,
    "screen": cmd_screen,
    "cards": cmd_cards,
    "answer": cmd_answer,
    "sieve": cmd_sieve,
    "pack": lambda args, cfg: cmd_pack(args, cfg),
}
COMMANDS.update(agent_cli.COMMANDS)
COMMANDS.update(why_cli.COMMANDS)
COMMANDS.update(ondemand_cli.COMMANDS)
COMMANDS.update(quickstart_cli.COMMANDS)


# ---------------------------------------------------------------------------------------------------- open data pack
# `jevscreen pack build|pull|fetch-open` (jevscreen.pack, docs/OPEN_PACK.md). Exit codes: 0 ok (also: pack already
# imported), 1 error (nothing imported), 2 blocked (GitHub/SEC/EDINET answered 403/429/challenge; no retry),
# 4 another process holds the rate budget.


def add_pack_parser(sub) -> None:
    pk = sub.add_parser("pack", help="open data pack: build the redistributable subset of the store, or pull the "
                        "newest published pack into it")
    ps = pk.add_subparsers(dest="pack_command", required=True)
    pb = ps.add_parser("build", help="write the open pack (licence-clean sources only) from the local store")
    pb.add_argument("--out", type=Path, default=None, metavar="DIR",
                    help="output directory (default: <home>/open_pack/<tag>)")
    pb.add_argument("--tag", default=None, metavar="TAG", help="pack tag written into the manifest "
                    "(default: pack-YYYY-MM-DD, UTC)")
    pp = ps.add_parser("pull", help="download the newest open pack from GitHub Releases, verify sha256, import")
    pp.add_argument("--tag", default="latest", metavar="TAG", help="'latest' (default) or an exact pack-* tag")
    pp.add_argument("--repo", default=None, metavar="OWNER/NAME",
                    help="GitHub repository publishing the pack (default: JEVSCREEN_PACK_REPO, else "
                         "the project's own repository)")
    pp.add_argument("--force", action="store_true", help="import again even if this manifest was imported before")
    pp.add_argument("--after-block", action="store_true",
                    help=f"run even though the last pull was blocked less than {COOLDOWN_HOURS} h ago")
    pf = ps.add_parser("fetch-open", help="(daily pack job) fetch the SEC ticker list and the EDINET code list")
    pf.add_argument("--seed-lines", action="store_true",
                    help="add one line per listed EDINET filer so sync-edinet can run in a store without "
                         "TradingView data (refused in a store that has any)")
    pf.add_argument("--after-block", action="store_true",
                    help=f"run even though the last run was blocked less than {COOLDOWN_HOURS} h ago")


def cmd_pack(args, cfg) -> int:
    from . import guard, pack
    sub = args.pack_command
    if sub == "build":
        try:
            tag = args.tag
            out = args.out
            if out is None:
                import datetime as _dt
                tag = tag or f"{pack.TAG_PREFIX}{_dt.datetime.now(_dt.timezone.utc).date().isoformat()}"
                out = cfg.home / "open_pack" / tag
            result = pack.build(cfg, out, tag=tag, wait_s=READ_WAIT_S)
        except pack.PackError as e:
            print(f"error: pack build: {e}", file=sys.stderr)
            return EXIT_ERROR
        _emit({"command": "pack build", "status": "ok", **result["summary"],
               "sources": sorted(result["sources"]), "excluded": sorted(result["excluded"])})
        return EXIT_OK
    if sub == "pull":
        try:
            with guard.budget_lock(cfg, "api.github.com"):
                result = pack.pull(cfg, tag=args.tag, repo=args.repo, force=args.force,
                                   after_block=args.after_block)
        except guard.Busy as e:
            print(f"error: pack pull: {e}", file=sys.stderr)
            return EXIT_BUSY
        _emit(result)
        if result["status"] in ("error", "blocked"):
            print(f"error: pack pull: {result.get('note')}", file=sys.stderr)
        unmapped = (result.get("imported") or {}).get("edinet_documents_unmapped")
        if unmapped:
            print(f"note: {unmapped} EDINET texts have no line yet. Build the universe (jevscreen refresh-universe), "
                  "then run `jevscreen pack pull` again: it links them locally without re-importing.", file=sys.stderr)
        return {"ok": EXIT_OK, "already_imported": EXIT_OK, "blocked": EXIT_BLOCKED}.get(result["status"], EXIT_ERROR)
    if sub == "fetch-open":
        secrets: tuple = (cfg.sec_user_agent(),) if cfg.sec_user_agent() else ()
        try:
            result = pack.fetch_open(cfg, seed_lines=args.seed_lines, after_block=args.after_block)
        except guard.Busy as e:
            print(f"error: pack fetch-open: {e}", file=sys.stderr)
            return EXIT_BUSY
        _emit(result, secrets)
        return {"ok": EXIT_OK, "blocked": EXIT_BLOCKED}.get(result["status"], EXIT_ERROR)
    return EXIT_ERROR


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point. Exit codes: 0 ok, 1 error (incl. a run stopped by consecutive errors or a locked store),
    2 stopped because the provider blocked us (http.Blocked) or cooldown, 3 the database stayed locked by another
    process longer than the command was willing to wait, 4 another process holds the same rate budget (for screen:
    another process is using Jev), 5 screen: Jev budget exhausted before layer 1 finished / answer: estimate over
    --apply-budget, 6 screen / answer: Jev unavailable (missing key, HTTP 401/402/403), 7 answer: every proposed rule rejected (answers still applied),
    130 interrupted."""
    args = build_parser().parse_args(argv)
    from . import config
    if args.command in agent_cli.COMMANDS:   # doctor/keys/consent work even without duckdb (doctor reports it)
        return agent_cli.COMMANDS[args.command](args, config.Config())   # no ensure(): doctor stays read-only
    from . import store
    try:
        return COMMANDS[args.command](args, config.load())
    except store.StoreLocked as e:
        return _locked(args.command, e)


if __name__ == "__main__":
    sys.exit(main())
