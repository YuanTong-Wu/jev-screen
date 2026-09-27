"""Local secrets for jev-screen: set / check / clear API keys without ever showing their values.

Rules:
- A key is read from its environment variable first, else from its file under the home directory (data/ by default,
  git-ignored). File names match what the adapters read (config.Config): openrouter_api_key, edinet_api_key,
  opendart_api_key, sec_user_agent.
- set: the value comes from a hidden prompt (getpass) or, with --stdin when stdin is not a terminal, from one line of
  standard input. It is validated lightly (shape only, never against the provider) and written atomically with mode
  0600. No output, error text or exception message ever contains the value.
- check: presence, source (env | file | none), file mode and shape. Reads the value only to check its
  shape inside this process; prints none of it.
- presence(): stat only, never reads the value (used by `jevscreen doctor`). recorded = True for the file recorded with
  `keys set openrouter --from-file`, whose path is never printed.
"""
from __future__ import annotations

import contextlib
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import Config


@dataclass(frozen=True)
class KeySpec:
    name: str
    filename: str
    env: str
    purpose: str
    used_by: str
    default_enabled: bool
    shape: str            # human hint of the expected shape (no example of a real key)
    register_url: str


KEYS: dict[str, KeySpec] = {
    "openrouter": KeySpec(
        "openrouter", "openrouter_api_key", "OPENROUTER_API_KEY",
        "Paid Jev calls (layer 1 and layer 2 of `jevscreen screen`)", "screen", True,
        "an OpenRouter API key: starts with 'sk-or-', no spaces", "https://openrouter.ai/settings/keys"),
    "sec-email": KeySpec(
        "sec-email", "sec_user_agent", "JEVSCREEN_SEC_USER_AGENT",
        "SEC EDGAR fair-access User-Agent '<name> <email>' for US annual reports (sent to sec.gov)", "sync-sec", True,
        "your name and email, e.g. 'Jane Doe jane@example.com'", "https://www.sec.gov/os/accessing-edgar-data"),
    "edinet": KeySpec(
        "edinet", "edinet_api_key", "JEVSCREEN_EDINET_API_KEY",
        "Japanese annual reports (EDINET 有価証券報告書) for layer 2", "sync-edinet", False,
        "the EDINET API v2 Subscription-Key: 16-64 letters and digits (usually 32)",
        "https://api.edinet-fsa.go.jp/api/auth/index.aspx?mode=1"),
    "opendart": KeySpec(
        "opendart", "opendart_api_key", "JEVSCREEN_OPENDART_API_KEY",
        "Korean annual reports (DART 사업보고서) for layer 2", "sync-dart", False,
        "the OpenDART crtfc_key: exactly 40 letters and digits", "https://opendart.fss.or.kr/"),
}

MAX_LEN = 4096


class KeyProblem(ValueError):
    """A key operation failed. The message never contains the key value."""


def spec(name: str) -> KeySpec:
    try:
        return KEYS[name]
    except KeyError:
        raise KeyProblem(f"unknown key name {name!r}; expected one of: {', '.join(KEYS)}") from None


def key_path(cfg: Config, name: str) -> Path:
    return Path(cfg.home) / spec(name).filename


def normalize(value: str) -> str:
    """Strip whitespace, one pair of surrounding quotes and a trailing newline (pasting habits)."""
    v = (value or "").strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"":
        v = v[1:-1].strip()
    return v


def shape_problem(name: str, value: str) -> str | None:
    """None when the value has the expected shape, else a short reason that never quotes the value."""
    v = value
    if not v:
        return "empty value"
    if len(v) > MAX_LEN:
        return "value is too long"
    if any(ord(c) < 32 or ord(c) == 127 for c in v):
        return "value contains control characters (a line break or tab?)"
    if name == "sec-email":
        tokens = v.split()
        emails = [t for t in tokens if re.fullmatch(r"<?[^@\s<>]+@[^@\s<>]+\.[A-Za-z]{2,}>?", t)]
        if not emails:
            return "no email address found; expected " + KEYS[name].shape
        if len(tokens) < 2:
            return "a name is needed before the email; expected " + KEYS[name].shape
        if len(v) > 200:
            return "value is too long for a User-Agent (keep it to name and email)"
        if any(ord(c) > 126 for c in v):
            return "use plain ASCII letters for the User-Agent (HTTP headers are ASCII)"
        return None
    if any(ord(c) < 33 or ord(c) > 126 for c in v):
        return "value contains spaces or non-ASCII characters"
    if name == "openrouter" and not (v.startswith("sk-or-") and len(v) >= 20):
        return "expected " + KEYS[name].shape
    if name == "edinet" and not re.fullmatch(r"[A-Za-z0-9]{16,64}", v):
        return "expected " + KEYS[name].shape
    if name == "opendart" and not re.fullmatch(r"[A-Za-z0-9]{40}", v):
        return "expected " + KEYS[name].shape
    return None


