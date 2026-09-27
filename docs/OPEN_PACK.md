# Open data pack

The owner publishes a free daily **open data pack** as GitHub Releases (`pack-YYYY-MM-DD`). It holds only data
whose licence allows redistribution, so a new user gets official identifiers and Japanese annual-report business
text without API keys, and without the personal-use sources ever leaving anyone's machine.

```
jevscreen pack pull                 # newest pack-* release -> verify -> import (idempotent)
jevscreen pack pull --tag pack-2026-09-27
jevscreen pack build --out DIR      # build a pack from your own store (what the daily job runs)
```

`pack pull` reads the project's own releases (`YuanTong-Wu/jev-screen`, `jevscreen.pack.DEFAULT_PACK_REPO`); `--repo OWNER/NAME`
or `JEVSCREEN_PACK_REPO` point it at a fork. It uses the public GitHub API without a token (a pull costs
one release-list request plus two requests per file: GitHub redirects every download once).

## What the pack contains

| File | Source (`provenance.py`) | Rows (live 2026-09-27) | Content |
|---|---|---|---|
| `sec_tickers.jsonl.gz` | `sec_tickers` (official-open) | 10,413 | SEC `company_tickers_exchange.json`: `cik`, `ticker`, `name`, `exchange` |
| `edinet_codes.jsonl.gz` | `edinet_yuho` (PDL 1.0, allowlisted) | 3,818 (of 11,389 in the full list) | EDINET code list, only filers with a `sec_code` and never an individual: `edinet_code`, `sec_code` (5-char 証券コード), `listed`, `name`, `name_en`, `filer_type`, `jcn` |
| `edinet_business.jsonl.gz` | `edinet_yuho` (PDL 1.0, allowlisted) | 1,613 | Newest valid 有価証券報告書 per EDINET code (a missing, altered or too-short newest text falls back to the next older filing; filers the code list marks delisted are left out): `edinet_code`, `sec_code`, `filer_name`, `doc_id`, `form`, `filing_date`, `period_end`, `source_url`, `lang`, `text` (事業の内容 only), `text_sha256`, `text_chars` |
| `ATTRIBUTION.txt` | - | - | Licence and attribution text per source, and the list of excluded sources |
| `manifest.json` | - | - | See below |

`manifest.json` (format `jevscreen-open-pack`, `format_version` 1): `created_at`, `tag`, `sources` (per source:
name, tier, licence, licence URL, attribution text, why it may ship, `fetched_at`), `files` (name, table, source,
rows, bytes, sha256), `excluded` (every registered source that does not ship, with the reason). Files are gzip'd
JSON lines, written deterministically (sorted keys, gzip mtime 0): the same content gives the same sha256.

## What it does not contain

- Nothing from TradingView (scanner, symbol pages), FinanceDatabase / Yahoo summaries: gray-private.
- Nothing from CNINFO (A-shares, `cninfo_annual_report`), OpenDART / the DART website (Korea,
  `dart_business_report`), MOPS / TWSE (Taiwan, `mops_annual_report`, `mops_basic`) or BSE (India,
  `bse_annual_report`): official-private, no redistribution grant. Every manifest lists them under `excluded` with
  the reason from `pack.EXCLUDED_REASONS`. Any future source (e.g. NSE, HKEX) ships only when it is added to
  `pack.PACK_SOURCES`.
- No SEC 10-K Item 1 / 20-F Item 4 text (issuer copyright) and no SEC filing metadata rows.
- Of EDINET filings only 事業の内容 (DescriptionOfBusinessTextBlock). The 経営方針、経営環境及び対処すべき課題等
  block that `sync-edinet` appends locally is cut off before export.
- No individual filers. The EDINET code list names about 3,200 private persons (filer types 個人(組合発行者を除く)
  and 個人(非居住者)…, mostly large-shareholding filers). Their rows never ship, and a pack row of that kind is
  dropped on import. The code list also ships only filers with a 証券コード, the only rows a line can map to.
- No market data, universe lines, TradingView-style security ids, ISINs, descriptions, crawl state, Jev requests,
  labels or screen results.

## Licence and attribution

