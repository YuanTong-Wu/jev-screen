"""CLI pieces for the shells filter, `jevscreen why` and the sieve author commands (wired into jevscreen.cli with a
few lines: add_screen_arguments, add_sieve_subparsers, add_parsers, COMMANDS, SIEVE_COMMANDS, screen_idea).
"""
from __future__ import annotations

import argparse
from typing import Any, Callable

EXIT_OK, EXIT_ERROR = 0, 1


def add_screen_arguments(sc: Any, given_action: type) -> None:
    """screen --shells drop|keep and --idea-of RUN_ID (the idea text of an earlier run, so commands that `why`
    suggests never carry the idea itself)."""
    sc.add_argument("--shells", choices=("drop", "keep"), default=None, action=given_action,
                    help="shells filter before L1: drop (default) removes blank-check companies (SPACs), never a "
                         "company your sieve names; keep screens them too. ST / *ST are only marked")
    sc.add_argument("--idea-of", default=None, metavar="RUN_ID",
                    help="screen the idea of that earlier run (a fresh screen: L1 is asked again, cached answers "
                         "are reused); the idea argument may then be left out")


def _run_idea(cfg: Any, run_id: str) -> str:
    """The idea of a screen run: its screen_runs row, else its ledger / results.json (a dry run is only there; an
    output directory works too)."""
    from . import store, why
    idea = None
    if cfg.db_path.exists():
        with store.session(cfg, read_only=True, wait_s=120.0) as con:
            row = con.execute("SELECT idea FROM screen_runs WHERE run_id = ?", [run_id]).fetchone()
        idea = row[0] if row else None
    if not (idea or "").strip():
        try:
            idea = why.pick_run(why.list_runs(cfg, None), run_id).idea
        except (why.WhyError, OSError, ValueError, KeyError):
            idea = None
    if not (idea or "").strip():
        raise ValueError(f"unknown screen run {run_id!r}")
    return idea


def screen_idea(cfg: Any, args: argparse.Namespace) -> str:
    """The idea a `screen` command screens: the argument, else the idea of --idea-of / --from-run. ValueError when
    none is given or the argument disagrees with the --idea-of run (--from-run is checked by screen itself)."""
    idea = getattr(args, "idea", None)
    idea_of = getattr(args, "idea_of", None)
    ref = idea_of or getattr(args, "from_run", None)
    if idea:
        if idea_of:
            other = _run_idea(cfg, idea_of)
            if other.strip() != idea.strip():
                raise ValueError(f"--idea-of: run {idea_of!r} screened another idea ({other[:60]!r}); leave the idea "
                                 "out or drop --idea-of")
        return idea
    if not ref:
        raise ValueError("give the idea (quoted), or --from-run RUN_ID / --idea-of RUN_ID")
    return _run_idea(cfg, ref)


# ------------------------------------------------------------------------------------------------ parsers

def _address(p: Any, *, idea: bool = True) -> None:
    p.add_argument("--run", default=None, metavar="RUN_ID|latest", help="the sieve of that screen run's idea")
    p.add_argument("--key", default=None, metavar="HEX", help="the sieve with this idea key (16 hex characters)")
    if idea:
        p.add_argument("--idea", default=None, metavar="TEXT", help="the sieve of this idea text")
    p.add_argument("--json", action="store_true", help="print JSON (for AI agents)")


def add_sieve_subparsers(svs: Any) -> None:
    """sieve new / set / add / remove / pin / unpin / list, and --run / --key / --idea / --json on sieve check."""
    nw = svs.add_parser("new", help="create the sieve of an idea (from --run, --idea or the draft's idea); an "
                        "existing one is updated like `sieve set` (free)")
    nw.add_argument("--from", dest="from_file", default=None, metavar="FILE|-", help="an author draft (JSON)")
    _address(nw)
    st = svs.add_parser("set", help="merge an author draft (JSON merge patch of idea_en, seed_terms, facets, "
                        "should_pass, should_fail, notes ...) into the sieve; prints what changed (free)")
    st.add_argument("target", nargs="?", default=None, metavar="PATH|IDEA")
    st.add_argument("--from", dest="from_file", required=True, metavar="FILE|-", help="the draft (JSON); - = stdin")
    st.add_argument("--reprice", action="store_true",
                    help="let a changed idea_en replace the one a paid run already used (the next run asks step 1 "
                         "again: ask the human first)")
    st.add_argument("--dry", action="store_true", help="only print what would change")
    _address(st)
    for name, hlp in (("add", "add a company to should_pass or should_fail (resolved once, now; free)"),
                      ("remove", "remove a company from should_pass or should_fail (free)")):
        a = svs.add_parser(name, help=hlp)
        a.add_argument("list", choices=("should_pass", "should_fail"))
        a.add_argument("company", metavar="COMPANY", help="a ticker (2330, NYSE:ABC), code or exact name")
        if name == "add":
            a.add_argument("--want", choices=("explicit", "partial"), default="explicit")
            a.add_argument("--why", default=None, metavar="TEXT", help="why (for the human; never sent to Jev)")
        _address(a)
    pn = svs.add_parser("pin", help="pin a company yes / no: the human's own judgement (ask them first)")
    pn.add_argument("company", metavar="COMPANY")
    pn.add_argument("verdict", choices=("yes", "no"))
    pn.add_argument("--want", choices=("explicit", "partial"), default="explicit")
    _address(pn)
    un = svs.add_parser("unpin", help="remove a pin made with `sieve pin` (ask the human first)")
    un.add_argument("company", metavar="COMPANY")
    _address(un)
    ls = svs.add_parser("list", help="every sieve: idea, key, version, last run, counts (free)")
    ls.add_argument("--json", action="store_true")
    chk = svs.choices["check"]
    _address(chk)


