"""Paths and runtime settings. Everything is overridable by environment variables; no secrets live here."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

REPO_ROOT = Path(__file__).resolve().parents[2]

# Optional FinanceDatabase-backed DuckDB (read-only). Default: <home>/financedatabase.duckdb; override with
# JEVSCREEN_FD_DUCKDB or `import-fd --fd-duckdb PATH`. No machine-specific path is hard-coded.
FD_DUCKDB_FILENAME = "financedatabase.duckdb"
# Jev API keys (one per provider; any one of them is enough). Per provider: the environment variable holding the key,
# the variable naming a key file, the key file name inside <home> (data/ is git-ignored) and the file in which
# `jevscreen keys set <name> --from-file PATH` records PATH (a location, never the key). The recorded location is read
# at every call, so a worker that is already running finds a key recorded after it started.
@dataclass(frozen=True)
class JevKeySource:
    name: str             # keys.KEYS name and jev.PROVIDERS name
    label: str            # shown to humans
    env: str              # the key itself
    file_env: str         # names a key file (read instead of <home>/<filename>)
    filename: str         # <home>/<filename>, written by `jevscreen keys set <name>`
    location: str         # <home>/<location> holds the path recorded by `keys set <name> --from-file`


JEV_KEY_SOURCES: dict[str, JevKeySource] = {
    "typesafe": JevKeySource("typesafe", "TypeSafe", "TYPESAFE_API_KEY", "JEVSCREEN_TYPESAFE_KEY_FILE",
                             "typesafe_api_key", "typesafe_key_location"),
    "openrouter": JevKeySource("openrouter", "OpenRouter", "OPENROUTER_API_KEY", "JEVSCREEN_OPENROUTER_KEY_FILE",
                               "openrouter_api_key", "openrouter_key_location"),
    "vercel": JevKeySource("vercel", "Vercel AI Gateway", "AI_GATEWAY_API_KEY", "JEVSCREEN_VERCEL_KEY_FILE",
                           "vercel_api_key", "vercel_key_location"),
}
OPENROUTER_KEY_FILENAME = JEV_KEY_SOURCES["openrouter"].filename
OPENROUTER_KEY_LOCATION = JEV_KEY_SOURCES["openrouter"].location


@dataclass(frozen=True)
class Config:
    home: Path = field(default_factory=lambda: Path(os.environ.get("JEVSCREEN_HOME", REPO_ROOT / "data")))
    # None -> <home>/financedatabase.duckdb (resolved in __post_init__ so an explicit `home` is honoured).
    fd_duckdb: Path | None = field(default_factory=lambda: (
        Path(os.environ["JEVSCREEN_FD_DUCKDB"]) if os.environ.get("JEVSCREEN_FD_DUCKDB") else None))
    user_agent: str = field(default_factory=lambda: os.environ.get(
        "JEVSCREEN_USER_AGENT", "Mozilla/5.0 (compatible; jev-screen/0.1.1; personal research)"))
    # Politeness: minimum seconds between requests to the same host.
    min_interval_s: float = field(default_factory=lambda: float(os.environ.get("JEVSCREEN_MIN_INTERVAL_S", "1.0")))
    timeout_s: float = 60.0

    def __post_init__(self) -> None:
        if self.fd_duckdb is None:
            object.__setattr__(self, "fd_duckdb", Path(self.home) / FD_DUCKDB_FILENAME)

    def sec_user_agent(self) -> str | None:
        """SEC fair-access User-Agent ('<name> <email>'). Env JEVSCREEN_SEC_USER_AGENT, else <home>/sec_user_agent.

        Kept outside git (data/ is ignored) and never logged.
        """
        env = os.environ.get("JEVSCREEN_SEC_USER_AGENT")
        if env:
            return env.strip()
        f = self.home / "sec_user_agent"
        return f.read_text().strip() if f.exists() else None

    # -- Jev API keys (typesafe | openrouter | vercel). Read at call time only; never print or log a value.
    def jev_key(self, name: str) -> str | None:
        """The key of Jev provider `name`: its environment variable if set; else, if its *_KEY_FILE variable is set,
        that file and nothing else (a named but missing file is not skipped); else the file recorded with
        `keys set <name> --from-file`; else <home>/<name>_api_key (data/ is git-ignored). No machine-specific
        fallback path."""
        src = JEV_KEY_SOURCES[name]
        kind, path = self.jev_key_source(name)
        if kind == "env":
            return os.environ[src.env].strip()
        return path.read_text().strip() if path is not None and path.exists() else None

    def jev_key_source(self, name: str) -> tuple[str, Path | None]:
        """Where jev_key(name) reads: ("env", None), ("named-file", path), ("recorded-file", path) or
        ("home-file", path). No key value. Read at call time (a running worker sees a later `keys set`)."""
        src = JEV_KEY_SOURCES[name]
        if os.environ.get(src.env):
            return "env", None
        named = os.environ.get(src.file_env)
        if named:
            return "named-file", Path(named)
        recorded = self.jev_key_location(name)
        if recorded is not None:
            return "recorded-file", recorded
        return "home-file", self.home / src.filename

    def jev_key_location(self, name: str) -> Path | None:
        """The key file recorded by `keys set <name> --from-file PATH` (<home>/<name>_key_location holds the path
        only), or None."""
        try:
            text = (self.home / JEV_KEY_SOURCES[name].location).read_text(encoding="utf-8").strip()
        except (OSError, UnicodeDecodeError):
            return None
        return Path(text) if text else None

    def jev_key_hint(self, name: str) -> str:
        """A 'key missing' message naming the exact place that was checked (never the key itself)."""
        src = JEV_KEY_SOURCES[name]
        kind, path = self.jev_key_source(name)
        if kind == "named-file":
            return (f"{src.label} key missing: {src.file_env} names {path}, which does not exist or is empty "
                    f"({self.home / src.filename} is not checked while it is set). Fix that file, unset the "
                    f"variable, or set {src.env}.")
        if kind == "recorded-file":
            return (f"{src.label} key missing: the key file recorded with `jevscreen keys set {name} --from-file` "
                    f"does not exist or is empty. Record it again, or run `jevscreen keys set {name}`.")
        return (f"{src.label} key missing: set {src.env}, or put the key (one line, chmod 600) in {path} "
                f"(data/{src.filename}, git-ignored), or point {src.file_env} at a key file.")

    def jev_key_files(self, name: str) -> list[Path]:
        """The key file jev_key(name) reads when its environment variable is not set (for `jevscreen keys` /
        doctor): the file named by its *_KEY_FILE variable when set (and nothing else), else the recorded file, else
        <home>/<name>_api_key (written by `jevscreen keys set <name>`)."""
        src = JEV_KEY_SOURCES[name]
        named = os.environ.get(src.file_env)
        if named:
            return [Path(named)]
        recorded = self.jev_key_location(name)
        return [recorded] if recorded is not None else [self.home / src.filename]

    def jev_keys(self) -> list[str | None]:
        """Every configured Jev key value (for redaction and secret scans only)."""
        out = []
        for name in JEV_KEY_SOURCES:
            try:
                out.append(self.jev_key(name))
            except OSError:
                out.append(None)
        return out

    # OpenRouter shorthands (kept: the plain names read better where only OpenRouter is meant)
    def openrouter_key(self) -> str | None:
        return self.jev_key("openrouter")

    def openrouter_key_source(self) -> tuple[str, Path | None]:
        return self.jev_key_source("openrouter")

    def openrouter_key_location(self) -> Path | None:
        return self.jev_key_location("openrouter")

    def openrouter_key_hint(self) -> str:
        return self.jev_key_hint("openrouter")

    def openrouter_key_files(self) -> list[Path]:
        return self.jev_key_files("openrouter")

    def _local_secret(self, env: str, filename: str) -> str | None:
        value = os.environ.get(env)
        if value:
            return value.strip()
        f = self.home / filename
        return f.read_text().strip() if f.exists() else None

    def edinet_api_key(self) -> str | None:
        """EDINET API v2 Subscription-Key: env JEVSCREEN_EDINET_API_KEY or data/edinet_api_key (git-ignored)."""
        return self._local_secret("JEVSCREEN_EDINET_API_KEY", "edinet_api_key")

    def opendart_api_key(self) -> str | None:
        """OpenDART crtfc_key: env JEVSCREEN_OPENDART_API_KEY or data/opendart_api_key (git-ignored)."""
        return self._local_secret("JEVSCREEN_OPENDART_API_KEY", "opendart_api_key")

    @property
    def db_path(self) -> Path:
        return self.home / "jevscreen.duckdb"

    @property
    def raw_dir(self) -> Path:
        return self.home / "raw"

    def ensure(self) -> "Config":
        self.home.mkdir(parents=True, exist_ok=True)
        self.raw_dir.mkdir(parents=True, exist_ok=True)
        return self


def load() -> Config:
    return Config().ensure()


SECRET_PLACEHOLDER = "<sec-user-agent>"


def secret_variants(secret: str | None) -> list[str]:
    """The secret as it can appear in recorded text: verbatim and JSON-escaped (ensure_ascii on/off), longest first.

    json.dumps(..., ensure_ascii=True) writes 'Müller' as 'M\\u00fcller', which a plain replace would miss."""
    if not secret:
        return []
    out = {secret, json.dumps(secret)[1:-1], json.dumps(secret, ensure_ascii=False)[1:-1]}
    return sorted((v for v in out if v), key=len, reverse=True)


def redact(text: Any, secrets: Iterable[str | None], placeholder: str = SECRET_PLACEHOLDER) -> Any:
    """Replace every occurrence (incl. JSON-escaped forms) of each secret by `placeholder`. None stays None.

    Redact BEFORE truncating: a cut through the secret would leave a partial copy that no longer matches."""
    if text is None:
        return None
    text = str(text)
    for secret in secrets:
        for v in secret_variants(secret):
            text = text.replace(v, placeholder)
    return text
