# Shells filter (排除壳公司) and risk marks

`jevscreen screen` removes blank-check companies before layer 1 and marks A shares under an exchange risk warning.
It is free, local and deterministic, and every decision can be explained by `jevscreen why` (see [WHY.md](WHY.md)).
Code: `src/jevscreen/shells.py`. Rule version: `SHELLS_VERSION = 1` (recorded in every run's params and ledger).

## Rules

| id | effect | when |
|---|---|---|
| `spac` | **dropped** before L1 | the name looks like a blank-check vehicle (`… Acquisition Corp/Inc/Ltd/Holdings`, `SPAC`, `… Merger Corp`, `special purpose acquisition`) **or** the first 300 characters of a profile say it *is* one ("is a blank check company", "operates as a blank check company"; "formerly a blank check company" does not count) **and** TTM revenue is below $1M or missing **and** the TradingView industry is Financial Conglomerates, Finance/Rental/Leasing or missing |
| `spac_like` | mark only | a name / profile hit that fails the revenue or industry guard: an operating company that kept "Acquisition Corp" in its name, or a stale profile of the SPAC it merged with |
| `st` / `star_st` | mark only | the newest CNINFO stock list names the A share `ST…` / `*ST…` (`S*ST` counts as `*ST`) |

- `--shells keep` screens the shells too (they are then marked `SPAC` in the tables). Default `drop`.
- **Protected companies are never dropped**: every company the idea's sieve names (card answers and pins,
  `should_pass`, `should_fail`, `sieve pin`). The filter must not cancel a human's answer.
- ST / *ST never changes the rank. When a row in the top 20 carries it, the console and the top of report.md print:
  「注意：交易所风险警示（ST）——公司财务或经营有问题；*ST 表示可能退市。排名不变，这是事实提示。」
- The ST list is joined to lines through `identifiers.cninfo_orgid` (never by re-mapping exchange codes). A missing
  list, or one older than 14 days, is reported as a gap when the universe has A shares; it is never a clean bill.
- A `--from-run` base made before this filter existed is inherited as `keep`, so its L1 answers still line up.

## Outputs

- funnel: `shells_dropped` (only when it is not 0), one line in report.md, the dropped rows in `shells_dropped.csv`
  (rule, pattern id, industry, TTM revenue; never the profile text) and in `results.json` `gaps.shells_dropped`.
  **Personal use only**: `shells_dropped.csv` carries TradingView industry and revenue (gray-private) and the run
  ledger `funnel.jsonl.gz` the whole universe; keep the run folder local, never share it.
- rows: `flags` (`st`, `star_st`, `spac`, `spac_like`; omitted when empty); results.csv gains a trailing `flags`
  column (`;`-joined).
- `results.json` `shells`: mode, version, dropped, kept_protected, kept_mode, flag counts among the companies that
  reached L1, the ST list used (`snapshot_id`, `fetched_at`, `stale`) and `st_gap`.
- `funnel.jsonl.gz` (the run ledger): per company `r` (rules that matched), `kept` (`protected` / `keep`), `f`
  (marks), `e` (pattern id + character offsets, never text).

## Measured on the maintainer's store (2026-09-27, read-only)

| floor | universe | `spac` dropped | `spac_like` | `st` | `star_st` |
|---|---|---|---|---|---|
| ≥ $200M | 20,573 | 174 | 3 | 105 | 82 |
| ≥ $1B | 10,249 | 0 | 0 | 17 | 7 |

Timing: facts query 0.04 s, ST list 0.06 s, rules 0.16 s at ≥ $200M. The drop saves about 174 L1 items
(≈ $0.006) per run at ≥ $200M and nothing at ≥ $1B: its value is an honest funnel, `why`, and the ST warning.

## Deferred

tiny float, no trades in 10 days, pooled funds, BDCs, OTC shells without business, duplicate lines, stale prices,
dollar-volume rules, KR 관리종목 / JP 監理 marks, `--shells strict`.