def add_parsers(sub: Any) -> None:
    w = sub.add_parser("why", help="why a company is (not) in a screen's result: one plain sentence, the facts and "
                       "what would change it (free, read-only)")
    w.add_argument("targets", nargs="*", metavar="TARGET", help="tickers, codes or names (any script)")
    w.add_argument("--run", default=None, metavar="RUN_ID|OUT_DIR|latest",
                   help="the screen run (default: the newest run of --idea / --key, or the only idea run lately)")
    w.add_argument("--idea", default=None, metavar="TEXT")
    w.add_argument("--key", default=None, metavar="HEX")
    w.add_argument("--checks", action="store_true", help="explain every company the run's sieve names")
    w.add_argument("--lang", choices=("auto", "zh", "en"), default="auto")
    w.add_argument("--show-text", action="store_true", help="quote up to 200 characters of the evidence (personal "
                   "use only)")
    w.add_argument("--wait", type=float, default=5.0, metavar="S",
                   help="wait at most S seconds for the database (default 5), else answer from the run's files")
    w.add_argument("--json", action="store_true", help="print JSON (for AI agents)")
    w.add_argument("--verbose", action="store_true", help="also print the run id")


# ------------------------------------------------------------------------------------------------ why

def _emit(obj: Any) -> None:
    import json
    print(json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=True, default=str))


def cmd_why(args: argparse.Namespace, cfg: Any) -> int:
    """Exit 0 explained (also from files only), 1 unknown run / choose_run / no runs / a target not found or
    ambiguous, 3 the run's files are unreadable."""
    import sys
    from . import why
    try:
        out = why.run(cfg, args.targets, run_ref=args.run, idea=args.idea, key=args.key, checks=args.checks,
                      wait_s=max(0.0, args.wait), show_text=args.show_text)
    except why.WhyError as e:
        body = {"command": "why", "status": e.status, "text_zh": e.text_zh, "text_en": e.text_en, **e.extra}
        if args.json:
            _emit(body)
        else:
            lang = _why_lang(cfg, args.idea, args.lang)
            print(e.text_zh if lang == "zh" else e.text_en, file=sys.stderr)
            for r in e.extra.get("runs") or []:
                print(f"  {r['run_id']}  {r['started_at'][:16]}  {r['idea']}", file=sys.stderr)
        return 3 if e.status == "unreadable" else EXIT_ERROR
    if args.json:
        _emit(out)
    else:
        lang = _why_lang(cfg, out["run"].get("idea"), args.lang)
        if not out["results"]:
            print("没有要解释的公司：给公司代码或名字，或加 --checks" if lang == "zh" else
                  "Nothing to explain: give tickers or names, or --checks")
        for i, exp in enumerate(out["results"]):
            if i:
                print()
            print(why.render_text(exp, lang, verbose=args.verbose))
        if out.get("note_zh"):
            print(out["note_zh"] if lang == "zh" else out["note_en"])
    if not out["results"]:
        return EXIT_ERROR
    return EXIT_ERROR if out["status"] in ("not_found", "partly_resolved") else EXIT_OK


def _why_lang(cfg, idea: str | None, lang: str) -> str:
    """--lang zh|en as given; auto: the idea's quickstart job language (the page's), else from the idea."""
    from . import quickstart_cli, why
    if lang in ("zh", "en"):
        return lang
    if idea and idea.strip():
        try:
            return quickstart_cli.page_lang(cfg, idea)
        except Exception:  # noqa: BLE001
            pass
    return why.lang_of(idea, lang)


# ------------------------------------------------------------------------------------------------ sieve commands

class _Stop(Exception):
    def __init__(self, code: int, body: dict[str, Any]):
        super().__init__(body.get("text_en"))
        self.code, self.body = code, body


def _fail(command: str, status: str, zh: str, en: str, **extra: Any) -> _Stop:
    return _Stop(EXIT_ERROR, {"command": f"sieve {command}", "ok": False, "status": status, "text_zh": zh,
                              "text_en": en, **extra})


