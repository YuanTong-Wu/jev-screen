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
# OpenRouter key file name inside <home> (data/ is git-ignored). Override the path with JEVSCREEN_OPENROUTER_KEY_FILE.
OPENROUTER_KEY_FILENAME = "openrouter_api_key"
# `jevscreen keys set openrouter --from-file PATH` records PATH here (a location, never the key). It is read at every
# call, so a worker that is already running finds a key recorded after it started.
OPENROUTER_KEY_LOCATION = "openrouter_key_location"


@dataclass(frozen=True)
class Config:
    home: Path = field(default_factory=lambda: Path(os.environ.get("JEVSCREEN_HOME", REPO_ROOT / "data")))
    # None -> <home>/financedatabase.duckdb (resolved in __post_init__ so an explicit `home` is honoured).
    fd_duckdb: Path | None = field(default_factory=lambda: (
        Path(os.environ["JEVSCREEN_FD_DUCKDB"]) if os.environ.get("JEVSCREEN_FD_DUCKDB") else None))
    user_agent: str = field(default_factory=lambda: os.environ.get(
        "JEVSCREEN_USER_AGENT", "Mozilla/5.0 (compatible; jev-screen/0.0.1; personal research)"))
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

    def openrouter_key(self) -> str | None:
        """OpenRouter key for Jev: env OPENROUTER_API_KEY if set; else, if JEVSCREEN_OPENROUTER_KEY_FILE is set, that
        file and nothing else (a named but missing file is not skipped); else <home>/openrouter_api_key (data/ is
        git-ignored). Read at call time only; never print or log it. There is no machine-specific fallback path."""
        kind, path = self.openrouter_key_source()
        if kind == "env":
            return os.environ["OPENROUTER_API_KEY"].strip()
        return path.read_text().strip() if path is not None and path.exists() else None

    def openrouter_key_source(self) -> tuple[str, Path | None]:
        """Where openrouter_key() reads: ("env", None), ("named-file", path), ("recorded-file", path) or
        ("home-file", path). No key value. Read at call time (a running worker sees a later `keys set`)."""
        if os.environ.get("OPENROUTER_API_KEY"):
            return "env", None
        named = os.environ.get("JEVSCREEN_OPENROUTER_KEY_FILE")
        if named:
            return "named-file", Path(named)
        recorded = self.openrouter_key_location()
        if recorded is not None:
            return "recorded-file", recorded
        return "home-file", self.home / OPENROUTER_KEY_FILENAME

    def openrouter_key_location(self) -> Path | None:
        """The key file recorded by `keys set openrouter --from-file PATH` (<home>/openrouter_key_location holds
        the path only), or None."""
        try:
            text = (self.home / OPENROUTER_KEY_LOCATION).read_text(encoding="utf-8").strip()
        except (OSError, UnicodeDecodeError):
            return None
        return Path(text) if text else None

    def openrouter_key_hint(self) -> str:
        """A 'key missing' message naming the exact place that was checked (never the key itself)."""
        kind, path = self.openrouter_key_source()
        if kind == "named-file":
            return (f"OpenRouter key missing: JEVSCREEN_OPENROUTER_KEY_FILE names {path}, which does not exist or is "
                    f"empty ({self.home / OPENROUTER_KEY_FILENAME} is not checked while it is set). Fix that file, "
                    "unset the variable, or set OPENROUTER_API_KEY.")
        if kind == "recorded-file":
            return ("OpenRouter key missing: the key file recorded with `jevscreen keys set openrouter --from-file` "
                    "does not exist or is empty. Record it again, or run `jevscreen keys set openrouter`.")
        return (f"OpenRouter key missing: set OPENROUTER_API_KEY, or put the key (one line, chmod 600) in {path} "
                f"(data/{OPENROUTER_KEY_FILENAME}, git-ignored), or point JEVSCREEN_OPENROUTER_KEY_FILE at a key file.")

    def openrouter_key_files(self) -> list[Path]:
        """The key file openrouter_key() reads when OPENROUTER_API_KEY is not set (for `jevscreen keys` / doctor):
        the file named by JEVSCREEN_OPENROUTER_KEY_FILE when set (and nothing else), else <home>/openrouter_api_key
        (written by `jevscreen keys set openrouter`)."""
        named = os.environ.get("JEVSCREEN_OPENROUTER_KEY_FILE")
        if named:
            return [Path(named)]
        recorded = self.openrouter_key_location()
        return [recorded] if recorded is not None else [self.home / OPENROUTER_KEY_FILENAME]

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