def write_key(cfg: Config, name: str, value: str, *, check_shape: bool = True) -> dict[str, Any]:
    """Validate and store one key (atomic, mode 0600). Returns a summary without the value."""
    s = spec(name)
    v = normalize(value)
    problem = shape_problem(name, v)
    if problem and (check_shape or not v or any(ord(c) < 32 for c in v)):
        raise KeyProblem(f"{name}: not stored: {problem}")
    home = Path(cfg.home)
    home.mkdir(parents=True, exist_ok=True)
    path = home / s.filename
    tmp = home / f".{s.filename}.{os.getpid()}.tmp"
    try:
        with contextlib.suppress(FileNotFoundError):
            tmp.unlink()
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            os.fchmod(fd, 0o600)
            os.write(fd, (v + "\n").encode("utf-8"))
        finally:
            os.close(fd)
        os.replace(tmp, path)
        os.chmod(path, 0o600)
    except OSError as e:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise KeyProblem(f"{name}: could not write {path}: {e.strerror or type(e).__name__}") from None
    out: dict[str, Any] = {"name": name, "path": str(path), "mode": "0600", "shape_ok": problem is None}
    if name == "openrouter":
        _forget_location(cfg)           # a key typed in now replaces a recorded --from-file location
    reads = _candidate_files(cfg, name)[0][0]   # the one file Config reads when the variable is not set
    if os.environ.get(s.env):
        out["warning"] = f"environment variable {s.env} is set and takes precedence over this file"
    elif reads != path:
        out["warning"] = (f"JEVSCREEN_OPENROUTER_KEY_FILE names {reads}, which is read instead of this file; unset "
                          "or change it")
    return out


def _location_path(cfg: Config) -> Path:
    from .config import OPENROUTER_KEY_LOCATION
    return Path(cfg.home) / OPENROUTER_KEY_LOCATION


def _forget_location(cfg: Config) -> bool:
    p = _location_path(cfg)
    try:
        p.unlink()
        return True
    except FileNotFoundError:
        return False
    except OSError as e:
        raise KeyProblem(f"could not remove the recorded key location: {e.strerror or type(e).__name__}") from None


def record_key_file(cfg: Config, name: str, path: str | Path) -> dict[str, Any]:
    """`keys set openrouter --from-file PATH`: record where the human keeps the key (a file they created). Only
    the location is written (<home>/openrouter_key_location); the key is never copied, never
    printed, and neither is the path in the output. The file is read inside this process only to check that it holds
    just the key (Config sends its text as it is, so an .env line or a note would be rejected by OpenRouter).
    Config reads the file at every call, so a quickstart worker that is already running uses it from its next paid
    step on."""
    if name != "openrouter":
        raise KeyProblem(f"--from-file is only for openrouter (got {name!r})")
    p = Path(path).expanduser()
    try:
        p = p.resolve(strict=True)
        st = p.stat()
    except OSError:
        raise KeyProblem("--from-file: that file does not exist or cannot be opened; nothing recorded") from None
    if not p.is_file():
        raise KeyProblem("--from-file: that is not a regular file; nothing recorded")
    if st.st_size == 0:
        raise KeyProblem("--from-file: that file is empty; nothing recorded")
    if st.st_size > MAX_LEN + 16:
        raise KeyProblem("--from-file: the file must contain only the key (one line starting with sk-or-); "
                         "nothing recorded")
    try:                     # Config sends the file's text as it is (stripped): it must be the key and nothing else
        problem = shape_problem(name, p.read_text(encoding="utf-8", errors="replace").strip())
    except OSError:
        raise KeyProblem("--from-file: that file cannot be read; nothing recorded") from None
    if problem:
        raise KeyProblem("--from-file: the file must contain only the key, one line starting with sk-or- (no "
                         "'OPENROUTER_API_KEY=', no quotes, no other text); nothing recorded")
    home = Path(cfg.home)
    home.mkdir(parents=True, exist_ok=True)
    dest = _location_path(cfg)
    tmp = home / f".{dest.name}.{os.getpid()}.tmp"
    tmp.write_text(str(p) + "\n", encoding="utf-8")
    os.replace(tmp, dest)
    out: dict[str, Any] = {"name": name, "recorded": True, "source": "recorded-file", "configured": True,
                           "mode_ok": _mode_ok(p)}
    if not out["mode_ok"]:
        out["warning"] = "other users on this computer can read that file; chmod 600 it (the key stays where it is)"
    s = spec(name)
    if os.environ.get(s.env):
        out["warning"] = f"environment variable {s.env} is set and takes precedence over the recorded file"
    elif os.environ.get("JEVSCREEN_OPENROUTER_KEY_FILE"):
        out["warning"] = "JEVSCREEN_OPENROUTER_KEY_FILE is set and takes precedence over the recorded file"
    return out


