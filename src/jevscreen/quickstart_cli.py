"""CLI of `jevscreen quickstart` and `jevscreen page` (wired into jevscreen.cli with two lines, like agent_cli).

- `jevscreen quickstart "<idea>" [--idea-en TEXT] [--approve-budget USD] [--min-mcap 1e9] [--countries A,B]
  [--lang auto|zh|en] [--fd-file PATH] [--no-open] [--retry] [--new-run] [--json]`: the fast front command (no
  network; spawns the detached worker). Exit codes = the JSON 'status' (docs/AGENT_API.md): 0 running / done /
  partial, 1 failed, 2 blocked, 3 store busy, 5 budget exhausted, 6 AI unavailable, 10 needs the human, 11 needs
  the agent, 12 declined. An argparse usage error exits 2 with no JSON (a typo, not a block).
- `jevscreen quickstart --status [IDEA | --key K] [--wait S] [--json]`: the same JSON from the job file; --wait
  blocks until the state changes (pass S, max 110). A job that waits on nothing any more is started again here, as
  the front would (see quickstart.status).
- `jevscreen page [RUN_ID|OUT_DIR|latest] [--lang zh|en] [--open] [--text] [--json]`: rebuild page.html of a screen
  run ($0), in ONE language. `--export-strings FILE` writes the texts that still need the agent's translation (a JSON
  list, <= 60 items); `--import-translations FILE` stores the agent's translations and rebuilds the page
  (jevscreen.translations).
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
    qs.add_argument("--facets", default=None, metavar="JSON",
                    help="optional, from your AI before the screen starts: the idea's category / target in English and "
                         "Chinese and at most 2 implied_no defaults (see AGENTS.md); it lets the result ask at most 2 "
                         "plain scope questions")
    qs.add_argument("--status", action="store_true", help="print the job's state (starts the background work "
                    "again only when it waits on nothing; never anything that needs an answer)")
    qs.add_argument("--key", default=None, metavar="IDEA_KEY", help="with --status: the job's idea key")
    qs.add_argument("--wait", type=float, default=None, metavar="S",
                    help="with --status: wait up to S seconds (max 110) for the state to change")
    qs.add_argument("--json", action="store_true", help="print JSON only (for AI agents)")
    pg = sub.add_parser("page", help="rebuild the result page (page.html) of a screen run (free)")
    pg.add_argument("target", nargs="?", default="latest", metavar="RUN_ID|OUT_DIR|latest")
    pg.add_argument("--lang", choices=("zh", "en"), default=None, help="the page's one language (default: the "
                    "idea's quickstart --lang, else the idea's own)")
    pg.add_argument("--open", action="store_true", help="open it in the browser")
    pg.add_argument("--text", action="store_true", help="also print the ranked list as plain text (for an AI agent "
                    "that cannot open a browser; with --json it is the 'text' field)")
    pg.add_argument("--export-strings", default=None, metavar="FILE",
                    help="write the page's texts that are in another language than the page (English profiles on a "
                         "Chinese page, Chinese excerpts on an English one, names without an official Chinese name) "
                         "as a JSON list for your AI agent to translate (at most --batch items, most important "
                         "first); the page itself is not changed")
    pg.add_argument("--import-translations", default=None, metavar="FILE",
                    help="store the translations the agent filled into an exported file (provenance 'agent "
                         "translation', reused by later runs) and rebuild the page")
    pg.add_argument("--batch", type=int, default=None, metavar="N",
                    help="with --export-strings: at most N items per file (default and maximum 60)")
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
    tr = out.get("translation") or {}
    if tr.get("pending") and tr.get("export_command"):
        lines.append(f"[agent] {tr['pending']} texts on the page are not in the page's language yet: run "
                     f"{tr['export_command']}, translate the file, then {tr['import_command']} (AGENTS.md)")
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
                       fill_descriptions=getattr(args, "fill_descriptions", None),
                       facets=getattr(args, "facets", None))
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


def translation_block(cfg, run_id: str | None, data: dict[str, Any] | None) -> dict[str, Any] | None:
    """What the agent needs to translate the page: {lang, pending, translated, export_command, import_command, file,
    batch_max}; None when nothing on the page is in another language."""
    from . import translations as tr
    info = (data or {}).get("translation") or {}
    if not run_id or not info.get("foreign"):
        return None
    lang = info.get("lang") or (data or {}).get("lang") or "zh"
    path = tr.export_path(cfg, run_id, lang)
    return {"lang": lang, "pending": int(info.get("pending") or 0), "translated": int(info.get("translated") or 0),
            "batch_max": tr.BATCH_MAX, "file": str(path),
            "export_command": _cmd("jevscreen", "page", run_id, "--export-strings", str(path), "--lang", lang,
                                   "--json") if info.get("pending") else None,
            "import_command": _cmd("jevscreen", "page", run_id, "--import-translations", str(path), "--lang", lang,
                                   "--json") if info.get("pending") else None}


def _cmd(*argv: str) -> str:
    import shlex
    return " ".join(shlex.quote(a) for a in argv)


def cmd_page(args: argparse.Namespace, cfg: Any) -> int:
    from . import calib, cli, page, store, translations as tr
    imported = None
    if getattr(args, "import_translations", None):
        try:
            items = tr.read_file(args.import_translations)
            with store.session(cfg, wait_s=60.0) as con:
                imported = tr.import_items(cfg, con, items, default_lang=args.lang)
        except ValueError as e:
            return _page_fail(args, "error", 1, str(e))
        except store.StoreLocked:
            return _page_fail(args, "store_busy", 3, "the database is busy (a screen or sync is writing); run the "
                              "same command again in a minute")
    try:
        with cli._read_session(cfg) as con:
            run_id, out_dir, result = cli._resolve_run(con, args.target,
                                                       lang=args.lang or cli._default_lang(cfg, con))
    except ValueError as e:
        print(f"error: page: {e}", file=sys.stderr)
        return 1
    deck = None
    if (Path(out_dir) / "cards.json").exists():
        try:
            deck = calib.load_deck(out_dir)
        except ValueError:
            deck = None
    if getattr(args, "export_strings", None) and imported is None:
        return _export(args, cfg, run_id, result, deck)
    # an import without --lang rebuilds the page in the language its items were translated into (the page the
    # human asked for), not the idea's default
    lang = args.lang or (imported or {}).get("lang") or page_lang(cfg, result.get("idea") or "")
    path, data = page.write_page(cfg, out_dir, result, deck, lang=lang)
    if path is None:
        return 1
    if (data or {}).get("store_busy"):
        # the store stayed busy while the page was built: the earlier page is kept (never a poorer one)
        return _page_fail(args, "store_busy", 3, (
            f"{imported['imported']} translation(s) stored, but " if imported else "") + "the page was not rebuilt: "
            "the database is busy (a screen or sync is writing); run the same command again in a minute")
    stable = page.stable_path(cfg, result["idea"]) if page.stable_holds(cfg, result["idea"], run_id) else None
    show = stable or path          # the idea's one stable page whenever it holds this run (the newest)
    opened = page.open_page(show) if args.open else False
    info = {"command": "page", "status": "ok", "run_id": run_id, "page": str(show), "lang": lang,
            "page_uri": Path(show).resolve().as_uri(), "stable_page": str(stable) if stable else None,
            "run_page": str(path), "page_opened": opened, "deck_id": (deck or {}).get("deck_id"),
            "summary": page.summary_of(data, deck), "translation": translation_block(cfg, run_id, data),
            "translation_pending": int(((data or {}).get("translation") or {}).get("pending") or 0)}
    code = 0
    if imported is not None:
        info.update(action="import_translations", imported=imported["imported"], skipped=imported["skipped"],
                    rejected=imported["rejected"])
        if imported["rejected"]:
            info["status"] = "partial" if imported["imported"] else "rejected"
            code = 0 if imported["imported"] else 1
    if getattr(args, "text", False):
        info["text"] = page.render_text(data, lang)
    if args.json:
        _emit(info)
    else:
        if imported is not None:
            print(f"imported {imported['imported']} translation(s), skipped {imported['skipped']}, rejected "
                  f"{len(imported['rejected'])}" + "".join(f"\n  {r['id']}: {r['problem']}" for r in imported["rejected"]))
        if info.get("text"):
            print(info["text"])
        print(("已在浏览器打开：" if lang == "zh" else "Opened: ") + info["page_uri"] if opened else
              ("用浏览器打开这个文件：" if lang == "zh" else "Open this file in a browser: ") + info["page_uri"])
    return code


def _page_fail(args: argparse.Namespace, status: str, code: int, msg: str) -> int:
    if args.json:
        _emit({"command": "page", "status": status, "exit_code": code, "error": msg})
    else:
        print(f"error: page: {msg}", file=sys.stderr)
    return code


def _export(args: argparse.Namespace, cfg: Any, run_id: str, result: dict[str, Any],
            deck: dict[str, Any] | None) -> int:
    """--export-strings: the page's untranslated foreign texts, most important first, at most --batch per file."""
    from . import page, translations as tr
    from . import store
    lang = args.lang or page_lang(cfg, result.get("idea") or "")
    try:        # strict: a busy store would export texts already translated (and waste the agent's work)
        data = page.page_data(cfg, result, deck, lang=lang, strict=True)
    except store.StoreLocked:
        return _page_fail(args, "store_busy", 3, "the database is busy (a screen or sync is writing); run the same "
                          "command again in a minute")
    items = tr.pending_items(data)
    batch = max(1, min(tr.BATCH_MAX, int(args.batch or tr.BATCH_MAX)))
    chosen = items[:batch]
    try:
        path = tr.write_export(args.export_strings, chosen)
    except OSError as e:
        return _page_fail(args, "error", 1, f"cannot write {args.export_strings} ({type(e).__name__})")
    info = {"command": "page", "action": "export_strings", "status": "ok" if chosen else "nothing_to_translate",
            "run_id": run_id, "lang": lang, "file": str(path), "items": len(chosen), "pending_total": len(items),
            "remaining_after": len(items) - len(chosen),
            "kinds": {k: sum(1 for i in chosen if i["kind"] == k) for k in tr.KINDS if any(i["kind"] == k
                                                                                        for i in chosen)},
            "instructions_en": tr.INSTRUCTIONS_EN.format(target=tr.TARGET_WORDS[lang]),
            "import_command": _cmd("jevscreen", "page", run_id, "--import-translations", str(path), "--lang", lang,
                                   "--json") if chosen else None}
    if args.json:
        _emit(info)
    else:
        print(f"{len(chosen)} of {len(items)} texts to translate -> {path}" + (
            f"\n{info['instructions_en']}\nthen: {info['import_command']}" if chosen else ""))
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
