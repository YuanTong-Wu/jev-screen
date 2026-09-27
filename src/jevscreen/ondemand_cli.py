"""CLI of the on-demand annual-report fetch (jevscreen.ondemand), wired into jevscreen.cli with a few lines.

- `jevscreen screen ... [--fetch-docs auto|off] [--fetch-time S] [--fetch-sources sec,cninfo,bse,mops,dart]`: after
  the screen's report is written (phase 1), companies whose layer-2 input was a profile get their annual report
  fetched (free, at most --fetch-time seconds, default 120) and the same report is updated from the stored L1
  answers (the update pass: at most min($0.05, budget left)). Default auto; off with --from-run or --dry-run unless
  asked for. Fetch problems never change the screen's exit code (a blocked source is a warning, agent_action none).
- `jevscreen fetch-docs [RUN_ID|latest] [--time 300] [--sources ...] [--retry-failed] [--no-update] [--budget 0.05]
  [--dry-run] [--json]`: the same for an earlier run (deferred companies). Exit 0 ok (also a partial fetch), 1 bad
  arguments or unknown run, 2 a source was blocked during this invocation (results still updated), 3 store busy at
  planning, 4 every planned source busy / Jev busy during the update, 5 update budget ran out, 6 Jev unavailable
  during the update, 130 second interrupt.
Schemas: docs/AGENT_API.md ("On-demand annual reports").
"""
from __future__ import annotations

import argparse
import contextlib
import json
import os
import sys
from pathlib import Path
from typing import Any, Callable

EXIT_OK, EXIT_ERROR, EXIT_BLOCKED, EXIT_LOCKED, EXIT_BUSY, EXIT_INTERRUPTED = 0, 1, 2, 3, 4, 130
EXIT_BUDGET, EXIT_JEV_UNAVAILABLE = 5, 6
SOURCE_CHOICES = ("sec", "cninfo", "bse", "mops", "dart")


def _seconds(text: str) -> float:
    try:
        v = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a number: {text!r}") from None
    if not (0 <= v <= 1800):
        raise argparse.ArgumentTypeError("must be between 0 and 1800 seconds")
    return v


def _sources(text: str) -> list[str]:
    out = [t.strip().lower() for t in text.split(",") if t.strip()]
    bad = [t for t in out if t not in SOURCE_CHOICES]
    if bad or not out:
        raise argparse.ArgumentTypeError(f"sources are a comma list of {','.join(SOURCE_CHOICES)}")
    return out


def _budget(text: str) -> float:
    try:
        v = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a number: {text!r}") from None
    if not (v >= 0 and v == v and v != float("inf")):
        raise argparse.ArgumentTypeError("must be a finite number >= 0")
    return v


def add_screen_flags(sc: argparse.ArgumentParser) -> None:
    sc.add_argument("--fetch-docs", choices=("auto", "off"), default=None,
                    help="after the report is written, fetch missing annual reports of first-round companies from the "
                         "official sites (free) and update the same report (default auto; off with --from-run or "
                         "--dry-run)")
    sc.add_argument("--fetch-time", type=_seconds, default=120.0, metavar="SECONDS",
                    help="at most this long for that fetch (default 120, 0..1800)")
    sc.add_argument("--fetch-sources", type=_sources, default=None, metavar="sec,cninfo,bse,mops,dart",
                    help="only these sources (narrows the set; never turns on MOPS without a recorded consent)")


def add_parsers(sub: Any) -> None:
    fd = sub.add_parser("fetch-docs", help="fetch missing annual reports of a screen run's first-round companies "
                        "(free) and update its report (at most $0.05)")
    fd.add_argument("target", nargs="?", default="latest", metavar="RUN_ID|latest")
    fd.add_argument("--time", type=_seconds, default=300.0, metavar="SECONDS",
                    help="at most this long for the fetch (default 300)")
    fd.add_argument("--sources", type=_sources, default=None, metavar="sec,cninfo,bse,mops,dart")
    fd.add_argument("--retry-failed", action="store_true",
                    help="retry companies that failed recently (normally not retried for 1-30 days)")
    fd.add_argument("--no-update", action="store_true", help="only fetch; do not update the report")
    fd.add_argument("--budget", type=_budget, default=0.05, metavar="USD",
                    help="most the update pass may spend (default 0.05)")
    fd.add_argument("--dry-run", action="store_true", help="show what would be fetched; no request")
    fd.add_argument("--json", action="store_true", help="print a JSON summary (for AI agents)")


