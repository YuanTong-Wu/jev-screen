# `jevscreen why`: why a company is (not) in the list

```bash
jevscreen why TARGET [TARGET ...] [--run RUN_ID|OUT_DIR|latest] [--idea TEXT | --key HEX] [--checks]
              [--lang auto|zh|en] [--show-text] [--wait S] [--json] [--verbose]
```

Free, read-only, no network, no Jev. It answers "why is X missing / why is X at #3" in one plain sentence, then the
stages, then what would change it. Code: `src/jevscreen/why.py`, `src/jevscreen/names.py`.

## Which run

- `--run RUN_ID` or the run's output directory: that run. Always pass the run id the screen printed.
- `--run latest` (default) with `--idea` / `--key`: the newest run of that idea, whatever its status (ok, partial,
  budget_exhausted, dry run).
- Nothing given: the newest run, but only when a single idea ran in the last 7 days; otherwise exit 1 with
  `status: "choose_run"` and up to 5 runs to choose from. No run at all: `status: "no_runs"`.

## Which company

A target may be a security id (`NYSE:ABC`), a ticker or exchange code (`2330`, `300352`), a company key, or a name
in any script. Names come from the store: TradingView names, the CNINFO short names 简称 (also without `ST` / `*ST`),
the EDINET code list (Japanese and English names), the DART corp code list (Korean and English names) and the SEC
ticker list, joined to lines through `identifiers`. A missing list is simply skipped (`sources_used`).

Scores: id / symbol / exact name = 1.0; a CJK name of >= 3 characters (or an ST-stripped short name) contained in
the query or the reverse = shorter / longer; character-bigram Jaccard >= 0.5. Unique when the best is >= 0.8 and
the next company is at least 0.15 lower; a tie goes to the company that is in the run. `why` may use a near match
and says so (「按名称近似匹配」); the sieve commands accept only exact ones. A second line of a company (an ADR, a B
share) resolves to the line the screen used, and the answer says so.

Nicknames the store does not know (台积电, 英伟达) are the AI's job: turn them into tickers first. A not-found answer
carries `agent_hint_en`.

## Stops (first failing stage; stable ids)

