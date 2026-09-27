#!/usr/bin/env python3
"""Pre-publication scan: fail when a tree contains material that must not be published.

    python3 tools/release_check.py [ROOT] [--json] [--git-history] [--max-kb N]

Checks (each finding is one line: category, path[:line], short redacted evidence):
  email          e-mail addresses outside the reserved example domains (example.*, *.invalid, *.test) and known no-reply addresses
  home-path      absolute home paths (/Users/<name>, /home/<name>, C:\\Users\\<name>) and the current machine's
                 home directory / user name (read at run time, so no personal value lives in this file)
  old-project    key files under a .secrets/ folder, plus private patterns from an untracked
                 .release_private_patterns file (so no maintainer path is written into this file)
  api-key        strings that look like real API keys or private keys (fake test keys are recognised)
  secret-file    tracked files that hold local secrets or data (data/, .secrets/, *_api_key, sec_user_agent,
                 .env, *.duckdb)
  large-file     any file over --max-kb (default 256 KB); binary fixtures over --max-binary-kb (default 128 KB)
  fixture        a file in tests/fixtures not declared in tests/fixtures/MANIFEST.json, or a declared one
                 whose origin is not synthetic / hand-made / transformed / own-output
  gray-domain    a fixture that mentions a gray-tier or third-party dump domain (TradingView, Yahoo, CNINFO,
                 BSE, TWSE/MOPS/TPEx, DART web, SEC Archives) without being declared synthetic, hand-made or
                 transformed, or that is bigger than a hand-made fixture can plausibly be (--max-gray-kb, default
                 128 KB)
  history        (--git-history only) commit author/committer e-mails that are not no-reply addresses and
                 names that match the owner's git user.name (global/system config) or this machine's user name;
                 and every blob reachable from any ref that is not in the current tree: the text checks above on
                 old versions of every file, every earlier or removed fixture version (MANIFEST.json vouches only
                 for the current one), secret-file names and large files. Anything here means: publish from a
                 fresh orphan squash (purging only the listed paths is not enough)

Files are taken from `git ls-files` when ROOT is a git work tree (so git-ignored data/ is never read), else from a
walk that skips .git, data, .secrets, caches and build output. A line containing `release-check: ignore` is
skipped. Exit status: 0 clean, 1 findings, 2 usage error.
"""
from __future__ import annotations

import argparse
import getpass
import gzip
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import zipfile
from dataclasses import asdict, dataclass
from pathlib import Path

IGNORE_PRAGMA = "release-check: ignore"
SKIP_DIRS = {".git", "data", ".secrets", "__pycache__", ".pytest_cache", "build", "dist", ".venv", "venv",
             "node_modules", ".mypy_cache", ".ruff_cache"}
TEXT_SUFFIXES = {".py", ".md", ".txt", ".json", ".toml", ".cfg", ".ini", ".yml", ".yaml", ".html", ".htm", ".xml",
                 ".csv", ".sh", ".js", ".css", ".sql", ".gitignore", ""}
FIXTURE_DIR = "tests/fixtures"
MANIFEST = FIXTURE_DIR + "/MANIFEST.json"
OK_ORIGINS = {"synthetic", "hand-made", "transformed", "own-output"}
GRAY_OK_ORIGINS = {"synthetic", "hand-made", "transformed"}

# --- patterns (split literals so this file does not match itself) -------------------------------------------------
EMAIL_RE = re.compile(r"(?<![\w.+-])[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}(?![\w-])")
EMAIL_OK_DOMAINS = ("example.com", "example.org", "example.net", "example.invalid", "example", "invalid", "test",
                    "localhost", "users.noreply.github.com")
EMAIL_OK_EXACT = {"noreply@anthropic.com", "noreply@github.com"}
HOME_RE = re.compile(r"(?:/" + r"Users/|/" + r"home/|[A-Za-z]:\\\\?" + r"Users\\\\?)([A-Za-z0-9][A-Za-z0-9._-]*)")
HOME_OK_NAMES = {"name", "you", "user", "username", "runner", "me", "example", "shared", "USER", "Shared"}
# Machine-specific paths of a maintainer's earlier private workspace are NOT listed here (that would publish
# them). Put one regex per line in an untracked, git-ignored `.release_private_patterns` file at the repo root
# (or name a file with JEVSCREEN_RELEASE_PATTERNS_FILE); they are added to the generic pattern below.
OLD_PROJECT_GENERIC = r"\.secrets/[\w./-]*_api_key"
PRIVATE_PATTERNS_FILE = ".release_private_patterns"


