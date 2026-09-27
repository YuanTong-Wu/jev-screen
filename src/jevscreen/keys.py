"""Local secrets for jev-screen: set / check / clear API keys without ever showing their values.

Rules:
- A key is read from its environment variable first, else from its file under the home directory (data/ by default,
  git-ignored). File names match what the adapters read (config.Config): typesafe_api_key, openrouter_api_key,
  vercel_api_key (the three Jev providers; one is enough), edinet_api_key, opendart_api_key, sec_user_agent.
- set: the value comes from a hidden prompt (getpass) or, with --stdin when stdin is not a terminal, from one line of
  standard input. It is validated lightly (shape only, never against the provider) and written atomically with mode
  0600. No output, error text or exception message ever contains the value.
- check: presence, source (env | file | none), file mode and shape. Reads the value only to check its
  shape inside this process; prints none of it.
- presence(): stat only, never reads the value (used by `jevscreen doctor`). recorded = True for the file recorded with
  `keys set <typesafe|openrouter|vercel> --from-file`, whose path is never printed.
- The Jev provider the human chose is saved in <home>/jev_provider (a provider name, not a secret): every successful
  `keys set <typesafe|openrouter|vercel>` (typed, --dialog, --stdin or --from-file) writes it, so the last Jev key
  set is the one used; `keys use <provider>` switches without a new key; `keys clear <provider>` forgets it when it
  names that provider. When the active provider changes, the output carries `provider_switch` with a one-line notice
  (zh / en) of what was already paid through the old provider (answers are cached per provider) and the command that
  switches back.
"""
from __future__ import annotations

import contextlib
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import JEV_KEY_SOURCES, Config


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


