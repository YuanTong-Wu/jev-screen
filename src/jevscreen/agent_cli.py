"""CLI commands for agent-first operation: doctor, keys, consent (wired into jevscreen.cli with two lines).

- `jevscreen doctor [--json] [--check-jev]`: readiness report; exit 0 when ready for a screen, else 1.
- `jevscreen keys set NAME [--stdin] [--skip-shape-check]`: hidden prompt (getpass) on a terminal; without a terminal
  only with --stdin (one line). Never echoes, prints or logs the value. Exit 0 stored, 1 refused, 130 cancelled.
- `jevscreen keys set NAME --dialog`: a native hidden-input box (macOS osascript, Linux zenity --password; 300 s);
  the value goes from the dialog straight to the key file. Any failure: exit 1 with the terminal fallback (the
  absolute jevscreen path and how to open a terminal). An AI agent may start it; only the human types in it.
- `jevscreen keys check [NAME]`: presence / source / file mode / shape, never the value. Exit 1 when a NAMED key is
  missing or malformed, else 0.
- `jevscreen keys clear NAME`: remove the key file. Exit 0.
- `jevscreen consent set TOPIC yes|no` / `jevscreen consent show`: the human's recorded answer (consent.json).
JSON schemas: docs/AGENT_API.md.
"""
from __future__ import annotations

import argparse
import getpass
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable

from .keys import KeyProblem

EXIT_OK, EXIT_ERROR, EXIT_INTERRUPTED = 0, 1, 130


def add_parsers(sub: Any) -> None:
    from . import consent, keys
    dr = sub.add_parser("doctor", help="check that this install is ready for a first screen; prints the next command")
    dr.add_argument("--json", action="store_true", help="print the report as JSON (for AI agents)")
    dr.add_argument("--check-jev", action="store_true",
                    help="also ask OpenRouter whether the key works and what is left of its spending limit (one free "
                         "request, no paid call; the account balance is not checked)")
    ks = sub.add_parser("keys", help="set / check / clear API keys without ever showing them")
    kss = ks.add_subparsers(dest="keys_action", required=True)
    kset = kss.add_parser("set", help="store a key from a hidden prompt (file mode 0600)")
    kset.add_argument("name", choices=list(keys.KEYS))
    kset.add_argument("--stdin", action="store_true",
                      help="read one line from standard input when it is not a terminal (pipes)")
    kset.add_argument("--skip-shape-check", action="store_true",
                      help="store the value even if it does not look like the usual key shape")
    kset.add_argument("--dialog", action="store_true",
                      help="open a native hidden input box (macOS / Linux desktop) for the human to paste the key")
    kset.add_argument("--from-file", default=None, metavar="PATH",
                      help="openrouter only: record where the human keeps the key (a file they made); only the "
                           "location is stored, the key is never read, copied or printed")
    kchk = kss.add_parser("check", help="which keys are configured (never the values)")
    kchk.add_argument("name", nargs="?", choices=list(keys.KEYS))
    kclr = kss.add_parser("clear", help="remove a stored key file")
    kclr.add_argument("name", choices=list(keys.KEYS))
    cs = sub.add_parser("consent", help="record the human's answer to a consent question (e.g. gray-sources)")
    css = cs.add_subparsers(dest="consent_action", required=True)
    cset = css.add_parser("set", help="record yes or no, with a timestamp, in data/consent.json")
    cset.add_argument("topic", choices=list(consent.TOPICS))
    cset.add_argument("value", choices=list(consent.VALUES))
    cset.add_argument("--lang", choices=list(consent.LANGS), default="en",
                      help="the language the question was asked in; the exact text shown is recorded (default en)")
    css.add_parser("show", help="show the recorded answers")


def _emit(obj: Any) -> None:
    from .cli import _emit as emit
    emit(obj)


def cmd_doctor(args: argparse.Namespace, cfg: Any) -> int:
    from . import doctor
    result = doctor.run(cfg, check_jev_flag=args.check_jev)
    if args.json:
        _emit(result)
    else:
        print(doctor.format_text(result))
    return EXIT_OK if result["ok"] else EXIT_ERROR


def _read_secret(name: str, use_stdin: bool, getpass_fn: Callable[[str], str] | None = None) -> str:
    from . import keys
    spec = keys.spec(name)
    stdin = sys.stdin
    try:
        tty = bool(stdin is not None and stdin.isatty())
    except (AttributeError, ValueError):
        tty = False
    if tty:
        print(f"Get it from: {spec.register_url}", file=sys.stderr)
        return (getpass_fn or getpass.getpass)(f"{name}: paste or type {spec.shape} (input stays hidden), then press Enter: ")
    if not use_stdin:
        raise keys.KeyProblem("standard input is not a terminal, so the value cannot be typed hidden here. Ask the "
                              f"human to run `jevscreen keys set {name}` in their own terminal, or pipe the value "
                              "with --stdin")
    return stdin.readline() if stdin is not None else ""