def clear_key(cfg: Config, name: str) -> dict[str, Any]:
    s = spec(name)
    path = key_path(cfg, name)
    if name == "openrouter":
        _forget_location(cfg)
    existed = path.exists()
    if existed:
        try:
            path.unlink()
        except OSError as e:
            raise KeyProblem(f"{name}: could not remove {path}: {e.strerror or type(e).__name__}") from None
    out: dict[str, Any] = {"name": name, "path": str(path), "removed": existed}
    still = presence(cfg, name)
    if still["configured"]:
        where = f"environment variable {s.env}" if still["source"] == "env" else still["path"]
        out["warning"] = f"the key is still provided by {where}; remove that too to stop using it"
    return out


def _mode_ok(path: Path) -> bool:
    try:
        return stat.S_IMODE(path.stat().st_mode) & 0o077 == 0
    except OSError:
        return False


def _candidate_files(cfg: Config, name: str) -> list[tuple[Path, str]]:
    if name == "openrouter":
        return [(p, "file") for p in cfg.openrouter_key_files()]
    return [(key_path(cfg, name), "file")]


def presence(cfg: Config, name: str) -> dict[str, Any]:
    """Where the key comes from, by stat only: the value is never read.

    Mirrors Config's resolution exactly: a non-empty environment variable wins, else the FIRST existing candidate
    file, even when it is empty (Config then returns an empty key and never looks further). A blank variable or an
    empty file is reported as that source with configured = False, so doctor never says 'set' while the adapter
    gets nothing."""
    s = spec(name)
    base = {"name": name, "env": s.env, "used_by": s.used_by, "default_enabled": s.default_enabled}
    env = os.environ.get(s.env)
    if env:
        return {**base, "configured": bool(env.strip()), "source": "env", "path": None, "mode_ok": None}
    for path, kind in _candidate_files(cfg, name):
        try:
            if not path.exists():
                continue
            filled = path.is_file() and path.stat().st_size > 0
            return {**base, "configured": filled, "source": kind, "path": str(path),
                    "mode_ok": _mode_ok(path) if filled else None,
                    "recorded": name == "openrouter" and path == cfg.openrouter_key_location()}
        except OSError:
            continue
    return {**base, "configured": False, "source": "none", "path": str(key_path(cfg, name)), "mode_ok": None}


def effective_file(cfg: Config, name: str) -> Path | None:
    """The file Config would read the key from (the first existing candidate), ignoring the environment."""
    for path, _ in _candidate_files(cfg, name):
        with contextlib.suppress(OSError):
            if path.exists():
                return path
    return None


def _read_value(cfg: Config, name: str, where: dict[str, Any]) -> str | None:
    if where["source"] == "env":
        return os.environ.get(spec(name).env, "")
    if where["path"]:
        try:
            return Path(where["path"]).read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None
    return None


def check(cfg: Config, name: str) -> dict[str, Any]:
    """presence() plus a shape check of the value (read inside this process, never returned)."""
    where = presence(cfg, name)
    out = {**where, "shape_ok": None, "problem": None}
    if where["configured"]:
        value = _read_value(cfg, name, where)
        problem = "unreadable" if value is None else shape_problem(name, normalize(value))
        out["shape_ok"] = problem is None
        out["problem"] = problem
        del value
    return out


def check_all(cfg: Config) -> list[dict[str, Any]]:
    return [check(cfg, n) for n in KEYS]