# ---------------------------------------------------------------------------------------------------------------

def _say(out: Callable[[str], None]) -> Callable[[str], None]:
    def f(line: str) -> None:
        out(line)
        with contextlib.suppress(Exception):
            sys.stdout.flush()
    return f


def sieve_for_update(cfg: Any, result: dict[str, Any]) -> tuple[Any, str | None]:
    """(sieve argument for the update pass, None) or (None, why the update is skipped): the base run's sieve file
    must still be the one it was screened with (sieve_sha256)."""
    from . import calib
    params = result.get("params") or {}
    p, sha = params.get("sieve_path"), params.get("sieve_sha256")
    if not p:
        return "none", None
    if p == "<inline>":
        return None, "inline_sieve"
    try:
        sv = calib.load_sieve(Path(p))
    except ValueError:
        sv = None
    if sv is None or calib.sieve_sha256(sv, Path(p)) != sha:
        return None, "sieve_changed"
    return str(p), None


SIEVE_CHANGED = {"zh": "上次用的 sieve 已经改过，没有自动更新结果；运行 jevscreen screen --from-run {run} --sieve PATH",
                 "en": "The sieve this run used has changed since, so the results were not updated automatically; run "
                       "jevscreen screen --from-run {run} --sieve PATH"}
SKIP_LINES = {
    "no_l2": ("补抓年报：跳过（没有公司进入第二轮）", "Fetching annual reports: skipped (no company reached layer 2)"),
    "no_profile": ("补抓年报：不需要（第二轮的公司都读了年报原文）",
                   "Fetching annual reports: not needed (every layer-2 company was read from an annual report)"),
    "status": ("补抓年报：跳过（这次筛选没有完成，先处理上面的问题）",
               "Fetching annual reports: skipped (the screen did not finish; see above)"),
    "store_busy": ("补抓年报：" + "数据库正被另一个 jevscreen 任务使用（例如后台的 crawl-descriptions），这次先不补抓",
                   "Fetching annual reports: the database is in use by another jevscreen task (e.g. a background "
                   "crawl-descriptions); fetch skipped this time"),
}


# the update pass did not run although annual reports are stored for companies the run read on a profile
UPDATE_PENDING = {"zh": "新存到本地的年报还没用来更新结果（{why}）；稍后运行 {cmd}",
                  "en": "The results are not updated with the newly stored annual reports yet ({why}); run {cmd} later"}
PENDING_WHY = {"no_budget": ("这次的预算已经用完", "no budget left for the update"),
               "budget_exhausted": ("更新的预算不够", "the update budget ran out"),
               "jev_unavailable": ("Jev 暂时不可用", "Jev is unavailable"), "jev_busy": ("Jev 正忙", "Jev is busy"),
               "store_busy": ("数据库正被另一个 jevscreen 任务使用", "the database is in use by another jevscreen task"),
               "no_update": ("用了 --no-update", "--no-update"), "not_written": ("更新没有完成", "the update did not finish"),
               "interrupted": ("补抓被中断", "the fetch was interrupted"),
               "inline_sieve": ("这次用的是内联 sieve", "the run used an inline sieve"),
               "error": ("更新出错", "the update failed")}
FETCH_LATER = "jevscreen fetch-docs latest"


def _skip_line(key: str, lang: str) -> str:
    zh, en = SKIP_LINES[key]
    return zh if lang == "zh" else en


def _write_atomic(path: Path, text: str) -> None:
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def annotate_outputs(result: dict[str, Any], out_dir: Path, pl: Any, oc: dict[str, Any],
                     summary: dict[str, Any]) -> dict[str, Any]:
    """No update pass: add layers['fetch'] and gaps['l2_doc_unavailable'] to the run's results.json and re-render
    report.md (atomic replace; the ranking itself is unchanged, so fetched / stored_since companies are rows too,
    as 'update_pending')."""
    from . import ondemand, report
    res = json.loads(json.dumps(result, default=str))
    res.setdefault("layers", {})["fetch"] = summary
    res.setdefault("gaps", {})["l2_doc_unavailable"] = ondemand.outcome_rows(pl, oc, pending=True)
    with contextlib.suppress(OSError):
        _write_atomic(out_dir / "results.json", json.dumps(res, ensure_ascii=False, indent=2, default=str))
        _write_atomic(out_dir / "report.md", report.render_markdown(res))
    return res


