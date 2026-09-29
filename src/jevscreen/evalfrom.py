"""`jevscreen eval run --from-run SPEC` (also spelled `--from-runs`): screen each idea again from a finished base run
(its stored L1 answers), so a lever A/B pays only for the lever's own questions (plus any L2 text that changed), never
for L1 again.

SPEC is a comma list whose items are any of: the folder of an earlier `eval run` (its scores.json names each idea's
run_id; only finished ideas), that scores.json itself, a JSON file mapping idea ids to run ids (`{"a": "scr-1"}` or
`{"a": {"run_id": "scr-1"}}`), or an `idea=RUN_ID` pair. Later items win.

Before anything is sent (check_bases): every selected idea needs a base run, every idea an `idea=RUN` pair names must
be selected, and each base run must hold stored L1 answers for that very idea (same text, same English question when
the idea sets one). Otherwise the whole eval stops (ValueError; the caller turns it into EvalError). The Jev client
factory is also wrapped so that a request for an L1 client aborts that idea (L1WouldRerun) before anything is sent.

Levers (lever_kwargs): a lever the command line names on (--judge ARM, --lang-terms, ...) is passed on; under
--from-run every other lever is passed explicitly off, so a control arm never inherits a lever its base run was made
with (screen(from_run=...) inherits a lever it is not given).

Pure helpers plus reading scores.json and (check_bases) the store, read-only. No network.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

# (argparse dest, screen() kwarg) of the on/off levers `eval run` passes; --judge is an arm, handled apart
BOOL_LEVERS = ("l2_constraints", "shortlist", "lang_terms", "second_search")


class L1WouldRerun(ValueError):
    """The screen asked for an L1 client although it runs from a base run: nothing was sent for this idea."""


def spec_of(args: Any) -> str | None:
    """The --from-run SPEC of an `eval run` namespace (dest from_run; older callers used from_runs)."""
    v = getattr(args, "from_run", None) or getattr(args, "from_runs", None)
    return str(v) if v and str(v).strip() else None


def _scores_file(item: str) -> Path | None:
    p = Path(item).expanduser()
    if p.is_dir():
        p = p / "scores.json"
    return p if p.is_file() else None


def _read_json(f: Path) -> Any:
    try:
        return json.loads(f.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise ValueError(f"--from-run: cannot read {f} ({type(e).__name__})") from None


def parse(spec: str | None) -> tuple[dict[str, str], set[str]]:
    """({idea id: run_id}, the idea ids named by an explicit idea=RUN pair) of a SPEC ({}, set() for
    None / ''). ValueError for an unreadable file or an item that is neither a pair nor a file / folder."""
    out: dict[str, str] = {}
    named: set[str] = set()
    for item in [x.strip() for x in str(spec or "").split(",") if x.strip()]:
        f = _scores_file(item)
        if f is None and "=" in item:
            iid, _sep, rid = (x.strip() for x in item.partition("="))
            if not iid or not rid:
                raise ValueError(f"--from-run: {item!r} is not idea=RUN_ID (or give an eval output folder)")
            out[iid] = rid
            named.add(iid)
            continue
        if f is None:
            if item.endswith(".json"):
                raise ValueError(f"--from-run: cannot read {item} (FileNotFoundError)")
            raise ValueError(f"--from-run: {item!r} is not idea=RUN_ID (or give an eval output folder)")
        data = _read_json(f)
        if isinstance(data, dict) and isinstance(data.get("scores"), list):      # an eval run's scores.json
            got = 0
            for s in data["scores"]:
                if isinstance(s, dict) and s.get("id") and s.get("run_id") and s.get("status") in ("ok", "partial"):
                    out[str(s["id"])] = str(s["run_id"])
                    got += 1
            if not got:
                raise ValueError(f"--from-run: {f} names no finished run")
        elif isinstance(data, dict):                                            # {idea: run} map
            for k, v in data.items():
                rid = v.get("run_id") if isinstance(v, dict) else v
                if not isinstance(rid, str) or not rid.strip():
                    raise ValueError(f"--from-run: no run id for idea {k!r}")
                out[str(k)] = rid.strip()          # a map file is a catalogue (like a folder): --ideas may narrow it
        else:
            raise ValueError(f"--from-run: {f} must be an eval scores.json or map idea ids to run ids")
    return out, named


def base_runs(spec: str | None) -> dict[str, str]:
    """{idea id: run_id} of a --from-run SPEC ({} for None / ''). ValueError for an unreadable folder or item."""
    return parse(spec)[0]


def _load_bases(cfg: Any, run_ids: Iterable[str]) -> dict[str, dict[str, Any] | str]:
    """{run_id: screen.load_base_run(...)} (read-only), or {run_id: 'why'} for a run that cannot serve as a base."""
    from . import ondemand, screen, store
    out: dict[str, dict[str, Any] | str] = {}
    try:
        with store.session(cfg, read_only=True, wait_s=ondemand.PLAN_WAIT_S) as con:
            for rid in run_ids:
                try:
                    out[rid] = screen.load_base_run(con, rid)
                except ValueError as e:
                    out[rid] = str(e)
    except store.StoreLocked:
        raise ValueError("--from-run: the database is busy, the base runs could not be checked; nothing was "
                         "screened, run again when it is free") from None
    except ValueError:
        raise
    except Exception as e:  # noqa: BLE001 - e.g. no database yet: no base run can exist
        raise ValueError(f"--from-run: the base runs could not be read ({type(e).__name__}); nothing was "
                         "screened") from None
    return out


def _norm(s: Any) -> str:
    import unicodedata
    return unicodedata.normalize("NFKC", str(s or "")).strip()


def check_bases(cfg: Any, sets: Iterable[Mapping[str, Any]], spec: str | None) -> dict[str, str] | None:
    """{idea id: run id} for a --from-run SPEC (None without one), after every check made before the first send (see
    the module). ValueError naming what is wrong: then nothing was screened."""
    if not spec:
        return None
    from . import screen
    got, named = parse(spec)
    sets = list(sets)
    ids = [str(d["id"]) for d in sets]
    missing = [i for i in ids if i not in got]
    unknown = sorted(k for k in named if k not in ids)
    if missing or unknown:
        parts = ([f"no base run for {', '.join(missing)} (each selected idea needs one, or narrow --ideas); L1 "
                  "would be read again"] if missing else []) + \
                ([f"not a selected idea: {', '.join(unknown)}"] if unknown else [])
        raise ValueError("--from-run: " + "; ".join(parts) + "; nothing was sent")
    resolved = {i: str(screen.resolve_from_run(got[i])) for i in ids}
    stored = _load_bases(cfg, sorted(set(resolved.values())))
    bad = []
    for d in sets:
        rid = resolved[str(d["id"])]
        base = stored.get(rid)
        if not isinstance(base, dict):
            bad.append(f"{d['id']}={rid}: {base or 'no such run'}")
        elif _norm(base.get("idea")) != _norm(d.get("idea")):
            bad.append(f"{d['id']}={rid}: run {rid!r} screened another idea than {d['id']!r}; its L1 answers do "
                       "not apply")
        else:
            base_en = _norm((base.get("params") or {}).get("idea_en"))
            if d.get("idea_en") and base_en and _norm(d["idea_en"]) != base_en:
                bad.append(f"{d['id']}={rid}: run {rid!r} asked Jev in English as {base_en[:80]!r}, the idea as "
                           f"{str(d['idea_en'])[:80]!r}; its L1 answers do not apply")
    if bad:
        raise ValueError("--from-run: " + "; ".join(bad) + "; nothing was sent")
    return resolved


def lever_kwargs(args: Any, from_run: bool = False) -> dict[str, Any]:
    """screen() kwargs of the levers of an `eval run` command line. A lever named on (True, --judge ARM) is passed;
    one named off is passed off; one not named is passed off under --from-run (never inherited from the base run)
    and left out otherwise (off by default). The shortlist (on by default in screen) is always passed: the eval
    screens the bare system, a single list, unless --shortlist / --product-flow names it on (the main list's
    precision is scored from the rows' tiers either way: evalset.score)."""
    kw: dict[str, Any] = {}
    for k in BOOL_LEVERS:
        v = getattr(args, k, None)
        if v:
            kw[k] = True
        elif from_run or k == "shortlist":
            kw[k] = False
    j = getattr(args, "judge", None)
    if j and j != "none":
        kw["judge"] = str(j)
    elif from_run:
        kw["judge"] = None
    return kw


def no_l1_factory(cfg, base_factory: Callable | None = None) -> Callable:
    """A Jev client factory for a from-run screen: L1 is refused (L1WouldRerun), every other layer is the real one.
    The real factory is resolved once, on first use (screen._run_factory: one provider for every layer of a run)."""
    real: list[Callable] = [base_factory] if base_factory is not None else []

    def make(cfg_, **kw: Any):
        if kw.get("layer") == "l1" and not kw.get("dry_run"):
            raise L1WouldRerun("L1 would be read again (a --from-run screen never re-reads L1); nothing was sent")
        if not real:
            from . import screen
            real.append(screen._run_factory(cfg))
        return real[0](cfg_, **kw)
    return make