DIALOG_TIMEOUT_S = 100          # fits an agent's ~2-minute command timeout; a timeout is retried when the key is ready
CONSOLE_BROWSERS = ("lynx", "w3m", "links", "elinks", "www-browser")


def abs_jevscreen() -> str:
    """The jevscreen command of THIS install as an absolute path (the human's terminal may not have the venv on
    PATH): <venv>/bin/jevscreen, .venv\\Scripts\\jevscreen.exe on Windows, else `<python> -m jevscreen.cli`."""
    exe = Path(sys.executable)
    for cand in (exe.parent / "jevscreen", exe.parent / "jevscreen.exe", exe.parent / "Scripts" / "jevscreen.exe"):
        if cand.is_file():
            return shlex.quote(str(cand)) if os.name != "nt" else f'"{cand}"'
    return f"{shlex.quote(str(exe))} -m jevscreen.cli"


def terminal_hint(lang: str = "en") -> str:
    """One line on how to open a terminal on this OS."""
    if sys.platform == "darwin":
        return ("打开『终端』：按 ⌘+空格，输入“终端”，回车" if lang == "zh" else
                "Open Terminal: press Cmd+Space, type Terminal, press Enter")
    if os.name == "nt":
        return ("打开终端：按 Win 键，输入 PowerShell，回车" if lang == "zh" else
                "Open a terminal: press the Windows key, type PowerShell, press Enter")
    return ("打开终端：按 Ctrl+Alt+T" if lang == "zh" else "Open a terminal: press Ctrl+Alt+T")


