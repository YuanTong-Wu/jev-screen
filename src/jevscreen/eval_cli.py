"""`jevscreen eval`: the open evaluation set (jevscreen.evalset, docs/EVAL.md).

    jevscreen eval check [--set DIR]                         free: every eval file is well formed
    jevscreen eval score RUN --idea ID [--set DIR] [--json]  free: one screen run against one labelled idea
    jevscreen eval run --budget-each USD --budget-total USD [--ideas a,b] [--set DIR] [--json]
                                                             PAID: screen every idea (Jev), then score them

`eval run` screens with each idea's own English sentence (never the local translation model), floor and countries,
no sieve (the eval measures the system, not a calibration). It never spends more than --budget-each per idea, and
it stops before an idea whose budget could take the total past --budget-total (no defaults: the human says both
numbers, and the total is the amount they approve). Its report and scores go to a new folder
<home>/evals/<timestamp>/ (report.md, scores.json), also when a screen fails or the run stops early; only runs that
finished (ok / partial) enter the means. Exit codes: 0 ok, 1 an eval file is broken, a run is unknown or one idea's
screen failed (the others still scored), 2 usage, 3 the database is locked, 4 another process is using Jev (stopped;
run again when it finishes), 5 a screen ran out of budget or --budget-total stopped the run (the others still
scored), 6 Jev is unavailable, 130 interrupted.
"""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any, Callable

EXIT_OK, EXIT_ERROR, EXIT_LOCKED, EXIT_BUSY, EXIT_BUDGET, EXIT_JEV, EXIT_INTERRUPTED = 0, 1, 3, 4, 5, 6, 130
STATUS_OF = {EXIT_OK: "ok", EXIT_ERROR: "error", EXIT_LOCKED: "locked", EXIT_BUSY: "jev_busy",
             EXIT_BUDGET: "budget_exhausted", EXIT_JEV: "ai_unavailable", EXIT_INTERRUPTED: "interrupted"}
# which exit code wins when several ideas end differently: a stop over a failed idea over a budget stop
_WEIGHT = {EXIT_OK: 0, EXIT_BUDGET: 1, EXIT_ERROR: 2, EXIT_LOCKED: 3, EXIT_BUSY: 3, EXIT_JEV: 3, EXIT_INTERRUPTED: 3}


def default_set_dir() -> Path:
    """evals/ideas of this checkout (an editable install); `--set` otherwise."""
    return Path(__file__).resolve().parents[2] / "evals" / "ideas"


def add_parsers(sub: Any) -> None:
    ev = sub.add_parser("eval", help="the open evaluation set: check it, score a run against it (free), or screen "
                                     "every idea and score them (paid)")
    evs = ev.add_subparsers(dest="eval_command", required=True)
    for name, text in (("check", "check that every eval file is well formed (free)"),
                       ("score", "score one screen run against one labelled idea (free)"),
                       ("run", "screen every labelled idea with Jev (paid; --budget-each and --budget-total are "
                               "required), then score")):
        p = evs.add_parser(name, help=text)
        p.add_argument("--set", type=Path, default=None, metavar="DIR",
                       help="the folder of eval files (default: evals/ideas of this checkout)")
        p.add_argument("--json", action="store_true", help="print JSON (for AI agents)")
        if name == "score":
            p.add_argument("run", metavar="RUN_ID|OUT_DIR", help="the screen run")
            p.add_argument("--idea", required=True, metavar="ID", help="the eval idea's id")
        if name == "run":
            p.add_argument("--budget-each", type=float, required=True, metavar="USD",
                           help="the Jev budget of each idea's screen (no default: say the number)")
            p.add_argument("--budget-total", type=float, required=True, metavar="USD",
                           help="the most the whole run may spend: it stops before an idea whose budget could pass "
                                "it (no default: the amount the human approved)")
            p.add_argument("--ideas", default=None, metavar="a,b", help="only these eval ideas (ids)")
            p.add_argument("--reads", type=int, default=None, metavar="K", help="L2 reads near the boundary")
            p.add_argument("--read-offset", type=int, default=0, metavar="N",
                           help="fresh reads N..N+K-1 for the boundary items (a run-to-run noise check)")


def _emit(obj: Any) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=True, default=str))


def _set_dir(args: argparse.Namespace) -> Path:
    return args.set if args.set is not None else default_set_dir()


def cmd_eval(args: argparse.Namespace, cfg: Any) -> int:
    from . import evalset
    try:
        if args.eval_command == "check":
            return _check(args)
        if args.eval_command == "score":
            return _score(args, cfg)
        return _run(args, cfg)
    except evalset.EvalError as e:
        if getattr(args, "json", False):
            _emit({"command": "eval", "status": "error", "error": str(e)})
        else:
            print(f"eval: {e}")
        return EXIT_ERROR


