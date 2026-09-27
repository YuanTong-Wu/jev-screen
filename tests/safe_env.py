"""Suite-wide network kill switch for the unit tests (imported by the shared fixtures, so it covers the whole suite).

`jevscreen screen` defaults to --fetch-docs auto: a test that screens a profile-only company would otherwise start a
real `jevscreen.ondemand child` (CNINFO, SEC, ...). JEVSCREEN_TESTING=1 makes ondemand.launch() and the child refuse the
real adapters unless a test maps them to fakes (JEVSCREEN_ONDEMAND_MODULES). The developer's key variables are dropped
too, so a key exported in the shell can never switch on a real source during a test run."""
from __future__ import annotations

import os
from typing import MutableMapping

TESTING = {"JEVSCREEN_TESTING": "1"}
KEY_ENVS = ("OPENROUTER_API_KEY", "JEVSCREEN_OPENROUTER_KEY_FILE", "JEVSCREEN_SEC_USER_AGENT",
            "JEVSCREEN_EDINET_API_KEY", "JEVSCREEN_OPENDART_API_KEY", "JEVSCREEN_ONDEMAND_MODULES")


def apply(env: MutableMapping[str, str] = os.environ) -> None:
    env.update(TESTING)
    for k in KEY_ENVS:
        env.pop(k, None)


apply()
