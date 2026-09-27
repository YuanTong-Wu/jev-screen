"""Release logic of the daily pack job (ci/daily-pack.yml; copy it to .github/workflows/ to enable), kept out of shell so it can be tested.

    python -m jevscreen.pack_ci tag --date YYYY-MM-DD   < existing tags   -> the tag to publish
    python -m jevscreen.pack_ci newest                  < existing tags   -> the newest pack tag (or nothing)
    python -m jevscreen.pack_ci prune --keep=N          < existing tags   -> pack tags to delete, one per line
    python -m jevscreen.pack_ci gate --manifest NEW [--previous OLD] [--min-ratio 0.95] [--allow-shrink]
                                                                           -> exit 0 publish, 1 do not publish

- tag: the date is fixed once at job start (a run that ends after midnight UTC keeps its date). A published pack
  is never overwritten: a second run for the same date gets pack-DATE.2, pack-DATE.3, ...
- prune: keeps the newest N pack releases; N must be a whole number (anything else means DEFAULT_KEEP), at least 1.
- gate: a pack is published only when it holds data rows and no table shrank below MIN_RATIO (95 %) of the
  previous published pack's rows (e.g. after a lost Actions cache). --allow-shrink is the owner's manual override.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

TAG_PREFIX = "pack-"
DEFAULT_KEEP = 7
MIN_RATIO = 0.95
_PACK_TAG_RE = re.compile(r"^pack-(\d{4}-\d{2}-\d{2})(?:\.(\d{1,4}))?$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def tag_key(tag: str) -> tuple:
    """Sort key of a pack tag: (date, run number); pack-D < pack-D.2 < pack-D.10 < pack-D+1. Other tags sort first."""
    m = _PACK_TAG_RE.match(tag or "")
    if not m:
        return ("", 0, tag or "")
    return (m.group(1), int(m.group(2) or 1), "")


def pack_tags(tags: Iterable[str]) -> list[str]:
    """The pack-DATE[.N] tags among `tags`, newest first."""
    return sorted({t.strip() for t in tags if _PACK_TAG_RE.match(t.strip())}, key=tag_key, reverse=True)


def release_tag(date: str, existing: Iterable[str]) -> str:
    if not _DATE_RE.match(date or ""):
        raise ValueError(f"bad date {date!r}")
    taken = {t.strip() for t in existing}
    tag = f"{TAG_PREFIX}{date}"
    n = 2
    while tag in taken:
        tag = f"{TAG_PREFIX}{date}.{n}"
        n += 1
    return tag


def parse_keep(value: Any) -> int:
    s = str(value if value is not None else "").strip()
    if not s.isdigit() or not s.isascii():
        return DEFAULT_KEEP
    return max(1, int(s))


def tags_to_prune(tags: Iterable[str], keep: Any) -> list[str]:
    return pack_tags(tags)[parse_keep(keep):]


def _table_rows(manifest: Mapping[str, Any]) -> dict[str, int]:
    return {f["table"]: int(f.get("rows") or 0) for f in manifest.get("files") or []
            if isinstance(f, dict) and f.get("table")}


def gate(new: Mapping[str, Any], previous: Mapping[str, Any] | None, *, min_ratio: float = MIN_RATIO,
         allow_shrink: bool = False) -> tuple[bool, list[str]]:
    """(publish?, reasons). Refuses a pack without data rows, and (unless allow_shrink) one in which any table the
    previous pack had keeps fewer than min_ratio of that table's rows."""
    rows = _table_rows(new)
    if not any(rows.values()):
        return False, ["the pack holds no data rows"]
    reasons = []
    if previous is not None and not allow_shrink:
        for table, before in sorted(_table_rows(previous).items()):
            after = rows.get(table, 0)
            if before > 0 and after < min_ratio * before:
                reasons.append(f"{table}: {after} rows < {min_ratio:.0%} of the previous pack's {before}")
    return not reasons, reasons


def _stdin_tags() -> list[str]:
    return [ln.strip() for ln in sys.stdin.read().splitlines() if ln.strip()]


def main(argv: Sequence[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m jevscreen.pack_ci", description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    t = sub.add_parser("tag")
    t.add_argument("--date", required=True)
    sub.add_parser("newest")
    pr = sub.add_parser("prune")
    pr.add_argument("--keep", default=str(DEFAULT_KEEP))
    g = sub.add_parser("gate")
    g.add_argument("--manifest", type=Path, required=True)
    g.add_argument("--previous", type=Path, default=None)
    g.add_argument("--min-ratio", type=float, default=MIN_RATIO)
    g.add_argument("--allow-shrink", action="store_true")
    args = p.parse_args(argv)
    if args.cmd == "tag":
        print(release_tag(args.date, _stdin_tags()))
    elif args.cmd == "newest":
        tags = pack_tags(_stdin_tags())
        if tags:
            print(tags[0])
    elif args.cmd == "prune":
        for tag in tags_to_prune(_stdin_tags(), args.keep):
            print(tag)
    else:
        new = json.loads(args.manifest.read_text(encoding="utf-8"))
        prev = json.loads(args.previous.read_text(encoding="utf-8")) if args.previous else None
        ok, reasons = gate(new, prev, min_ratio=args.min_ratio, allow_shrink=args.allow_shrink)
        for r in reasons:
            print(f"not publishing: {r}", file=sys.stderr)
        return 0 if ok else 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