# The three ways to pay for Jev (same model family, same price: US$0.042 per million input tokens, output free). One
# key is enough; jev.resolve_provider() picks the active one (JEVSCREEN_JEV_PROVIDER wins, else the saved choice of
# the human: the last Jev key set, or `keys use`; else the first configured of openrouter, typesafe, vercel).
JEV_KEYS = ("typesafe", "openrouter", "vercel")
KEYS: dict[str, KeySpec] = {
    "typesafe": KeySpec(
        "typesafe", "typesafe_api_key", "TYPESAFE_API_KEY",
        "Paid Jev calls through TypeSafe's official API (layer 1 and layer 2 of `jevscreen screen`)", "screen", True,
        "a TypeSafe API key from console.typesafe.ai/keys: one line, no spaces", "https://console.typesafe.ai/keys"),
    "openrouter": KeySpec(
        "openrouter", "openrouter_api_key", "OPENROUTER_API_KEY",
        "Paid Jev calls through OpenRouter (layer 1 and layer 2 of `jevscreen screen`)", "screen", True,
        "an OpenRouter API key: starts with 'sk-or-', no spaces", "https://openrouter.ai/settings/keys"),
    "vercel": KeySpec(
        "vercel", "vercel_api_key", "AI_GATEWAY_API_KEY",
        "Paid Jev calls through Vercel AI Gateway (layer 1 and layer 2 of `jevscreen screen`)", "screen", True,
        "a Vercel AI Gateway API key (usually starts with 'vck_'): one line, no spaces",
        "https://vercel.com/d?to=%2F%5Bteam%5D%2F%7E%2Fai-gateway%2Fapi-keys&title=AI+Gateway+API+Keys"),
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
CHOICE_FILE = "jev_provider"       # <home>/jev_provider: the Jev provider the human chose (see the module docstring)


class KeyProblem(ValueError):
    """A key operation failed. The message never contains the key value."""


def spec(name: str) -> KeySpec:
    try:
        return KEYS[name]
    except KeyError:
        raise KeyProblem(f"unknown key name {name!r}; expected one of: {', '.join(KEYS)}") from None


def key_path(cfg: Config, name: str) -> Path:
    return Path(cfg.home) / spec(name).filename


def choice_path(cfg: Config) -> Path:
    return Path(cfg.home) / CHOICE_FILE


def saved_provider(cfg: Config) -> str | None:
    """The Jev provider the human chose (keys set / keys use), or None (none saved, or the file names no provider)."""
    try:
        name = choice_path(cfg).read_text(encoding="utf-8").strip().lower()
    except (OSError, UnicodeDecodeError):
        return None
    return name if name in JEV_KEYS else None


def _save_choice(cfg: Config, name: str) -> None:
    home = Path(cfg.home)
    home.mkdir(parents=True, exist_ok=True)
    dest = choice_path(cfg)
    tmp = home / f".{CHOICE_FILE}.{os.getpid()}.tmp"
    try:
        tmp.write_text(name + "\n", encoding="utf-8")
        os.replace(tmp, dest)
    except OSError as e:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise KeyProblem(f"could not save the Jev provider choice: {e.strerror or type(e).__name__}") from None


def _active(cfg: Config) -> tuple[str | None, bool]:
    """(active Jev provider, whether its key is configured); (None, False) for an unknown JEVSCREEN_JEV_PROVIDER."""
    from . import jev
    try:
        prov = jev.resolve_provider(cfg)[0]
    except jev.ProviderError:
        return None, False
    return prov.name, bool(presence(cfg, prov.name).get("configured"))


def _switch_notes(cfg: Config, before: tuple[str | None, bool], out: dict[str, Any], chosen: str | None) -> None:
    """Add active_provider / provider_switch / provider_warning to a keys set|use|clear result (`chosen`: the
    provider the human just chose, None for clear)."""
    from . import jev
    after, _ = _active(cfg)
    out["active_provider"] = after
    env = os.environ.get(jev.PROVIDER_ENV, "").strip()
    if env and chosen and after != chosen:
        out["provider_warning"] = (f"{jev.PROVIDER_ENV}={env} is set and wins over this choice: Jev still goes "
                                   f"through {after}; unset it to use {chosen}")
    if not before[0] or not before[1] or after is None or after == before[0]:
        return
    old, new = jev.PROVIDERS[before[0]], jev.PROVIDERS[after]
    paid = jev.paid_via(cfg, old.name)
    back = f"jevscreen keys use {old.name}"
    if paid and paid["requests"]:
        cost_en = (f" What was already read through {old.label} (${paid['usd']:.2f}, {paid['requests']} requests) "
                   "is not reused: ideas screened before are read again and paid again.")
        sep = " " if old.label_zh[-1].isascii() else ""        # 'TypeSafe 官方读过' / 'OpenRouter 读过'
        cost_zh = (f"之前经 {old.label_zh}{sep}读过的内容（${paid['usd']:.2f}，{paid['requests']} 次请求）不会沿用："
                   "以前筛过的想法会重新读一遍、重新付费。")
    else:
        cost_en = " Answers are cached per provider, so an idea screened before is read again and paid again."
        cost_zh = "AI 的回答按渠道分别缓存，以前筛过的想法会重新读一遍、重新付费。"
    out["provider_switch"] = {
        "from": old.name, "to": new.name, "paid_before": paid, "switch_back_command": back,
        "notice_en": f"Jev now goes through {jev.provider_title(new, 'en')} instead of {old.label}.{cost_en} "
                     f"To go back: `{back}`.",
        "notice_zh": f"Jev 原来用 {old.label_zh}，现在改用 {jev.provider_title(new, 'zh')}。{cost_zh}想换回去：`{back}`。"}


def use_provider(cfg: Config, name: str) -> dict[str, Any]:
    """`keys use <typesafe|openrouter|vercel>`: save the human's choice of Jev provider (no key is read or written)."""
    if name not in JEV_KEYS:
        raise KeyProblem(f"`keys use` is only for the Jev providers ({', '.join(JEV_KEYS)}), not {name!r}")
    before = _active(cfg)
    _save_choice(cfg, name)
    out: dict[str, Any] = {"name": name, "saved": True, "configured": bool(presence(cfg, name).get("configured"))}
    if not out["configured"]:
        out["warning"] = f"no {name} key is set yet: run `jevscreen keys set {name}`"
    _switch_notes(cfg, before, out, name)
    return out


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
    if name in JEV_KEYS and re.match(r"[A-Z][A-Z0-9_]*=", v):
        return "that is an .env line (NAME=...): keep only the key itself"
    if name == "openrouter" and not (v.startswith("sk-or-") and len(v) >= 20):
        if v.startswith("vck_"):
            return "that looks like a Vercel AI Gateway key: store it with `jevscreen keys set vercel`"
        return "expected " + KEYS[name].shape
    if name in ("typesafe", "vercel"):
        if v.startswith("sk-or-"):
            return "that looks like an OpenRouter key: store it with `jevscreen keys set openrouter`"
        if name == "typesafe" and v.startswith("vck_"):
            return "that looks like a Vercel AI Gateway key: store it with `jevscreen keys set vercel`"
        if len(v) < 16:
            return "value is too short for an API key; expected " + KEYS[name].shape
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
    before = _active(cfg) if name in JEV_KEYS else (None, False)
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
    if name in JEV_KEYS:
        _forget_location(cfg, name)     # a key typed in now replaces a recorded --from-file location
        _save_choice(cfg, name)         # the last Jev key set is the one used
        _switch_notes(cfg, before, out, name)
    reads = _candidate_files(cfg, name)[0][0]   # the one file Config reads when the variable is not set
    if os.environ.get(s.env):
        out["warning"] = f"environment variable {s.env} is set and takes precedence over this file"
    elif reads != path:
        out["warning"] = (f"{JEV_KEY_SOURCES[name].file_env} names {reads}, which is read instead of this file; "
                          "unset or change it")
    return out


def _location_path(cfg: Config, name: str = "openrouter") -> Path:
    return Path(cfg.home) / JEV_KEY_SOURCES[name].location


def _forget_location(cfg: Config, name: str = "openrouter") -> bool:
    p = _location_path(cfg, name)
    try:
        p.unlink()
        return True
    except FileNotFoundError:
        return False
    except OSError as e:
        raise KeyProblem(f"could not remove the recorded key location: {e.strerror or type(e).__name__}") from None


def record_key_file(cfg: Config, name: str, path: str | Path) -> dict[str, Any]:
    """`keys set <typesafe|openrouter|vercel> --from-file PATH`: record where the human keeps the key (a file they
    created). Only the location is written (<home>/<name>_key_location); the key is never copied, never printed, and
    neither is the path in the output. The file is read inside this process only to check that it holds just the key
    (Config sends its text as it is, so an .env line or a note would be rejected by the provider). Config reads the
    file at every call, so a quickstart worker that is already running uses it from its next paid step on."""
    if name not in JEV_KEYS:
        raise KeyProblem(f"--from-file is only for the Jev keys ({', '.join(JEV_KEYS)}), not {name!r}")
    only = ("one line starting with sk-or-" if name == "openrouter" else "one line")
    env_name = JEV_KEY_SOURCES[name].env
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
        raise KeyProblem(f"--from-file: the file must contain only the key ({only}); nothing recorded")
    try:                     # Config sends the file's text as it is (stripped): it must be the key and nothing else
        problem = shape_problem(name, p.read_text(encoding="utf-8", errors="replace").strip())
    except OSError:
        raise KeyProblem("--from-file: that file cannot be read; nothing recorded") from None
    if problem:
        raise KeyProblem(f"--from-file: the file must contain only the key, {only} (no '{env_name}=', no quotes, "
                         f"no other text); nothing recorded")
    before = _active(cfg)
    home = Path(cfg.home)
    home.mkdir(parents=True, exist_ok=True)
    dest = _location_path(cfg, name)
    tmp = home / f".{dest.name}.{os.getpid()}.tmp"
    tmp.write_text(str(p) + "\n", encoding="utf-8")
    os.replace(tmp, dest)
    _save_choice(cfg, name)             # the last Jev key set is the one used
    out: dict[str, Any] = {"name": name, "recorded": True, "source": "recorded-file", "configured": True,
                           "mode_ok": _mode_ok(p)}
    _switch_notes(cfg, before, out, name)
    if not out["mode_ok"]:
        out["warning"] = "other users on this computer can read that file; chmod 600 it (the key stays where it is)"
    s = spec(name)
    if os.environ.get(s.env):
        out["warning"] = f"environment variable {s.env} is set and takes precedence over the recorded file"
    elif os.environ.get(JEV_KEY_SOURCES[name].file_env):
        out["warning"] = f"{JEV_KEY_SOURCES[name].file_env} is set and takes precedence over the recorded file"
    return out


def clear_key(cfg: Config, name: str) -> dict[str, Any]:
    s = spec(name)
    path = key_path(cfg, name)
    before = _active(cfg) if name in JEV_KEYS else (None, False)
    if name in JEV_KEYS:
        _forget_location(cfg, name)
        if saved_provider(cfg) == name:     # a cleared provider is no longer the choice: the next key found is used
            with contextlib.suppress(FileNotFoundError):
                choice_path(cfg).unlink()
    existed = path.exists()
    if existed:
        try:
            path.unlink()
        except OSError as e:
            raise KeyProblem(f"{name}: could not remove {path}: {e.strerror or type(e).__name__}") from None
    out: dict[str, Any] = {"name": name, "path": str(path), "removed": existed}
    if name in JEV_KEYS:
        _switch_notes(cfg, before, out, None)
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
    if name in JEV_KEYS:
        return [(p, "file") for p in cfg.jev_key_files(name)]
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
                    "recorded": name in JEV_KEYS and path == cfg.jev_key_location(name)}
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