- **SEC** (`sec_tickers`): information compiled by the SEC is public and may be copied or further distributed
  (https://www.sec.gov/about/privacy-information#dissemination). Attribution shipped: "Source: U.S. Securities and
  Exchange Commission, EDGAR company_tickers_exchange.json. The SEC does not endorse jev-screen."
- **EDINET** (`edinet_yuho`): EDINET site content is offered under the Public Data License 1.0 (公共データ利用規約
  第1.0版, compatible with CC BY 4.0, https://www.digital.go.jp/resources/open_data/public_data_license_v1.0), which
  requires a source notice and, for edited content, a statement that it was edited. Shipped text:

  > 出典：EDINET（金融庁 電子開示システム, https://disclosure2.edinet-fsa.go.jp/）の EDINETコードリスト及び
  > 有価証券報告書「事業の内容」を jev-screen が加工して作成（XBRL→CSV の該当テキストブロックを抽出し、
  > 空白・改行を整形）。
  > Source: EDINET (Financial Services Agency of Japan): EDINET code list and the 'Description of Business'
  > (事業の内容) section of annual securities reports, extracted and whitespace-normalised by jev-screen. Edited by
  > jev-screen; the FSA does not endorse jev-screen.

  Anyone redistributing the pack (or text from it) must keep this notice.

  **Open question (third-party rights).** PDL 1.0 does not apply to content in which a third party holds rights,
  and 事業の内容 is written by each filing company, not by the FSA. Whether issuer-written text falls under the
  EDINET grant is not confirmed. Until the owner confirms it, the pack says so: the manifest (`caveat`),
  `ATTRIBUTION.txt` and the release notes carry this caveat, and anyone republishing the text should check first.
  (The SEC 10-K / 20-F text is kept out of the pack for the same kind of reason.)

  `source_url` in `edinet_business` is the public EDINET viewer link for the filing
  (`https://disclosure2.edinet-fsa.go.jp/WZEK0040.aspx?<docID>,,`), never the key-gated API URL that
  `sync-edinet` records locally; packs from before this change are rewritten to it on import.

`pack.assert_redistributable()` enforces the rule for every file at build time and for every manifest entry at
pull time: the source must be in `PACK_SOURCES`, registered in `provenance.SOURCES`, never gray-private, and a
non-official-open tier needs an explicit allowlist grant. A build also refuses to write a pack in which a configured
secret (SEC User-Agent, EDINET/OpenDART keys, the Jev keys of TypeSafe/OpenRouter/Vercel) appears.

## Pull: verification and import

1. The newest non-draft, non-prerelease release whose tag starts with `pack-` (or the exact `--tag`).
2. `manifest.json` is downloaded and validated (format, version, file names, sizes, allowlisted sources only; a
   newer format asks to upgrade jevscreen). If this manifest's sha256 was imported before, the pull downloads
   nothing more (`already_imported`; `--force` imports again). It still re-links locally (`relinked` in the
   output): identifiers, the line of pack documents that had none, and their descriptions. So `pack pull` before
   `refresh-universe`, then `pack pull` again, is enough: the text becomes visible to layer 2 without `--force`.
3. Every file is downloaded and checked against the manifest's size and sha256 (and GitHub's asset digest when
   present) before anything touches the store. Downloads follow at most 5 redirects, only to GitHub hosts
   (`github.com`, `api.github.com`, `*.githubusercontent.com`). HTTP 403/429 or a challenge stops the pull at once
   (cooldown marker `pack pull`, exit 2, no retry; the next pull within 24 h refuses before any request unless
   `--after-block`).
4. Files are kept verbatim under `<home>/raw/open_pack/<tag>-<manifest sha256[:12]>/` (a re-released tag never
   overwrites files that earlier snapshots cite); rows are imported in one short write session:
   - one snapshot per file: `source_id` = the original source, `kind` = `pack:<table>`, note
     `pack-imported tag=<tag>`, raw path/sha256 of the pack file; plus a `pack:manifest` snapshot;
   - `identifiers` (`sec_cik`, `edinet_code`) only for local lines that have none (a local mapping is never
     replaced); mapping uses the adapters' own rules (`map_securities_to_cik`, `map_securities_to_edinet`);
   - `documents` rows `edinet_yuho:<code>:<docID>:business` with `cik` = EDINET code (layer 2 reaches them through
     the `edinet_code` identifier), extractor `open-pack-v1`, text under `<home>/docs/edinet/<code>/`. A document
     written by your own `sync-edinet` is never overwritten; a pack document is rewritten only when its text or its
     resolved line changed. Because the extractor differs, your own `sync-edinet` (with your key) later re-extracts
     these filings with the richer local extractor (事業の内容 + 経営方針);
   - `descriptions` (short Japanese description from 事業の内容) only where none exists or the existing one came
     from a pack.
   Rows that fail validation (codes, doc ids, text length) are counted in `rows_rejected` and skipped.
5. The run is journaled in `runs` (`pack pull`).

## Daily job (`ci/daily-pack.yml`)

The workflow ships as a template in `ci/`. To enable it in your repository, copy it to
`.github/workflows/daily-pack.yml` (pushing a workflow file needs a GitHub token with the `workflow` scope,
e.g. `gh auth refresh -s workflow`) and add the repository secrets it reads.

Scheduled 21:40 UTC (06:40 JST) and manual (`keep`, `edinet_minutes`, `allow_shrink` inputs). Steps:

1. Fix the pack date (UTC) at job start, so a long EDINET backfill that ends after midnight keeps its date.
2. Restore the job's own store from the Actions cache. On a cache miss (evicted, or a failed restore) the store is
   bootstrapped with `jevscreen pack pull --repo <this repository>`, so the job never starts from nothing.
3. `jevscreen pack fetch-open --seed-lines`: SEC ticker list if `JEVSCREEN_SEC_USER_AGENT` is set, EDINET code list,
   and one `TSE:<code>` line per listed EDINET filer so `sync-edinet` has lines to map (seeding is refused in any
   store that holds TradingView data).
4. `jevscreen sync-edinet` if `EDINET_API_KEY` is set, under a time budget (at most 280 min; SIGTERM -> flush,
   resumes the next day). It reuses the code list step 3 downloaded (`JEVSCREEN_EDINET_REUSE_CODELIST_HOURS=6`),
   so Edinetcode.zip is fetched once per run.
5. `jevscreen pack build`, with the SEC User-Agent and the EDINET key in the environment only so the build can scan
   its output for them. Tag: `pack-<date>`; if that release already exists (a second run on the same date),
   `pack-<date>.2`, `.3`, ... A published pack is never overwritten.
6. Publish gate (`jevscreen.pack_ci gate`): the pack is published only if it holds data rows and no table has fewer
   than **95 %** of the rows of the previous published pack (a partial store after a lost cache must not become
   what `pack pull` fetches). If the previous manifest cannot be read, nothing is published. The manual
   `allow_shrink` input overrides the 95 % rule for one run (e.g. after an intended change that removes rows).
7. Prune raw payloads older than 14 days, save the store, publish the release (not marked "latest", so code
   releases keep that), and delete pack releases beyond the newest `keep` (default 7; anything that is not a whole
   number means 7, and at least one pack is always kept).

A missing secret skips its source with a notice; a 403/429 stops that source without retry. Secrets live only in
repository secrets and are never printed; the build scans its output for the SEC User-Agent and the EDINET key.

## Size (measured 2026-09-27 from the owner's store, read-only)

Built by `pack.build()` from the live store (read-only session, 0.7 s) into a temp directory:

| File | Rows | Bytes |
|---|---|---|
| `sec_tickers.jsonl.gz` | 10,413 | 163,828 |
| `edinet_codes.jsonl.gz` | 3,818 (filers with a 証券コード; the full list has 11,389) | 124,759 |
| `edinet_business.jsonl.gz` | 1,613 (2.77 M chars; median 1,051, max 22,491; 8.2 MB uncompressed; measured before delisted filers were left out) | 2,218,291 |
| `ATTRIBUTION.txt` + `manifest.json` | - | ~5,800 |
| **Total** | | **~2.51 MB** |

Importing it into an empty store takes ~3.5 s (a re-import: 0.1 s, every document 'unchanged'). EDINET coverage
grows with the daily job: ~3,800 listed filers when complete, so expect roughly 5-6 MB.
