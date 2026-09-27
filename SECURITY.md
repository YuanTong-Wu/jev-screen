# Security policy

## Reporting a vulnerability or a leak

Please do **not** open a public issue for:

- a leaked secret (API key, token, User-Agent contact e-mail) anywhere in the repository or its history;
- personal data or a real third-party document that should not be in the repository;
- a way to make jev-screen send a key, a local file or database content to a host other than the official
  source it is talking to.

Use GitHub's private vulnerability reporting instead ("Security" tab -> "Report a vulnerability"). Describe where the
problem is without pasting the secret or the material itself. You should get an answer within a week.

## How jev-screen handles secrets

| Secret | Where it is read from (first hit wins) | Notes |
|---|---|---|
| OpenRouter key (Jev) | env `OPENROUTER_API_KEY`; the file named by `JEVSCREEN_OPENROUTER_KEY_FILE`; `<JEVSCREEN_HOME>/openrouter_api_key` | Read at call time only. A set `JEVSCREEN_OPENROUTER_KEY_FILE` is authoritative: if the file it names is missing, `<JEVSCREEN_HOME>/openrouter_api_key` is not tried. There is no built-in fallback path |
| EDINET Subscription-Key | env `JEVSCREEN_EDINET_API_KEY`; `<JEVSCREEN_HOME>/edinet_api_key` | Travels in the query string; every recorded URL, error and note shows `<edinet-api-key>` |
| OpenDART `crtfc_key` | env `JEVSCREEN_OPENDART_API_KEY`; `<JEVSCREEN_HOME>/opendart_api_key` | Same redaction, as `<opendart-api-key>` |
| SEC User-Agent (`Name email`) | env `JEVSCREEN_SEC_USER_AGENT`; `<JEVSCREEN_HOME>/sec_user_agent` | Sent to SEC only; redacted as `<sec-user-agent>` everywhere else |

`JEVSCREEN_HOME` defaults to `data/` in the checkout, which is git-ignored. Key files: one line, the key only,
`chmod 600`. Keys are never printed, logged, journaled, stored in snapshots or written into screen reports; the
test suite checks this with fake keys.

## Before you publish a fork or a pull request

Run the release scan; it must print `release_check: clean`:

    python3 tools/release_check.py

It fails on e-mail addresses, home-directory paths, API-key-looking strings, secret or data files, large or
binary files, undeclared test fixtures and third-party domains in fixtures. See
[docs/RELEASE_CHECKLIST.md](docs/RELEASE_CHECKLIST.md) and [DATA_LICENSES.md](DATA_LICENSES.md).

## Network behaviour

jev-screen talks only to the sources listed in `src/jevscreen/provenance.py` and to OpenRouter (Jev, paid, only in
`screen` without `--dry-run`). It keeps at least 1 s between requests per host by default and stops a whole run on
the first HTTP 403/429 or challenge page, without retrying.