def _sieve_address(cfg: Any, args: argparse.Namespace, con: Any, command: str, *, create: bool = False,
                   draft: dict[str, Any] | None = None) -> tuple[Any, str | None, str | None]:
    """(sieve path, idea or None, run_id or None) from --run / --key / --idea / a positional PATH|IDEA / the draft."""
    from pathlib import Path
    from . import calib, keywords, why
    run = getattr(args, "run", None)
    if run:
        try:
            ref = why.pick_run(why.list_runs(cfg, con), run)
        except why.WhyError as e:
            raise _fail(command, e.status, e.text_zh, e.text_en, **e.extra) from None
        if ref.out_dir is not None and (ref.out_dir / "results.json").exists():
            import json
            res = json.loads((ref.out_dir / "results.json").read_text(encoding="utf-8"))
            sp = (res.get("params") or {}).get("sieve_path")
            if sp and sp != "<inline>":
                return Path(sp), ref.idea, ref.run_id
        return calib.sieve_path(cfg, ref.idea), ref.idea, ref.run_id
    key = getattr(args, "key", None)
    if key:
        if not (len(key) == 16 and all(c in "0123456789abcdef" for c in key)):
            raise _fail(command, "bad_key", f"idea key 应是 16 个十六进制字符：{key}", f"Bad idea key: {key}")
        return Path(cfg.home) / "sieves" / f"{key}.json", None, None
    idea = getattr(args, "idea", None)
    target = getattr(args, "target", None)
    if not idea and target:
        p = Path(target).expanduser()
        if p.is_file():
            return p, None, None
        if target.endswith(".json"):
            raise _fail(command, "missing_file", f"没有这个文件：{target}", f"No such file: {target}")
        idea = target
    if not idea and draft and isinstance(draft.get("idea"), str):
        idea = draft["idea"]
    if not idea:
        raise _fail(command, "no_target", "请用 --run RUN_ID（或 --key / --idea）指定是哪个想法的筛子",
                    "Say which idea: --run RUN_ID, --key HEX or --idea TEXT")
    path = calib.sieve_path(cfg, idea)
    if not path.exists() and not create:
        near = calib.closest_sieve(cfg, idea)
        raise _fail(command, "no_sieve", "这个想法还没有筛子" + (f"；最接近的是「{near['idea'][:40]}」（--key "
                                                          f"{Path(near['path']).stem}）" if near else ""),
                    "This idea has no sieve yet" + (f"; closest: {near['idea'][:40]!r}" if near else ""),
                    closest_sieve=near, idea_key=keywords.idea_key(idea))
    return path, idea, None


def _open_read(cfg: Any):
    from . import store
    if not cfg.db_path.exists():
        import contextlib
        return contextlib.nullcontext(None)
    return store.session(cfg, read_only=True, wait_s=120.0)


def _load_or_new(path: Any, idea: str | None, command: str, create: bool) -> dict[str, Any]:
    from . import calib
    try:
        sv = calib.load_sieve(path)
    except ValueError as e:
        raise _fail(command, "invalid", f"筛子文件读不了：{e}", f"Invalid sieve: {e}") from None
    if sv is None:
        if not create or not idea:
            raise _fail(command, "no_sieve", f"没有筛子文件 {path}", f"No sieve at {path}")
        sv = calib.new_sieve(idea)
    return sv


def _read_draft(spec: str | None, command: str) -> dict[str, Any] | None:
    import json
    import sys
    if spec is None:
        return None
    try:
        raw = sys.stdin.read() if spec == "-" else open(spec, encoding="utf-8").read()
        return json.loads(raw)
    except (OSError, ValueError) as e:
        raise _fail(command, "bad_draft", f"草稿读不了（{type(e).__name__}）", f"Cannot read the draft "
                    f"({type(e).__name__})") from None


def _big_companies(con: Any) -> list[tuple[str, str]]:
    from . import sieve_author
    if con is None:
        return []
    return [(n, sid.rpartition(":")[2]) for n, sid in con.execute(
        "SELECT name, security_id FROM universe WHERE market_cap_usd >= ?", [sieve_author.BIG_MCAP_USD]).fetchall()]


def _idea_en_state(cfg: Any, con: Any, sv: dict[str, Any], idea: str) -> dict[str, Any]:
    """{source, frozen, used, differs, approved, effective, reprice: {l1_usd, l2_usd, companies} | None} of the
    sieve's idea_en; effective is the idea_en the next run uses (the frozen one unless --reprice approved this text;
    None for an English idea; the last paid run's when the local model decides)."""
    from . import screen, sieve_author, why
    en = (sv.get("idea_en") or "").strip()
    frozen, used = (screen._frozen_idea_en(con, idea) if con is not None else (False, None))
    approved = sieve_author.reprice_approved(sv)
    differs = bool(en) and frozen and en != (used or "")
    mine = bool(en) and (sv.get("idea") or "").strip() == idea.strip()
    if not screen.needs_translation(idea):
        effective = None
    elif mine and differs and not approved:
        effective = used
    elif mine:
        effective = en
    else:
        effective = used if frozen else None
    out: dict[str, Any] = {"source": "sieve" if en and screen.needs_translation(idea) else None, "frozen": frozen,
                           "used": used, "differs": differs, "approved": approved, "effective": effective,
                           "reprice": None}
    if out["differs"] and con is not None:
        row = con.execute("SELECT output_dir FROM screen_runs WHERE trim(idea) = ? ORDER BY started_at DESC LIMIT 1",
                          [idea.strip()]).fetchone()
        try:
            import json
            from pathlib import Path
            res = json.loads((Path(row[0]) / "results.json").read_text(encoding="utf-8"))
            l1u, l2u = why.units(res)
            n1 = int((res.get("funnel") or {}).get("l1_sent") or 0)
            n2 = int(((res.get("layers") or {}).get("l2") or {}).get("items") or 0)
            out["reprice"] = {"l1_usd": round(n1 * l1u, 4), "l2_usd": round(n2 * l2u, 4), "companies": n1}
        except (OSError, ValueError, TypeError, KeyError):
            out["reprice"] = None
    return out


