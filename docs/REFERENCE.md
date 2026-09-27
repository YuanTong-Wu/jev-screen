# jev-screen reference

The full reference for people who drive jev-screen by hand, and for contributors. The short version is the
[README](../README.md); the step-by-step path for AI agents is [AGENTS.md](../AGENTS.md); every JSON format is in
[AGENT_API.md](AGENT_API.md). Deeper rules: [DATA_RULES.md](DATA_RULES.md) (data and calibration),
[SIEVE.md](SIEVE.md) (what your AI may write), [WHY.md](WHY.md) (`jevscreen why`), [SHELLS.md](SHELLS.md) (the
blank-check filter), [OPEN_PACK.md](OPEN_PACK.md) (the open data pack).

Measured facts quoted below were taken on the dates given; they drift as sources change.

## The data layer

A local DuckDB data layer that the Jev screening steps read from:

1. **Universe**: every primary common stock worldwide plus primary depositary receipts, one row per company.
2. **Fundamentals**: latest TTM/FY values and annual history.
3. **Descriptions**: business descriptions, so ideas can be matched to companies.
4. **Official annual reports**: the business section of each company's latest annual report from SEC EDGAR (US),
   CNINFO 巨潮资讯 (China A-shares), EDINET (Japan), OpenDART (South Korea), MOPS (Taiwan) and BSE (India), used as
   layer-2 evidence.
5. **Coverage**: what is present and what is missing, by region, source and licence tier.

Measured facts, as of 2026-09-26: one scanner request returns ~49.4k primary common stocks, and a second returns
270 primary DRs (needed for BABA, PDD, ARM, NIO and similar). History columns (`*_fy_h`) come back in local currency,
not USD.

## Install

```bash
pip install -e '.[pdf]'   # Python >= 3.10; duckdb, plus pypdf for annual-report PDFs (China, India, Taiwan)
jevscreen --help
```

Data goes under `./data` by default (git-ignored). You can override paths and settings with environment variables:
`JEVSCREEN_HOME`, `JEVSCREEN_FD_DUCKDB`, `JEVSCREEN_USER_AGENT`, `JEVSCREEN_MIN_INTERVAL_S`,
`JEVSCREEN_SEC_USER_AGENT`, `JEVSCREEN_EDINET_API_KEY`, `JEVSCREEN_OPENDART_API_KEY`.

Optional, for automatic translation of non-English ideas (`jevscreen keywords`, `screen`): `torch` and
`transformers` with the Qwen3.5-4B model already in the local Hugging Face cache. The model is loaded offline
(`HF_HUB_OFFLINE=1`, `TRANSFORMERS_OFFLINE=1`), so nothing is downloaded; without it, screening still works and warns.

### SEC User-Agent

SEC EDGAR requires a declared User-Agent of the form `<name> <email>`. `sync-sec` reads it from the environment
variable `JEVSCREEN_SEC_USER_AGENT`, or else from the file `data/sec_user_agent` (under `JEVSCREEN_HOME`; `data/` is
git-ignored). Without one, `sync-sec` refuses to run. The value is never printed, logged, journaled or stored in
snapshots: wherever a request is recorded it appears as `<sec-user-agent>`. Do not commit it.

### EDINET and OpenDART API keys

`sync-edinet` and `sync-dart` need your own free API key. `sync-cninfo` needs none.

| Source | Register (free) | Put the key in | or set |
|---|---|---|---|
| EDINET (Japan, FSA) | EDINET API v2 sign-up: <https://api.edinet-fsa.go.jp/api/auth/index.aspx?mode=1> (you get a `Subscription-Key`) | `data/edinet_api_key` | `JEVSCREEN_EDINET_API_KEY` |
| OpenDART (Korea, FSS) | <https://opendart.fss.or.kr/> -> 인증키 신청 (you get a 40-character `crtfc_key`, ~20,000 calls/day) | `data/opendart_api_key` | `JEVSCREEN_OPENDART_API_KEY` |

The files live under `JEVSCREEN_HOME` (default `data/`, which is git-ignored): one line, the key only, e.g.
`printf '%s' '<your key>' > data/edinet_api_key && chmod 600 data/edinet_api_key`. **Never commit a key** and never
paste it into an issue or a report. Without a key the command refuses before sending any request (exit 1) and says
where to put it. Keys travel in request URLs, so every printed line, journal note, error text and cooldown marker
shows `<edinet-api-key>` / `<opendart-api-key>` instead of the key.