def _diff_en(before: dict[str, Any], after: dict[str, Any]) -> str:
    b = {r["company_key"]: r for r in before.get("rows") or []}
    a = {r["company_key"]: r for r in after.get("rows") or []}
    added = [a[k].get("name") or a[k]["security_id"] for k in a if k not in b]
    removed = [b[k].get("name") or b[k]["security_id"] for k in b if k not in a]
    lines = ["Update (newly fetched annual reports; L1 reused at $0)"]
    if not (added or removed):
        return "\n".join(lines + ["  list unchanged"])
    if removed:
        lines.append(f"  out {len(removed)}: " + " · ".join(removed))
    if added:
        lines.append(f"  in {len(added)}: " + " · ".join(added))
    return "\n".join(lines)


def run_fetch(cfg: Any, result: dict[str, Any], *, time_s: float, sources: list[str] | None = None,
              retry_failed: bool = False, update_budget: float, update: bool = True, mode: str = "auto",
              out: Callable[[str], None] = print, fetch_dir: Path | None = None, jev_factory: Any = None,
              on_progress: Callable[[int, int], None] | None = None, interrupt_stops: bool = False
              ) -> dict[str, Any]:
    """Plan, fetch and (when anything arrived, now or by an earlier fetch whose update was skipped: stored_since) run
    the update pass for a finished run `result` (results.json). Returns {'fetch': layers.fetch | None, 'result': the
    final result (updated or annotated), 'update': {...} | None, 'update_error': exit-worthy status or None,
    'store_busy': bool, 'blocked': bool, 'all_busy': bool, 'next_command': str | None}. When stored reports were not
    read (the update was skipped or failed) one line says so and layers.fetch.next_command names the command that
    finishes it. KeyboardInterrupt from a second Ctrl-C propagates (the phase-1 files stay).
    on_progress(done, total): companies settled during the fetch (ondemand.launch). interrupt_stops=True (the
    quickstart worker, whose SIGTERM means stop): after an interrupted fetch the update pass is skipped
    ('interrupted'), the run's files are annotated and KeyboardInterrupt is raised again."""
    from . import calib, cli, ondemand, screen
    say = _say(out)
    lang = ondemand.lang_of(result.get("idea"))
    run_id = result["run_id"]
    out_dir = Path(result["output_dir"])
    ret: dict[str, Any] = {"fetch": None, "result": result, "update": None, "update_error": None,
                           "store_busy": False, "blocked": False, "all_busy": False, "next_command": None}
    pl = ondemand.plan(cfg, run_id, sources=sources, retry_failed=retry_failed, time_s=time_s)
    if pl.store_busy:
        say(_skip_line("store_busy", lang))
        ret["store_busy"] = True
        ret["fetch"] = ondemand.skipped_summary(pl, "store_busy", mode=mode, time_s=time_s, base_run=run_id)
        return ret
    if not pl.entries:
        say(_skip_line("no_profile", lang))
        return ret
    say(ondemand.start_line(pl, lang, time_s))
    fetch_dir = fetch_dir or (out_dir / ("fetch" if mode == "auto" else
                                         f"fetch-{screen.store.now_utc().strftime('%Y%m%d%H%M%S')}"))
    if pl.n_planned:
        with cli.sigterm_as_interrupt():
            fr = ondemand.launch(cfg, pl, time_s=time_s, fetch_dir=fetch_dir, retry_failed=retry_failed, lang=lang,
                                 out=say, mode=mode, on_progress=on_progress)
    else:
        oc = ondemand.outcomes(pl, {}, {}, {}, {}, {})
        fr = {"summary": ondemand.fetch_summary(pl, oc, {}, {}, {}, seconds=0.0, time_s=time_s, mode=mode),
              "outcomes": oc, "fetched": [], "docs_by_ck": {}, "interrupted": False, "store_ok": True}
    summ = fr["summary"]
    ret["fetch"] = summ
    ret["blocked"] = any(v.get("status") == "blocked" for v in summ["sources"].values())
    ret["all_busy"] = bool(summ["sources"]) and all(v.get("status") == "source_busy"
                                                     for v in summ["sources"].values())
    upd: dict[str, Any] | None = None
    final = None
    skip_why = None
    pending = ondemand.pending_update(fr)
    if not fr.get("store_ok", True):
        skip_why = "store_busy"
    elif interrupt_stops and fr.get("interrupted"):
        skip_why = "interrupted"
    elif not pending:
        skip_why = "nothing_fetched"
    elif not update:
        skip_why = "no_update"
    elif update_budget <= 0:
        skip_why = "no_budget"
    sieve_arg = None
    if skip_why is None:
        sieve_arg, why = sieve_for_update(cfg, result)
        if why:
            skip_why = why
            if why == "sieve_changed":
                say(SIEVE_CHANGED[lang].format(run=run_id))
    if skip_why is None:
        summ["update"] = None
        try:
            kw = {"jev_factory": jev_factory} if jev_factory is not None else {}
            updated = screen.screen(cfg, result["idea"], from_run=run_id, supersedes=run_id, out_dir=out_dir,
                                    budget_usd=round(update_budget, 6), sieve=sieve_arg,
                                    fetch_info=ondemand.fetch_info(fr), **kw)
        except Exception as e:  # noqa: BLE001 - Jev errors / a changed base: the phase-1 report stays
            code = cli._jev_exit(e)
            if code is None and not isinstance(e, ValueError):
                raise
            status = {EXIT_BUSY: "jev_busy", EXIT_JEV_UNAVAILABLE: "jev_unavailable",
                      EXIT_BUDGET: "budget_exhausted"}.get(code, "error")
            upd = {"skipped": status, "error": f"{type(e).__name__}: {str(e)[:200]}"}
            ret["update_error"] = status
        else:
            upd = {"run_id": updated["run_id"], "status": updated["status"], "cost_usd": updated.get("cost_usd"),
                   "report_path": str(out_dir / "report.md") if updated.get("outputs_written") else None,
                   "written": bool(updated.get("outputs_written"))}
            if updated["status"] in ("budget_exhausted", "jev_unavailable", "jev_busy"):
                ret["update_error"] = updated["status"]
            elif ((updated.get("layers") or {}).get("l2") or {}).get("by_status", {}).get("skipped_budget"):
                ret["update_error"] = "budget_exhausted"      # L2 re-reads ran out of the update budget
            if updated.get("outputs_written"):
                final = updated
                n_new = sum(1 for r in [*(updated.get("rows") or []), *(updated.get("unverified") or [])]
                            if r.get("doc_fetch") == "fetched_now")
                n_new = (updated.get("funnel") or {}).get("l2_doc_fetched", n_new)
                if lang == "zh":
                    say(calib.render_diff_zh(result, updated, title=ondemand.DIFF_TITLE_ZH))
                else:
                    say(_diff_en(result, updated))
                say(ondemand.end_line(n_new, float(updated.get("cost_usd") or 0.0),
                                      int((updated.get("funnel") or {}).get("l2_doc_unavailable") or 0), lang))
            else:
                upd["skipped"] = "not_written"
    else:
        upd = {"skipped": skip_why}
    summ["update"] = upd
    ret["update"] = upd
    if final is None and (pending or skip_why == "store_busy"):
        # stored annual reports the ranking did not read: say so, and name the command that finishes the update
        sieve_skip = (upd or {}).get("skipped") in ("sieve_changed", "inline_sieve")
        nxt = (f"jevscreen screen --from-run {run_id} --sieve PATH" if sieve_skip else FETCH_LATER)
        why = ret["update_error"] or (upd or {}).get("skipped") or "error"
        if (upd or {}).get("skipped") != "sieve_changed":      # SIEVE_CHANGED above already names its command
            zh, en = PENDING_WHY.get(why, PENDING_WHY["error"])
            say(UPDATE_PENDING[lang].format(why=zh if lang == "zh" else en, cmd=nxt))
        summ.update(ondemand.summary_texts(pl, fr["outcomes"], pending_next=nxt), next_command=nxt)
        ret["next_command"] = nxt
    if final is not None:
        # the update run wrote layers.fetch before it knew its own outcome: record it in its results.json too
        final.setdefault("layers", {})["fetch"] = summ
        with contextlib.suppress(OSError):
            _write_atomic(out_dir / "results.json", json.dumps(final, ensure_ascii=False, indent=2, default=str))
        ret["result"] = final
    else:
        ret["result"] = annotate_outputs(result, out_dir, pl, fr["outcomes"], summ)
    say(summ["summary_zh"] if lang == "zh" else summ["summary_en"])
    if skip_why == "interrupted":
        raise KeyboardInterrupt("annual-report fetch interrupted")
    return ret


