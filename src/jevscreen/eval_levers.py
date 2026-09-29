"""`jevscreen eval run` options for the retrieval levers (plan step 3), kept out of eval_cli.

- `--lang-terms`, `--second-search`: passed to screen() as the flags of `jevscreen screen` (both default off).
- `--from-run SPEC` (also spelled `--from-runs`; jevscreen.evalfrom): every idea is screened `--from-run` its base
  run, so L1 is never read again (its stored answers are loaded; $0) and unchanged L2 inputs are cache hits. Before
  anything is sent, an idea without a base run, or with a base run the database does not hold for that idea, stops
  the whole run (EvalError); after each screen, a base run that still cost L1 money stops it too (check_l1). Every
  lever is passed to screen() explicitly (off unless asked), so a control arm never inherits a lever.
- Before anything is sent, `--lang-terms` / `--second-search` need the local keyword model's terms for every idea
  (its cache <home>/keywords, or the model): without them the lever falls back to the idea's Latin acronyms and the
  A/B would pay for the fallback, so the run stops (EvalError).
"""
from __future__ import annotations

from typing import Any, Iterable


def add_run_args(p: Any) -> None:
    for flag, dest, text in (
            ("lang-terms", "lang_terms", "search terms for every document language, generic ones weak (screen "
                                         "--lang-terms"),
            ("second-search", "second_search", "one more L2 read over other filing passages for L1-core rows L2 "
                                               "found insufficient, at most related (screen --second-search")):
        p.add_argument(f"--{flag}", dest=dest, action="store_const", const=True, default=None,
                       help=f"lever: {text}; default off, also under --from-run)")
        p.add_argument(f"--no-{flag}", dest=dest, action="store_const", const=False,
                       help=f"turn --{flag} off (the default)")


def parse_from_runs(spec: str | None) -> dict[str, str]:
    """{idea id: base run id} from SPEC (evalfrom.parse); EvalError for an item that is neither a pair nor a readable
    file / folder."""
    from . import evalfrom, evalset
    try:
        return evalfrom.base_runs(spec)
    except ValueError as e:
        raise evalset.EvalError(str(e)) from None


def bases(args: Any, sets: Iterable[dict[str, Any]], cfg: Any = None) -> dict[str, str] | None:
    """{idea id: base run} when --from-run is given (evalfrom.check_bases: every idea of `sets` has one and the
    database holds it for that idea, else EvalError before anything is sent), else None."""
    from . import evalfrom, evalset
    try:
        return evalfrom.check_bases(cfg, sets, evalfrom.spec_of(args))
    except ValueError as e:
        raise evalset.EvalError(str(e)) from None


def _keywords(cfg: Any, idea: str) -> dict[str, Any]:
    from . import screen
    return screen._default_keywords(cfg, idea)


def check_keywords(args: Any, cfg: Any, sets: Iterable[dict[str, Any]]) -> None:
    """EvalError, before anything is sent, when --lang-terms / --second-search is on and the local keyword model
    (its cache, else the model itself: free and offline) gives no terms for an idea."""
    from . import evalset
    if not (getattr(args, "lang_terms", False) or getattr(args, "second_search", False)):
        return
    bad = []
    for d in sets:
        try:
            out = _keywords(cfg, d["idea"])
        except Exception as e:  # noqa: BLE001 - KeywordsUnavailable or any local-model failure
            bad.append(f"{d['id']} ({type(e).__name__}: {str(e)[:120]})")
            continue
        kw = (out or {}).get("keywords") if isinstance(out, dict) else None
        if not isinstance(kw, dict) or not any(kw.get(lang) for lang in ("zh", "ja", "ko")):
            bad.append(f"{d['id']} (no zh / ja / ko terms)")
    if bad:
        raise evalset.EvalError("--lang-terms / --second-search: the local keyword model gave no terms for "
                                + "; ".join(bad) + ". The lever would fall back to the ideas' Latin acronyms and the "
                                "A/B would measure that fallback, so nothing was sent. Run `jevscreen keywords "
                                "\"<idea>\"` where the model works (torch + transformers; the cache <home>/keywords "
                                "is what the run reads)")


def before_sending(args: Any, cfg: Any, sets: Iterable[dict[str, Any]]) -> dict[str, str] | None:
    """Every check made before the first send: the base runs (bases) and the keyword model (check_keywords)."""
    sets = list(sets)
    got = bases(args, sets, cfg)
    check_keywords(args, cfg, sets)
    return got


def screen_kwargs(args: Any, data: dict[str, Any], base: dict[str, str] | None) -> dict[str, Any]:
    """The extra screen() arguments of one idea: the levers (evalfrom.lever_kwargs: with a base run every lever is
    explicit, so a control arm never inherits one) and from_run."""
    from . import evalfrom
    kw = evalfrom.lever_kwargs(args, from_run=base is not None)
    if base is not None:
        kw["from_run"] = base[data["id"]]
    return kw


def check_l1(res: dict[str, Any], kw: dict[str, Any], data: dict[str, Any] | None = None,
             scores: list[dict[str, Any]] | None = None) -> None:
    """EvalError when a --from-runs screen read L1 again (it never should: the run stops here). The idea is first
    added to `scores` as 'stopped' with its run id and cost, so what it spent is on record."""
    from . import evalset
    if not kw.get("from_run"):
        return
    l1 = (res.get("layers") or {}).get("l1") or {}
    if l1.get("from_run") != kw["from_run"] or float(l1.get("cost_usd") or 0.0) > 0 or l1.get("new"):
        msg = f"--from-run: run {res.get('run_id')} read L1 again (base {kw['from_run']}); stopped"
        if data is not None and scores is not None:
            scores.append(evalset.unscored(data, "stopped", run_id=res.get("run_id"), cost_usd=res.get("cost_usd"),
                                           error=msg))
        raise evalset.EvalError(msg)