def _check(args: argparse.Namespace) -> int:
    from . import evalset
    d = _set_dir(args)
    files = sorted(d.glob("*.json"))
    probs: dict[str, list[str]] = {}
    first: dict[str, str] = {}
    for f in files:
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            probs[f.name] = [f"{type(e).__name__}: {e}"]
            continue
        p = evalset.check(data) if isinstance(data, dict) else ["not a JSON object"]
        iid = data.get("id") if isinstance(data, dict) else None
        if isinstance(iid, str) and iid in first:
            p.append(f"id {iid} is also the id of {first[iid]}")
        elif isinstance(iid, str):
            first[iid] = f.name
        if p:
            probs[f.name] = p
    out = {"command": "eval check", "status": "ok" if files and not probs else "error", "set": str(d),
           "files": len(files), "problems": probs}
    if args.json:
        _emit(out)
    else:
        print(f"{len(files)} eval files in {d}: " + ("ok" if out["status"] == "ok" else
                                                     "no files" if not files else f"{len(probs)} with problems"))
        for name, ps in probs.items():
            for p in ps:
                print(f"  {name}: {p}")
    return EXIT_OK if out["status"] == "ok" else EXIT_ERROR


def _score(args: argparse.Namespace, cfg: Any) -> int:
    from . import cli, evalset, store
    data = evalset.load_dir(_set_dir(args), [args.idea])[0]
    try:
        with store.session(cfg, read_only=True, wait_s=60.0) as con:
            _rid, _out, result = cli._resolve_run(con, args.run, lang="en")
            keys = _label_keys(con, [data])
    except (ValueError, store.StoreLocked) as e:
        raise evalset.EvalError(str(e)) from None
    s = evalset.score(data, result, keys=keys)
    if args.json:
        _emit({"command": "eval score", "status": "ok", "score": s})
    else:
        print(evalset.report_md([s], evalset.aggregate([s]), title=f"eval: {data['id']}"), end="")
    return EXIT_OK


def _label_keys(con: Any, sets: list[dict[str, Any]]) -> dict[str, str]:
    """{label id: company_key} of every label the store knows (a label then also matches the company's other line)."""
    from . import evalset
    sids = sorted({evalset._norm_sid(x["security_id"]) for d in sets for x in d["labels"]})
    if not sids:
        return {}
    return {sid: ck or f"sec:{sid}" for sid, ck in con.execute(
        "SELECT security_id, company_key FROM securities WHERE security_id IN (SELECT unnest(?::VARCHAR[]))",
        [sids]).fetchall()}


def _store_keys(cfg: Any, sets: list[dict[str, Any]]) -> dict[str, str] | None:
    """_label_keys from a short read of the store; None (labels match by security_id only) when there is no store or
    it is locked."""
    from . import store
    if not Path(cfg.db_path).exists():
        return None
    try:
        with store.session(cfg, read_only=True, wait_s=60.0) as con:
            return _label_keys(con, sets)
    except (store.StoreLocked, store.duckdb.Error):
        return None


def _new_out_dir(cfg: Any) -> Path:
    """<home>/evals/<timestamp>, or <timestamp>-2, -3 ... : a folder no other eval run writes into (mkdir is atomic,
    so two runs started in the same second get two folders)."""
    from . import store
    base = Path(cfg.home) / "evals"
    base.mkdir(parents=True, exist_ok=True)
    stamp = store.now_utc().strftime("%Y%m%d-%H%M%S")
    path, n = base / stamp, 2
    while True:
        try:
            path.mkdir()
            return path
        except FileExistsError:
            path, n = base / f"{stamp}-{n}", n + 1


def _positive(name: str, v: Any) -> float:
    from . import evalset
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v <= 0:
        raise evalset.EvalError(f"{name} must be a number above 0")
    return float(v)