def _last_paid_result(con: Any, idea: str) -> dict[str, Any] | None:
    """results.json of the newest paid run of the idea (screen_runs holds paid runs only), or None."""
    import json
    from pathlib import Path
    if con is None:
        return None
    for (od,) in con.execute("SELECT output_dir FROM screen_runs WHERE trim(idea) = ? ORDER BY started_at DESC",
                             [idea.strip()]).fetchall():
        try:
            res = json.loads((Path(od) / "results.json").read_text(encoding="utf-8"))
        except (OSError, TypeError, ValueError):
            continue
        if isinstance(res, dict) and not res.get("dry_run"):
            return res
    return None


def _reprice_warning(con: Any, sv: dict[str, Any], idea: str) -> dict[str, Any] | None:
    """Spec: a sieve edit that changes what the next run asks (idea_en approved by --reprice, facets / rules in the
    L2 question, seed_terms / keywords in the excerpts) is priced against the newest paid run."""
    from . import why
    res = _last_paid_result(con, idea)
    if res is None:
        return None
    d = why.drift_of(con, idea, res, sv)
    if not d["l1"] and d["l2"] is False:
        return None
    rc = why.reprice_cost(res, d, fresh=True)
    usd = rc["l1_usd"] + rc["l2_usd"]
    if d["l1"]:
        zh = f"idea_en 改了（你已同意）：下次第一步和第二步都要重问，约 ${usd:.2f}"
        en = f"idea_en changed (approved): the next run asks steps 1 and 2 again, about ${usd:.2f}"
    elif rc["maybe"]:
        zh = f"筛子改了：下次可能要重读第二步，最多约 ${usd:.2f}（第一步不变）"
        en = f"The sieve changed: the next run may read step 2 again, at most about ${usd:.2f} (step 1 unchanged)"
    else:
        zh = f"改了第二步的问题或关键词（facets / 规则 / seed_terms）：下次第二步要重读，约 ${usd:.2f}（第一步不变）"
        en = f"The step-2 question or terms changed: the next run reads step 2 again, about ${usd:.2f}"
    return {"code": "l2_reprice", "text_zh": zh, "text_en": en, "l1_usd": rc["l1_usd"], "l2_usd": rc["l2_usd"],
            "maybe": rc["maybe"], "run_id": res.get("run_id")}


def _warnings(cfg: Any, con: Any, sv: dict[str, Any], idea: str) -> list[dict[str, Any]]:
    from . import keywords, sieve_author
    ws = sieve_author.idea_en_warnings(sv.get("idea_en"), sv, _big_companies(con))
    rw = _reprice_warning(con, sv, idea)
    if rw:
        ws.append(rw)
    for lang, terms in (sv.get("seed_terms") or {}).items():
        kept = keywords.clean_list(lang, list(terms))
        dropped = [t for t in terms if t not in kept]
        if dropped:
            ws.append({"code": "seed_terms_dropped", "text_zh": f"seed_terms.{lang} 里这些词用不上（文字不对或太长）："
                       f"{'、'.join(dropped)}", "text_en": f"seed_terms.{lang}: unusable terms {dropped}"})
    ws += sieve_author.conflicts(sv)
    en = sv.get("idea_en")
    if en and (sv.get("idea") or "").strip() == idea.strip():
        from . import screen
        if not screen.needs_translation(idea):
            ws.append({"code": "idea_en_unused", "text_zh": "想法本身是英文：idea_en 不会用到",
                       "text_en": "The idea is English: idea_en is not used"})
    return ws


def _resolved_rows(con: Any, sv: dict[str, Any], floor: float | None) -> list[dict[str, Any]]:
    """Stored vs today for every check entry: ok | moved | gone | unresolved, in the universe at the floor."""
    from . import sieve_author
    out = []
    for lst in sieve_author.CHECK_LISTS:
        for e in sv.get(lst) or []:
            r = e.get("resolved") or {}
            row = {"list": lst, "company": e["company"], "security_id": r.get("security_id"),
                   "company_key": r.get("company_key"), "name": r.get("name"), "state": "unresolved"}
            if r and con is not None:
                u = con.execute("SELECT security_id, market_cap_usd FROM universe WHERE company_key = ?",
                                [r.get("company_key")]).fetchone()
                if u is None:
                    row["state"] = "gone"
                else:
                    row["state"] = "ok" if u[0] == r.get("security_id") else "moved"
                    row["current_security_id"] = u[0]
                    row["market_cap_usd"] = u[1]
                    if floor is not None and (u[1] is None or u[1] < floor):
                        row["below_floor"] = True
                        row["note_zh"] = "它在你的市值门槛以下：只读证据写进报告，不会进名单"
                        row["note_en"] = "Below your market-cap floor: its evidence is read and reported, never listed"
            elif r:
                row["state"] = "stored"
            out.append(row)
    return out