### OpenRouter key (Jev) and the optional FinanceDatabase file

Jev calls read the OpenRouter key at call time: the environment variable `OPENROUTER_API_KEY` if set; else, if
`JEVSCREEN_OPENROUTER_KEY_FILE` is set, the file it names and nothing else (a named but missing file is an error,
not skipped); else the file `data/openrouter_api_key` (under `JEVSCREEN_HOME`, git-ignored; one line, the key only,
`chmod 600`). There is no other fallback. `import-fd` opens
`--fd-duckdb PATH` if given, else `JEVSCREEN_FD_DUCKDB`, else `data/financedatabase.duckdb` (read-only).
`jevscreen quickstart` does not need that file: it downloads FinanceDatabase's `equities.bz2` (about 15 MB, one
request, from GitHub's raw.githubusercontent.com, else jsDelivr) and imports the profiles from it. That is
gray-private data (personal use only) and needs the recorded gray-sources consent first; `--fd-file PATH` imports a
copy you downloaded yourself. Details: docs/DATA_RULES.md "Quickstart and the result page".

## Commands

The main flags of each command; `jevscreen <command> --help` lists them all.

| Command | What it does |
|---|---|
| `jevscreen init` | Create the DuckDB store and register sources with their licence tiers |
| `jevscreen refresh-universe [--with-history] [--history-batch N] [--after-block]` | Pull the universe (stocks + DRs); optionally pull annual history in local currency. Securities are deactivated only after a complete pull (otherwise the run is `partial`) |
| `jevscreen import-fd [--fd-duckdb PATH]` | Import descriptions from a local FinanceDatabase DuckDB (opened read-only) |
| `jevscreen crawl-descriptions [--limit N] [--rate R] [--workers N] [--countries CN,HK] [--min-mcap X] [--all] [--after-block]` | Crawl TradingView symbol pages for descriptions. `--rate` defaults to and is capped at 4.4 req/s (higher values are clamped with a warning); `--workers` concurrent fetches (default 3, 1..4) never raise the rate above `--rate`; `--countries` / `--min-mcap` limit the crawl (e.g. the China gap fill). Pages are streamed and the connection closes right after the FinancialProduct JSON-LD (~90 KB of ~256 KB cap); companies already covered by SEC annual-report text are skipped (re-checked at every flush). The first 403/429/challenge stops the run: one `BLOCKED url=... status=... reason=...` line, exit 2. SIGTERM flushes and exits 130 like Ctrl-C. `--all` re-crawls pages that already have a description. Progress is written every 50 pages in a short session |
| `jevscreen sync-sec [--limit N] [--refresh] [--forms 10-K,20-F] [--after-block]` | Map universe lines to SEC CIKs (`identifiers`) and fetch annual-report business sections (10-K Item 1, 20-F Item 4) into `documents`. Needs the SEC User-Agent (above); about 6-7 req/s, shared across `www.sec.gov` and `data.sec.gov`. `--refresh` re-fetches companies that already have a document |
| `jevscreen sync-cninfo [--limit N] [--kind summary\|full] [--min-mcap X] [--codes 300386,600519] [--workers N] [--since DATE] [--per-company] [--refresh] [--after-block]` | Map A-share lines (SSE/SZSE) to CNINFO orgIds (`identifiers` `cninfo_orgid`) and fetch the latest annual report PDF's business section into `documents`. `--kind summary` (default) reads the short 年度报告摘要 (falls back to the full report when there is none), `--kind full` the full 年度报告. Fast mode (default): one bulk announcement listing from `--since` (default 13 months ago), queries at 1 req/s, PDFs downloaded by `--workers` threads (default 3, 1..4) at most 4 per second on the PDF host; `--per-company` is the old path, everything sequential at 1 req/s. No key needed |
| `jevscreen sync-edinet [--limit N] [--min-mcap X] [--codes 7203,6758] [--refresh] [--after-block]` | Map Japanese lines to EDINET codes (`edinet_code`) and fetch 事業の内容 of the latest 有価証券報告書. Needs your EDINET key (above); 1 req/s |
| `jevscreen sync-dart [--limit N] [--min-mcap X] [--codes 005930,000660] [--refresh] [--after-block]` | Map KRX lines to DART corp codes (`dart_corp_code`) and fetch '1. 사업의 개요' of the latest 사업보고서. Needs your OpenDART key (above); 2 req/s |
| `jevscreen sync-mops [--limit N] [--min-mcap X] [--codes 2330,6223] [--mode basic\|annual] [--full-pass] [--refresh] [--after-block]` | Taiwan (TWSE / TPEx), no key. Default `--mode basic`: the short MOPS 主要經營業務 field (1 request per company). `--mode annual`: the 營運概況 / 業務內容 section of the latest 股東會年報 from TWSE's e-document server (3 requests and ~5 MB per company, ~22 s). That server's robots.txt disallows crawlers, so `--mode annual` needs `--codes` (the companies you need) or `--full-pass` (all ~953 companies, ~4.5 GB, 2.5-6 h: at most one slow pass a year) |
| `jevscreen sync-bse [--limit N] [--min-mcap X] [--codes 500325,TCS] [--workers N] [--refresh] [--after-block]` | Map Indian lines (BSE/NSE) to BSE scrip codes (`bse_scrip_code`) and fetch the latest annual report PDF's Management Discussion and Analysis (plus the report's own company overview page) into `documents`. Personal use only; 1 req/s per host, `--workers` PDF downloads (default 2); no key needed. Use `--codes` or `--min-mcap` for the companies a screen needs rather than a full sync |
| `jevscreen keywords "<idea>" [--refresh]` | Translate an idea with the local model (offline, free): prints `idea_en` and keywords per language (`en`, `zh`, `ja`, `ko`) as JSON. Results are cached in `data/keywords/`; `--refresh` generates again. Exit 1 when the model is unavailable |
| `jevscreen coverage [--json]` | Coverage report: companies by region, market cap, TTM revenue, >=3 / >=10 fiscal years, descriptions by source, SEC documents by form, companies with official annual-report text by region and source (SEC / CNINFO / EDINET / DART / MOPS / BSE), licence tiers, freshness, top-20 gaps |
| `jevscreen status` | Row counts per table (including `documents` by source/form, companies with text by source, and `identifiers` by type) and the last runs |
| `jevscreen screen "<idea>" [--min-mcap 2e8] [--min-volume N] [--countries US,JP] [--max-out 40] [--budget 3] [--l2-max 600] [--keywords a,b] [--keywords-zh a,b] [--keywords-ja a,b] [--keywords-ko a,b] [--no-translate] [--retry-uncertain] [--reads 3] [--read-offset N] [--cards 8] [--sieve auto\|none\|PATH] [--from-run RUN_ID\|OUT_DIR] [--l1-new] [--idea-of RUN_ID] [--idea-en TEXT] [--rank label\|ev] [--shells drop\|keep] [--fetch-docs auto\|off] [--fetch-time 120] [--fetch-sources sec,cninfo] [--no-page] [--dry-run]` | Screen an investment idea with Jev (paid calls unless `--dry-run`); see [Screening](#screening). The defaults are a $3 budget and companies from $200M: an AI agent always passes `--budget` (the amount the human approved) and `--min-mcap`. Blank-check shells are dropped before layer 1 (`--shells keep` keeps them; [SHELLS.md](SHELLS.md)). Items near the layer-2 boundary are read `--reads` times and decided on the mean; after the first report the missing annual reports are fetched and the same report updated (`fetch-docs` below); the run ends with up to `--cards` calibration cards and the result page `page.html`; `--from-run` (a run id, its output directory or results.json) reuses an earlier run's parameters and L1 answers (L1 $0); with `--l1-new` the described companies that run has no L1 answer for (e.g. after `crawl-descriptions`) are read by L1 now, everyone else keeps its answer ($0). Exit `0` ok / partial / dry run, `1` bad parameter (non-finite or negative number, unknown country token), `3` database locked, `4` another process is using Jev, `5` budget exhausted before layer 1 finished (partial report still written), `6` Jev unavailable (missing key, HTTP 401/402/403), `130` interrupted |
| `jevscreen fetch-docs [RUN_ID\|latest] [--time 300] [--sources sec,cninfo,bse,mops,dart] [--retry-failed] [--no-update] [--budget 0.05] [--dry-run] [--json]` | Fetch the newest annual report of a screen run's layer-2 companies that read only a profile (free, official sites) and update that run's report (at most `--budget`, L1 $0). `screen` does the same automatically after its first report (`--fetch-docs auto\|off`, `--fetch-time 120`, `--fetch-sources`); see docs/AGENT_API.md "On-demand annual reports". Exit `2` a source was blocked, `3` store busy, `4` source / Jev busy, `5` update budget ran out, `6` Jev unavailable |
| `jevscreen cards [RUN_ID\|OUT_DIR\|latest] [--max 8] [--out DIR] [--json]` | Rebuild or reprint a screen run's calibration cards from its stored results (free) |
| `jevscreen answer ["1要a 2不要c 3不要h 4?"] [--run RUN_ID] [--deck ID\|PATH] [--file answers.json] [--no-apply] [--apply-budget 0.05] [--undo N] [--verbose] [--json]` | Record answers to the cards (要 a/b; 不要 c-g about the evidence, h-k about the company's role: h buyer, i holding only, j upstream parts, k hardware to operators) in the idea's sieve (`data/sieves/<idea_key>.json`), try the rules they point to and screen again from the same run within `--apply-budget` (L1 $0). The printout is in plain words (你：要（年报没写，按你的判断））: its title says whether the new screen finished (重新筛选完成 / 只完成了一部分 / 没有完成) and gives the new run id and the updated page; `--verbose` adds the rule ids, keyword weighting, cost breakdown, trial table and the sieve path. Exit `5` estimate over budget, `6` Jev unavailable, `7` every proposed rule rejected (answers still saved) |
| `jevscreen sieve check PATH\|IDEA` / `sieve show IDEA\|PATH [--json]` | Check or show an idea's calibration file (free); rules in docs/DATA_RULES.md "Calibration (sieve)" |
| `jevscreen sieve new\|set\|add\|remove\|pin\|unpin\|list ... --json` | The fields the human's AI writes in a sieve (idea_en, seed terms, facets, should_pass / should_fail checks; pins only on the human's yes); free, no Jev: [SIEVE.md](SIEVE.md) |
| `jevscreen why TARGET ... [--run RUN_ID\|OUT_DIR\|latest] [--json]` | Why a company (ticker, code or name) is or is not in a run's result, and what would change it (free, read-only): [WHY.md](WHY.md) |
| `jevscreen pack build\|pull\|fetch-open` | Open data pack: see [Open data pack](#open-data-pack) |
| `jevscreen doctor` / `keys` / `consent` | Agent-first setup: see [Using jev-screen with your AI](#using-jev-screen-with-your-ai) |

Every command prints a short JSON summary whose `status` is the adapter's own result. Exit codes: `0` for success,
`1` for an error (including a run stopped by consecutive errors or by a store that stayed locked), `2` when the
provider blocked the run (HTTP 403/429 or a challenge page), `3` when the database stayed locked by another process
for longer than the command waits, `4` when another jevscreen process is already using the same host's rate budget
(`sec.gov`, `www.tradingview.com`, `scanner.tradingview.com`, `cninfo`, `edinet`, `dart`, `mops`, `bse`,
`api.github.com` for `pack pull`, and `openrouter-jev` for `screen` / `answer`; lock files under `data/locks/`), `130` when interrupted (Ctrl-C, or SIGTERM for the crawl and sync
commands). `screen` adds `5` (Jev budget exhausted) and `6` (Jev unavailable). A blocked run stops completely. The tool does not retry it and never works around bot protection.
After a blocked run, the same command refuses to send any request for 24 hours (exit `2`, status `cooldown`)
unless you pass `--after-block`. The cooldown is tracked per command, so a blocked `sync-sec` does not stop
`crawl-descriptions` and vice versa. Besides the `runs` journal, the block is recorded in
`data/cooldown/<command>.json` the moment it happens, so the cooldown holds even if the final database write failed;
the next clean run removes that file.

### Short-session rule (DuckDB locking)

DuckDB lets only one process write to `data/jevscreen.duckdb`, and while that process has the file open other
processes cannot even open it read-only. So jev-screen never keeps a connection open while it waits on the network:

- Long crawls (`crawl-descriptions`, `sync-sec`, `sync-cninfo`, `sync-edinet`, `sync-dart`) work out their queue in one short session, fetch with **no**
  connection open, then open a session every N pages (50 by default) and at the end, stop or Ctrl-C, write the
  batch, and close it. If another process has the lock at a mid-run flush, the batch is kept and retried after
  another N pages; after 5 locked attempts in a row the run stops (`store_locked`) and makes one final, longer wait.
- `coverage` and `status` open a read-only session and wait up to 120 s for a lock to clear. If it does not clear,
  they print a clear "locked by another process" message and exit `3`.
- `refresh-universe`, `import-fd` and `init` hold one write session for their run (about a minute), and wait up to
  600 s for the lock.
- New code must use `store.session(cfg, read_only=..., wait_s=...)`, never a connection that lives for a whole crawl.

## Screening

`jevscreen screen "<idea>"` turns one idea (any language) into a ranked list of a few dozen companies:

0. **Translation** (local, free): when the idea is not English (it contains Chinese / Japanese / Korean characters,
   or mostly non-Latin letters; smart quotes, dashes and accents do not count), the local model (`jevscreen keywords`, Qwen3.5-4B from the Hugging Face cache, offline) turns it into
   an English idea (`idea_en`) and keywords per document language (`en`, `zh`, `ja`, `ko`) before layer 1; results are
   cached per idea in `data/keywords/`. The Jev questions then read `"<idea_en> (original: <idea>)"`: the model sees
   an unambiguous English statement and the exact original. Dry runs translate too and show the result. If the model
   is unavailable the idea is used verbatim, with a loud warning (the old behaviour). English `--keywords` replace
   only the English excerpt terms (the idea is still translated for the questions); `--no-translate` turns it off.
   Note: `idea_en` is part of the paid Jev question text, so regenerating it (`keywords --refresh`, deleting
   `data/keywords/`, a new prompt version or model) re-prices the L1/L2 answers of that idea.
1. **Local pre-screen** (one short read-only session): companies from the `universe` view with
   `market_cap_usd >= --min-mcap` (NULL market cap is excluded and counted), optional 10-day average volume
   `>= --min-volume`, optional `--countries` (ISO-2 codes such as `US,JP`, TradingView country names, or coverage
   regions such as `Europe & UK`; an unknown token is an error). Country tokens match the country of incorporation:
   `CN`/`China` leaves out Cayman- or Bermuda-incorporated Chinese companies, which the regions `Hong Kong` and `US`
   catch by listing venue. Each company
   gets its best description: TradingView profile, else FinanceDatabase, else the SEC short description; at most two
   distinct ones are joined with source tags (up to ~1,800 chars). Companies without any description are reported
   as a gap, not screened.
2. **Jev layer 1** (`fit`): does the description show that the company's current business is `core`, `adjacent`,
   `unrelated` or `insufficient` for the idea? Pass: `core`, or `adjacent` with p(core) + p(adjacent) >= 0.6.
3. **Jev layer 2** (`evidence`, only the top `--l2-max` passes by p(core), then market cap): up to three
   deterministic excerpts from the company's latest **official annual report** in `documents`, whichever source
   has it: SEC 10-K Item 1 / 20-F Item 4 (`sync-sec`), CNINFO 年度报告 or 年度报告摘要 (`sync-cninfo`), EDINET
   有価証券報告書 事業の内容 (`sync-edinet` or the open data pack), DART 사업보고서 사업의 개요 (`sync-dart`), MOPS
   股東會年報 營運概況 / 業務內容 (`sync-mops --mode annual`), BSE annual-report MD&A (`sync-bse`); after the first
   report, the on-demand fetch adds missing SEC, CNINFO, BSE, MOPS (with consent) and DART (with a key) reports for
   profile-only companies. The newest filing wins; a
   full report beats a summary only when both are for the same fiscal year and the full text is on disk; the
   summary of that year (CNINFO) is then read first, since its 主要业务或产品简介 is a compact business overview.
   Excerpts: the opening overview, the best paragraph for the keywords of the document's language, and the paragraph
   closest to the company's own description; at most 700 characters each (450 for Chinese / Japanese / Korean, which
   cost far more tokens per character). For Chinese / Japanese / Korean the overview skips paragraphs dominated by
   financial figures and runs to ~300 characters, and free slots are filled with the next business paragraphs. The text is tagged with source, form, filing date and language, e.g.
   `[annual report excerpts: CNINFO annual_report filed 2026-04-19; language zh]`. Without any official text,
   layer 2 reads the profile itself (`l2_evidence = profile`: the same kind of text as layer 1, so a weaker check).
   **Keywords per language**: the document language is detected from its script (Hangul -> `ko`; kana -> `ja`; Han
   -> `zh`; else `en`). English documents use `--keywords`, else the generated English keywords, else the Latin
   words of the idea; they match phrases and simple inflections (`agent` also finds `agents`, `agentic`) and keep
   acronyms (`AI`, `EV`, `5G`). Chinese / Japanese / Korean documents use `--keywords-zh` / `--keywords-ja` /
   `--keywords-ko`, else the generated keywords of that language, else the idea's own words in that script; CJK
   terms match as substrings (no word boundaries) with spaces optional (`AI 代理权限控制` finds `AI代理权限控制`). A non-English idea with neither translation nor
   `--keywords` gets a loud warning. Labels: `explicit`, `partial`, `contradicted` (only when the text says the
   activity was sold, discontinued or is not offered), `insufficient` (including excerpts about other businesses).
   Filings older than three years are flagged (`l2_doc_stale`). Layer 2 gets whatever budget layer 1 left.
4. **Rank**: only layer-2 `explicit` / `partial` companies are ranked: explicit 3, partial 1.5 (halved when read from
   a profile), plus 2 x p(core), plus a small log(market cap) tie-break; top `--max-out` rows. `contradicted` is
   dropped (listed as a gap); the other layer-1 passes (`insufficient`, L2 failed / skipped / beyond `--l2-max`)
   go to a separate **unverified** list with their `l2_status`. Revenue 3-year CAGR is computed in local currency
   from `fundamentals_annual` (NULL when a year is missing or currencies differ).

Paid calls are journaled per physical request in `jev_requests` (a resend is a new row, so no charge is ever
overwritten) and per item in `jev_items`, which is also the reuse cache: an item answered once (same question, same
text) is free on every later run, whatever packet it lands in. An item whose earlier send had an unknown outcome
(timeout, gateway 502/504/524, crash) is not resent unless you pass `--retry-uncertain`. Only one process uses Jev at
a time (`data/locks/openrouter-jev.lock`). The budget is hard: one request is in flight until the first priced
answer, reservations scale with the observed actual/estimated cost, and a price change is reported.

Outputs go to `data/screens/<YYYYmmdd-HHMM>-<slug>/`: `results.csv` (ranked rows), `results.json` and `report.md`
(idea, the English idea and keywords per language with the model and time used, warnings, parameters, the full
funnel from all universe rows through the L1/L2 label mix and the annual-report-vs-profile split of L2 inputs (by
source and language), requests/cost/cache hits/time per layer, ranked table with evidence
excerpts, links and filing dates, the unverified list, gaps, and the licence note). Each run is recorded in `screen_runs`, and every judgement in `screen_results` with the licence tier
of the text Jev read (`gray-private` for descriptions, `official-private` for annual-report excerpts from SEC,
CNINFO, EDINET, DART, MOPS or BSE). `--dry-run` builds all
Jev inputs and prints the estimated requests, cost, reservation and time per layer and in total against the budget
(layer 2 as an upper bound on the number of companies); it sends no paid request and writes nothing to the store,
only the report files.

Screens built from gray-private descriptions are **personal use only**, like the data behind them.

## Data rules (summary)

Full rules: [DATA_RULES.md](DATA_RULES.md).

- **Missing stays NULL**, never 0.
- **Currency**: TradingView's default mode converts TTM/FY values to USD. History arrays are never converted.
  History is stored in local currency with its currency code, and growth is computed in local currency. USD FY
  values are never mixed with local history.
- **Provenance**: raw payloads are saved verbatim with their sha256. Every row points to a snapshot, every snapshot
  to a source, and every source to a licence tier.
- **Politeness**: at most 1 request per second per host (TradingView symbol pages: at most 4.4/s, the measured
  ceiling; SEC: fair-access interval; CNINFO queries and EDINET 1/s, CNINFO fast-mode PDFs at most 4/s on the PDF
  host; DART 2/s; BSE 1/s per host; MOPS annual reports at least 1.5 s apart and only for the companies needed). A run stops on the first 403/429, a
  challenge marker, or 3 errors in a row (5 for the annual-report syncs). Crawls resume from `crawl_state`.

## Licence tiers

| Tier | Meaning | May be redistributed? |
|---|---|---|
| `official-open` | Official source whose licence allows redistribution (e.g. SEC bulk data, TWSE OpenAPI) | Yes, with attribution |
| `official-private` | Official source, personal use only (e.g. exchange downloads) | No |
| `gray-private` | Public but terms-restricted endpoint or text (TradingView forbids automated use of its data) | **No, never** |

Profile text and short annual-report excerpts are sent to the AI service (OpenRouter and the provider that serves
Jev) to be read; nothing is published.

### Notice: the default sources are gray-private

| Source | Tier |
|---|---|
| TradingView scanner endpoint (`scanner.tradingview.com`) | gray-private |
| TradingView symbol pages (JSON-LD descriptions) | gray-private |
| FinanceDatabase summaries (Yahoo text; local DuckDB, or `equities.bz2` downloaded by quickstart / `--fd-file`) | gray-private |

SEC sources (`sync-sec`): `sec_tickers` (ticker/CIK map and submissions metadata) is **official-open**;
`sec_filing_text` (the issuer-written 10-K / 20-F sections) is **official-private**. Keep the verbatim text local.

Other official annual reports are **official-private** too (personal use, text kept local, see
[DATA_RULES.md](DATA_RULES.md)):

| Source | Command | Tier | Why |
|---|---|---|---|
| `cninfo_annual_report` (CNINFO 巨潮资讯, A-share annual reports / summaries) | `sync-cninfo` | official-private | Exchange-designated disclosure site; the exchanges assert exclusive rights over securities information; no redistribution grant |
| `edinet_yuho` (EDINET 有価証券報告書, 事業の内容) | `sync-edinet` | official-private | Site content is under PDL 1.0 with attribution; only the code list and 事業の内容 are republished (open data pack, with attribution), the rest stays local |
| `dart_business_report` (OpenDART 사업보고서, 사업의 개요) | `sync-dart` | official-private | No explicit redistribution grant in the OpenDART terms |
| `mops_annual_report` (TWSE / MOPS 股東會年報, 業務內容) and `mops_basic` (MOPS 主要經營業務) | `sync-mops` | official-private | Issuer-authored reports on a TWSE-operated server; no redistribution grant |
| `bse_annual_report` (BSE India annual reports, MD&A / company overview) | `sync-bse` | official-private | Exchange-hosted issuer filings; no redistribution grant found, BSE terms-of-use page not located (checked 2026-09-27) |

These sources are for **personal use only**. The one exception is what the open data pack republishes from
`edinet_yuho` (the EDINET code list and 事業の内容, with attribution; see [OPEN_PACK.md](OPEN_PACK.md)).
Otherwise never redistribute the data, the database, or outputs derived from them, including labels, screens,
lists, reports, pages and charts. What you may share: the code, your idea, and outputs built only from the open data
pack. `jevscreen coverage` shows the licence tier behind every
company's data.

## Open data pack

`jevscreen pack pull` downloads the newest daily open data pack (GitHub Releases `pack-YYYY-MM-DD`), verifies every
file's sha256 and imports it: SEC ticker/CIK list, EDINET code list, and EDINET 事業の内容 text for Japanese
companies (PDL 1.0, attribution in the pack's ATTRIBUTION.txt; about 1,600 companies so far, growing daily). Until
the first pack is published, quickstart skips this step and Japanese companies are read from their profiles. No API key needed; it reads this project's releases
(`YuanTong-Wu/jev-screen`; `--repo OWNER/NAME` or `JEVSCREEN_PACK_REPO` for a fork). Pull after `refresh-universe` if you can; if you pulled first, run `pack pull`
again after it and the texts are linked locally. `jevscreen pack build --out DIR`
builds the same pack from your own store (licence-clean sources only). See [OPEN_PACK.md](OPEN_PACK.md).

## Using jev-screen with your AI

You can let your AI coding agent (Claude Code, Codex, Cursor and the like) install and run jev-screen for you. Point
it at [AGENTS.md](../AGENTS.md): ordered steps with a check after each, what it must ask you first, and what it must
never do (read or echo your keys, spend more than $1 without your yes, work around a block, share your data).

**Before you start** (the fast path, `jevscreen quickstart`), the agent says:

> Before we start: (1) you agree to use two data sources (TradingView's stock list, Yahoo company profiles) whose
> terms restrict automated use: for your personal research only, never shared; (2) you need an OpenRouter account with
> credit. The first top-up has a minimum (usually a few dollars, plus a payment fee; see the payment page on
> openrouter.ai) and needs a card that pays in US dollars or another method OpenRouter accepts; this screen uses about
> $0.30 of it and the rest stays for later. (3) About 15–25 minutes the first time, including the OpenRouter sign-up.
> On a Mac, a box may offer to install the command line developer tools: click Install (a few minutes). You answer one
> round of questions.

1. Your agent runs `jevscreen quickstart "<your idea>" --json` and asks you everything in one message: the data
   sources, the spend cap (the English sentence the AI will read is shown), and the OpenRouter key (a hidden box
   opens; the key never passes through the chat).
2. Downloads start in the background while you make the key (about 1 minute); the AI screen and the annual-report
   fetch run next (about 3 minutes); the agent tells you the progress about once a minute.
3. A page opens in your browser: a ranked list where every company carries its evidence (annual-report quote with a
   link, or the profile), labelled fact / inference / gap / your call, with optional cards to sharpen it. It is one
   page per idea (`data/pages/<key>.html`): every later version (your card answers, a profile fill) updates it.
4. When many companies of your idea's market have no profile, the agent asks once whether to fill them (optional,
   free, in the background; then a re-rank of a few cents within your cap).

The commands behind it:

| Command | What it does |
|---|---|
| `jevscreen doctor [--json] [--check-jev]` | Checks Python, dependencies, the store, universe freshness, description and annual-report coverage, which keys are set (never their values), consent and cooldowns, then prints the next command. Exit 0 when a screen can run. `--check-jev` asks OpenRouter whether the key works (one free request, no paid call) |
| `jevscreen keys set NAME` / `keys check [NAME]` / `keys clear NAME` | Stores `openrouter`, `sec-email`, `edinet` or `opendart` from a hidden prompt into `data/` with mode 0600; never prints them |
| `jevscreen quickstart "<idea>" [--idea-en TEXT] [--approve-budget USD] [--fill-descriptions yes\|no] [--json]` / `--status [--wait S]` | The fast path: returns in seconds, asks one round of questions (`pending`), runs the downloads, the key check, the estimate, the screen, the annual-report fetch (the same one as `screen --fetch-docs auto`) and the page in a detached worker; the agent polls `--status --wait`. `--fill-descriptions yes` answers the optional profile-fill question (crawl-descriptions for the idea's market, then an incremental re-rank). Exit code = JSON `status` (0 running/done, 10 needs you, 11 needs the agent, …) |
| `jevscreen page [RUN_ID\|latest] [--open] [--text]` | Rebuilds the result page `page.html` (self-contained, zh/en, cards that build the `answer` line); `--text` also prints the ranked list as plain text for an agent without a browser |
| `jevscreen keys set openrouter --dialog` | Opens a hidden input box on your screen for the key (macOS / Linux desktop) |
| `jevscreen keys set openrouter --from-file PATH` | Records where you saved the key yourself (only the location is stored; the key is never copied or printed, and the file must hold only the key: one line starting with `sk-or-`). A running quickstart uses it from its next paid step |
| `jevscreen consent set gray-sources yes\|no` / `consent show` | Records your answer on gray-private sources (TradingView, FinanceDatabase/Yahoo text; personal use only; the statement also covers official annual reports as personal use) with a timestamp in `data/consent.json`. `refresh-universe`, `crawl-descriptions` and `import-fd` refuse to run (exit 1, `consent_required`) until it is `yes` |

Type keys yourself when `jevscreen keys set NAME` asks (in your own terminal, give the full path
`<jev-screen folder>/.venv/bin/jevscreen` unless the venv is on your PATH); do not paste them into the chat. First-run
defaults the agent uses: companies of $1B market cap and more, a $1 budget, and a dry run first. An agent always
passes `--budget` with the amount you approved and `--min-mcap` on every `screen`, because the program's own
defaults are $3 and $200M. JSON formats:
[AGENT_API.md](AGENT_API.md).

## Licence, data licences and security

The code is MIT-licensed ([LICENSE](../LICENSE)); the data it fetches is not: see [DATA_LICENSES.md](../DATA_LICENSES.md)
for each source's tier and what may never be redistributed. Two test PDFs embed an Apache-2.0 font subset
([LICENSES/Apache-2.0.txt](../LICENSES/Apache-2.0.txt)). Report leaks and vulnerabilities privately
([SECURITY.md](../SECURITY.md)). Before publishing a fork or opening a pull request, run
`python3 tools/release_check.py` (must print `release_check: clean`); the release steps are in
[RELEASE_CHECKLIST.md](RELEASE_CHECKLIST.md).