def fetch_phase(args: argparse.Namespace, cfg: Any, result: dict[str, Any], out: Callable[[str], None] = print
                ) -> dict[str, Any]:
    """screen's phase 2 (after the phase-1 console summary). Returns the final result (for the cards)."""
    from . import ondemand
    lang = ondemand.lang_of(result.get("idea"))
    if result.get("status") not in ("ok", "partial"):
        out(_skip_line("status", lang))
        return result
    l2 = (result.get("layers") or {}).get("l2") or {}
    if not l2.get("inputs"):
        out(_skip_line("no_l2", lang))
        return result
    if not l2.get("profile_inputs"):
        out(_skip_line("no_profile", lang))
        return result
    budget_left = float(result.get("budget_usd") or 0.0) - float(result.get("cost_usd") or 0.0)
    try:
        ret = run_fetch(cfg, result, time_s=float(getattr(args, "fetch_time", 120.0)),
                        sources=getattr(args, "fetch_sources", None),
                        update_budget=min(ondemand.UPDATE_BUDGET_DEFAULT, max(0.0, budget_left)), out=out)
    except (ValueError, OSError) as e:
        print(f"warning: annual-report fetch skipped: {type(e).__name__}: {str(e)[:200]}", file=sys.stderr)
        return result
    except Exception as e:  # noqa: BLE001 - a fetch problem never changes the screen's result or exit code
        from . import store
        if isinstance(e, store.StoreLocked):
            out(_skip_line("store_busy", lang))
        else:
            print(f"warning: annual-report fetch stopped: {type(e).__name__}: {str(e)[:200]}; the first report is "
                  "kept", file=sys.stderr)
        return result
    return ret["result"]