def _run(args: argparse.Namespace, cfg: Any, screen_fn: Callable | None = None) -> int:
    """Screen each idea, score it, write <home>/evals/<ts>/report.md and scores.json (also when a screen fails or the
    run stops: every idea already paid for is kept)."""
    from . import cli, evalset, screen, store
    each = _positive("--budget-each", args.budget_each)
    total = _positive("--budget-total", args.budget_total)
    if each > total:
        raise evalset.EvalError(f"--budget-each ${each:g} is above --budget-total ${total:g}: no idea would fit")
    if args.reads is not None and (isinstance(args.reads, bool) or int(args.reads) < 1):
        raise evalset.EvalError("--reads must be 1 or more")
    if int(args.read_offset or 0) < 0:
        raise evalset.EvalError("--read-offset must be 0 or more")
    ids = [x.strip() for x in (args.ideas or "").split(",") if x.strip()] or None
    sets = evalset.load_dir(_set_dir(args), ids)
    keys = _store_keys(cfg, sets)
    out_dir = _new_out_dir(cfg)
    run = screen_fn or screen.screen
    # a Jev status (returned, or its error escaped screen(); cli._jev_exit gives the same exit codes as here)
    jev_status = {EXIT_BUSY: screen.STATUS_BUSY, EXIT_JEV: screen.STATUS_UNAVAILABLE, EXIT_BUDGET: screen.STATUS_BUDGET}
    jev_exit = {v: k for k, v in jev_status.items()}
    scores: list[dict[str, Any]] = []
    code, spent = EXIT_OK, 0.0
    current: dict[str, Any] | None = None       # the idea last sent to screen() (Ctrl-C there: paid, cost unknown)

    def left_out(entry: dict[str, Any], c: int) -> None:
        nonlocal code
        scores.append(entry)
        code = c if _WEIGHT[c] > _WEIGHT[code] else code
        if not args.json:
            print(f"{entry['id']}: {entry['status']}" + (f" ({entry['error']})" if entry.get("error") else ""))

    if not args.json:
        print(f"{len(sets)} ideas, at most ${each:.2f} each; the run stops before the total could pass ${total:.2f}")
    try:
        for data in sets:
            if spent + each > total + 1e-9:        # this idea's own budget could take the total past the cap
                left_out(evalset.unscored(data, "not_run", error=f"--budget-total ${total:g} reached"), EXIT_BUDGET)
                break
            kw: dict[str, Any] = {"budget_usd": each, "countries": data.get("countries") or None, "sieve": "none",
                                  "min_mcap_usd": 1e9 if data.get("min_mcap_usd") is None
                                  else float(data["min_mcap_usd"]),
                                  "out_dir": out_dir / data["id"], "idea_en": data.get("idea_en") or None,
                                  "translate": False, "read_offset": int(args.read_offset or 0)}
            if args.reads:
                kw["reads"] = int(args.reads)
            current = data
            try:
                res = run(cfg, data["idea"], **kw)
            except Exception as e:  # noqa: BLE001 - mapped like cmd_screen: this idea's fields, the lock, Jev errors
                c = cli._jev_exit(e)
                if c is not None:
                    status = jev_status[c]
                elif isinstance(e, ValueError):     # e.g. an idea_en that names a company, an unknown country
                    status, c = "error", EXIT_ERROR
                elif isinstance(e, store.StoreLocked):
                    status, c = "locked", EXIT_LOCKED
                else:
                    raise
                spent += each                        # what it spent is unknown: count its whole budget
                left_out(evalset.unscored(data, status, error=f"{type(e).__name__}: {str(e)[:300]}"), c)
                if c in (EXIT_ERROR, EXIT_BUDGET):   # this idea only
                    continue
                break
            cost = res.get("cost_usd")
            spent += each if cost is None else float(cost)
            status = res.get("status")
            if status in (screen.STATUS_BUSY, screen.STATUS_UNAVAILABLE):     # nothing (or only part) was read
                left_out(evalset.unscored(data, status, run_id=res.get("run_id"), cost_usd=cost), jev_exit[status])
                break
            s = evalset.score(data, res, keys=keys)
            if status == screen.STATUS_BUDGET:       # scored for the record, left out of the means
                left_out(s, EXIT_BUDGET)
                continue
            scores.append(s)
            if not args.json:
                a = s["at"]["10"]
                print(f"{data['id']}: P@10 {a['precision'] if a['precision'] is not None else '-'} "
                      f"({a['right']}/{a['edge']}/{a['wrong']}/{a['unlabelled']} unlabelled), "
                      f"${float(s.get('cost_usd') or 0):.4f}")
    except KeyboardInterrupt:
        code = EXIT_INTERRUPTED
        if current is not None and current["id"] not in {s["id"] for s in scores}:     # stopped inside its screen
            spent += each                            # what it spent is unknown: count its whole budget
            left_out(evalset.unscored(current, "interrupted"), EXIT_INTERRUPTED)
    finally:        # the ideas already paid for are written even when an unexpected error escapes
        done = {s["id"] for s in scores}
        scores += [evalset.unscored(d, "not_run") for d in sets if d["id"] not in done]
        agg = evalset.aggregate(scores)
        (out_dir / "scores.json").write_text(json.dumps({"scores": scores, "aggregate": agg}, ensure_ascii=False,
                                                        indent=1, sort_keys=True, default=str) + "\n",
                                             encoding="utf-8")
        (out_dir / "report.md").write_text(evalset.report_md(scores, agg), encoding="utf-8")
    if args.json:
        _emit({"command": "eval run", "status": STATUS_OF[code], "out_dir": str(out_dir), "aggregate": agg,
               "scores": scores, "budget": {"each": each, "total": total, "spent_usd": round(spent, 6)}})
    else:
        if code in (EXIT_BUSY, EXIT_LOCKED):
            print("stopped: another process is using " + ("Jev" if code == EXIT_BUSY else "the database")
                  + "; run again when it finishes (answers already paid for come from the cache)")
        print(f"spent at most ${spent:.4f} of ${total:.2f}; report: {out_dir / 'report.md'}")
    return code


COMMANDS: dict[str, Callable[[argparse.Namespace, Any], int]] = {"eval": cmd_eval}