def _summary_lines(sv: dict[str, Any], diff: list[dict[str, Any]], lang: str = "zh") -> list[str]:
    from . import sieve_author
    parts_zh, parts_en = [], []
    for d in diff:
        f = d["field"]
        if f in sieve_author.CHECK_LISTS:
            for sign, key in (("+", "added"), ("-", "removed")):
                if d.get(key):
                    parts_zh.append(f"{sieve_author.LIST_ZH[f]} {sign}{len(d[key])}（{'、'.join(d[key])}）")
                    parts_en.append(f"{sieve_author.LIST_EN[f]} {sign}{len(d[key])} ({', '.join(d[key])})")
        elif f == "examples":
            parts_zh.append(f"旧的检查 {d['migrated']} 条移进了应该有/不该有")
            parts_en.append(f"{d['migrated']} legacy checks migrated")
        else:
            parts_zh.append(f"{f} 已改")
            parts_en.append(f"{f} changed")
    v = sv.get("version")
    if lang == "zh":
        return [f"已更新筛子（版本 {v}）：" + ("；".join(parts_zh) or "没有变化")]
    return [f"Sieve updated (v{v}): " + ("; ".join(parts_en) or "no change")]


def _write(cfg: Any, args: argparse.Namespace, command: str, mutate: Callable[..., Any], *, create: bool,
           draft: dict[str, Any] | None = None, dry: bool = False) -> int:
    """Shared flow of the writing sieve commands: address, load (or create), mutate, resolve, validate, save."""
    from . import calib, names, sieve_author
    try:
        with _open_read(cfg) as con:
            path, idea, run_id = _sieve_address(cfg, args, con, command, create=create, draft=draft)
            sv = _load_or_new(path, idea, command, create)
            idea = sv.get("idea") or idea or ""
            if draft and draft.get("idea") is not None and draft["idea"].strip() != idea.strip():
                raise _fail(command, "idea_mismatch", "草稿的 idea 和这个筛子的想法不一样（一个字都不能差）",
                            "The draft's idea differs from this sieve's idea (it must match exactly)")
            index = names.alias_index(con) if con is not None else None
            sv2, diff, extra = mutate(sv, index)
            errs = []
            if index is not None:
                errs += sieve_author.resolve_checks(sv2, index)
            probs = calib.validate_sieve({**sv2, "examples": sieve_author.strip_author(sv2.get("examples"))})
            errs += [{"code": "schema", "text_zh": p, "text_en": p} for p in probs]
            errs += sieve_author.idea_en_errors(sv2.get("idea_en"), sv2)
            warns = _warnings(cfg, con, sv2, idea)
            ien = _idea_en_state(cfg, con, sv2, idea)
            if ien["differs"] and not ien["approved"]:
                warns.append({"code": "idea_en_frozen", "text_zh": "idea_en 已冻结：这个想法已经付费跑过。要改请用 --reprice"
                              + (f"（下次第一步重问一遍，约 ${ien['reprice']['l1_usd'] + ien['reprice']['l2_usd']:.2f}，"
                                 "需要你同意）" if ien.get("reprice") else "（需要你同意）"),
                              "text_en": "idea_en is frozen: a paid run used another one; --reprice to change it "
                                         "(asks step 1 again, needs the human's yes)"})
    except _Stop as e:
        return _out_stop(args, e)
    body: dict[str, Any] = {"command": f"sieve {command}", "path": str(path), "idea_key": sv2.get("idea_key"),
                            "diff": diff, "errors": errs, "warnings": warns, "idea_en": ien, **(extra or {})}
    if errs or dry:
        body.update(ok=not errs, version=sv.get("version"), saved=False)
        return _print_write(args, body, sv, diff, EXIT_ERROR if errs else EXIT_OK)
    try:
        saved = calib.save_sieve(path, sv2)
    except calib.SieveStale as e:
        body.update(ok=False, status="sieve_stale", text_zh=f"{e}", saved=False)
        return _print_write(args, body, sv, diff, EXIT_ERROR)
    body.update(ok=True, version=saved.get("version"), saved=True,
                resolved=_resolved_rows(None, saved, None),
                next_command={"argv": ["jevscreen", "sieve", "check", "--key", saved["idea_key"], "--json"],
                              "command": f"jevscreen sieve check --key {saved['idea_key']} --json"})
    return _print_write(args, body, saved, diff, EXIT_OK)


def _out_stop(args: argparse.Namespace, e: _Stop) -> int:
    import sys
    if getattr(args, "json", False):
        _emit(e.body)
    else:
        print(f"error: {e.body['command']}: {e.body.get('text_zh')}", file=sys.stderr)
        for c in e.body.get("candidates") or []:
            print(f"  {c.get('security_id')}  {c.get('name')}", file=sys.stderr)
    return e.code


