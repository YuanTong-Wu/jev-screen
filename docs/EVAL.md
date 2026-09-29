# The open evaluation set (`jevscreen eval`)

How accurate is a screen? This folder of labelled ideas lets anyone measure it the same way and reproduce the number.

## What is in it

`evals/ideas/<id>.json`, one idea per file (format `jevscreen.eval/1`):

| field | meaning |
|---|---|
| `id` | lower-case letters, digits and dashes; one file per id |
| `idea` / `idea_en` | the idea as a person would type it, and the English sentence the Jev questions use |
| `type` | `product_category`, `supply_chain`, `geography`, `customer_segment`, `technology` or `other` |
| `markets`, `countries`, `min_mcap_usd` | where the idea points (ISO-2 codes) and the screen's filters (default floor $1B); `eval run` screens only `countries`, or `markets` when `countries` is null |
| `labels[]` | `security_id` (EXCHANGE:SYMBOL), `name`, `label`, `must_include`, `source_url`, `note`, `by`, `reviewed`, `checked` |

- `idea_en` is required when the idea is not English, so every machine asks Jev the same question.
- `label`: **right** (the company's official filing or issuer IR material states the business the idea names), **edge**
  (related but not stated: see the policy below), **wrong** (another role, such as a customer or buyer, or no such
  business).
- `must_include`: the list is incomplete without this company (recall). Only on a `right` label.
- `security_id`: TradingView's `EXCHANGE:SYMBOL`, as the store and a screen's result write it (KOSPI and KOSDAQ
  lines are `KRX:`, Hong Kong symbols have no leading zeros: `HKEX:700`). `KOSDAQ:` / `KOSPI:`, `HKG:`, `TYO:` and
  `HKEX:0700` are read as the store's form. When the store knows the label's line, a screen that shows the company's
  other line (a dual listing) still matches it; labels the store does not know are listed in the report: fix them.
- `source_url`: an **official** filing (SEC, CNINFO / SSE / SZSE / BSE China, HKEXnews, EDINET, DART, MOPS, BSE India)
  or the company's own investor-relations page. Never TradingView or Yahoo text: that is gray-private and is not
  quoted or linked here. `jevscreen eval check` refuses such a label.
- `note`: the labeller's own words (at most 300 characters), never a copied profile.
- `checked`: `filing_read` means the annual report, formal filing or exchange disclosure itself was read;
  `official_ir_read` means the issuer's IR release, investor presentation or official company announcement was
  read; `search_summary` means only a search-engine summary was available. This field records what was actually
  opened, not whether the maintainer agreed with the label. The report counts the latter two classes separately.
- `diversified` (optional, right or edge): the company is broader than the idea and the idea's line may be a small
  part of it. `evidence_at` (optional, at most 200 characters): where in the cited filing the statement is (page or
  section, never a long quote). `unresolved` (optional, only with `checked: search_summary`): no official source has
  established the label yet. `reviewed_by: owner` (only with `reviewed: true`): the owner decided this label.
- `by`: `ai` (a careful AI reader), `ai-adjudicated` (two blind AI labellers; where they disagreed, a third AI
  read the filing and decided) or `human`; `reviewed`: whether the maintainer checked it. Reports count
  unreviewed labels (and those `checked: search_summary`), overall and per idea; treat numbers built on them as
  provisional.

## Label policy (2026-09-28)

1. **Diversified companies.** A company is **right** when its latest annual report or statutory filing explicitly
   states that it supplies or offers the idea's product or service: a named product line, segment or business that
   serves the idea's target, even when that line is a small share of revenue. Mark it `diversified: true` so reports
   can show it, and record where the filing says it (`evidence_at`).
2. It stays **edge** when the filing shows only a generic capability ("can be used in ..."), an adjacent product (a
   component, material or tool around the idea's product), an outlook or plan (R&D, pilot, samples, planned
   capacity), or does not name the idea's specific target.
3. A label decided by the owner is `reviewed: true, reviewed_by: owner`; every other label keeps its AI status.
4. Scores: report **strict** P@k (right only; the headline) and **lenient** P@k (right + edge) together. A list with
   fewer than k rows is scored over the n rows it shows (P@min(k, n)) and n is printed beside it. Must-include
   recall is reported separately and is never folded into precision.

## Commands

```bash
jevscreen eval check                                   # free: every eval file is well formed
jevscreen eval score <run_id> --idea <id> [--json]     # free: one finished screen against one labelled idea
jevscreen eval run --budget-each 0.5 --budget-total 3 [--ideas a,b]   # PAID: screen each idea (Jev), score them
```

Two accuracy levers are off in `eval run` by default so that numbers stay comparable; pass them to `eval run` to
measure one: `--l2-constraints` (an explicit answer also needs the idea's end market, place or role stated in the
same text; one cheap extra question over the explicit companies) and `--shortlist` (the rows in the product's
order: the main list, L1 core + L2 explicit, then the to-confirm section). `screen` lists the main list and the
to-confirm section by default since 2026-09-29 (the list is never padded to 10; `screen --no-shortlist` keeps the
old single list); `eval run` still screens the bare single list unless `--shortlist` or `--product-flow` names it,
so the full list's numbers stay comparable with earlier runs. The main list's own numbers are scored for every run
either way (below). Two more fetch the official text on demand after each idea's screen, then re-read
the changed rows (update pass, L1 $0, at most $0.05 of the idea's budget): `--fetch-profile-only-topn N` (the top-N
rows that read only a profile: BSE, DART, CNINFO, EDINET in that order, one at a time, stopping at the first site
that refuses access; CNINFO / DART / EDINET read deep) and `--deepen-official-topn N` (the top-N rows with a short
CNINFO / DART / EDINET report re-read deep); `--topn-fetch-time` caps each fetch (default 300 s). The fetch itself is
free but live (polite, official sites). Deep texts are kept apart (their own rows, read only by the top-N update
pass), so a run without the levers reads what it read before; BSE / MOPS / SEC reports fetched for profile rows are
ordinary rows that later runs read. The update pass keeps `--read-offset`.
`eval run --from-run IDEA=RUN[,IDEA=RUN]` (or a JSON file mapping idea ids to run ids) screens the ideas as a
`--from-run` of the given baseline runs: their stored L1 answers are reused ($0). Before any spend, every selected
idea must be mapped, every key must be a selected idea and each base run must hold L1 answers for that idea, else
the eval stops, so an arm never re-prices L1. A score whose L2 inputs differ from its base run's (the store changed
in between) carries `store_drift` and a warning. `eval score RUN --idea ID --shortlist` re-scores a finished run in the product's order
(main list first), for free; the main list's own strict and lenient precision with its size n_main and the
must-include companies it holds are reported for every scored run, with or without the flag. Each score (and the report header) names the levers its run was made with. A
`screen --from-run` keeps the base run's levers unless the command line names them (`--no-l2-constraints`,
`--no-shortlist`, `--judge none`, `--no-lang-terms`, `--no-second-search` turn them off); `eval run --from-run`
passes every lever explicitly, so a lever it does not name is off and a control arm never inherits one.

