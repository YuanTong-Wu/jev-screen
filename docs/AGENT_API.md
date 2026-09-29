# Agent API: JSON output of doctor, keys, consent and quickstart

These commands are made for AI agents that install and operate jev-screen for a human (see
[AGENTS.md](../AGENTS.md)). Each prints one JSON object on stdout (pretty-printed, keys sorted); human hints go to
stderr. No output ever contains a key value or the SEC name/email.

Types below: `str?` means string or `null`. Unknown extra fields may be added later; ignore them.

## `jevscreen doctor [--json] [--check-jev]`

Read-only: it opens the store read-only for a few seconds and never writes or creates anything (not even the data
folder or its `raw/` subfolder). Dependencies are detected without importing them. Without `--check-jev`
it sends no request. `--check-jev` sends one free request to the active Jev provider (no model call, nothing is
charged): OpenRouter `GET https://openrouter.ai/api/v1/key` (key validity and what is left of the key's own spending
limit; the account balance is not in that answer and is not checked), Vercel AI Gateway
`GET https://ai-gateway.vercel.sh/v1/credits` (key validity and the team's credit balance), TypeSafe
`GET https://api.typesafe.ai/v1/models` (key validity and whether `jev-1.13.0` is listed; TypeSafe publishes no
balance). Key presence is checked with `stat` only; key files are not read.

Every `fix_command` and `next_command` is a complete command, safe to run exactly as written: no placeholders, no
shell operators, and never a consent write. (The one angle-bracket text, `"<idea in plain words>"` in the
first-screen suggestion, is a quoted argument: replace it with the human's idea; run as-is it is a harmless dry run.)

Exit code: `0` when `ok` is true (no check failed), else `1`. Without `--json` it prints the same report as text.

```jsonc
{
  "command": "doctor",
  "ok": true,                         // no check has status "fail"
  "ready_for": "screen",              // "screen" when ok, else null
  "next_command": "jevscreen screen \"<idea in plain words>\" --min-mcap 1e9 --budget 1 --dry-run",
                                      // fix_command of the first failed check that has one, else this
                                      // first-screen dry run; null when the next step is the human's decision,
                                      // and always null after the human declined gray-private sources
  "ask_human": ["key_sec_email", "consent_gray_sources"],
                                      // ids of warn/fail checks whose fix needs the human's decision
  "summary": {"ok": 11, "warn": 2, "fail": 0, "skip": 3},
  "first_run_defaults": {"min_mcap_usd": 1e9, "budget_usd": 1.0, "dry_run_first": true},
                                      // a convention for agents, not the CLI defaults (see below)
  "checks": [
    {"id": "python", "status": "ok", "detail": "Python 3.12.0", "fix_command": null, "ask_human": false}
    // ... one object per check, in the order below
  ]
}
```

### Check object

| Field | Type | Meaning |
|---|---|---|
| `id` | str | stable identifier (table below) |
| `status` | str | `ok` \| `warn` \| `fail` \| `skip` |
| `detail` | str | one plain-English sentence for the human |
| `fix_command` | str? | the command that fixes it (or advances it); `null` when there is none |
| `ask_human` | bool | true: the fix needs the human's decision or action first (keys, consent, spending, cooldown override) |
| `human_question` | str | only on checks that need a yes/no from the human (gray-sources consent unset or unreadable): the exact question to ask (AGENTS.md step 5) |
| `record_answer_commands` | [str] | with `human_question`: `["jevscreen consent set gray-sources yes", "jevscreen consent set gray-sources no"]`; run only the one matching the human's own answer |
| extra fields | | per check, below |

`fail` blocks a real screen; `warn` does not; `skip` means not needed or not checked.

### Checks

| id | fail / warn when | extra fields |
|---|---|---|
| `python` | fail: Python < 3.10 | |
| `dep_duckdb` | fail: duckdb not importable | |
| `dep_pymupdf` | ok with PyMuPDF or pypdf (pypdf, from the `[pdf]` extra, is enough; PyMuPDF is optional and AGPL-3.0); warn: no PDF library (`fix_command` `python3 -m pip install pypdf`) | |
| `home` | fail: data folder missing, not a folder or not writable | |
| `store` | fail: no database (`jevscreen init`) or it cannot be opened; warn: locked by another jevscreen process (data checks skipped) | |
| `universe` | fail: schema incomplete or universe empty; warn: market data older than 7 days (with consent `no` the detail says the human declined; no fix) | `companies`, `market_as_of` (YYYY-MM-DD?), `age_days` (int?) |
| `descriptions` | warn: under 80% of companies >= $1B have a description | `companies`, `with_description` |
| `official_text` | warn: no company >= $1B has official annual-report text | `companies`, `with_official_text` |
| `key_typesafe`, `key_openrouter`, `key_vercel` | skip when not configured (one Jev key is enough; `jev_provider` fails when there is none); warn: its variable is blank / its first file is empty, or the key file is readable by other users | `configured`, `source` |
| `jev_provider` | fail: no Jev key at all (`ask_human`, `human_question` / `human_question_zh`: the one account question, `key_commands` {provider: `jevscreen keys set <provider> --dialog`}, `fix_command` null; the text output prints the question and the commands too), the chosen provider (`JEVSCREEN_JEV_PROVIDER` or `keys use`) has no key (`fix_command` `jevscreen keys set <provider>`) or an unknown provider | `provider` (str?), `label`, `shown_as` / `shown_as_zh` (the name to show: Vercel AI Gateway always carries "version cannot be pinned" / "版本无法锁定"), `model`, `pinned` (bool), `endpoint`, `reason` (`saved`\|`key`\|`explicit`\|`default`: `saved` = the last Jev key set or `keys use`), `configured` ([str]) |
| `key_sec_email` | warn: not configured, blank or empty (only `sync-sec` needs it) | `configured`, `source` |
| `key_edinet`, `key_opendart` | skip when not configured (off by default); warn on a blank variable, an empty file or loose file mode | `configured`, `source` |
| `consent_gray_sources` | warn: unset or unreadable (treated as no; `human_question` + `record_answer_commands`, `recipient_note` / `recipient_note_zh` to say right after the question, `answer_words` {yes: [...], no: [...]}: the clear replies); fail: recorded `no` (today the universe and descriptions are gray-private, so no screen) — `fix_command` is always null | `state` (`yes`\|`no`\|`unset`\|`unreadable`), `recorded_at` (str?) |
| `cooldown` | warn: a command is inside its 24 h cooldown after a provider block | `cooldowns` ({command: blocked_at ISO}) |
| `on_demand_docs` | warn: no PDF reader (`fix_command` `python3 -m pip install pypdf`), or no SEC contact email (`ask_human`, `human_question`, `record_answer_commands`, `fix_command` null); skip after a recorded `consent set sec-email-ask no`; ok otherwise | |
| `mops_annual` | skip: Taiwan annual-report downloads off (default; `human_question` only while unset, ask it only when a screen's `questions[]` has `mops_annual`); ok after a recorded yes. Never drives `next_command` | |
| `jev` | skip unless `--check-jev`; fail: no key, key rejected (401/403), or no credit (402: the human must add credit, `ask_human`); warn: unreachable, other HTTP status, or less than $1 left (OpenRouter: on the key's limit; Vercel: AI Gateway balance). `ok` does not prove an OpenRouter or TypeSafe account has credit | `provider`, `http_status`; OpenRouter: `limit_remaining` (number?), `usage_usd`, `is_free_tier`; Vercel: `balance_usd` (number?); TypeSafe: `model_listed` (bool) |

`source` of a key: `env` (environment variable), `file` (under the data folder, or for a Jev key the file named by
its `JEVSCREEN_*_KEY_FILE` variable or recorded with `--from-file`), `none`. Resolution mirrors the adapters: a
non-empty variable wins, else the key's file, even when it is empty (then `configured` is false). For a Jev key, a set
`JEVSCREEN_TYPESAFE_KEY_FILE` / `JEVSCREEN_OPENROUTER_KEY_FILE` / `JEVSCREEN_VERCEL_KEY_FILE` is the only file read:
when it names a missing file, `data/<name>_api_key` is not tried.

The active Jev provider (`jev.resolve_provider`): `JEVSCREEN_JEV_PROVIDER` (`typesafe` | `openrouter` | `vercel`)
when set, even when that provider has no key; else the first of typesafe, openrouter, vercel with a configured key;
else openrouter (and `jev_provider` fails: no key).

The universe fix (empty or stale universe) depends on the recorded gray-sources answer, because today the universe
comes from a gray-private source:

| consent | universe `fix_command` | `ask_human` | also |
|---|---|---|---|
| `yes` | `jevscreen refresh-universe` | false | |
| unset / unreadable | null | true | `human_question`, `record_answer_commands` |
| `no` | null | true | detail: the human declined, wait for the open data pack; `next_command` is null |

`first_run_defaults` ($1 budget, $1B market cap, dry run first) is a convention, not the CLI defaults:
`jevscreen screen` on its own uses `--budget 3` and `--min-mcap 2e8` ($3, $200M). Agents must always pass
`--budget` and `--min-mcap` explicitly.

## `jevscreen keys ...`

Names: `typesafe`, `openrouter`, `vercel` (the Jev keys: Jev via TypeSafe's official API, OpenRouter or Vercel AI
Gateway, same price; one is enough), `sec-email`, `edinet`, `opendart`.

| Name | File under the data folder | Environment variable (wins over the file) | Shape check |
|---|---|---|---|
| `typesafe` | `typesafe_api_key` | `TYPESAFE_API_KEY` | >= 16 printable ASCII chars, no spaces; an OpenRouter (`sk-or-`) or Vercel (`vck_`) key is refused with a pointer to the right name |
| `openrouter` | `openrouter_api_key` | `OPENROUTER_API_KEY` | starts with `sk-or-`, >= 20 chars, no spaces |
| `vercel` | `vercel_api_key` | `AI_GATEWAY_API_KEY` | >= 16 printable ASCII chars, no spaces (usually `vck_...`); an OpenRouter key is refused |
| `sec-email` | `sec_user_agent` | `JEVSCREEN_SEC_USER_AGENT` | a name and an email, ASCII, <= 200 chars |
| `edinet` | `edinet_api_key` | `JEVSCREEN_EDINET_API_KEY` | 16-64 letters and digits |
| `opendart` | `opendart_api_key` | `JEVSCREEN_OPENDART_API_KEY` | exactly 40 letters and digits |

For a Jev key, its `JEVSCREEN_<NAME>_KEY_FILE` variable (`JEVSCREEN_TYPESAFE_KEY_FILE`,
`JEVSCREEN_OPENROUTER_KEY_FILE`, `JEVSCREEN_VERCEL_KEY_FILE`; when set) names the only file read: `data/<name>_api_key`
is not tried while it is set, even when the named file is missing. Next comes a file recorded with `keys set <name>
--from-file PATH` (below), then `data/<name>_api_key`. All of these are read at every call, so a quickstart
worker that is already running uses a key recorded (or typed) after it started; an environment variable set in the
agent's shell after the worker started never reaches it (use `--from-file` instead).

### `keys set typesafe|openrouter|vercel --from-file PATH`

For a human who saved the key in a file themselves. Only the location is recorded (`data/<name>_key_location`
holds the path); the key is never copied or printed, and neither is the path (`doctor` says "the recorded key
file"). The file is read inside the process only to check that it holds just the key (one line; for OpenRouter
starting with `sk-or-`): an `.env` line (`OPENROUTER_API_KEY=...`), quotes or notes around it are refused (exit 1)
with a plain message, since the file's text is sent as it is. `keys set` (prompt / dialog) and `keys clear` forget the recorded
location.

```jsonc
{"command": "keys set", "status": "ok", "via": "from-file", "name": "openrouter", "recorded": true,
 "source": "recorded-file", "configured": true, "mode_ok": true,
 "warning": "other users on this computer can read that file; chmod 600 it (the key stays where it is)"}  // only then
```

`keys check openrouter` then shows `"source": "recorded-file", "path": null`.

### `keys set NAME [--stdin] [--skip-shape-check]`

On a terminal: a hidden prompt (getpass). Not a terminal: refused unless `--stdin`, then one line is read from stdin.
Surrounding whitespace and one pair of quotes are stripped. The file is written atomically with mode `0600`.
`--skip-shape-check` stores a value that fails the shape check (never an empty one or one with control characters).

Exit `0` stored, `1` refused (nothing stored), `130` cancelled at the prompt.

```jsonc
{"command": "keys set", "status": "ok", "name": "openrouter", "path": ".../data/openrouter_api_key",
 "mode": "0600", "shape_ok": true,
 "warning": "environment variable OPENROUTER_API_KEY is set and takes precedence over this file"}  // only when set
{"command": "keys set", "status": "refused", "name": "openrouter",
 "error": "openrouter: not stored: expected an OpenRouter API key: starts with 'sk-or-', no spaces",
 "hint": "..."}
```

### `keys check [NAME]`

Exit `1` when a NAMED key is not configured or fails the shape check, else `0` (also `0` for the all-keys form).

```jsonc
{"command": "keys check", "keys": [
  {"name": "openrouter", "configured": true, "source": "file", "path": ".../data/openrouter_api_key",
   "mode_ok": true, "shape_ok": true, "problem": null, "env": "OPENROUTER_API_KEY",
   "used_by": "screen", "default_enabled": true}
]}
```

`mode_ok` is null for `env` and unconfigured keys. `problem` is a short reason that never quotes the value.

### `keys clear NAME`

```jsonc
{"command": "keys clear", "status": "ok", "name": "edinet", "path": ".../data/edinet_api_key", "removed": true}
```

`removed` is false when there was no file. A `warning` says so when another source (the environment variable, or
for a Jev key a named file) still provides the key. `keys set` likewise warns when the variable, or a set
`JEVSCREEN_<NAME>_KEY_FILE`, is read instead of the file it just wrote.

### Which Jev provider: `keys set` of a Jev key, `keys use PROVIDER`, `keys clear`

Every successful `keys set typesafe|openrouter|vercel` (prompt, `--dialog`, `--stdin` or `--from-file`) saves that
provider as the human's choice (`data/jev_provider`): the last Jev key set is the one used. `keys use` switches
without a new key (no key is read or written); `keys clear` of the chosen provider forgets the choice. Their JSON
adds `active_provider` and, when the active provider changed, `provider_switch`:

```jsonc
{"command": "keys use", "status": "ok", "name": "openrouter", "saved": true, "configured": true,
 "active_provider": "openrouter",
 "provider_switch": {"from": "typesafe", "to": "openrouter",
                     "paid_before": {"requests": 312, "usd": 0.84},   // ok requests through "from"; null: no store
                     "switch_back_command": "jevscreen keys use typesafe",
                     "notice_zh": "…", "notice_en": "…"}}             // tell the human in one line
```

`warning` says when the chosen provider has no key yet; `provider_warning` when `JEVSCREEN_JEV_PROVIDER` overrides
the choice.

## `jevscreen consent ...`

Topic: `gray-sources` (TradingView scanner and symbol pages, FinanceDatabase / Yahoo summaries: public but
terms-restricted, personal use only). Only record an answer the human gave.

### `consent set gray-sources yes|no`

```jsonc
{"command": "consent set", "status": "ok", "topic": "gray-sources", "value": "yes",
 "recorded_at": "2026-09-27T08:15:00Z", "via": "cli", "interactive": false,
 "statement": "I understand that gray-private sources ...", "path": ".../data/consent.json"}
```

`interactive` records whether stdin was a terminal (a human typing) or not (an agent running the command).
`--lang zh|en` (default en) names the language the question was asked in: the record keeps `statement` (the exact
text shown, from `consent.STATEMENTS`), `statement_en`, `statement_version` (currently 4) and `lang`. `consent show` reports
`statement_version` per topic (1 for an answer recorded before versions existed; quickstart asks once more whenever it is below the current version).

### `consent show`

```jsonc
{"command": "consent show", "path": ".../data/consent.json", "problem": null,
 "topics": {"gray-sources": {"topic": "gray-sources", "state": "yes", "recorded_at": "...", "problem": null}},
 "history": [{"topic": "gray-sources", "value": "yes", "recorded_at": "...", "via": "cli", "interactive": false}]}
```

### File `data/consent.json`

```jsonc
{
  "version": 1,
  "topics": {"gray-sources": {"value": "yes", "recorded_at": "...", "via": "cli", "interactive": false,
                              "statement": "..."}},
  "history": [ /* every answer ever recorded, oldest first */ ]
}
```

A missing file, a malformed one, or a value other than `yes`/`no` all mean **no**. `consent set` on a malformed
file moves it to `consent.json.bad-<UTC timestamp>` (earlier copies are kept) and starts a new record.

For code: `jevscreen.consent.require_gray_sources(cfg, source_id)` raises `ConsentRequired` unless the source is
official (per `provenance.SOURCES`) or the recorded answer is `yes`; unknown source ids need consent too. The CLI
calls it before any request or file read in `refresh-universe` (`tradingview_scanner`), `crawl-descriptions`
(`tradingview_profile`) and `import-fd` (`financedatabase_local`). Without a `yes` these commands exit 1 and print

```json
{"command": "refresh-universe", "status": "consent_required", "source_id": "tradingview_scanner", "consent": "unset",
 "hint": "ask the human (AGENTS.md step 5), then record the answer: jevscreen consent set gray-sources yes|no"}
```

(`consent` is the recorded state: `unset`, `no` or `unreadable`.)

## `jevscreen why TARGET ... [--run RUN_ID|OUT_DIR|latest] [--idea TEXT | --key HEX] [--checks] [--json]`

Free and read-only. Why each target is (not) in the run's result (`results[]`; the top level also carries `plain_zh`
/ `plain_en` and, for one target, `who_zh` / `who_en`: `who_zh` uses the official Chinese short name when the store
has one): `stop` (a stable id), `plain_zh` / `plain_en`,
`stages`, `facts` / `inferences` / `gaps`, and `changes[]` with complete commands (`argv`, `command`), `cost_usd`,
`seconds` and `ask_human`. With the sections, a listed company is `in_output` ("in the confirmed list at #n", with
`main_via` agent / user named) or `to_confirm` ("in the To confirm section, not in the confirmed list", with the
reasons in `plain_<lang>` and the rank stage's `data.reasons`). Full schema and the stop table: [WHY.md](WHY.md). Exit 0 explained (also
`partial_files_only`), 1 unknown run / `choose_run` / `no_runs` / a target `not_found` or `ambiguous` (with
`candidates` and `agent_hint_en`), 3 unreadable run files.

## `jevscreen sieve new|set|add|remove|pin|unpin|list|check ... [--run RUN_ID | --key HEX | --idea TEXT] --json`

The sieve author commands (free, no Jev). Fields, rules and every JSON shape: [SIEVE.md](SIEVE.md); the draft schema:
[sieve.schema.json](sieve.schema.json). Writes print `{command, ok, saved, version, diff, errors, warnings, idea_en,
resolved, next_command}`; errors are `{code, text_zh, text_en}` (codes: `pins_refused`, `rules_refused`,
`unknown_field`, `tool_field`, `schema`, `unresolved` with `candidates`, `idea_en_names_company`); `status:
"sieve_stale"` means re-read and apply again. `sieve pin` / `unpin` always carry `ask_human: true`.

## `jevscreen screen ... --shells drop|keep | --idea-of RUN_ID | --idea-en TEXT | --no-page`

`--shells` (default `drop`) is the shells filter before L1 ([SHELLS.md](SHELLS.md)); `--idea-of RUN_ID` screens that
run's idea afresh (the idea argument may be left out, as with `--from-run`). `--idea-en TEXT` is the English sentence
every Jev question carries, used instead of the local translation and before a sieve's `idea_en` (`keywords.status`
and `params.idea_en_source` `agent`; checked for company names); a `--from-run` of such a run inherits it and refuses
a different one. `--no-page` skips `page.html` (written after the cards otherwise). Every run writes `funnel.jsonl.gz`;
`results.json` gains `shells`, `st_warning` (when a top-20 row is ST / *ST), `gaps.shells_dropped` and
`calibration.extras`; rows gain `flags`.

**The main list and the to-confirm section (default; owner decision 2026-09-29: never padded to 10).** Every screen
(and every `--from-run` / free new version of one) tiers its listed rows: `shortlist_tier` `high` = the main list
(L1 core + L2 explicit after the `--l2-constraints` check when on; with `--judge`, tier A; plus your AI's explicit
yes with 1-3 quote_ids, `main_via: "agent"`, and the human's yes pins, `main_via: "user"`), `confirm` = the
to-confirm section (the rest, in `shortlist.confirm_order`; a main row your AI said no to while it waits for the
human: `main_via: "agent_no"`, last). The main list comes first and is numbered 1..n; the to-confirm rows follow and
never fill it. A row its page would mark borderline (`shown_gap`: `no_mention`, the excerpt shown does not mention
the idea, or `edge`, split reads) is to confirm too, with that reason, unless the human said yes (`shortlist.info.gap`
counts them). A row a scope answer moved down (`scope_demoted`) is always to confirm, whatever your AI said; only
the human's yes moves it back. `results.json` carries `shortlist` `{high, confirm, rule, agent?, agent_no?, user?,
by?}` (counted over every row, a user pin listed below the cut too) and
`params.list` `main`; `results.csv` gains `section` (`main` / `to_confirm`), `shortlist_tier`, `main_via`,
`rank_before_shortlist`; `report.md` has "Main list (confirmed)" and "To confirm (not in the confirmed list)"; the console
table a `list` column. `--no-shortlist` keeps the old single padded list (`params.list` `padded`, no tiers, no
sections). A result written before the sections renders as it was; a new version of it gets the sections.

## On-demand annual reports (`screen` phase 2, `jevscreen fetch-docs`)

After a paid screen (status `ok` / `partial`) writes its report, `screen` (default `--fetch-docs auto`; off with
`--from-run` / `--dry-run` unless given) fetches the newest annual report of every layer-2 company that read only a
profile, from the official source of its market (US SEC, China CNINFO, India BSE, Taiwan MOPS after a recorded yes,
Korea DART with an OpenDART key), free and within `--fetch-time` (default 120 s). When at least one arrived, an update
pass (`screen --from-run` of the same run: L1 $0, at most min($0.05, budget left)) replaces `report.md`,
`results.json`, `results.csv` and `l2_inputs.jsonl` in the same folder; the earlier `results.json` is kept as
`results.v1.json` (`v2`, ... on later updates). The exit code of `screen` is the first report's; a fetch problem never
changes it. When nothing arrived, `results.json` and `report.md` only gain `layers.fetch` and
`gaps.l2_doc_unavailable`. A report stored by an earlier fetch whose update was skipped (no budget, Jev down,
`--no-update`, store busy) counts as `stored_since`: the next `fetch-docs latest` runs the update for it even when it
fetches nothing new. Until then a console line and `layers.fetch.next_command` say so, the summary counts those
companies apart ("stored but the results are not updated yet"), and their gap rows read `update_pending`. The update
run records the user's own `budget_usd` in its params (its cap is `update_budget_usd`), so a later `--from-run` of it
inherits the original budget.

### `results.json` fields (only when a fetch ran)

```jsonc
"layers": {"fetch": {
  "mode": "auto",                    // auto (screen) | fetch-docs | topn (screen / eval run --fetch-profile-only-topn,
                                     //   --deepen-official-topn; then also "topn": {"topn", "deepen", "selected",
                                     //   "order", "deep", "deepen_ready", "deepened", "deepen_outcomes", "stopped"})
  "status": "ok",                    // ok | partial (a source stopped / companies deferred) | interrupted | skipped
  "skip_reason": null,               // store_busy | ... when status is skipped
  "base_run": "scr-...", "time_budget_s": 120, "seconds": 71.4,
  "planned": {"sec": 21, "bse": 9},  // companies handed to each source
  "fetched": {"sec": 20, "bse": 7},  // companies that now have readable annual-report text
  "by_reason": {"fetched": 27, "no_adapter": 16, "deferred_time": 4},   // every profile-only L2 input, REASONS codes
  "sources": {"sec": {"status": "ok", "stopped_reason": null, "requests": 43, "seconds": 26.1, "s_per_company": 1.2,
                      "mb": 30.5, "agent_action": "none"}},
  "abandoned": [],                   // sources killed after the grace period (committed rows stay)
  "readiness": {"pdf_reader": true, "sec_email": true, "opendart": false, "mops_consent": null},
  "questions": [{"id": "sec_email", "human_question_zh": "...", "human_question_en": "...",
                 "record_answer_commands": ["jevscreen keys set sec-email", "jevscreen consent set sec-email-ask no"],
                 "then_command": "jevscreen fetch-docs scr-...",   // run after a yes (key / consent recorded)
                 "names": ["NYSE:GEV", "NYSE:CAT", "NYSE:GNRC"], "thin": ["NYSE:GEV"],
                 "ask_human": true}],     // sec_email: not asked again after a recorded no (consent sec-email-ask);
                                          // the question names up to 3 US companies it helps, thin-profile ones
                                          // first ("thin": they cannot be listed without the report)
  "summary_zh": "62 家读了官方年报（其中 27 家本次新抓）；20 家只读了简介：16 家市场暂无来源，4 家时间到",
  "summary_en": "62 read official annual reports (27 fetched now); 20 read the profile only: ...",
  "next_command": null,              // "jevscreen fetch-docs latest" when stored reports are not read yet
  "update": {"run_id": "scr-...", "status": "ok", "cost_usd": 0.003, "report_path": ".../report.md", "written": true},
            // or {"skipped": "nothing_fetched" | "no_update" | "no_budget" | "sieve_changed" | "not_written" | ...}
  "thin_waiting": [{"security_id": "NYSE:GEV", "name": "GE Vernova", "country": "United States",
                    "market_cap_usd": 2.5e11, "reason": "no_key_sec"}]
            // the L1 misses read only because their profile is too thin (screen.l1_rescued) whose annual report did
            // not come in: they cannot be listed on the profile, so the page's gap section names them (largest 5)
}},
"gaps": {"l2_doc_unavailable": [{"security_id": "IDX:XXXX", "name": "...", "country": "Indonesia", "exchange": "IDX",
                                 "market_cap_usd": 2.1e9, "source": null, "reason": "no_adapter", "note": null}]},
"funnel": {"l2_doc_fetch_planned": 31, "l2_doc_fetched": 27, "l2_doc_deferred": 4, "l2_doc_unavailable": 20},
"supersedes": "scr-..."             // on the update run: the run whose files it replaced
```

Each row (`rows`, `unverified`, `results.csv` last column) and each `l2_inputs.jsonl` line of an updated run has
`doc_fetch`: `stored` (annual report already there), `fetched_now`, or the reason code it still reads a profile. Only
`questions[]` needs the human; everything else is for the report.

Reason codes (`by_reason`, `reason`, `doc_fetch`): `fetched`, `stored_since` (stored after the run read the profile;
the update pass reads it), `update_pending` (gap rows only: stored, the update did not run), `already_stored`
(stored before the run, yet no usable business text: a gap), `no_adapter` (no official source for
the market yet), `edinet_pack` (Japanese text comes from the open pack), `no_pdf_reader`, `no_key_sec`, `no_key_dart`,
`mops_off`, `disabled` (`--sources`), `not_mapped`, `no_filing`, `form_not_supported`, `extract_failed`,
`known_failure` (tried recently; not retried for 1-30 days, also 14 days after the adapter found the newest report
unchanged and unreadable; `--retry-failed` overrides), `deferred_time`,
`deferred_cap` (MOPS 8, DART 20, BSE 40 per run), `source_paused` (blocked < 24 h ago), `blocked` (blocked during
this run), `source_busy`, `store_busy`, `error`, `unresolved`; the top-N levers add `outside_topn` (a profile row
below the top N: not fetched), `no_key_edinet` (EDINET needs the human's key: ask, never read it) and
`stopped_after_block` (an earlier source refused access, so the sequential fetch stopped; nothing to do). zh/en text:
`jevscreen.ondemand.REASONS`.

The top-N levers (`screen` / `eval run --fetch-profile-only-topn N`, `--deepen-official-topn N`; both off by default)
fetch one source at a time (BSE, DART, CNINFO, EDINET, then MOPS with its consent and SEC with its email) and stop at
the first block. CNINFO / DART / EDINET texts they store are extracted deep: after the business section, bounded
blocks of the same filing headed 【营业收入构成】 / 【核心竞争力分析】 / 【管理层讨论与分析（节选）】 (CNINFO full report),
【経営者による財政状態、経営成績及びキャッシュ・フローの状況の分析】 / 【セグメント情報】 (EDINET), 【매출 및 수주상황】 /
【이사의 경영진단 및 분석의견】 (DART), at most 20,000 characters in all (`jevscreen.sources.deep_sections`). They are the
filing's own text (facts), never a summary; extractors `cninfo-v4`, `dart-web-v2`, `edinet-v3`. Each deep text is
its own documents row (section `business_deep`) next to the untouched shallow one; only the top-N update pass reads
it (its results.json `params.deep_view` lists those companies), so runs without the levers are unchanged. The update
pass keeps the run's `--read-offset`. `topn.deepen_outcomes` codes: `deepened` (a deep text that adds to the shallow
one is stored, now or by an earlier fetch), `unchanged` (the deep read found nothing to add), `not_stored`, the
source's skip reasons, `known_failure` (a deep re-read failed in the last 14 days), `deferred_cap`.

### `jevscreen fetch-docs [RUN_ID|latest] [--time 300] [--sources sec,cninfo,bse,mops,dart] [--retry-failed] [--no-update] [--budget 0.05] [--dry-run] [--json]`

The same fetch and update for an earlier run (the idea, parameters and sieve come from the run). If the run's sieve
file changed since, it fetches but does not update (`update.skipped: "sieve_changed"`). `--dry-run` prints the planned
companies per source, the estimate and the skip counts; no request. `--json`:

```jsonc
{"command": "fetch-docs", "run_id": "scr-...", "status": "ok", "exit_code": 0, "fetch": { /* layers.fetch */ },
 "update": {"run_id": "scr-...", "status": "ok", "cost_usd": 0.001, "report_path": "...", "written": true} /* or null */,
 "update_skipped": null, "summary_zh": "...", "summary_en": "...", "questions": [],
 "next_command": "jevscreen fetch-docs latest" /* companies deferred, or stored reports not read yet (the update
                                                  was skipped or failed); after a changed sieve the screen --from-run
                                                  command; else null */, "ask_human": false,
 "cards_path": ".../cards.md" /* rebuilt for the updated run when the folder had cards or a page, else null */,
 "console": ["..."]}
```

After a written update the folder's `page.html` (and the idea's stable page, when this is its newest run) is rewritten
with the new run and `deck_id`, and a finished quickstart job of the replaced run now reports the update (`quickstart
--status` shows the new `run_id`, `deck_id`, top rows and `fetch`; its cost adds the update's).

| exit | meaning |
|---|---|
| 0 | ok, also a partial fetch |
| 1 | bad arguments or unknown run |
| 2 | a source was blocked during this invocation (results and report still updated); hard rule 6 applies |
| 3 | store busy at planning |
| 4 | every planned source busy (another jevscreen process uses it), or Jev busy during the update |
| 5 | the update budget ran out |
| 6 | Jev unavailable during the update |
| 130 | interrupted twice (the report is unchanged) |

Consent topic `mops-annual` (`jevscreen consent set mops-annual yes|no`): Taiwan annual reports from MOPS /
doc.twse.com.tw, whose robots.txt discourages automated downloads; off unless the human says yes; at most 8 companies
per run, 1.5 s between requests.

## `jevscreen quickstart`

```
jevscreen quickstart "<idea>" [--idea-en TEXT] [--approve-budget USD] [--min-mcap 1e9] [--countries A,B]
        [--lang auto|zh|en] [--fd-file PATH] [--no-open] [--retry] [--new-run] [--fill-descriptions yes|no]
        [--facets JSON] [--json]
jevscreen judge --deck ADECK_ID (--file ANSWERS.json | --skip) [--json]         # $0, your AI's review
jevscreen decide "s1=no c2=yes keep=TICKER" --run RUN_ID [--via chat|page] [--json]   # $0, the human's answers
jevscreen quickstart --status [IDEA | --key K] [--wait S] [--json]
jevscreen page [RUN_ID|OUT_DIR|latest] [--lang zh|en] [--open] [--text] [--json] # $0, rebuilds page.html
jevscreen page RUN --export-strings FILE | --import-translations FILE [--json]   # $0, your translations
```

The front command returns in under 5 s and never sends a request itself. It reads or creates the job file
`<home>/quickstart/<idea_key>.json`, applies the flags, lists what is still open (`pending`) and, when there is work
it can do, starts a detached worker (`python -m jevscreen.quickstart --worker <idea_key>`, output in
`<home>/quickstart/<idea_key>.log`). The worker never prompts. `--status` prints the same JSON from the job file;
`--wait S` (at most 110, counted from the start of the call) blocks until the state or the pending items change, so
one call fits a 2-minute agent timeout. Neither the front nor `--status` waits on the database lock: while another
process writes, the money fields show the worker's last ledger reading. While a worker runs, a front command does
not start a second one: its flags are left in `<idea_key>.flags.json` for the worker to pick up. When a job waits on
nothing any more (the key was stored after the worker stopped at it, flags were left for a worker that has
exited, a queued idea whose turn came), `--status` starts the worker exactly as the front would; it never starts
anything that still needs an answer. A front for a second idea while another idea's worker runs writes that idea's
job file and flags and answers `store_busy` (queued) with a `poll_command`.

With `--json`, stdout is this one JSON object; progress goes to stderr. Every printed command (`next_command`,
`poll_command`, `human_command`) is shell-quoted (`shlex`), never contains a key, and never records consent or
approves money unless the agent already passed that flag.

Exit code = `exit_code` = the table in [AGENTS.md](../AGENTS.md#fast-path-jevscreen-quickstart-use-this-first):
0 `running` / `done` / `partial`, 1 `failed`, 2 `blocked`, 3 `store_busy`, 5 `budget_exhausted`, 6
`ai_unavailable`, 10 `needs_human`, 11 `needs_agent`, 12 `declined`. An argparse usage error exits 2 with no JSON.

```jsonc
{
  "command": "quickstart", "format": "jevscreen.quickstart/1",
  "status": "running",              // see above; agents trust this field
  "exit_code": 0,
  "idea": "…", "idea_key": "c895e776d4712fa0", "lang": "zh",
  "state": "running",               // job state: new | running | waiting | done | failed | interrupted | declined
  "phase": 4,                       // 1 check · 2 download · 3 prepare · 4 AI screen + annual reports · 5 page
  "progress": {"phase": 4, "done": 3100, "total": 7240, "text_zh": "[4/5] AI 初读 3,100/7,240 家简介…",
               "text_en": "[4/5] AI first read 3,100/7,240 profiles…", "heartbeat_at": "2026-09-27T10:02:11Z"},
  "intro_zh": "…", "intro_en": "…",                    // with needs_human / needs_agent: what happens next (before
                                                        // the first result) and what is still needed, e.g. only a key
  "before_you_start_zh": "…", "before_you_start_en": "…",
  "pending": [                                          // agent item first, then the human items
    {"id": "idea_en", "ask_agent": true, "instructions_en": "…", "rerun_with": "--idea-en '<sentence>'",
     "problems": [],               // why the last sentence was refused (it names a company or ticker)
     "refused_idea_en": null, "suggested_idea_en": null,   // the refused sentence; a rewrite without the names
     "rerun_command": "jevscreen quickstart '…' --idea-en '<sentence>' --approve-budget 1"},   // complete command
    {"id": "consent_gray_sources", "ask_human": true, "question_zh": "…", "question_en": "…",
     "statement_version": 4,
     "note_zh": "…", "note_en": "…",   // say right after the question: who receives the text sent to Jev
                                       // (the active provider, and the company in between if any)
     "answer_words": {"yes": ["yes", "ok", "可以", "同意", "好", …], "no": ["no", "不要", "不同意", "不行", …]},
     "record_answer_commands": ["jevscreen consent set gray-sources yes --lang zh",
                                "jevscreen consent set gray-sources no --lang zh"]},
    // before the English sentence exists (idea_en pending) approve_budget is already listed, with "idea_en": null,
    // "waits_for": "idea_en" and a question without the sentence: write idea_en first, then ask the item as it
    // comes back (with the sentence) in the same one human round
    {"id": "approve_budget", "ask_human": true, "kind": "first",   // first | over | topup | uncertain
     "question_zh": "…", "question_en": "…", "idea_en": "…", "estimate_usd": null, "reserved_usd": null,
     "remaining_usd": null, "rerun_with": "--approve-budget 1",
     "alternatives": [{"args": {"countries": ["CN", "HK"]}, "flags": "--countries CN,HK", "label_zh": "…",
                       "label_en": "…", "estimate_usd": 0.12, "reserved_usd": 0.15, "companies": 3000}]},
    {"id": "reprice_idea_en", "ask_human": true, "old_idea_en": "…", "new_idea_en": "…", "question_zh": "…",
     "question_en": "…", "approve_usd": 1, "rerun_with": "--idea-en '…' --approve-budget 1" /* a yes */,
     "decline_with": "--idea-en '<the old sentence>'" /* a no: keeps the old English, drops the question */,
     "old_refused": false},        // true: the old sentence was refused; decline_with is null and a no means the
                                   // agent writes another sentence (on_no_en)
    {"id": "key_jev", "human_action": true, "rejected": false,
     "provider": null,              // no Jev key yet: the one account question (TypeSafe / OpenRouter / Vercel)
     "question_zh": "…", "question_en": "…", "text_zh": "…", "text_en": "…",   // text = question + terminal fallback
     "choices": [{"provider": "typesafe", "label": "TypeSafe (official API)", "label_zh": "TypeSafe 官方",
                  "signup_url": "https://console.typesafe.ai", "key_url": "https://console.typesafe.ai/keys",
                  "agent_try": "jevscreen keys set typesafe --dialog",
                  "agent_try_file": "jevscreen keys set typesafe --from-file <the file the human saved it in>",
                  "human_command": "/abs/path/.venv/bin/jevscreen keys set typesafe"},
                 {"provider": "openrouter", …}, {"provider": "vercel", …}]},
    // provider known (its key was rejected: rejected true, or the human chose it and its key is missing): the item
    // itself carries "provider", "label", "label_zh", "agent_try", "agent_try_file", "human_command", "text_zh",
    // "text_en", plus "choices" (the OTHER providers: setting one of their keys switches to it) and "clear_command"
    // ("jevscreen keys clear <provider>"). With JEVSCREEN_JEV_PROVIDER set: no choices, no clear_command
    {"id": "fill_descriptions", "ask_human": true, "optional": true,   // only with a done result (status stays done)
     "country": "CN", "missing": 1114, "companies": 3200, "share": 0.35, "minutes": 10, "estimate_usd": 0.06,
     "biggest": [{"name": "…", "ticker": "301018", "security_id": "SZSE:301018", "market_cap_usd": 5.4e9}],
     "question_zh": "…", "question_en": "…",
     "rerun_with": "--fill-descriptions yes", "decline_with": "--fill-descriptions no"}
  ],
  "idea_en": "Suppliers of speed reducers for humanoid robots",
  "idea_en_source": "agent",        // agent | sieve | model | verbatim
  "approved_usd": 1.0, "spent_usd": 0.27, "remaining_usd": 0.73,
  "next_command": "jevscreen quickstart '…' --idea-en '…' --approve-budget 1",   // null while running / when done
                                    // (budget_exhausted / a pending fill_descriptions: set, plus the item's
                                    // rerun_with). Keeps --approve-budget while an approval is held for a fix of a
                                    // refused English sentence.
  "poll_command": "jevscreen quickstart --status --key c895e776d4712fa0 --wait 100 --json",
                                    // whenever the worker runs (also with needs_human / needs_agent: the free
                                    // downloads go on) and when queued
  "retry_after": null,              // ISO time (UTC) for blocked / network failures (the real 24 h cooldown)
  "relay_every_s": 60,
  "run_id": "scr-…", "page": "/…/pages/<idea_key>.html", "page_uri": "file:///…", "page_opened": true,
                                    // page: always the idea's one stable page; run_id / top / summary / deck_id
                                    // are those of the newest run of the idea (the page's), also after `answer`
  "deck_id": "deck-scr-…-1", "cost_usd": 0.27,      // cost_usd: this run's (+ its update pass)
  "total_cost_usd": 0.16, "total_seconds": 342.0,   // the idea so far: every Jev cost of its runs + the key test;
                                    // the background work's own time (downloads, AI, fetches, fill) plus runs made
                                    // outside it, not the time spent waiting for answers
  "version": 2, "change_zh": "补抓 14 家年报后重排", "change_en": "re-ranked after fetching 14 annual reports",
  "idea_en_changes": [{"old": "…", "new": "…", "words": ["…"], "kind": "false_positive_fix"}],
  "timing": {"check": 0.4, "universe": 41.2, "descriptions": 28.9, "ai_check": 2.1, "estimate": 4.8,
             "screen": 290.3, "fetch": 95.0, "finish": 2.2},
  "fetch": {"status": "ok", "fetched": {"sec": 4}, "seconds": 95.0,     // the on-demand annual reports (null when
            "update": {"run_id": "scr-…", "status": "ok", "cost_usd": 0.002},  // none ran): layers.fetch in brief
            "next_command": null, "summary_zh": "…", "summary_en": "…",
            "questions": [{"id": "sec_email", "optional": true, "then_command": "jevscreen fetch-docs <run_id>",
                           "human_question_zh": "…（可选，…）", "human_question_en": "… (optional: …)"}]},
                                    // only questions that help a company in the top 10 (sec_email: a US one, or
                                    // a US company whose profile is too thin to list without its report,
                                    // mops_annual: a Taiwan one, opendart: a Korean one); then_command names run_id
  "summary": {"listed": 40, "annual_report": 3, "profile_only": 1, "edge": 5, "no_mention": 2, "cards": 8,
              "main": 4, "to_confirm": 36, "main_by_agent": 1},
                                    // no_mention: rows whose read text names none of the idea's words (gap);
                                    // listed: every listed row; main: the confirmed list (never padded to 10: owner
                                    // decision 2026-09-29), to_confirm: the separate to-confirm section,
                                    // main_by_agent: main rows your review confirmed; annual_report / profile_only
                                    // count the main list. A run made with --no-shortlist (one padded list) has no
                                    // main / to_confirm and counts every listed row
  "to_confirm": {"n": 36, "rows": [{"name": "…", "name_en": "…", "ticker": "…", "security_id": "…",
                                    "country": "…", "verdict_words_zh": "相关", "verdict_words_en": "Related",
                                    "evidence_kind": "profile", "agent": null, "moved_by_agent": false,
                                    "unchecked": false}]},
                                    // the page's 待核对 / To confirm section in brief (the first 10 rows): never
                                    // numbered with `top`, never used to fill it; moved_by_agent: your review said no
                                    // to a main-list row. null for a single padded list
  "top_more": 0,                    // confirmed rows beyond the 10 in `top` (the page lists every confirmed row;
                                    // text_<lang> then says so in one line); 0 when all fit or without the sections
  "top": [{"rank": 1, "verdict": "explicit",   // verdict: explicit | partial | edge (the page's verdict word)
           "name": "…",             // the MAIN LIST only (the first 10 of it at most, often fewer than 10: say so
                                    // calmly, never pad);
                                    // the name the page shows: on a Chinese page the official Chinese short name
                                    // (CNINFO 简称, MOPS 公司簡稱), else your translation, else the English name;
                                    // on an English page always the English name
           "name_en": "…", "name_zh": "…",   // name_zh: the official short name, null without one
           "name_translated": false,         // true: "name" is your (the agent's) translation
           "ticker": "…", "country": "China", "country_zh": "中国", "verdict_words_zh": "明确符合",
           "verdict_words_en": "Clearly fits", "evidence_kind": "annual_report",
           "one_line": "…",                 // what the page shows: your translation once imported, else the
                                            // annual report's own business sentence when the row was checked on
                                            // one (its overview), else the first sentence of the profile, in its
                                            // own language
           "one_line_source": "annual_report",   // annual_report | profile (null: no line); the annual report's
                                         // sentence must name the company (公司 / 当社 / 당사 / We / its name) and
                                         // not the economy, the industry, a definition or the legal set-up
           "one_line_translated": false, "one_line_original": null,   // the verbatim text when translated
           "one_line_needs_translation": true,   // still in another language than the page
           "excerpt_mentions_idea": true,   // false: none of the text the AI read names the idea (say "gap");
                                            // null: nothing to check
           "edge": false,                   // borderline (may change on re-reading, or a gap); the chat line says
                                            // 边缘 / borderline once (not again when the verdict is edge)
           "user": false,                   // the human answered this company on a card
           "verdict_from_user": false,      // listed only because of that answer: say "your call", not
                                            // "annual report" (up to 10 rows)
           "checked_by_agent": false,       // in the main list because your review confirmed it from the text (yes,
                                            // explicit, 1-3 quote_ids): say 你的 AI 核对 / "checked by your AI"
           "unchecked": false}],            // entered after a fill / re-rank and your AI has not checked it yet:
                                            // say 未核对 / "not yet checked" (the chat line already does)
  "translation_pending": 37,        // texts on the page in another language than the page's, not translated yet
  "translation": {"lang": "zh", "pending": 37, "translated": 0, "batch_max": 60,   // null: nothing to translate
                  "file": "/…/translate/scr-…-zh.json",
                  "export_command": "jevscreen page scr-… --export-strings /…/translate/scr-…-zh.json --lang zh --json",
                  "import_command": "jevscreen page scr-… --import-translations /…/translate/scr-…-zh.json --lang zh --json"},
                                    // do this right after relaying the first result ("Translating the page")
  "next_steps": [{"text_zh": "…", "text_en": "…", "command": null, "cost_usd": 0.25, "minutes": 4}],   // 3
                                    // a main list under 10 rows starts with a free `jevscreen why <ticker> --run …`
                                    // step for a company to confirm; text_<lang> then says calmly that the list only
                                    // takes companies whose texts state the business and is not padded, with three
                                    // companies to confirm; an empty main list says so plainly and points at the
                                    // to-confirm section; "the list may still change" when the run did not finish
                                    // (status ok) or the check did not read every company that passed the first read
                                    // (short_list {sections: true, main, to_confirm, unchecked, complete,
                                    // examples_*, why_ticker}; a --no-shortlist run keeps the earlier
                                    // {listed, unverified, unchecked, complete, examples_*} and its wording)
  "gaps": [{"id": "no_description", "count": 1111, "text_zh": "…", "text_en": "…",
            "command": "jevscreen crawl-descriptions --countries CN --min-mcap 1e9"}],
  "error": null, "notes": [],       // error: the technical detail; text_<lang> is the sentence for the human
  "text_zh": "…", "text_en": "…",   // what to tell the human for this status (its top lines already say
                                    // gap / borderline / your call like the page)
  "idea_en_problems": ["…"],        // only when an idea_en was refused (needs_agent again), with
  "refused_idea_en": "…", "suggested_idea_en": "…", "rerun_command": "…"
}
```

The English sentence check (`--idea-en`): refused when it names a company or ticker of the universe that the idea
itself does not name. Names match case-sensitively as proper nouns; a single ordinary English word that happens to be
a company name ("Core", "Main", "Global", "Energy" …) and technical acronyms (BESS, HVAC …) never count, nor does
the capital letter a sentence would have anyway ("Immersion cooling …", after ". " / "; " / ": ", or a sentence in
Title Case: a one-word name that is an ordinary word counts only where it is capitalised mid-sentence). The English
name of a company a Chinese / Japanese / Korean idea names in its own script is allowed (特斯拉 → Tesla, 宁德时代 →
CATL; calib.IDEA_EN_ALIASES). `suggested_idea_en` only drops names given as examples ("such as X and Y", "(e.g.
X)"); when a name carries the phrase ("X supply chain", "suppliers to X", "X's robot") it is null. The front
checks against the store, else the names cache (`<home>/quickstart/names.json`, written after every stock-list
download); only on the very first run, before any stock list, does the worker check right after the list is in
(about 30 s into the downloads, which go on). When the human had already approved the budget for the refused
sentence, that approval is held: `next_command` / `rerun_command` keep `--approve-budget`; only a fix that lower-cases
a flagged ordinary word ("Harmonic" → "harmonic") or swaps it within Core / Main / Key / Leading / Major / Primary
keeps the approval (noted in `idea_en_changes` and `text_<lang>`, nobody is asked again); any other sentence (a
company name dropped or replaced included) comes back as `reprice_idea_en` with `old_refused: true`. The refusal
fields come back on every answer of the front, also when it returns a finished result unchanged.

The top-gap fill (`top_fill` in the job file: `{status, exit, ok, n, at}`): once per job, right after the free profile
download and before the default fill and any estimate, the 50 largest companies of the idea's scope (its
`--countries`, else every market) at the floor that have no profile at all get one from TradingView, on the same
polite `crawl-descriptions` path (consent, 24 h cooldown, rate budget, journal; stops at the first refusal). About
30 s on a first run, nothing to do later; skipped after `--fill-descriptions no`.

The default profile fill (`fill_default`: `{market, state: planned | running | done | skipped | blocked | failed |
timeout | stopped | opted_out, reason, added, missing, minutes, opt_out_with: "--fill-descriptions no", text_zh,
text_en}`, null for an idea without a market, also once it was narrowed to other markets after the fill started: that
fill is then stopped and not waited for; `stopped`: the fill was interrupted from outside, e.g. a shutdown; `done` with `reason: "floor"`:
the human raised the floor while the first read waited and every company at the new floor was covered, so the rest of
the fill was stopped instead of waited for): China
only (a Chinese idea, or one narrowed to CN; another single market keeps the
fallback question below): when China >= 30% of its companies at the floor without any profile, or >= 3 of its 100 largest, the worker starts
`crawl-descriptions` for that market at the floor right after the free profile download, as a detached child (the
same consent, 24 h cooldown, rate budget and journal as the plain command), while the human answers; the first paid
read waits for it (at most max(20 min, 3 x its estimate); the page shows it as one progress bar) and reads the new
profiles with everything else, so no re-rank and no question follow. `added` counts the profiles it brought in at
the floor it started with (`text_<lang>` says they were read in the first pass only when that read covered its market
and floor). A new floor or market merged while the first read waits is estimated again before any paid read (the
`over` question comes back when it no longer fits). `--fill-descriptions no` before or while it runs
opts out (a running fill is stopped); a later `--fill-descriptions yes` brings it back (before the first read the
default fill is decided again; after a result with missing China profiles the fill and its re-rank start as after a
yes to the question below). An idea without a market whose first read passes >= 40% (and >= 3) A-shares
gets the China fill right after that read, and only the new companies are read and ranked in before the first
result (`when: "after_l1"`; also when the worker was stopped in that wait and resumed).

The fallback question: after a result, only when the default fill did not run to its end (failed, timed out,
stopped, not possible) or did not cover the result (a floor lowered after it started), not after a block (its 24 h
cooldown holds the question back) and not after an opt-out; or for an idea narrowed to another single market (HK,
TW, JP, KR, IN), which has no default fill. With the same thresholds, and when the re-rank fits what is left of the
approval, `pending` holds one `fill_descriptions` item (optional; the status
stays `done`, exit 0). A yes (`--fill-descriptions yes`) starts the worker: `crawl-descriptions` for that market at
the run's floor (consent, cooldown and rate budget of the plain command), then `screen --from-run <newest run>
--l1-new` (only the newly described companies are read by L1; everyone else keeps its answer at $0), the annual
reports of new profile-only layer-2 companies, and the same stable page. Meanwhile the status is `running` with the
first result's fields. The question names up to 5 missing companies that carry the idea's own words in their
name (English or the exchange short name) or industry, largest first (`names_basis: "idea_words"`); else those in the
industries of the result's layer-1 passes (banks and insurers only when the passes are financial; else the larger
non-financial ones), with the exchange short name when a CNINFO stock list is stored. The companies the fill brings
into the list go to your AI (a follow-up deck, blocking when one is in the top 10) and are marked 未核对 / not yet
checked until it has read them. A yes given while another worker holds the lock is kept:
the status is `store_busy` with `poll_command`, and `--status` starts the fill once the lock is free. A block, no
new profile, a busy database, a failed re-rank or a re-rank the budget could not finish keeps the result, and
`text_<lang>` says what came in and why nothing changed. A no (`--fill-descriptions no`) is never asked again.
`next_steps` offers the plain China `crawl-descriptions` only when no fill question is open, the human did not opt
out of the default fill, and no 24 h crawl cooldown runs (a block).

`jevscreen answer` on the cards of an older version (a page tab opened before a fill, an update pass or other
answers) applies the answers to the newest version screened from it, so the companies added since are kept; the
output says so in one line (`applied_to_run` in `--json`).

Money: `approved_usd` is the total cap the human approved for this idea and this English sentence. The screen always
runs with `budget = remaining_usd`; the first run under an approval needs the dry run's estimate to fit (the
reservation, its safety margin, may go over: the budget is hard, the run stops at the cap), else
`approve_budget` comes back with `kind: "over"` (the question names the estimate, with a third decimal when two would
read as the cap) and up to three narrower `alternatives`, each one's estimate within
what is left of the approval (rerunning with an alternative's `flags` estimates again under the same approval). The canary (one paid
request of about $0.00005 that proves the key can pay, run after the approval and before the estimate) counts
against it, and so does the update pass after the on-demand fetch (the `fetch` step: the same fetch and update as
`screen --fetch-docs auto`, see "On-demand annual reports" above; 150 s, update at most min($0.05, what is left);
`run_id` and `cost_usd` are then the updated run's id and the screen's plus the update's cost). A different
`--idea-en` voids the approval. The English sentence is taken from `--idea-en`, else the
sieve's `idea_en`, else the job's pinned one; a sieve `idea_en` that differs from a pin already approved or
screened comes back as `reprice_idea_en`. `budget_exhausted` carries a pending `approve_budget` of kind `topup`: a
larger total (`--approve-budget <total>`) screens again, and answers already paid for come from the cache for $0.
Only an `ok` / `partial` result is reused as is.

The job file holds no key and no SEC name or e-mail: it is scanned for every configured secret before each write,
and a hit refuses the write. The result page is scanned the same way.

`jevscreen doctor --json` carries `quickstart_command` (the front command with a placeholder idea).

### Scope questions and your AI's review (`judge`, `decide`)

After the first result the human answers no calibration cards. Instead:

1. **Facet layer** (inside the screen, `facet_scan`): a cheap Jev choice question per L2-verified company (at most
   150, plus pins and checks) over the **same L2 text**: `role` (supplier / buyer / holding / upstream parts /
   hardware sold to operators / target only / unclear), for a technology idea `scope` (specific / general only),
   for a geography idea `geo` (in the target / outside only). Read 0 of every item, 2 more reads when read 0 lies in
   0.40-0.75 (the mean decides). Typical cost $0.01-0.02 (at most about $0.025), a cached rerun $0; skipped with a
   note when the approval's rest cannot cover it (labels are then carried from the base run by evidence_sha). L1 is
   never re-read or re-priced. `results.json` gains `facets` `{company_key: {role|scope|geo: {label, p, n,
   evidence_sha}}}`, `scope` and `excluded_by_scope` / `excluded_by_agent`.
2. **Your AI's review**: when the result is done the status is `needs_agent` (exit 11) with ONE pending item:

```jsonc
{"id": "agent_review", "ask_agent": true, "blocking": true, "deck_id": "adeck-scr-…-A",
 "deck_path": "/…/screens/…/agent_deck_A.json", "items": 41,
 "record_command": "jevscreen judge --deck adeck-scr-…-A --file <answers.json> --json",
 "skip_command": "jevscreen judge --deck adeck-scr-…-A --skip --json", "instructions_en": "…"}
```

   `text_<lang>` is only 「结果出来了，我正在逐家核对…」: the list is not relayed yet. The page shows the list with a
   "your AI is checking" note and keeps refreshing. If nothing is recorded within 1200 s, `--status` finalizes the
   questions without the AI (`agent_review.state: "timed_out"`, a note) and the status is `done`.

   The deck (`jevscreen.agent_deck/1`, a local file in the run folder; personal use, keep it on this computer):
   `items[]` = `{n, group: top|held|below_cut|gap|removed_by_scope|judge_demoted|followup, held_sid, held_kind,
   company_key, security_id, name, name_zh, rank, section: main|to_confirm|null, system: {label, p_pos, p_explicit, edge, facets, judge?}, evidence:
   {kind, source, form, filing_date, lang, sentences: [[1, "…"], …], more?: {sentences: [[13, "…"], …], source, form,
   filing_date, lang}}, evidence_sha, review_sha?, mentions_idea}` (`evidence.more`: when the company has a stored
   official filing, up to 5 more numbered sentences of it that the excerpt left out, picked by the idea's words and
   Latin anchors, the company's own product / business sentences first, industry-trend talk last; numbered after the
   excerpt's sentences and citable like them; a profile-only item gets them too when a filing is stored;
   `review_sha` covers the excerpt's evidence_sha and these sentences); the header carries `criteria_en` (the
   L2 criteria plus the human's scope answers as sentences), `chips` (c-k with their zh/en words), `held_questions`
   (`[{sid, kind, criterion_en}]`), `instructions_en`, `answer_schema`, `record_command`, `skip_command`,
   `licence_note`. Part A (<= 60, typically 30-45 items and 5-10 minutes of reading: the side-V rows of the
   provisional scope splits, then every company to confirm, then every row of the main list, up to max_out (the
   first 10 ranks for a --no-shortlist run; room is kept for the main list, the to-confirm rows fill what is left),
   <= 3 below-cut / gap rows) blocks. A yes, level explicit, with 1-3 quote_ids on a to-confirm item moves it into the main list
   (`main_via: "agent"`, 你的 AI 核对); partial or unsure leave it to confirm; a no on a main item takes it off the
   main list (applied: removed; waiting for the human: to confirm, `main_via: "agent_no"`); the human's pins win.
   Part B (listed rows part A did not hold, strong rows removed by scope, below-cut, gap; <= 45) never blocks
   (`agent_review.part_b`); follow-up parts `F<n>` come whenever a new version (your judge, a decide, a profile
   fill) brings companies your AI has not read into the list. One with a company in the main list's first 10 (the
   first 10 ranks for a --no-shortlist run) blocks like part A:
   `agent_review` becomes `{state: pending, part: "F<n>", deck_id, deck_path, items, record_command, skip_command,
   blocking: true}`, the status is `needs_agent` again and `text_<lang>` says new companies are being checked; `judge`
   and `decide` return it as `review_pending`. Review it the same way before relaying. Until your AI has checked them,
   those companies carry `unchecked: true` in `top` and 未核对 / "not yet checked" on the page (review.json
   `unchecked`).

   The answers file (`jevscreen.agent_answers/1`):

```jsonc
{"format": "jevscreen.agent_answers/1", "deck_id": "adeck-scr-…-A", "agent": "<optional: product/model>",
 "answers": {"1": {"v": "yes", "level": "explicit", "quote_ids": [3, 4], "why": "<human's language, <= 80 字 / 160>"},
             "2": {"v": "no", "chip": "k", "quote_ids": [2], "why": "…", "quote_tr": "<only when evidence.lang differs>"},
             "3": {"v": "unsure", "unsure_kind": "thin", "why": "…"},
             "7": {"v": "no", "chip": "k", "quote_ids": [1], "why": "…", "in_group": true, "short": "<= 10 字 / 40"}}}
```

   yes needs `level`; no needs a chip of the deck; yes / no cite 1-3 existing `quote_ids` (excerpt or
   `evidence.more` sentences; else the answer counts as unsure, `quote_bad`); the cited sentences are kept with the
   verdict as shown (`quotes`, plus `more_source` when one came from `evidence.more`), so the page, an escalation
   and `why` quote exactly them; a main-list item whose text contradicts a hard part of the idea (another geography,
   a different product kind, only a plan / goal / R&D, only named in a list of many) is a no with its chip, and
   partial on a main item is for a real but small or early offer; an unsure may cite the sentences it is unsure about (an escalation without cited
   sentences quotes the item's first sentences); `why` / `short` in the other language than `human_lang` are
   dropped (the chip or level words stand in); a missing item is "not reviewed". The answers go to `<home>/sieves/<idea_key>.agent.json`
   (`jevscreen.agent_verdicts/1`), a separate lower-precedence layer: they never set the human's `user_verdict`,
   never re-score and never list an unverified company; a verdict applies only to the evidence it was given for.
   Weak disagreements are applied without asking; an answer is **escalated** to the human (never applied) when:
   E1 unsure about the meaning on a listed / gap / below-cut row, E2 a no on a row the system is confident about,
   E3 a yes on an unverified row that would make the list, E4 a conflict with the human's own answer. Thin
   evidence gets a gap badge, never a question. An escalation carries `section`: about a to-confirm row it names the
   section, not a rank ("X（待核对）：…要不要放进确认名单？" / "X (to confirm): … Add it to the confirmed list?") and
   is not asked in chat (`in_relayed_top` false: the page's question box has it); an E2 on a main-list row says it
   was moved to confirm for now ("… Put it back in the confirmed list?").

   `jevscreen judge --deck ID (--file F | --skip) [--json]` (free, seconds): `{applied, queued, run_id, removed,
   escalated, questions, defaults, part_b, main_changed, text_zh, text_en}` (`main_changed`: the relayed list, the
   confirmed list with the sections, is not the same companies as before this judge). Exit 0; 1 a bad file / deck (`error_zh`,
   `error_en`); 3 the database stayed busy. With answers it makes a new version (`change_kind: agent`, rank_only:
   no Jev call, $0); `--skip` keeps the list as it is.
3. **The human round** (status `done`): `ask_now` (at most 3, optional, in order: `fill_descriptions`, at most 2
   `scope_question`s, at most 2 `confirm_company` escalations on the relayed list (the confirmed list's first 10; the
   first 10 ranks for a --no-shortlist run) or E3/E4, the fetch questions),
   `later` (the rest, JSON only; the page's question box also shows the open scope questions and up to 3 open
   escalations by rank), `scope` `{questions,
   defaults, answered}` (each default applied from the idea's own words has `undo_command`, the one-line undo;
   `text_<lang>` names it right after the counts, and the page shows it in its own box with a copy button),
   `escalations`, `agent_summary` `{read, annual, profile, removed: [<= 5 {security_id, name, chip_words_zh,
   chip_words_en}], removed_total, moved_in?, moved_out?, text_zh, text_en}` (`moved_in` / `moved_out`: `[{security_id,
   name_zh, name_en}]`, the rows your AI moved into the confirmed list or to the to-confirm section, said in
   `text_<lang>`; `read` counts every current verdict of your AI, all decks,
   and `removed_total` every company the page lists as removed, so the chat and the page say the same numbers;
   restated for the version the page shows: after a later version (a decide, a fill) `read` / `removed_total` /
   `text_<lang>` are that version's, and `removed` / `names_<lang>` are emptied when the removed count changed;
   `annual` / `profile` are null when the split is unknown), `agent_review` `{state: pending | done |
   skipped | timed_out | none, deck_id, deck_path, part_b}`, `next_action_en`, and `agent_optional` (the optional
   `facets` request before the screen). The first response whose `ask_now` is not empty fixes the chat round: each of
   its questions comes back in `ask_now` on every status, also across new versions (part B, a fill, a decide), until
   the human answers it; every new item goes to `later` (a response without questions, e.g. a timed-out review, uses
   no round). A fetch question the human already settled (the SEC contact set or `consent set sec-email-ask no`
   recorded, the Taiwan consent recorded, an OpenDART key set) is dropped. After a no to the SEC contact, the gap line
   and `next_steps` no longer offer `keys set sec-email`. A scope question:

```jsonc
{"id": "scope_question", "sid": "s1", "ask_human": true, "optional": true,
 "question_zh": "AI 读摘录后认为，名单前 40 家里有 9 家卖硬件或设备给提供「数字支付」的公司…这类公司要不要留在名单里？（不要：…）",
 "question_en": "Reading the excerpts, the AI thinks 9 of the top 40 … Keep this kind of company on the list? (Drop: …)",
 "answer_words": {"zh": {"yes": "要", "no": "不要", "unsure": "不确定"}, "en": {"yes": "Keep", "no": "Drop", "unsure": "Not sure"}},
 "tokens": {"yes": "s1=yes", "no": "s1=no", "unsure": "s1=?"},
 "record_command": "jevscreen decide '<tokens>' --run scr-… --via chat --json"}
```

   A question is asked only when the list really splits on that boundary (at least max(4, 10%) of the listed
   companies on the side it would remove, at least max(4, 20%) that clearly supply), is about kinds of company
   (never one company), says it is the AI's inference, and gives 3 + 2 example companies. 不要 removes the kind
   (the scope-only "the excerpt names only the broad category" question demotes instead), 要 keeps it, 不确定 lets
   your AI's call on each one stand. The answer is recorded in `sieve.scope_answers` (never in `sieve.rules`; the L2
   question does not change) and applied directly. Idea-wording defaults (`--facets` `implied_no`) are applied
   the same way, shown as one line each with their undo token (`s2=yes`).

   `jevscreen decide "<tokens>" --run RUN [--via chat|page] [--json]` (free, seconds): tokens `sN=yes|no|?`,
   `cN=yes|no|?` (an escalation: yes/no becomes the human's pin, `via: escalation`), `keep=TICKER` / `drop=TICKER`
   (override your AI's call: a human pin, `via: override_agent`), `clear=TICKER` (undo those pins). Open questions
   the tokens omit are recorded as skipped (not asked again). Output `{applied, queued, run_id, diff_zh, diff_en,
   followup, later}`; exit 0, 1 an unknown token / id / ticker, 3 busy. While the idea's profile fill runs, judge
   and decide save the answers and return `queued: true`; the fill re-applies them when it ends.

`jevscreen why` explains removals by a scope answer (`stop: scope_removed`, with the answer, the inference and the
undo token) and by your AI (`stop: agent_removed`, with its words and the cited excerpt).

### The one page per idea

`<home>/pages/<idea_key>.html` is the only page the human needs (owner decision 2026-09-27). With JavaScript it is
one full-window 3D sand scene (three sieves: market-cap floor, first read, report check; each sieve's progress runs
around its rim) under a restrained HUD (owner, 2026-09-28): the idea as a faint title; ONE status dot for the
prerequisites below (green when all are ready, red with one plain sentence and its fix, the whole checklist on
hover, tap or focus); a tiny cost / time / stage instrument; the results and scope questions in a panel on the right
(a bottom sheet on a phone) whose rows and the amber grains light each other; model-viewer controls (wheel or pinch
zoom, drag to orbit, right-drag or two-finger pan, double-click a sieve to fly in, a reset control). A **文字视图 /
Text view** toggle (also `#text` at the end of the address, and what gets printed) shows the full plain page below,
top to bottom; so does a browser without JavaScript. `--text` is unchanged:

1. **Prerequisites**: Python and packages, the data-source consent, the Jev key configured **and** verified by the
   tiny paid test (with the provider's name; Vercel AI Gateway always says "version cannot be pinned"), the stock
   list, the company profiles, the open data pack, ready to screen; optional (grey): SEC contact, EDINET, OpenDART,
   MOPS annual reports. Each line is a green check, a spinner, a grey circle (not started / optional) or a red cross
   with one plain sentence and the fix. When everything passes it collapses to one green line.
2. **Progress**: per data item done / total with a bar (the stock list, the profiles with MB downloaded, the AI's
   first read n/N, its check n/N, the annual reports fetched n/N), an ETA from the stage's own pace, the dollars
   spent against the approved cap; blocks, cooldowns and stops in red, in plain words.
3. **Scope questions**: at most 2 questions about the idea's boundary (`data.questions.items`, from the version's
   `review.json`; hidden while empty and while your AI is still checking), each with its effect in plain words and
   要 / 不要 / 不确定 (Keep / Drop / Not sure) buttons whose values are `decide` tokens; the page joins the clicked
   ones into the line to paste to the AI (`data.questions.template`: `jevscreen decide "{answers}" --run <run_id>
   --via page`), plus a copy button. The idea-wording default lines (`data.questions.notes`) sit above them.
4. **Results**: a compact top-10 table, then one expandable row per company (evidence with the translation /
   original toggle; labels fact / inference / gap / your or your AI's call; source links), the companies your scope
   answers or your AI removed (each with its reason, marked inference), the unconfirmed companies, the gaps.

There are no card buttons and no answer bar on the page: the cards stay a CLI tool for you (`jevscreen cards`,
`jevscreen answer`). The status part is the data block's `live` object (jevscreen.pagestatus): it is built from the
quickstart job file and local files only (consent answers, key presence by stat, cooldown markers, doctor's local
checks), never from the network and never waiting on the store. The front writes the page from its first call, the
worker opens it once at its first step (`page_opened`; not with `--no-open`) and rewrites it atomically at every job
save and progress tick; while work runs (or waits for an answer or a key) the page reloads itself every 3 s
(`<meta name="jevscreen-refresh" content="3">` read by a small script that waits ~4 s after someone presses a pointer,
types or uses the sand scene (a drag, a zoom, a tooltip being read); `<noscript><meta http-equiv="refresh" content="3"></noscript>` without
JavaScript), which is gone once the job is done, declined or blocked for 24 hours. The
quickstart JSON carries `page` / `page_uri` from the first call on. `--text` and the `<noscript>` block start with the
same status lines.

### `jevscreen page`

Rebuilds `page.html` in the run's output directory and, when the run is the idea's newest (by `started_at`), the
stable copy `<home>/pages/<idea_key>.html` (free, read-only store session): the stable copy always holds the newest
run of the idea, and `<idea_key>.run.json` beside it names that run. `--json` prints `{command, status, run_id, page,
page_uri, stable_page, run_page, page_opened, deck_id, summary}`: `page` is the stable copy whenever it holds this run
(`stable_page` null and `page` = the run's own `page.html` for an older run). `answer` prints the stable page too.
The page header shows the idea's totals (cost and time so far, as `total_cost_usd` / `total_seconds`) and, from
version 2 on, what changed ("补抓 14 家年报后重排", "应用了你的 3 个回答后重排", "补了 120 家公司简介后重排").
`--text` also prints the ranked list as plain text (in the JSON it is `text`): read it instead of opening the page
when you have no browser. The same text sits in the page's `<noscript>` block, so reading `page.html` as a file gives
the list too. Without `--lang` the page uses the idea's quickstart job language, else the idea's. `screen` (unless `--no-page`), `answer` and `cards` write it too, after the cards, so the page
always carries the current `deck_id`.

**One language per page.** A `zh` page is fully Chinese and an `en` page fully English: labels, sources (巨潮资讯 /
CNINFO, 美国年报 10-K / SEC 10-K, 日本有价证券报告书 / Japan annual securities report), dates (2026年4月1日 / 1 Apr
2026), market caps (21 亿美元 / $2.1B), countries, the `--text` list and `<noscript>`. There is no language toggle;
`--lang` rebuilds it in the other language. Company names: a Chinese page shows the official Chinese short name
(CNINFO 简称 for A-shares, MOPS 公司簡稱 for Taiwan when `sync-mops` stored the basic data), else your translation
with the original name small, else the English name; an English page always shows the English (TradingView) name.
The JSON also carries `lang`, `translation` (as in quickstart, above) and `translation_pending`.

#### Translating the page (`--export-strings` / `--import-translations`)

The data holds text in other languages (English profiles, Chinese / Japanese / Korean annual-report excerpts,
company names without an official Chinese name). You, the human's own AI agent, translate it; no other model is
called and nothing is paid.

```bash
jevscreen page RUN --export-strings FILE --lang zh --json    # FILE: a JSON list, at most 60 items, most important first
# fill every item's "translation" (leave null when unsure), keep the other fields as they are
jevscreen page RUN --import-translations FILE --lang zh --json   # stores them and rebuilds the page in that language
```

Item: `{"id": "d-1a2b…", "kind": "name|description|excerpt|reason", "text": "…", "source_lang": "en",
"target_lang": "zh", "sha": "<sha256 of text>", "context": "ROBO · United States", "translation": null}`. Export
prints `{action: "export_strings", status: "ok" | "nothing_to_translate", file, items, pending_total,
remaining_after, kinds, instructions_en, import_command}`; the page is not changed. Import prints the page JSON plus
`{action: "import_translations", imported, skipped, rejected: [{id, problem}]}` and the new `translation_pending`:
while it is above 0 and the round imported something, export again (the same command) for the next batch (an
import without `--lang` rebuilds the page in the items' `target_lang`). A text that has no translation (a product
code, a brand) is resolved with `"keep_original": true` instead of a translation; a `name` may stay in Latin letters
(the name unchanged, or its short form: "Zscaler"): it is then no longer pending and the page shows the original.
A `null` translation is skipped and comes back in the next export. A translation is refused when it is not in the
target language (except a Latin name), is the text itself (use `keep_original`), carries markup, is much longer
than the text, or its `sha` no longer matches `text`; exit 1 only when the file is unreadable or nothing could be
imported, 3 (`store_busy`) when the database stayed busy: nothing is exported then, and a page is never rebuilt
without its stored translations (the earlier page stays; run the same command again in a minute). Translations go to the
store table `translations` (key: sha + target language, provenance `agent translation`) and are reused by every
later run and idea showing the same text. The original text is never changed: the page shows the translation first
with an "AI 翻译 / AI translation" tag and a "看原文 / Show original" button that reveals the verbatim original (the
original stays the fact: a translated excerpt is tagged "AI 翻译（不是原文） / AI translation (not the filing
text)", never "事实 / Fact"); an untranslated text shows the original marked "原文（未翻译） / original (not
translated)". A translated company name always carries the original name (small below it, or in brackets in the
lists and in `text_zh`). `jevscreen cards` prints the stored translations of the cards' texts, marked AI 翻译 / AI
translation.

### `jevscreen keys set NAME --dialog`

Opens a native hidden-input box (macOS `osascript … with hidden answer`, Linux `zenity --password` with a display;
100 s, so the call fits a 2-minute agent timeout: open it only when the human says the key is ready). The value goes from the box to the key file (mode 0600) and is never printed. On success:
`{"command": "keys set", "status": "ok", "via": "dialog", "name", "path", "mode", "shape_ok"}`. Any failure (no box
on this machine, cancelled, timed out, malformed value) exits 1 with `{"status": "refused", "error",
"human_command", "timed_out", "text_zh", "text_en"}`: `human_command` is the absolute path of this install's
`jevscreen`, for the human to run in their own terminal. A timeout (`timed_out: true`) adds `retry_command` (the
same `--dialog` call) for when the human is ready. Windows is untested and has no box yet: use `human_command`.