def _applescript_quote(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def dialog_command(name: str, platform: str | None = None, env: dict[str, str] | None = None,
                   which: Callable[[str], str | None] = shutil.which) -> list[str] | None:
    """The argv of a native hidden-input dialog for key `name`, or None when this machine has none."""
    from . import keys
    spec = keys.spec(name)
    platform = platform or sys.platform
    env = os.environ if env is None else env
    prompt = f"jev-screen: paste your {name} key ({spec.shape}). It stays hidden and is saved only on this computer."
    if platform == "darwin" and which("osascript"):
        return ["osascript", "-e", f"text returned of (display dialog {_applescript_quote(prompt)} default answer \"\" "
                f"with hidden answer with title \"jev-screen\")"]
    if platform.startswith("linux") and (env.get("DISPLAY") or env.get("WAYLAND_DISPLAY")) and which("zenity"):
        return ["zenity", "--password", "--title=jev-screen: " + prompt[:120]]
    return None


class DialogTimeout(KeyProblem):
    """The box stayed open DIALOG_TIMEOUT_S without an answer (the human is still making the key)."""


def read_secret_dialog(name: str, runner: Callable[..., Any] = subprocess.run, **kw: Any) -> str:
    """The value typed into the dialog (never printed). KeyProblem when there is no dialog, it was cancelled, timed
    out or failed; the message never contains the value."""
    from . import keys
    cmd = dialog_command(name, **kw)
    if cmd is None:
        raise keys.KeyProblem("no native input box on this machine (macOS osascript or Linux zenity with a display)")
    try:
        r = runner(cmd, capture_output=True, text=True, timeout=DIALOG_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        raise DialogTimeout(f"the input box got no answer within {DIALOG_TIMEOUT_S} s") from None
    except OSError as e:
        raise keys.KeyProblem(f"the input box could not start ({type(e).__name__})") from None
    if getattr(r, "returncode", 1) != 0:
        raise keys.KeyProblem("the input box was cancelled or failed")
    return (r.stdout or "").rstrip("\r\n")


def dialog_fallback(name: str, *, timed_out: bool = False) -> dict[str, Any]:
    cmd = f"{abs_jevscreen()} keys set {name}"
    if timed_out:
        return {"human_command": cmd, "timed_out": True, "retry_command": f"jevscreen keys set {name} --dialog",
                "text_en": f"The input box closed after {DIALOG_TIMEOUT_S} s without a key. When your key is ready, "
                           "say so and I will open the box again. Never paste the key into the chat.",
                "text_zh": f"输入框等了 {DIALOG_TIMEOUT_S} 秒没有收到 key，已经关掉。key 准备好了就告诉我，我再弹一次。千万不要把 key "
                           "发到聊天里。"}
    return {"human_command": cmd, "timed_out": False,
            "text_en": f"No input box appeared. {terminal_hint('en')}, run {cmd}, paste the key (it stays hidden) and "
                       "press Enter. Never paste the key into the chat.",
            "text_zh": f"输入框没有弹出来。{terminal_hint('zh')}，运行 {cmd}，粘贴后按回车（输入内容不会显示）。千万不要把 key "
                       "发到聊天里。"}


def cmd_keys(args: argparse.Namespace, cfg: Any) -> int:
    from . import keys
    action = args.keys_action
    if action == "check":
        names = [args.name] if args.name else list(keys.KEYS)
        rows = [keys.check(cfg, n) for n in names]
        if cfg.openrouter_key_source()[0] == "recorded-file":
            for r in rows:
                if r.get("name") == "openrouter":       # the recorded location is not echoed
                    r.update(path=None, source="recorded-file")
        _emit({"command": "keys check", "keys": rows})
        if args.name and not (rows[0]["configured"] and rows[0]["shape_ok"]):
            return EXIT_ERROR
        return EXIT_OK
    if action == "clear":
        try:
            out = keys.clear_key(cfg, args.name)
        except keys.KeyProblem as e:
            _emit({"command": "keys clear", "status": "error", "name": args.name, "error": str(e)})
            return EXIT_ERROR
        _emit({"command": "keys clear", "status": "ok", **out})
        return EXIT_OK
    # set
    if getattr(args, "from_file", None):
        try:
            out = keys.record_key_file(cfg, args.name, args.from_file)
        except keys.KeyProblem as e:
            _emit({"command": "keys set", "status": "refused", "name": args.name, "error": str(e)})
            return EXIT_ERROR
        _emit({"command": "keys set", "status": "ok", "via": "from-file", **out})
        return EXIT_OK
    if getattr(args, "dialog", False):
        try:
            value = read_secret_dialog(args.name)
        except keys.KeyProblem as e:
            _emit({"command": "keys set", "status": "refused", "name": args.name, "error": str(e),
                   **dialog_fallback(args.name, timed_out=isinstance(e, DialogTimeout))})
            return EXIT_ERROR
        try:
            out = keys.write_key(cfg, args.name, value, check_shape=not args.skip_shape_check)
        except keys.KeyProblem as e:
            _emit({"command": "keys set", "status": "refused", "name": args.name, "error": str(e),
                   "hint": "nothing stored; run keys set --dialog again and paste the whole key",
                   **dialog_fallback(args.name)})
            return EXIT_ERROR
        finally:
            value = None  # noqa: F841
        _emit({"command": "keys set", "status": "ok", "via": "dialog", **out})
        return EXIT_OK
    try:
        value = _read_secret(args.name, args.stdin)
    except (KeyboardInterrupt, EOFError):
        print(f"error: keys set {args.name}: cancelled; nothing stored", file=sys.stderr)
        return EXIT_INTERRUPTED
    except keys.KeyProblem as e:
        _emit({"command": "keys set", "status": "refused", "name": args.name, "error": str(e)})
        return EXIT_ERROR
    except Exception as e:  # noqa: BLE001 - e.g. undecodable input; only the type name, never the text
        _emit({"command": "keys set", "status": "refused", "name": args.name,
               "error": f"could not read the value ({type(e).__name__}); nothing stored"})
        return EXIT_ERROR
    try:
        out = keys.write_key(cfg, args.name, value, check_shape=not args.skip_shape_check)
    except keys.KeyProblem as e:
        _emit({"command": "keys set", "status": "refused", "name": args.name, "error": str(e),
               "hint": "nothing stored; check the value and try again (use --skip-shape-check only if you are sure)"})
        return EXIT_ERROR
    finally:
        value = None  # noqa: F841 - drop the reference early
    _emit({"command": "keys set", "status": "ok", **out})
    return EXIT_OK


def cmd_consent(args: argparse.Namespace, cfg: Any) -> int:
    from . import consent
    if args.consent_action == "show":
        data, problem = consent.load(cfg)
        _emit({"command": "consent show", "path": str(consent.consent_path(cfg)), "problem": problem,
               "topics": {t: consent.get(cfg, t) for t in consent.TOPICS},
               "history": data.get("history", []) if not problem else []})
        return EXIT_OK
    try:
        out = consent.record(cfg, args.topic, args.value, lang=getattr(args, "lang", "en") or "en")
    except (ValueError, OSError) as e:
        _emit({"command": "consent set", "status": "error", "error": str(e)})
        return EXIT_ERROR
    _emit({"command": "consent set", "status": "ok", **out})
    return EXIT_OK


COMMANDS: dict[str, Callable[[argparse.Namespace, Any], int]] = {
    "doctor": cmd_doctor,
    "keys": cmd_keys,
    "consent": cmd_consent,
}