def old_project_re(root: Path | None = None) -> re.Pattern:
    """The generic secret-folder pattern plus any private patterns from the untracked patterns file."""
    parts = [OLD_PROJECT_GENERIC]
    named = os.environ.get("JEVSCREEN_RELEASE_PATTERNS_FILE")
    for f in ([Path(named)] if named else []) + ([Path(root) / PRIVATE_PATTERNS_FILE] if root else []):
        try:
            lines = f.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        parts += [ln.strip() for ln in lines if ln.strip() and not ln.lstrip().startswith("#")]
    return re.compile("|".join(f"(?:{p})" for p in parts))


OLD_PROJECT_RE = old_project_re(Path(__file__).resolve().parents[1])
KEY_PATTERNS = [
    ("openrouter/openai-style key", re.compile(r"\bsk-(?:or-v1-|proj-|ant-)?[A-Za-z0-9_-]{24,}")),
    ("Vercel AI Gateway key", re.compile(r"\bvck_[A-Za-z0-9_-]{24,}")),
    ("AWS access key id", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("GitHub token", re.compile(r"\b(?:ghp|gho|ghs|ghu)_[A-Za-z0-9]{36}\b|\bgithub_pat_[A-Za-z0-9_]{40,}")),
    ("Google API key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    ("Slack token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}")),
    ("private key block", re.compile(r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |PGP )?PRIVATE KEY-----")),
    ("key assignment", re.compile(r"(?i)(?:api[_-]?key|secret|token|crtfc_key|subscription-key|password)"
                                  r"\s*[:=]\s*['\"]?([A-Za-z0-9_\-]{24,})")),
]
FAKE_KEY_RE = re.compile(r"(?i)fake|test|dummy|example|placeholder|redacted|x{6,}|0{6,}|a{8,}|<[^>]*>|\{|\}")
GRAY_DOMAINS = ("tradingview.com", "finance.yahoo.com", "query1.finance.yahoo", "query2.finance.yahoo",
                "cninfo.com.cn", "bseindia.com", "dart.fss.or.kr", "sec.gov/Archives",
                # Taiwan: every TWSE host (mops., doc. which serves the MOPS reports, mopsov., openapi.) and TPEx
                "twse.com.tw", "tpex.org.tw",
                "morningstar.com", "investing.com")
SECRET_FILE_RE = re.compile(r"(?:^|/)(?:data/|\.secrets/|\.env$|[^/]*_api_key$|sec_user_agent$|[^/]*\.duckdb(?:\.wal)?$)")


@dataclass
class Finding:
    category: str
    path: str
    line: int
    evidence: str

    def __str__(self) -> str:
        where = f"{self.path}:{self.line}" if self.line else self.path
        return f"{self.category:12s} {where}  {self.evidence}"


def redact(s: str, keep: int = 3) -> str:
    """Show only the first few characters of a match: findings must not re-publish what they found."""
    s = s.strip()
    return s[:keep] + "***" + (f"({len(s)} chars)" if len(s) > keep else "")


# --- file listing -------------------------------------------------------------------------------------------------
def _export_ignored(root: Path, rel: str) -> bool:
    """True when .gitattributes marks the path export-ignore: `git archive` (the publishing path) leaves it out, so
    it is never published and is not scanned (e.g. a maintainer-only docs/dev/ folder)."""
    parts = rel.split("/")
    paths = [rel] + ["/".join(parts[:i]) + "/" for i in range(1, len(parts))]     # the file and each parent dir
    try:
        out = subprocess.run(["git", "-C", str(root), "check-attr", "export-ignore", "--", *paths],
                             capture_output=True, check=True, text=True)
    except (OSError, subprocess.CalledProcessError):
        return False
    return any(line.endswith(": set") for line in out.stdout.splitlines())


def list_files(root: Path) -> list[str]:
    try:
        out = subprocess.run(["git", "-C", str(root), "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
                             capture_output=True, check=True)
        files = [f for f in out.stdout.decode("utf-8", "replace").split("\0") if f]
        return sorted(f for f in files if (root / f).is_file() and not _export_ignored(root, f))
    except (OSError, subprocess.CalledProcessError):
        pass
    found = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.endswith(".egg-info")]
        for fn in filenames:
            found.append(str((Path(dirpath) / fn).relative_to(root)).replace(os.sep, "/"))
    return sorted(found)


def text_views(path: Path, data: bytes) -> list[tuple[str, str]]:
    """(label, text) pairs to scan: the file itself, or the members of a .gz / .zip, or a PDF's metadata + text."""
    name = path.name.lower()
    if name.endswith(".gz"):
        try:
            return [(path.name, gzip.decompress(data).decode("utf-8", "replace"))]
        except (OSError, EOFError):
            return []
    if name.endswith(".zip"):
        views = []
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                for info in z.infolist():
                    if not info.is_dir() and info.file_size <= 20 * 1024 * 1024:
                        views.append((f"{path.name}!{info.filename}", z.read(info).decode("utf-8", "replace")))
        except zipfile.BadZipFile:
            pass
        return views
    if name.endswith(".pdf"):
        return [(path.name, _pdf_text(data))]
    if b"\0" in data[:4096]:
        return []
    return [(path.name, data.decode("utf-8", "replace"))]


def _pdf_text(data: bytes) -> str:
    try:
        import fitz  # PyMuPDF, optional
    except ImportError:
        return data.decode("latin-1", "replace")      # still catches plain-text metadata such as /Author (...)
    try:
        with fitz.open(stream=data, filetype="pdf") as doc:
            meta = " ".join(str(v) for v in (doc.metadata or {}).values() if v)
            return meta + "\n" + "\n".join(page.get_text() for page in doc)
    except Exception:
        return data.decode("latin-1", "replace")


def is_binary(path: Path, data: bytes) -> bool:
    return path.suffix.lower() in {".pdf", ".zip", ".gz", ".png", ".jpg", ".jpeg", ".gif", ".xlsx", ".docx",
                                    ".duckdb", ".parquet", ".bin"} or b"\0" in data[:4096]


# --- checks -------------------------------------------------------------------------------------------------------
def personal_markers() -> list[re.Pattern]:
    """The running machine's home directory and user name (never stored in this file), as whole-word patterns."""
    out = []
    home = os.path.expanduser("~")
    if home and home not in ("/", "~") and len(home) > 6:
        out.append(re.compile(re.escape(home)))
    try:
        user = getpass.getuser()
    except Exception:
        user = ""
    if user and len(user) >= 5 and user.lower() not in {"runner", "root", "admin", "ubuntu", "user", "users"}:
        out.append(re.compile(r"(?<![A-Za-z0-9])" + re.escape(user) + r"(?![A-Za-z0-9])", re.IGNORECASE))
    return out


def scan_text(rel: str, label: str, text: str, markers: list[re.Pattern]) -> list[Finding]:
    found: list[Finding] = []
    where = rel if label == Path(rel).name else f"{rel}!{label.split('!', 1)[-1]}"
    for n, line in enumerate(text.splitlines(), 1):
        if IGNORE_PRAGMA in line:
            continue
        for m in EMAIL_RE.finditer(line):
            e = m.group(0).lower()
            dom = e.rsplit("@", 1)[1]
            if e in EMAIL_OK_EXACT or dom.startswith("example.") or any(
                    dom == d or dom.endswith("." + d) for d in EMAIL_OK_DOMAINS):
                continue
            found.append(Finding("email", where, n, redact(m.group(0))))
        homes = [m.group(0) for m in HOME_RE.finditer(line) if m.group(1) not in HOME_OK_NAMES]
        for h in homes:
            found.append(Finding("home-path", where, n, redact(h, keep=7)))
        if not homes and any(mk.search(line) for mk in markers):
            found.append(Finding("home-path", where, n, "current machine's home directory or user name"))
        if OLD_PROJECT_RE.search(line):
            found.append(Finding("old-project", where, n, redact(OLD_PROJECT_RE.search(line).group(0), keep=8)))
        for label_, rx in KEY_PATTERNS:
            for m in rx.finditer(line):
                value = m.group(m.lastindex or 0)
                if FAKE_KEY_RE.search(value) or FAKE_KEY_RE.search(m.group(0)) or len(set(value)) < 8:
                    continue
                found.append(Finding("api-key", where, n, f"{label_}: {redact(value, keep=4)}"))
    return found


def load_manifest(root: Path) -> dict | None:
    p = root / MANIFEST
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8")).get("files", {})
    except (ValueError, AttributeError):
        return {}


def scan(root: Path, max_kb: int = 256, max_binary_kb: int = 128, max_gray_kb: int = 128,
         git_history: bool = False) -> list[Finding]:
    root = root.resolve()
    files = list_files(root)
    markers = personal_markers()
    manifest = load_manifest(root)
    self_rel = None
    try:
        self_rel = str(Path(__file__).resolve().relative_to(root)).replace(os.sep, "/")
    except ValueError:
        pass
    findings: list[Finding] = []
    for rel in files:
        path = root / rel
        if SECRET_FILE_RE.search(rel):
            findings.append(Finding("secret-file", rel, 0, "local secret or data file must never be published"))
            continue
        try:
            data = path.read_bytes()
        except OSError:
            continue
        size_kb = len(data) / 1024
        binary = is_binary(path, data)
        if size_kb > max_kb or (binary and size_kb > max_binary_kb):
            findings.append(Finding("large-file", rel, 0, f"{size_kb:.0f} KB ({'binary' if binary else 'text'})"))
        in_fixtures = rel.startswith(FIXTURE_DIR + "/")
        views = text_views(path, data)
        if rel != self_rel:
            for label, text in views:
                findings.extend(scan_text(rel, label, text, markers))
        if in_fixtures and rel != MANIFEST:
            name = rel[len(FIXTURE_DIR) + 1:]
            entry = (manifest or {}).get(name)
            origin = entry.get("origin") if isinstance(entry, dict) else None
            if manifest is None:
                findings.append(Finding("fixture", rel, 0, f"no {MANIFEST}: every fixture must declare its origin"))
            elif origin not in OK_ORIGINS:
                findings.append(Finding("fixture", rel, 0,
                                        "not declared in MANIFEST.json" if entry is None else f"origin {origin!r}"))
            gray = sorted({d for _, text in views for d in GRAY_DOMAINS if d in text.lower()})
            if gray and (origin not in GRAY_OK_ORIGINS or size_kb > max_gray_kb):
                why = "not declared synthetic/hand-made/transformed" if origin not in GRAY_OK_ORIGINS else f"{size_kb:.0f} KB"
                findings.append(Finding("gray-domain", rel, 0, f"{', '.join(gray)} ({why})"))
    if manifest:
        present = {f[len(FIXTURE_DIR) + 1:] for f in files if f.startswith(FIXTURE_DIR + "/")}
        for name in sorted(set(manifest) - present):
            findings.append(Finding("fixture", f"{FIXTURE_DIR}/{name}", 0, "declared in MANIFEST.json but missing"))
    if git_history:
        findings.extend(scan_history(root, files, markers, max_kb, max_binary_kb, self_rel))
    return findings


def personal_names() -> list[str]:
    """The owner's git identity name (global/system config, never stored in this file).

    The repository's local config is not read: a release clone may set a neutral local name on purpose."""
    names: list[str] = []
    for scope in ("--global", "--system"):
        try:
            out = subprocess.run(["git", "config", scope, "--get", "user.name"],
                                 capture_output=True, text=True).stdout.strip()
        except OSError:
            out = ""
        if out and out not in names:
            names.append(out)
    return names


def git_blob_sha(data: bytes) -> str:
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def _history_blobs(root: Path) -> list[tuple[str, str, bytes]]:
    """(sha, path, content) of every blob reachable from any ref (one path per blob, as rev-list names it)."""
    objs = subprocess.run(["git", "-C", str(root), "rev-list", "--objects", "--all", "--filter=object:type=blob"],
                          capture_output=True, check=True).stdout.decode("utf-8", "replace")
    paths: dict[str, str] = {}
    for line in objs.splitlines():
        sha, _, path = line.partition(" ")
        if path and sha not in paths:
            paths[sha] = path
    if not paths:
        return []
    raw = subprocess.run(["git", "-C", str(root), "cat-file", "--batch"], input="\n".join(paths).encode() + b"\n",
                         capture_output=True, check=True).stdout
    out, pos = [], 0
    while pos < len(raw):
        nl = raw.index(b"\n", pos)
        header = raw[pos:nl].decode().split()
        pos = nl + 1
        if len(header) < 3 or header[1] != "blob":
            continue
        size = int(header[2])
        out.append((header[0], paths.get(header[0], ""), raw[pos:pos + size]))
        pos += size + 1
    return out


def scan_history(root: Path, files: list[str], markers: list[re.Pattern] | None = None, max_kb: int = 256,
                 max_binary_kb: int = 128, self_rel: str | None = None) -> list[Finding]:
    """Everything a published history would carry that the tree scan does not see.

    Commit metadata (author/committer e-mails and names) and every blob reachable from any ref whose content is not
    in the current tree: old versions of every file get the text checks, old or removed fixtures are reported
    (MANIFEST.json only vouches for the current content), secret-file names and sizes are checked too."""
    markers = personal_markers() if markers is None else markers
    found: list[Finding] = []
    try:
        log = subprocess.run(["git", "-C", str(root), "log", "--all", "--format=%ae%x00%ce%x00%an%x00%cn"],
                             capture_output=True, check=True, text=True).stdout
        blobs = _history_blobs(root)
    except (OSError, subprocess.CalledProcessError):
        return [Finding("history", ".", 0, "not a git repository; history not checked")]
    mails, names = set(), set()
    for line in log.splitlines():
        parts = line.split("\0")
        if len(parts) == 4:
            mails.update(x.strip().lower() for x in parts[:2] if x.strip())
            names.update(x.strip() for x in parts[2:] if x.strip())
    for e in sorted(mails):
        dom = e.rsplit("@", 1)[-1]
        if e in EMAIL_OK_EXACT or any(dom == d or dom.endswith("." + d) for d in EMAIL_OK_DOMAINS):
            continue
        found.append(Finding("history", "git log", 0, f"author/committer e-mail {redact(e)}"))
    own = {n.strip().lower() for n in personal_names() if n.strip()}
    for n in sorted(names):
        if n.lower() in own or any(mk.search(n) for mk in markers):
            found.append(Finding("history", "git log", 0, f"author/committer name {redact(n)} is a personal name"))

    present = set(files)
    tree_blobs = set()
    for rel in files:
        try:
            tree_blobs.add(git_blob_sha((root / rel).read_bytes()))
        except OSError:
            pass
    for sha, rel, data in sorted(blobs, key=lambda b: (b[1], b[0])):
        if sha in tree_blobs or not rel or rel == self_rel:
            continue
        at = f"{rel}@{sha[:7]}"
        if SECRET_FILE_RE.search(rel):
            found.append(Finding("history", at, 0, "local secret or data file in history"))
            continue
        path = Path(rel)
        size_kb = len(data) / 1024
        binary = is_binary(path, data)
        views = text_views(path, data)
        for f in (x for label, text in views for x in scan_text(rel, label, text, markers)):
            found.append(Finding("history", at + f.path[len(rel):], f.line, f"{f.category}: {f.evidence}"))
        if rel.startswith(FIXTURE_DIR + "/") and rel != MANIFEST:
            what = ("earlier version of a fixture (MANIFEST.json vouches only for the current one)" if rel in present
                    else "fixture removed from the tree but still in git history")
            gray = sorted({d for _, text in views for d in GRAY_DOMAINS if d in text.lower()})
            extra = (f"; mentions {', '.join(gray)}" if gray else "") + f"; {size_kb:.0f} KB"
            found.append(Finding("history", at, 0, what + extra))
        elif size_kb > max_kb or (binary and size_kb > max_binary_kb):
            found.append(Finding("history", at, 0, f"large file in history: {size_kb:.0f} KB"))
    return found


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("root", nargs="?", default=str(Path(__file__).resolve().parents[1]))
    ap.add_argument("--json", action="store_true", help="print findings as JSON")
    ap.add_argument("--max-kb", type=int, default=256)
    ap.add_argument("--max-binary-kb", type=int, default=128)
    ap.add_argument("--max-gray-kb", type=int, default=128)
    ap.add_argument("--git-history", action="store_true",
                    help="also check commit metadata and every blob reachable from any ref")
    a = ap.parse_args(argv)
    root = Path(a.root)
    if not root.is_dir():
        print(f"release_check: not a directory: {root}", file=sys.stderr)
        return 2
    findings = scan(root, a.max_kb, a.max_binary_kb, a.max_gray_kb, a.git_history)
    if a.json:
        print(json.dumps({"ok": not findings, "findings": [asdict(f) for f in findings]}, ensure_ascii=False, indent=1))
    else:
        for f in findings:
            print(f)
        print(f"release_check: {len(findings)} finding(s) in {root.resolve().name}" if findings
              else f"release_check: clean ({root.resolve().name})")
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