| stop | meaning | what would change it |
|---|---|---|
| `not_found`, `ambiguous` | the name did not resolve | use a ticker (candidates listed) |
| `not_in_universe` | not a primary common-stock line, or inactive | nothing to do |
| `null_mcap` | no market cap in the market data | gap only |
| `below_min_mcap`, `below_min_volume`, `other_country` | a run filter | add to should_pass (free) + rerun from the run: evidence read and reported, never listed; or lower the floor (free dry run first, then a priced run) / add the country (dry run) |
| `shell` | the shells filter removed it (the rule and its pattern in plain words; the pattern id is in `stages[].data`) | add to should_pass (protected) or screen again with `--shells keep` (dry run first, then priced) |
| `no_description` | no profile, so step 1 never saw it | gap (says whether an annual report exists) |
| `dry_run` | the run was a dry run | run it for real |
| `l1_not_sent` | budget ran out / provider failed | rerun with the same filters (cached answers free) |
| `l1_rejected` | step 1 judged it unrelated / unclear / adjacent below the threshold | add to should_pass (read and reported, not listed) or a pin (human's yes) |
| `l2_not_sent` | beyond `--l2-max` | raise `--l2-max` from the run (priced) |
| `l2_failed` | step 2 did not complete | rerun from the run |
| `l2_contradicted` | the text says it does not do this | a pin (human's yes) |
| `l2_unverified` | step 2 found no explicit evidence | download its annual report (`sync-cninfo/edinet/dart/bse --codes CODE`, `sync-mops --mode annual --codes CODE`, only when the key / consent exists) then rerun from the run; or better seed terms; or a pin. A market without such a command is a gap (no price shown) |
| `ranked_below_cut` | passed both steps, ranked below `--max-out` | nothing needed; `--max-out N` from the run |
| `excluded_by_user` | the human said no (card or `sieve pin`) | `answer --undo N` / `sieve unpin` (human's yes) |
| `in_output` | it is in the list (rank, marks, ST warning) | none |
| `forced_extra` | a sieve company outside the filters, read and reported | none (explains why it is not listed; a pinned company that only lacks a profile is ranked and gets `in_output` / `excluded_by_user`) |
| `pre_ledger` | the run predates the ledger and the store is busy | ask again later |

Every stage line is labelled ✓ / ✗ / —. The answer separates `facts` (what the data says, e.g. 「简介里没出现：…」),
`inferences` (the model's percentages: it gives no reasons) and `gaps` (what is missing). Costs are in words
(「不到 1 分钱」 / 「约 $0.35」); exact floats are only in JSON.

## JSON (`--json`)

```
{command: "why", status: "ok" | "partial_files_only" | "partly_resolved" | "not_found" | "no_targets",
 run: {run_id, idea, started_at, status, dry_run, output_dir, requested_run_id?}, files_only, sources_used,
 results: [{target, match: {status, security_id, company_key, name, score, method, matched_line, in_universe,
            candidates[<=5]}, run, stop, plain_zh, plain_en, who_zh, who_en,
            stages: [{id, ok, text_zh, text_en, rule_zh?, rule_en?, data?}],
            facts: [{zh, en, tier?}], inferences: [...], gaps: [...], flags: [...],
            changes: [{code, text_zh, text_en, steps: [{argv, command, ask_human, cost_usd, seconds}],
                       cost_usd, cost_text_zh, cost_text_en, seconds, ask_human, outcome_zh, outcome_en}],
            status, agent_hint_en?}],
 note_zh?, note_en?}
```

`--run <id>` of a run whose folder an on-demand update later replaced (`fetch-docs`, or phase 2 of `screen`) explains
the update, the run the folder's results and ledger now belong to: `run.run_id` is the update's id,
`run.requested_run_id` the id asked for, and `note_zh` / `note_en` say so. The replaced run's results stay in
`results.v<N>.json`.

- `argv` / `command` never contain the idea text (screens use `--from-run RUN_ID` or `--idea-of RUN_ID`).
- A fresh screen (`--idea-of`) carries every option of the explained run that differs from the defaults
  (`--no-translate`, `--sieve none|PATH`, `--shells`, `--reads`, `--rank`, `--keywords*`, the filters, and
  `--idea-en` when the run's English sentence was supplied by the caller: quickstart or `screen --idea-en`), so it
  asks the same questions and reuses every cached answer; it always starts with a free `--dry-run` step. `--from-run` steps
  carry the run's `--sieve` (the idea's own sieve when the change edits it).
- A dry run has no L1 answers to reuse: its steps are fresh dry runs (`screen --idea-of <dry run id> ... --dry-run`;
  `--idea-of` also takes a dry run's id or output directory), never `--from-run`.
- Prices include the re-asking a sieve edited after the run causes: a changed L2 question (`params.l2_question_sha`)
  or changed excerpt terms (`params.sieve_terms_sha`: seed_terms, keywords) re-read step 2 (as much as the run spent
  on it; 「可能 … 最多约」 for a run made before those values), an approved new idea_en asks step 1 again on a fresh
  screen. The step's `--budget` covers it and the change text says so. `sieve set` / `sieve check` warn the same way
  (`l2_reprice`).
- `ask_human` is true for every paid, networked or pinning step, and for every screen that is not a dry run. Run a
  step only when `ask_human` is false or the human said yes.
- Exit: 0 explained (also from files only), 1 unknown run / choose_run / no runs / a target not found or ambiguous, 3
  the run's files are unreadable.

## When the store is busy

`why` waits at most `--wait` seconds (default 5) for the database. If a screen or a sync holds it, the answer comes
from the run's files: `funnel.jsonl.gz` (every pre-L1 stage and the L1 / L2 answers) and `results.json`; status
`partial_files_only`, exit 0, and the text says 「筛选正在运行（数据库被占用）：先按运行记录回答，第二步的细节稍后再问。」

## The run ledger (`funnel.jsonl.gz`)

Written into every run's output directory (dry, partial and budget-exhausted runs too). Line 1 is the header
(`format: jevscreen.funnel/1`, run_id, idea, idea_key, status, started_at, market_as_of, the filters, shells version,
ST list). Then one line per universe company: `k` company key, `id` security id, `n` name, `m` market cap in whole
USD, `s` stage (`null_mcap | below_min_mcap | below_min_volume | other_country | shell | no_description | l1_sent |
l1_loaded | l1_not_sent`), `r` shells rules, `kept`, `f` marks, `e` evidence (pattern id + offsets, never text), `l1`
{lab, p: [core, adjacent, unrelated, insufficient], ok, src}, `l2` {lab, st, pp, ev, src}, `x` the filter a forced
sieve company failed. About 0.6-1 MB per run at the full universe.

**Personal use only.** The ledger is a near-complete copy of the universe (names and whole-USD market caps from the
TradingView scanner, gray-private; docs/DATA_RULES.md), kept with the run: never share or publish a run folder
(report.md's Licence section says so too). Delete old run folders under `<home>/screens` when you no longer need
them; `why` then cannot explain those runs.