def fetch_mode(args: argparse.Namespace) -> str:
    v = getattr(args, "fetch_docs", None)
    if v is None:
        return "off" if (getattr(args, "from_run", None) or getattr(args, "dry_run", False)) else "auto"
    return "off" if getattr(args, "dry_run", False) else v


def dry_run_text(args: argparse.Namespace, cfg: Any, result: dict[str, Any], text: str) -> str:
    """The dry run's console text with the fetch suffix on the 'total estimate:' line and the readiness lines."""
    from . import ondemand
    if getattr(args, "fetch_docs", None) == "off" or getattr(args, "from_run", None):
        return text
    lang = ondemand.lang_of(result.get("idea"))
    suffix = ondemand.dry_run_suffix(lang, float(getattr(args, "fetch_time", 120.0)))
    lines = text.split("\n")
    for i, line in enumerate(lines):
        if line.startswith("total estimate:"):
            lines[i] = line + suffix
            break
    lines += ondemand.readiness_lines(ondemand.readiness(cfg), lang)
    return "\n".join(lines)


# ---------------------------------------------------------------------------------------------------------------
# jevscreen fetch-docs

def _emit(obj: Any) -> None:
    from .cli import _emit as emit
    emit(obj)


def after_update(cfg: Any, replaced_run_id: str, ret: dict[str, Any], *, had_cards: bool, had_page: bool, lang: str,
                 out: Callable[[str], None]) -> None:
    """After `fetch-docs` wrote an update run into the folder: the folder's cards and page belonged to the replaced
    run (answer refuses a deck of another run), so rebuild the cards, rewrite page.html and the stable page with the
    new run and deck_id, and point a finished quickstart job of that run at the update (quickstart --status would
    otherwise keep the replaced run and its 'run fetch-docs' hint). Every step is a warning at worst."""
    from . import cli, quickstart, quickstart_cli
    new = ret["result"]
    deck, cards_path = None, None
    if had_cards or had_page:
        deck, cards_path = cli._write_run_cards(cfg, new, cli.CARDS_DEFAULT)
        if cards_path is not None:
            out(("校准卡已按更新后的结果重建：" if lang == "zh" else "Calibration cards rebuilt for the updated results: ")
                + str(cards_path))
    ret["cards_path"] = cards_path
    path = data = None
    if had_page:
        path, data = quickstart_cli.write_after_cards(cfg, new, deck)
    try:
        quickstart.follow_update(cfg, replaced_run_id, new, deck, path, data)
    except Exception as e:  # noqa: BLE001 - the report and page are written; the job view is a convenience
        print(f"warning: quickstart job not updated ({type(e).__name__}: {str(e)[:200]})", file=sys.stderr)


