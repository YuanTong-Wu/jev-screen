# The open evaluation set (`jevscreen eval`)

How accurate is a screen? This folder of labelled ideas lets anyone measure it the same way and reproduce the number.

## What is in it

`evals/ideas/<id>.json`, one idea per file (format `jevscreen.eval/1`):

| field | meaning |
|---|---|
| `id` | lower-case letters, digits and dashes; one file per id |
| `idea` / `idea_en` | the idea as a person would type it, and the English sentence the Jev questions use |
| `type` | `product_category`, `supply_chain`, `geography`, `customer_segment`, `technology` or `other` |
| `markets`, `countries`, `min_mcap_usd` | where the idea points and the screen's filters (default floor $1B) |
| `labels[]` | `security_id` (EXCHANGE:SYMBOL), `name`, `label`, `must_include`, `source_url`, `note`, `by`, `reviewed` |

- `idea_en` is required when the idea is not English, so every machine asks Jev the same question.
- `label`: **right** (the company's official filing states the business the idea names), **edge** (related but broad,
  early or a small part of the company), **wrong** (another role, such as a customer or buyer, or no such business).
- `must_include`: the list is incomplete without this company (recall). Only on a `right` label.
- `security_id`: TradingView's `EXCHANGE:SYMBOL`, as the store and a screen's result write it (KOSPI and KOSDAQ
  lines are `KRX:`, Hong Kong symbols have no leading zeros: `HKEX:700`). `KOSDAQ:` / `KOSPI:`, `HKG:`, `TYO:` and
  `HKEX:0700` are read as the store's form. When the store knows the label's line, a screen that shows the company's
  other line (a dual listing) still matches it; labels the store does not know are listed in the report: fix them.
- `source_url`: an **official** filing (SEC, CNINFO / SSE / SZSE / BSE China, HKEXnews, EDINET, DART, MOPS, BSE India)
  or the company's own investor-relations page. Never TradingView or Yahoo text: that is gray-private and is not
  quoted or linked here. `jevscreen eval check` refuses such a label.
- `note`: the labeller's own words (at most 300 characters), never a copied profile.
- `by`: `ai` (a careful AI reader) or `human`; `reviewed`: whether the maintainer checked it. Reports count
  unreviewed labels (and those `checked: search_summary`), overall and per idea; treat numbers built on them as
  provisional.

## Commands

```bash
jevscreen eval check                                   # free: every eval file is well formed
jevscreen eval score <run_id> --idea <id> [--json]     # free: one finished screen against one labelled idea
jevscreen eval run --budget-each 0.5 --budget-total 3 [--ideas a,b]   # PAID: screen each idea (Jev), score them
```

`eval run` screens with each idea's own English sentence (never the local translation model), floor and countries
and **no sieve** (it measures the system, not a calibration). `--budget-each` and `--budget-total` have no default.
**The total is what the human approves**: a run over N ideas can cost up to N x `--budget-each`, and it stops before
an idea whose budget could take the spend past `--budget-total`, so ask "OK to spend up to $X on these N ideas?"
and pass exactly that X (above $1 it needs their explicit yes to that number: AGENTS.md hard rule 5). Each idea
usually costs $0.2-0.4 on a full store; most answers are then cached. The report and the scores go to a new folder
`<home>/evals/<timestamp>/` (`report.md`, `scores.json`), also when a screen fails or the run stops early.

Only screens that finished (`ok` / `partial`) enter the means. A screen whose budget ran out, a busy or unavailable
Jev, a screen that failed and an idea not run are listed apart (the `status` column of `report.md`, `left_out` in
`scores.json`). Exit codes: 0 ok; 1 an eval file is broken or one idea's screen failed (the others are still
scored); 3 the database is locked; 4 another process is using Jev (the run stops: run it again when that process
finishes, answers already paid for come from the cache); 5 a screen's budget ran out or `--budget-total` stopped the
run; 6 Jev is unavailable (missing key, no credit); 130 interrupted (Ctrl-C: the report is still written).

It uses the store as it is: no download, no on-demand annual-report fetch (run `jevscreen fetch-docs <run>` on a run
first if you want its reports read), so coverage of the store changes the score. Record the store's coverage
(`jevscreen coverage`) with any published number.

## The numbers

- **P@10 / P@40**: of the labelled companies in the top 10 / 40, the share labelled right. Unlabelled companies are
  counted apart (`unlabelled`, `coverage` = labelled / shown), never as right or wrong; label them and score again.
- **lenient**: edge counts as right.
- **must-include recall**: the share of must-include companies anywhere in the list, and in the top 10.
- Means over the ideas whose screen finished, overall and per idea type; the total Jev cost.

## Noise

Jev answers are cached per question and text, so scoring the same run twice, or rerunning the same screen, gives the
same list. Fresh reads of the companies near the boundary come from `eval run --read-offset N` (reads N..N+K-1 of
the band): the spread of P@10 over a few offsets is the run-to-run noise to quote beside any difference between two
configurations. A change is worth keeping only when it raises P@10 by more than that spread without lowering
must-include recall.

## Adding an idea

1. Screen it (`jevscreen quickstart` or `screen`), then open each company in the top 40 in its **official** annual
   report or filing.
2. Label what the filing says, in your own words, with the filing's URL, under the `security_id` the screen's result
   shows. Mark companies you expected but did not see as `must_include` (again with their filing). A non-English idea
   also needs `idea_en`: one English sentence, with no company the idea does not name.
3. `jevscreen eval check` (it also refuses an id used by two files), then open a pull request. Labels by an AI stay
   `reviewed: false` until the maintainer has checked them.