`eval run` screens with each idea's own English sentence (never the local translation model), floor, **its
`countries`, or its `markets` when `countries` is null**, and **no sieve** (it measures the system, not a
calibration). The filter is matched as `screen --countries` matches it: an ISO-2 code is TradingView's country of
incorporation, so a Hong Kong-listed company incorporated in China needs `CN`, not `HK`. `eval check` refuses a
market code the filter does not know. The report lists each idea's filter and, when the store can be read, the
**right labels outside it** (the list can never show them: widen `markets` or fix the label).

**Runs before 2026-09-28 used `countries` only.** Most ideas have `countries: null`, so every `eval run` before this
fix (the five-idea pilot, the 19-idea baseline and the `--l2-constraints` A/B in the maintainers' experiment log) screened
the whole store (about 10k described companies worldwide): L1 read companies from every market and L2 read their
filings (india-ems's L2 inputs were mostly Chinese annual reports). Their numbers are not comparable with runs made
after the fix; compare like with like, or rerun. `--budget-each` and `--budget-total` have no default.

Two retrieval levers are also off by default: `--lang-terms` (annual-report search terms for every document
language, from the local keyword model even when `idea_en` is given; generic words and the two-character pieces of a
long Chinese compound count as weak terms; only filings whose excerpts change are read again) and `--second-search`
(companies L1 judged core whose annual-report excerpts L2 found insufficient get one more L2 read over other passages
of the same filing that match the widened terms; a yes there lists them as related at most, marked as the system's
inference, never high confidence). To measure them without asking layer 1 again, `eval run --from-run SPEC` (also spelled `--from-runs`) screens
every idea `--from-run` its base run (SPEC: earlier `eval run` folders or `id=RUN_ID` pairs): an idea without a base
run, or whose base run the database does not hold for that idea, stops the run before anything is sent, and a screen
that read layer 1 again stops it too (that idea is recorded as `stopped` with its run and cost). With `--from-run`
every lever is passed explicitly, so a control arm never inherits a lever its base run was made with. Both
retrieval levers need the local keyword model's terms for every idea (its cache `<home>/keywords` or the model,
which needs torch and transformers): without them `eval run` stops before sending, and a `screen` run warns and
is marked partial, since the lever would only have fallen back to the idea's Latin acronyms.
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

By default it uses the store as it is: no download, no on-demand annual-report fetch unless a top-N lever above
asks (run `jevscreen fetch-docs <run>` on a run first if you want its reports read), so coverage of the store changes
the score. Record the store's coverage
(`jevscreen coverage`) with any published number.

## The numbers

- **main list strict P@min(10, n_main)** (the headline since the owner decision of 2026-09-29, "如果凑不出十家，那就
  凑不出呗": the product lists only the confirmed companies and never pads the list to 10): the rows of the confirmed
  tier (L1 core + L2 explicit, or judge tier A; a run without tiers is scored by the tier each row would get,
  `shortlist.in_main`; a row a scope answer moved down, or one its page marks borderline, `shown_gap`, is never in
  it), their first ten, the share labelled right
  among the labelled ones. Across ideas the headline is **pooled** over the main-list rows (right / labelled over
  every idea's first ten main rows: the same kind of number as `tier_precision`'s explicit_core, so a holdout check
  compares like with like); the **mean over ideas** is printed beside it (there an idea with one main row weighs as
  much as one with ten, and an idea with an empty main list is left out). Beside them: **n_main**
  (rows scored; the report also gives all main rows and the ideas whose main list was empty), the lenient value and
  **must-include recall in the main list** (found in the whole main list / must-includes). A small n_main is not a
  failure: it is what the product shows. The live `eval run` line starts with it (`main list strict P@min(10,
  n_main) … (n_main …, must-include in it …/…)`), then the full list's numbers.
- **full list strict P@10 / P@40**: of the labelled companies in the top 10 / 40 of the whole ranked list, the share
  labelled right, as before.
  A list shorter than k is scored over its n rows (P@min(k, n)); the report prints n beside each value. Unlabelled companies are
  counted apart (`unlabelled`, `coverage` = labelled / shown), never as right or wrong; label them and score again.
  The live `eval run` line says `provisional` whenever any top-ten company is unlabelled or any eval label
  remains AI-unreviewed or checked only from a search summary; read its `coverage` before the fraction.
- **lenient P@10 / P@40**: edge counts as right; always shown next to strict. The report also counts how many top-10
  rights are `diversified`.
- **must-include recall**: the share of must-include companies anywhere in the list, and in the top 10 (the report's
  "Two lists" line also gives the pooled count, found in the top 10 / all must-includes).
- **estimated P@10** (strict and lenient): the top 10 with every unlabelled row credited at the measured precision
  of its evidence tier (`explicit_core`, `explicit_adjacent`, `profile_capped` = a profile-only row whose L2 read was
  explicit, capped at related, `partial_core`, `partial_adjacent`, `other`), pooled over the run's labelled rows at
  every rank; a tier with no labelled row is credited at the pooled precision of all tiers. The report's tier table
  shows each tier's labelled n and precision. It is an estimate: it assumes unlabelled rows are like the labelled
  rows of their tier.
- **high-confidence tier P@min(10, n_high)** (when the rows carry shortlist tiers: `--shortlist`, `--product-flow`,
  `eval score --shortlist`): the same numbers as the main list under their earlier name (`p_high` in the aggregate,
  `shortlist` in a score), kept so earlier reports compare.
- Means over the ideas whose screen finished, overall and per idea type; the total Jev cost.

## The numbers in the README (2026-09-29)

The README's accuracy table is the **main list strict** number above, pooled over 24 ideas (19 used while tuning, 5
held out), each scored twice: on the no-padding main list the screen showed, and on the same list after the user's AI
reviewed it (`quickstart`'s `agent_review` deck, recorded with `jevscreen judge`: a to-confirm row answered yes,
explicit moves into the main list; a main row answered no moves out). The review was answered by Claude Opus; the
same decks answered by Claude Sonnet and Claude Haiku were replayed through the same judge logic to measure a looser
reviewer. Every main-list row on both sides is labelled; the labels are AI filing reads (single AI labellers, and for
the later rows two independent AI labellers with an AI arbiter), none reviewed by a person yet, so the numbers are
provisional, and the labelled ideas are not yet in `evals/ideas/`. The full ranked list's top 10 was scored the same
way on the 19 tuning ideas (about 65% strict).

## The product flow (`--product-flow`, off by default)

`eval run` alone screens the bare system (`sieve: none`): no facets, no facet layer, no idea-wording defaults, no
tiers, which is not the list a user gets. `eval run --product-flow` screens each idea the way the product lists it,
from the idea text only (jevscreen.productflow): facets (category, target, type) derived deterministically from
`idea_en` with `constraints.derive` (its end market, else its place); the idea-wording defaults the product records
from `--facets implied_no` (role.target_only for a technology idea with a target, role.buyer, role.holding; at most
two), **always with effect demote**, so a row the facet layer reads as that kind moves down and is never removed; the
facet layer on (about $0.01 an idea); the shortlist tiers on; your AI's review layer off. The sieve lives in memory
(nothing is written to `<home>/sieves`) and has no rules, so the L2 question is unchanged: from a baseline
`--from-run` the arm pays only for the facet questions. Scores carry `product_flow` (facets, defaults) and the lever
name `product-flow`; it wins over `--no-shortlist`. The report gives both lists: the full list's strict / lenient
P@10 and estimated P@10, and the high tier's P@min(10, n_high).

Inside the to-confirm tier the order is: explicit rows, then profile-only rows capped at related whose L2 read was
explicit (84% right in the 2026-09-28 labels, against 52% for related + core), then the other related rows; rows
moved down (judge tier C, a scope demotion) stay last. This applies to every shortlist (screen `--shortlist`,
`eval score --shortlist`), free.

## The item-by-item check (`--judge`, off by default)

`eval run --judge atomic3` (or `single10`) turns on a third lever: every L2-verified company is read again over the
same L2 text with typed questions (atomic3: what it offers (its own product, a component, only inside its own
assembly, an adjacent product), its role (supplier, buyer or user, upstream, channel, minority holder, another link;
a consolidated subsidiary counts as the company) and, when the idea names one, the target (named, one of several,
other targets only, not stated); single10: one ten-class question, about a third of the cost). A fixed rule makes
tiers: A = L2 explicit + own product + supplier + target named or one of several; C = counter-evidence the text
states (at probability 0.6 or more), moved to the end but never removed, marked as an inference and sent to the
user's AI review; B = the rest (not stated / unclear never demotes). The list is ordered by tier, then by the old
score. `--from-run DIR|FILE.json|id=RUN,...` screens each idea from a finished base run (earlier `eval run` folders, JSON
maps or `idea=RUN_ID` pairs): L1 answers are reused and never read again (an idea without a base run, or whose base
run does not hold L1 answers for that idea, stops the run before anything is sent), so an A/B pays only for the
lever's questions plus any L2 text that changed. A layer that was
skipped (budget, Jev unavailable) keeps the old order and is counted as `judge-ARM skipped`, never as the arm. With
`--shortlist` too, the high tier is tier A (an inference, labelled so on the page), not L1 core + L2 explicit. Only
the first 150 verified companies are read; the rest are `not_read` (tier B), which does not make the run partial.

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