def _print_write(args: argparse.Namespace, body: dict[str, Any], sv: dict[str, Any], diff: list, code: int) -> int:
    if getattr(args, "json", False):
        _emit(body)
        return code
    if body.get("saved"):
        for line in _summary_lines(sv, diff):
            print(line)
    elif not body.get("errors"):
        print("（只是预览，没有保存）" + "；".join(_summary_lines({**sv, "version": sv.get("version")}, diff)))
    for e in body.get("errors") or []:
        print(f"错误：{e['text_zh']}")
    for w in body.get("warnings") or []:
        print(f"提醒：{w['text_zh']}")
    if body.get("status") == "sieve_stale":
        print(f"错误：{body.get('text_zh')}")
    return code


def cmd_sieve_set(args: argparse.Namespace, cfg: Any) -> int:
    from . import sieve_author
    try:
        draft = _read_draft(args.from_file, "set")
    except _Stop as e:
        return _out_stop(args, e)
    bad = sieve_author.validate_draft(draft)
    if bad:
        return _out_stop(args, _Stop(EXIT_ERROR, {"command": "sieve set", "ok": False, "status": "invalid_draft",
                                                  "errors": bad, "text_zh": "；".join(b["text_zh"] for b in bad),
                                                  "text_en": "; ".join(b["text_en"] for b in bad)}))

    def mutate(sv, _index):
        sv2, diff = sieve_author.merge_author(sv, draft)
        if getattr(args, "reprice", False):      # the human's yes, for this idea_en text only
            en = (sv2.get("idea_en") or "").strip()
            if en:
                sv2["idea_en_reprice"] = en
            else:
                sv2.pop("idea_en_reprice", None)
            if sv2.get("idea_en_reprice") != sv.get("idea_en_reprice"):
                diff.append({"field": "idea_en_reprice", "before": sv.get("idea_en_reprice"),
                             "after": sv2.get("idea_en_reprice")})
        return sv2, diff, None
    return _write(cfg, args, "set", mutate, create=True, draft=draft, dry=getattr(args, "dry", False))


def cmd_sieve_new(args: argparse.Namespace, cfg: Any) -> int:
    from . import sieve_author
    try:
        draft = _read_draft(args.from_file, "new") or {}
    except _Stop as e:
        return _out_stop(args, e)
    bad = sieve_author.validate_draft(draft)
    if bad:
        return _out_stop(args, _Stop(EXIT_ERROR, {"command": "sieve new", "ok": False, "status": "invalid_draft",
                                                  "errors": bad, "text_zh": "；".join(b["text_zh"] for b in bad),
                                                  "text_en": "; ".join(b["text_en"] for b in bad)}))

    def mutate(sv, _index):
        existed = bool(sv.get("version"))
        sv2, diff = sieve_author.merge_author(sv, draft)
        return sv2, diff, {"existed": existed}
    return _write(cfg, args, "new", mutate, create=True, draft=draft)


def _resolve_exact(index: Any, company: str, command: str):
    from . import names
    if index is None:
        raise _fail(command, "no_store", "没有数据库，查不了公司", "No store: cannot resolve the company")
    m = names.resolve(index, company)
    if m.status != "exact":
        raise _fail(command, "unresolved", f"「{company}」对不上唯一的公司：请用交易所代码",
                    f"{company!r} does not name exactly one company: use a ticker", candidates=m.candidates)
    return m


def cmd_sieve_add(args: argparse.Namespace, cfg: Any) -> int:
    from . import sieve_author

    def mutate(sv, index):
        m = _resolve_exact(index, args.company, "add")
        sv2 = sieve_author.merge_author(sv, {})[0]
        entry = {"company": args.company}
        if args.list == "should_pass":
            entry["want"] = args.want
        if args.why:
            entry["why"] = args.why
        entry["resolved"] = {"security_id": m.security_id, "company_key": m.company_key, "name": m.name,
                             "method": m.method, "resolved_at": sieve_author.now_iso()}
        lst = [e for e in sv2.get(args.list) or [] if (e.get("resolved") or {}).get("company_key") != m.company_key]
        other = "should_fail" if args.list == "should_pass" else "should_pass"
        if any((e.get("resolved") or {}).get("company_key") == m.company_key for e in sv2.get(other) or []):
            raise _fail("add", "in_other_list", f"{m.name} 已经在{sieve_author.LIST_ZH[other]}里：先 remove",
                        f"{m.name} is already in {other}: remove it first")
        sv2[args.list] = lst + [entry]
        return sv2, [{"field": args.list, "added": [f"{m.name} {m.security_id}"], "removed": []}], \
            {"company": {"security_id": m.security_id, "name": m.name}}
    return _write(cfg, args, "add", mutate, create=bool(getattr(args, "run", None) or getattr(args, "idea", None)))


