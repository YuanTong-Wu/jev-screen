"""CLI of `jevscreen quickstart` and `jevscreen page` (wired into jevscreen.cli with two lines, like agent_cli).

- `jevscreen quickstart "<idea>" [--idea-en TEXT] [--approve-budget USD] [--min-mcap 1e9] [--countries A,B]
  [--lang auto|zh|en] [--fd-file PATH] [--no-open] [--retry] [--new-run] [--json]`: the fast front command (no
  network; spawns the detached worker). Exit codes = the JSON 'status' (docs/AGENT_API.md): 0 running / done /
  partial, 1 failed, 2 blocked, 3 store busy, 5 budget exhausted, 6 AI unavailable, 10 needs the human, 11 needs
  the agent, 12 declined. An argparse usage error exits 2 with no JSON (a typo, not a block).
- `jevscreen quickstart --status [IDEA | --key K] [--wait S] [--json]`: the same JSON from the job file; --wait
  blocks until the state changes (pass S, max 110). A job that waits on nothing any more is started again here, as
  the front would (see quickstart.status).
- `jevscreen page [RUN_ID|OUT_DIR|latest] [--lang zh|en] [--open] [--json]`: rebuild page.html of a screen run ($0).
With --json, stdout is JSON only; otherwise the human text in the job's language. On a terminal (no --json) the
front follows the worker's progress until it stops; Ctrl-C stops the watching only, never the worker.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any, Callable


def add_parsers(sub: Any) -> None:
    from . import cli
    qs = sub.add_parser("quickstart", help="from an idea to a ranked result page in one round of questions (the "
                        "AI-agent fast path; returns in seconds and runs the work in the background)")
    qs.add_argument("idea", nargs="?", default=None, help="the investment idea, any language (quote it)")
    qs.add_argument("--idea-en", default=None, metavar="TEXT",
                    help="one English sentence (<= 400 chars) for the AI questions, written by the agent for a "
                         "non-English idea; the human sees it in the spend question")
    qs.add_argument("--approve-budget", type=cli.nonneg_float, default=None, metavar="USD",
                    help="the total the human approved (after their explicit yes); $1 by default in the question")
    qs.add_argument("--min-mcap", type=cli.nonneg_float, default=None, metavar="USD",
                    help="minimum market cap (default 1e9)")
    qs.add_argument("--countries", type=cli.parse_list, default=None, metavar="A,B",
                    help="only these countries / regions (ISO-2 codes such as CN,HK)")
    qs.add_argument("--lang", choices=("auto", "zh", "en"), default="auto",
                    help="language of the texts and the page (auto: from the idea, else LANG)")
    qs.add_argument("--fd-file", default=None, metavar="PATH",
                    help="a manually downloaded FinanceDatabase equities.bz2 (when both mirrors fail)")
    qs.add_argument("--no-open", action="store_true", help="do not open the result page in a browser")
    qs.add_argument("--retry", action="store_true", help="retry a network step that failed less than 15 min ago")
    qs.add_argument("--new-run", action="store_true", help="screen again even when an identical result exists "
                    "(the AI answers cache makes it cheap)")
    qs.add_argument("--fill-descriptions", choices=("yes", "no"), default=None,
                    help="the human's answer to the optional profile-fill question (pending item fill_descriptions): "
                         "yes fills the missing profiles in the background and re-ranks; no stops the question")
    qs.add_argument("--status", action="store_true", help="print the job's state (starts the background work "
                    "again only when it waits on nothing; never anything that needs an answer)")
    qs.add_argument("--key", default=None, metavar="IDEA_KEY", help="with --status: the job's idea key")
    qs.add_argument("--wait", type=float, default=None, metavar="S",
                    help="with --status: wait up to S seconds (max 110) for the state to change")
    qs.add_argument("--json", action="store_true", help="print JSON only (for AI agents)")
    pg = sub.add_parser("page", help="rebuild the result page (page.html) of a screen run (free)")
    pg.add_argument("target", nargs="?", default="latest", metavar="RUN_ID|OUT_DIR|latest")
    pg.add_argument("--lang", choices=("zh", "en"), default=None, help="first language of the page (default: the "
                    "idea's)")
    pg.add_argument("--open", action="store_true", help="open it in the browser")
    pg.add_argument("--text", action="store_true", help="also print the ranked list as plain text (for an AI agent "
                    "that cannot open a browser; with --json it is the 'text' field)")
    pg.add_argument("--json", action="store_true", help="print JSON")


def _emit(obj: Any) -> None:
    from .cli import _emit as emit
    emit(obj)


def _human(out: dict[str, Any]) -> str:
    lang = out.get("lang") if out.get("lang") in ("zh", "en") else "en"
    lines = [out.get(f"text_{lang}") or ""]
    for it in out.get("pending") or []:
        if it.get("ask_agent"):
            lines.append("[agent] " + it["instructions_en"])
    if out.get("idea_en_problems"):
        lines.append("[agent] --idea-en refused: " + "; ".join(out["idea_en_problems"]))
        if out.get("rerun_command"):
            lines.append("[agent] rerun: " + out["rerun_command"])
    if out.get("poll_command"):
        lines.append(("进度：" if lang == "zh" else "Progress: ") + out["poll_command"])
    return "\n".join(x for x in lines if x)


def follow(cfg, key: str, *, stream=None, poll: Callable[..., Any] | None = None, max_s: float = 3600.0
           ) -> dict[str, Any] | None:
    """Print progress lines until the worker stops (terminal only). Ctrl-C stops the watching, not the worker."""
    from . import quickstart as qs
    stream = stream or sys.stderr
    poll = poll or qs.status
    last = None
    out = None
    t_end = time.monotonic() + max_s
    try:
        while time.monotonic() < t_end:
            out = poll(cfg, key, wait_s=10.0)
            if out is None:
                return None
            lang = out.get("lang") if out.get("lang") in ("zh", "en") else "en"
            text = (out.get("progress") or {}).get(f"text_{lang}")
            if text and text != last:
                print(text, file=stream, flush=True)
                last = text
            if out["status"] != "running":
                return out
    except KeyboardInterrupt:
        print("(stopped watching; the work goes on in the background)", file=stream)
    return out


def cmd_quickstart(args: argparse.Namespace, cfg: Any) -> int:
    from . import quickstart as qs
    if args.status:
        out = qs.status(cfg, args.key, idea=args.idea, wait_s=args.wait or 0.0)
        if out is None:
            msg = {"command": "quickstart", "status": "failed", "exit_code": 1, "error": "no quickstart job found"}
            if args.json:
                _emit(msg)
            else:
                print("error: quickstart: no job found (start one with jevscreen quickstart \"<idea>\")",
                      file=sys.stderr)
            return 1
        if args.json:
            _emit(out)
        else:
            print(_human(out))
        return int(out["exit_code"])
    if not args.idea or not args.idea.strip():
        print("usage: jevscreen quickstart \"<idea>\" [options]   (or --status)", file=sys.stderr)
        return 2
    try:
        out = qs.front(cfg, args.idea, idea_en=args.idea_en, approve_budget=args.approve_budget,
                       min_mcap=args.min_mcap, countries=args.countries, lang=args.lang, fd_file=args.fd_file,
                       no_open=args.no_open, retry=args.retry, new_run=args.new_run,
                       fill_descriptions=getattr(args, "fill_descriptions", None))
    except qs.JobSecret as e:
        print(f"error: quickstart: {e}", file=sys.stderr)
        return 1
    if args.json:
        _emit(out)
        return int(out["exit_code"])
    print(_human(out))
    try:
        tty = sys.stdout.isatty()
    except (AttributeError, ValueError):
        tty = False
    if tty and out["status"] == "running":
        final = follow(cfg, out["idea_key"])
        if final is not None and final["status"] != "running":
            print(_human(final))
            return int(final["exit_code"])
    return int(out["exit_code"])


def cmd_page(args: argparse.Namespace, cfg: Any) -> int:
    from . import calib, cli, page, quickstart as qs
    try:
        with cli._read_session(cfg) as con:
            run_id, out_dir, result = cli._resolve_run(con, args.target)
    except ValueError as e:
        print(f"error: page: {e}", file=sys.stderr)
        return 1
    deck = None
    if (Path(out_dir) / "cards.json").exists():
        try:
            deck = calib.load_deck(out_dir)
        except ValueError:
            deck = None
    lang = args.lang or page_lang(cfg, result.get("idea") or "")
    path, data = page.write_page(cfg, out_dir, result, deck, lang=lang)
    if path is None:
        return 1
    stable = page.stable_path(cfg, result["idea"]) if page.stable_holds(cfg, result["idea"], run_id) else None
    show = stable or path          # the idea's one stable page whenever it holds this run (the newest)
    opened = page.open_page(show) if args.open else False
    info = {"command": "page", "status": "ok", "run_id": run_id, "page": str(show),
            "page_uri": Path(show).resolve().as_uri(), "stable_page": str(stable) if stable else None,
            "run_page": str(path), "page_opened": opened, "deck_id": (deck or {}).get("deck_id"),
            "summary": page.summary_of(data, deck)}
    if getattr(args, "text", False):
        info["text"] = page.render_text(data, lang)
    if args.json:
        _emit(info)
    else:
        if info.get("text"):
            print(info["text"])
        print(("已在浏览器打开：" if lang == "zh" else "Opened: ") + info["page_uri"] if opened else
              ("用浏览器打开这个文件：" if lang == "zh" else "Open this file in a browser: ") + info["page_uri"])
    return 0


COMMANDS: dict[str, Callable[[argparse.Namespace, Any], int]] = {"quickstart": cmd_quickstart, "page": cmd_page}


def page_lang(cfg, idea: str) -> str:
    """The page language of an idea: its quickstart job's (the human's --lang), else detected from the idea."""
    from . import quickstart as qs
    job = qs.load_job(cfg, qs.idea_key(idea)) if idea.strip() else None
    if job and job.get("lang") in ("zh", "en"):
        return job["lang"]
    return qs.detect_lang(idea)


def write_after_cards(cfg, result: dict[str, Any], deck: dict[str, Any] | None, *,
                      lang: str | None = None) -> tuple[Any, dict[str, Any] | None]:
    """cli hook: the page of a screen / answer / cards run, written after its cards (a failure only warns). The
    stable per-idea copy is rewritten only for the idea's newest run (page.write_page). Returns (path, page data),
    (None, None) when nothing was written."""
    from . import page
    if not result or result.get("dry_run") or not result.get("output_dir"):
        return None, None
    return page.write_page(cfg, result["output_dir"], result, deck,
                           lang=lang or page_lang(cfg, result.get("idea") or ""))


def stable_page_for(cfg, result: dict[str, Any]) -> Path | None:
    """The idea's one stable page (<home>/pages/<idea_key>.html) when it holds this run, else None."""
    from . import page
    idea = result.get("idea") or ""
    if idea.strip() and page.stable_holds(cfg, idea, result.get("run_id")):
        return page.stable_path(cfg, idea)
    return None


def json_dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True)
