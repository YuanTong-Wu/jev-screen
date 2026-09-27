# The sieve: what your AI may write

Every idea has one sieve file, `data/sieves/<idea_key>.json` (`jevscreen.sieve/1`; the key is
`sha256(idea.strip())[:16]`). It already holds the human's card answers, pins, adopted rules and learned keywords
(docs/DATA_RULES.md "Calibration (sieve)"). This page is about the **author fields**: the part the user's AI may draft.
Schema: [sieve.schema.json](sieve.schema.json). Code: `src/jevscreen/sieve_author.py`, `src/jevscreen/why_cli.py`.

## Author fields

| field | what it does | what reaches Jev |
|---|---|---|
| `idea` | must equal the sieve's idea exactly (after trimming spaces) | - |
| `idea_en` (≤ 300 chars) | English rendering of a non-English idea; replaces the local model's | yes: "idea_en (original: idea)" in both questions |
| `seed_terms` {en, zh, ja, ko: ≤ 15 × ≤ 60 chars} | excerpt keyword terms per annual-report language (after the human's `--keywords*`, before generated ones) | no (they pick excerpts) |
| `facets`, `facets_zh`, `target_terms` | as before: the slots the library rules are rendered with | only through adopted library rules |
| `should_pass` [≤ 30 × {company, want explicit/partial, why}] | 应该有（检查）: always read by step 2 and reported | no (company names never enter a question) |
| `should_fail` [≤ 30 × {company, why}] | 不该有（检查） | no |
| `notes` | free text for the human | no |

Everything else is written by the tool: `resolved` per check entry (security_id, company_key, name, method,
resolved_at: the company is resolved **once**, when written; a later rename only warns), `idea_en_reprice`, card
answers, pins, rules, keyword learning.

**Refused in a draft**: `include`, `exclude`, `pins`, `examples` (「钉选只能在你本人同意后加：jevscreen sieve pin …」) and
`rules` (only the card trials adopt rules).

## Rules the tool enforces

- **Checks are not pins.** A should_pass company is read and reported (report 「示例检查」, `why`); it is ranked only
  when the evidence ranks it. A card answer or pin for the same company wins (warning 「你的回答和 AI 的草稿矛盾：以你的回答为准」).
- **Companies outside the filters** (below the market-cap floor, another country, low volume, no profile) are
  still read by step 2 when the sieve names them, without an L1 call, and shown under 「你关心的公司」. Those outside
  the market-cap / country / volume filters are never listed, even when pinned; `sieve check` says 「它在你的市值门槛
  以下：只读证据写进报告，不会进名单」. A company inside the filters that only lacks a profile is ranked when the human
  pinned it (the pin decides; `why` says so) and is then not repeated under 「你关心的公司」.
- **The shells filter never drops a company the sieve names.**
- **idea_en** applies only to the exact idea it was written for and only when that idea is not English; otherwise it
  is ignored with a warning. Once a paid run of the idea exists, the idea_en that run used is **frozen**: the next
  run uses it again (keywords status `frozen`; the local model is not asked, so the questions and every cached answer
  stay the same) and a different sieve idea_en only warns (「idea_en 已冻结 …」) until `sieve set --reprice` (ask the
  human: the next run asks step 1 again; the estimate is printed). `--reprice` stores the approved text as
  `idea_en_reprice`: the yes covers that one idea_en, and a later rewrite is frozen again. idea_en may not name a company the sieve lists (error, also enforced by
  `screen` before anything is sent); words that are also names or tickers of companies ≥ $10B are warnings
  (「「Harmonic」也是一家公司的名字：会不会把模型引向它？…」), minus a stop list and the sieve's own terms.
- **Pins** come only from card answers or `jevscreen sieve pin COMPANY yes|no` (a card-style example with
  `via: "pin"`). The report labels them 「你让 AI 钉选的（日期）」. The tool cannot prove the human said yes: run it
  only after they did.

## Commands (free, no Jev; all accept `--json`)

Address a sieve with `--run RUN_ID|latest` (the run's idea), `--key HEX`, `--idea TEXT`, or (set / check) a
positional PATH or idea text. A positional ending in `.json` that does not exist is an error, never an idea. A
missing sieve for an idea text exits 1 and names the closest existing one.

| command | what it does | exit |
|---|---|---|
| `sieve new [--from FILE\|-]` | create the sieve (idea from `--run`, `--idea` or the draft); an existing one is merged like `set` (never refused) | 0 / 1 |
| `sieve set --from FILE\|- [--reprice] [--dry]` | JSON merge patch of the author fields; creates when missing; resolves companies (exact only); migrates legacy source-'sieve' examples into the check lists; prints the diff and warnings | 0 / 1 |
| `sieve add should_pass\|should_fail COMPANY [--want] [--why]` | resolve now and append | 0 / 1 (candidates) |
| `sieve remove should_pass\|should_fail COMPANY` | | 0 / 1 |
| `sieve pin COMPANY yes\|no [--want]` / `sieve unpin COMPANY` | the human's own judgement (ask first) | 0 / 1 |
| `sieve list` | idea, key, version, last run, counts | 0 |
| `sieve check` | schema, author errors, `resolved` (ok / moved / gone, below the floor), idea_en state and reprice estimate, a one-line `why` preview per check from the latest run, the L2 question | 0 (warnings allowed) / 1 errors |

Writes are version-checked: a sieve changed elsewhere since it was read gives `status: "sieve_stale"` (请重新读取).

`sieve set --json`: `{command, ok, saved, path, idea_key, version, diff, errors: [{code, text_zh, text_en}], warnings,
idea_en: {source, frozen, used, differs, reprice: {l1_usd, l2_usd, companies} | null}, resolved, next_command}`.

## For the AI agent

- Do not draft a sieve before the first result. After the dry run, only when results.json `keywords.status` shows no
  idea_en for a non-English idea (the local model is missing), write `{"idea": <exact idea>, "idea_en": ...,
  "seed_terms": ...}` and run `jevscreen sieve set --from draft.json --json`, then the dry run again. Do not ask the
  human. `ok: true` is enough; warnings never block; after two failed attempts screen with `--sieve none`.
- After the first result you may offer 「要不要我帮你核对几家你觉得应该在的公司？」 and, on yes,
  `jevscreen sieve add should_pass TICKER --run RUN_ID`.
- Never write pins yourself; `sieve pin` only after the human's explicit yes.