def cmd_sieve_remove(args: argparse.Namespace, cfg: Any) -> int:
    from . import names, sieve_author

    def mutate(sv, index):
        sv2 = sieve_author.merge_author(sv, {})[0]
        key = args.company.strip().casefold()
        m = names.resolve(index, args.company) if index is not None else None
        ck = m.company_key if m is not None and m.status == "exact" else None
        keep, gone = [], []
        for e in sv2.get(args.list) or []:
            if e["company"].strip().casefold() == key or (ck and (e.get("resolved") or {}).get("company_key") == ck):
                gone.append(e["company"])
            else:
                keep.append(e)
        if not gone:
            raise _fail("remove", "not_listed", f"{args.company} 不在{sieve_author.LIST_ZH[args.list]}里",
                        f"{args.company} is not in {args.list}")
        sv2[args.list] = keep
        return sv2, [{"field": args.list, "added": [], "removed": gone}], None
    return _write(cfg, args, "remove", mutate, create=False)


def cmd_sieve_pin(args: argparse.Namespace, cfg: Any) -> int:
    from . import sieve_author

    def mutate(sv, index):
        m = _resolve_exact(index, args.company, "pin")
        sv2 = sieve_author.merge_author(sv, {})[0]
        now = sieve_author.now_iso()
        keep = []
        for ex in sv2.get("examples") or []:
            same = ex.get("source", "card") == "card" and (ex.get("company_key") == m.company_key
                                                           or ex.get("security_id") == m.security_id)
            if same:
                sv2.setdefault("history", []).append({**ex, "superseded_at": now, "superseded_by": "sieve pin"})
            else:
                keep.append(ex)
        want = args.want if args.verdict == "yes" else "no"
        sv2["examples"] = keep + [sieve_author.pin_example(m, want, now=now)]
        return sv2, [{"field": "pins", "before": None, "after": f"{m.name} {m.security_id} {want}"}], \
            {"ask_human": True, "company": {"security_id": m.security_id, "name": m.name, "want": want}}
    return _write(cfg, args, "pin", mutate, create=bool(getattr(args, "run", None) or getattr(args, "idea", None)))


def cmd_sieve_unpin(args: argparse.Namespace, cfg: Any) -> int:
    from . import sieve_author

    def mutate(sv, index):
        m = _resolve_exact(index, args.company, "unpin")
        sv2 = sieve_author.merge_author(sv, {})[0]
        now = sieve_author.now_iso()
        keep, gone, card = [], 0, None
        for i, ex in enumerate(sv2.get("examples") or [], 1):
            same = ex.get("pin") and (ex.get("company_key") == m.company_key or ex.get("security_id") == m.security_id)
            if same and ex.get("via") in ("pin", "escalation", "override_agent"):   # sieve pin / jevscreen decide
                sv2.setdefault("history", []).append({**ex, "undone_at": now})
                gone += 1
                continue
            if same:
                card = i
            keep.append(ex)
        if not gone:
            raise _fail("unpin", "not_pinned", f"{m.name} 没有用 sieve pin 钉选" + (
                f"（是卡片回答：jevscreen answer --undo {card}）" if card else ""),
                f"{m.name} has no `sieve pin`" + (f" (a card answer: jevscreen answer --undo {card})" if card else ""))
        sv2["examples"] = keep
        return sv2, [{"field": "pins", "before": f"{m.name} {m.security_id}", "after": None}], {"ask_human": True}
    return _write(cfg, args, "unpin", mutate, create=False)


def cmd_sieve_list(args: argparse.Namespace, cfg: Any) -> int:
    from pathlib import Path
    from . import calib
    rows = []
    base = Path(cfg.home) / "sieves"
    with _open_read(cfg) as con:
        for p in sorted(base.glob("*.json")) if base.is_dir() else []:
            try:
                sv = calib.load_sieve(p)
            except ValueError as e:
                rows.append({"path": str(p), "idea_key": p.stem, "error": str(e)[:200]})
                continue
            if sv is None:
                continue
            last = None
            if con is not None and sv.get("idea"):
                r = con.execute("SELECT run_id, started_at FROM screen_runs WHERE trim(idea) = ? "
                                "ORDER BY started_at DESC LIMIT 1", [sv["idea"].strip()]).fetchone()
                last = {"run_id": r[0], "started_at": str(r[1])[:19]} if r else None
            exs = [e for e in sv.get("examples") or [] if not e.get("_author")]
            rows.append({"path": str(p), "idea_key": sv.get("idea_key") or p.stem, "idea": (sv.get("idea") or "")[:60],
                         "version": sv.get("version"), "updated_at": sv.get("updated_at"), "last_run": last,
                         "counts": {"should_pass": len(sv.get("should_pass") or []),
                                    "should_fail": len(sv.get("should_fail") or []),
                                    "answers": sum(1 for e in exs if e.get("source", "card") == "card"),
                                    "pins": sum(1 for e in exs if e.get("pin")),
                                    "rules": len(sv.get("rules") or [])}})
    if args.json:
        _emit({"command": "sieve list", "sieves": rows})
    else:
        if not rows:
            print("还没有筛子")
        for r in rows:
            c = r.get("counts") or {}
            print(f"{r['idea_key']}  v{r.get('version', '-')}  {r.get('idea', r.get('error', ''))}  应该有 "
                  f"{c.get('should_pass', 0)} 不该有 {c.get('should_fail', 0)} 回答 {c.get('answers', 0)}"
                  + (f"  上次 {r['last_run']['started_at'][:10]}" if r.get("last_run") else ""))
    return EXIT_OK