def cmd_fetch_docs(args: argparse.Namespace, cfg: Any) -> int:
    from . import cli, ondemand, store
    try:
        with cli._read_session(cfg) as con:
            run_id, out_dir, result = cli._resolve_run(con, args.target)
    except store.StoreLocked as e:
        return cli._locked("fetch-docs", e)
    except ValueError as e:
        print(f"error: fetch-docs: {e}", file=sys.stderr)
        return EXIT_ERROR
    result = dict(result, output_dir=str(out_dir))
    if result.get("run_id") != run_id:
        # the folder already holds an update of this run: continue from the run the files belong to
        run_id = result["run_id"]
    lang = ondemand.lang_of(result.get("idea"))
    lines: list[str] = []
    out = (lambda s: lines.append(s)) if args.json else print
    if args.dry_run:
        try:
            pl = ondemand.plan(cfg, run_id, sources=args.sources, retry_failed=args.retry_failed, time_s=args.time)
        except ValueError as e:
            print(f"error: fetch-docs: {e}", file=sys.stderr)
            return EXIT_ERROR
        if pl.store_busy:
            print(_skip_line("store_busy", lang), file=sys.stderr)
            return EXIT_LOCKED
        info = {"command": "fetch-docs", "run_id": run_id, "dry_run": True, "planned": pl.planned,
                "est_seconds": pl.est_seconds, "est_mb": pl.est_mb, "skipped": pl.skip_counts(),
                "profile_inputs": len(pl.entries), "readiness": {k: pl.readiness.get(k) for k in
                                                                 ("pdf_reader", "sec_email", "opendart",
                                                                  "mops_consent")}}
        if args.json:
            _emit(info)
        else:
            print(ondemand.start_line(pl, lang, args.time))
            print(("预计 " if lang == "zh" else "estimate: ") + f"~{pl.est_seconds:.0f} s, ~{pl.est_mb:.0f} MB")
            for code, n in pl.skip_counts().items():
                print(f"  {ondemand.short_reason(code, lang)}: {n}")
            for line in ondemand.readiness_lines(pl.readiness, lang):
                print(line)
        return EXIT_OK
    had_cards = (out_dir / "cards.json").exists()
    had_page = (out_dir / "page.html").exists()
    try:
        ret = run_fetch(cfg, result, time_s=args.time, sources=args.sources, retry_failed=args.retry_failed,
                        update_budget=args.budget, update=not args.no_update, mode="fetch-docs", out=out)
    except ValueError as e:
        print(f"error: fetch-docs: {e}", file=sys.stderr)
        return EXIT_ERROR
    except KeyboardInterrupt:
        print("error: fetch-docs: interrupted (the report is unchanged)", file=sys.stderr)
        return EXIT_INTERRUPTED
    summ = ret["fetch"]
    cards_path = None
    if (ret["update"] or {}).get("written"):
        after_update(cfg, run_id, ret, had_cards=had_cards, had_page=had_page, lang=lang, out=out)
        cards_path = ret.get("cards_path")
    if ret["store_busy"] and (summ or {}).get("status") == "skipped":
        code = EXIT_LOCKED
    elif ret["blocked"]:
        code = EXIT_BLOCKED
    elif ret["all_busy"] or ret["update_error"] == "jev_busy":
        code = EXIT_BUSY
    elif ret["update_error"] == "budget_exhausted":
        code = EXIT_BUDGET
    elif ret["update_error"] == "jev_unavailable":
        code = EXIT_JEV_UNAVAILABLE
    else:
        code = EXIT_OK
    if args.json:
        by_reason = (summ or {}).get("by_reason") or {}
        nxt = ret.get("next_command") or (FETCH_LATER if any(by_reason.get(c) for c in ondemand.DEFERRED) else None)
        qs = (summ or {}).get("questions") or []
        _emit({"command": "fetch-docs", "run_id": run_id, "status": (summ or {}).get("status") or "skipped",
               "exit_code": code, "fetch": summ, "update": ret["update"] if (ret["update"] or {}).get("run_id")
               else None, "update_skipped": (ret["update"] or {}).get("skipped"),
               "summary_zh": (summ or {}).get("summary_zh"), "summary_en": (summ or {}).get("summary_en"),
               "questions": qs, "next_command": nxt, "ask_human": bool(qs),
               "cards_path": str(cards_path) if cards_path else None, "console": lines})
    return code


COMMANDS: dict[str, Callable[[argparse.Namespace, Any], int]] = {"fetch-docs": cmd_fetch_docs}