def cmd_sieve_check(args: argparse.Namespace, cfg: Any) -> int:
    """sieve check: with --json / --run / --key / --idea the author checks (resolved companies, idea_en, preview,
    question) as JSON or text; a positional PATH|IDEA alone runs the original check and then the author checks."""
    from . import calib, screen, sieve_author, why
    legacy = getattr(args, "target", None) and not (args.json or args.run or args.key or args.idea)
    code = EXIT_OK
    if legacy:
        from . import cli
        code = cli.cmd_sieve_check(args, cfg)
        if code != EXIT_OK:          # the original check already said what is wrong
            return code
    try:
        with _open_read(cfg) as con:
            path, idea, run_id = _sieve_address(cfg, args, con, "check")
            sv = _load_or_new(path, idea, "check", False)
            idea = sv.get("idea") or idea or ""
            errs = [{"code": "schema", "text_zh": p, "text_en": p}
                    for p in calib.validate_sieve({**sv, "examples": sieve_author.strip_author(sv.get("examples"))})]
            errs += sieve_author.idea_en_errors(sv.get("idea_en"), sv)
            errs += [{"code": "unresolved", "text_zh": f"「{e['company']}」还没有对上公司（用 sieve add 重新加）",
                      "text_en": f"{e['company']!r} is not resolved (re-add it with sieve add)"}
                     for lst in sieve_author.CHECK_LISTS for e in sv.get(lst) or [] if not e.get("resolved")]
            warns = _warnings(cfg, con, sv, idea)
            ien = _idea_en_state(cfg, con, sv, idea)
            floor = None
            preview = []
            try:
                runs = why.list_runs(cfg, con)
                ref = why.pick_run(runs, None, key=sv.get("idea_key"))
                ctx = why.load_ctx(cfg, ref, con)
                floor = ctx.params.get("min_mcap_usd")
                for t in why.check_targets(ctx):
                    exp = why.explain(ctx, why.resolve_target(ctx, t))
                    preview.append({"list": "check", "security_id": t, "stop": exp["stop"],
                                    "one_line_zh": exp["plain_zh"], "one_line_en": exp["plain_en"]})
            except why.WhyError:
                pass
            resolved = _resolved_rows(con, sv, floor)
            for r in resolved:
                if r["state"] in ("gone", "moved"):
                    warns.append({"code": f"company_{r['state']}", "text_zh": f"{r['company']}："
                                  + ("已经不在股票池（改名或退市？）" if r["state"] == "gone" else
                                     f"主要上市线变成了 {r.get('current_security_id')}"),
                                  "text_en": f"{r['company']}: {r['state']}"})
    except _Stop as e:
        return _out_stop(args, e)
    q = screen.build_l2_question(idea, ien.get("effective"),
                                 rules=sv.get("rules") or [], facets=calib._facets(sv))
    body = {"command": "sieve check", "ok": not errs, "path": str(path), "idea_key": sv.get("idea_key"),
            "version": sv.get("version"), "errors": errs, "warnings": warns, "resolved": resolved,
            "idea_en": ien, "preview": preview,
            "question": {"instructions": q.instructions, "criteria": q.criteria},
            "next_command": ({"argv": ["jevscreen", "screen", "--idea-of", run_id or "RUN_ID", "--dry-run"],
                              "command": f"jevscreen screen --idea-of {run_id or 'RUN_ID'} --dry-run"}
                             if run_id else None)}
    if args.json:
        _emit(body)
    elif not legacy or errs or warns or resolved:
        print("作者字段：" + ("通过" if not errs else f"{len(errs)} 个错误"))
        for e in errs:
            print(f"  错误：{e['text_zh']}")
        for w in warns:
            print(f"  提醒：{w['text_zh']}")
        for r in resolved:
            print(f"  {sieve_author.LIST_ZH[r['list']]}：{r['company']} → {r.get('security_id') or '?'}（{r['state']}）"
                  + (f"；{r['note_zh']}" if r.get("note_zh") else ""))
        for pv in preview:
            print(f"  上次运行：{pv['security_id']} — {pv['one_line_zh']}")
    if errs:
        return EXIT_ERROR
    return code


COMMANDS: dict[str, Callable[[argparse.Namespace, Any], int]] = {"why": cmd_why}
SIEVE_COMMANDS: dict[str, Callable[[argparse.Namespace, Any], int]] = {
    "new": cmd_sieve_new, "set": cmd_sieve_set, "add": cmd_sieve_add, "remove": cmd_sieve_remove,
    "pin": cmd_sieve_pin, "unpin": cmd_sieve_unpin, "list": cmd_sieve_list, "check": cmd_sieve_check}
