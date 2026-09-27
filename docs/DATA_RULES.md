# Data rules (read before touching any adapter)

Measured against TradingView on 2026-09-26 (fixtures in tests/fixtures/).

## Universe
- One request per filter to `POST https://scanner.tradingview.com/global/scan`.
- Filters: `type=stock AND is_primary=true AND subtype=common` (≈49.4k rows) **plus** `type=dr AND is_primary=true` (≈270 rows).
  The DR set is required: TradingView marks the US ADR as the primary line for BABA, BIDU, JD, NTES, PDD, ARM, SE, NIO,
  and EURONEXT:ABN is typed `dr`. Without it these companies vanish.
- Unknown column names do not error; they silently return null. Validate column coverage after each pull.

## Currency (the trap)
- Default mode: `market_cap_basic`, `*_ttm`, `*_fy` are converted to **USD**. FY values use a period-end FX rate,
  market cap uses the current rate (e.g. GBP 1.3472 for revenue_fy vs 1.321 for market cap on the same day).
- `*_fy_h` history arrays are **never converted**: they are in the security's fundamental currency (main unit).
  In default mode `fundamental_currency_code` misleadingly says `USD`; request with
  `"price_conversion": {"to_symbol": false}` to get the real local code (GBP for GBX prices, ILS for ILA, ZAR for ZAC,
  HKD for Tencent even though it reports in CNY).
- Store history in local currency with its code. Compute growth in local currency.
  `fx_local_to_usd = market_cap_usd / market_cap_local` (both from the same day) is the current rate to use when a USD
  figure for history is needed. Never combine TradingView's USD FY values with local history.
- Price currency may be a subunit (GBX, ILA, ZAC). Do not use price currency for fundamentals.

## History arrays
- `fiscal_period_fy_h` gives the fiscal years for each index of `*_fy_h` (index 0 = latest). Zip by index; if lengths
  differ, zip only the overlapping prefix and record a note. Nulls inside arrays stay NULL.
- Metrics pulled: total_revenue, net_income, gross_profit, ebitda, free_cash_flow, total_assets, total_debt,
  earnings_per_share_diluted.

## Descriptions
- TradingView symbol page `https://www.tradingview.com/symbols/{EXCHANGE}-{SYMBOL}/` has a JSON-LD
  `FinancialProduct` with `description`. Robots allow `/symbols/` for generic agents.
- Crawl at ≤4.4 requests/second (measured ceiling; CLI default and hard maximum, faster --rate is clamped; NaN or
  <= 0 is rejected). Enforced as a minimum gap of 1/4.4 = 0.2273 s between request starts AND at most 4 starts in any
  rolling 1 s window, so the client never puts 5 starts in one second and the sustained rate is <= 4.0/s: a margin
  under the ceiling because the server sees each request after a variable connect/TLS delay, not at the limiter slot.
  Pages are fetched by a small worker pool (`--workers`, default 3, clamped to 1..4) so downloads overlap (~0.47 s
  each); the limiter is thread-safe (one lock per host held from reading the last starts to recording the new one),
  so both limits hold whatever the number of workers. Only a client declaring `thread_safe = True` (http.Client)
  gets more than one worker. Workers only fetch and parse; the main thread owns the queue, the
  covered-set check, the flushes, the counters and every stop decision. One process per budget: crawl() itself takes the
  www.tradingview.com budget lock, so a script cannot crawl next to the CLI with a second limiter.
- Stream each page in 16 KB chunks and disconnect as soon as a complete JSON-LD `FinancialProduct` with a non-empty
  description has been read (live probe of 10 pages, 2026-09-26: the block ends at 89.1-89.8 KB, mean 96 KB read).
  256 KB is only the hard cap; a cap hit before any complete FinancialProduct is the soft, retryable
  'no_product_within_cap', never 'no_description', and does not count toward the consecutive-error stop. A complete
  FinancialProduct with an empty/missing/over-long description also ends the stream and is the terminal
  'no_description'.
- Skip companies already covered by an SEC annual-report business section ('sec_filing_text' description, or a
  documents row with text whose cik matches the line's `sec_cik` identifier). The covered set is refreshed at every
  flush because sync-sec may be running at the same time, and checked just before each page is handed to a worker.
- On the FIRST HTTP 403/429 or challenge (including a 3xx whose Location is a challenge path): stop the whole run
  at once (no retry, no slowing down). Order: (1) http.Client sets its halt flag in the same step that observes the
  block, so no further request (or retry) starts from any worker; (2) the worker that got it writes the cooldown
  marker at once, before any database wait; (3) the main thread records the block and ignores SIGTERM from then on;
  (4) requests already in flight (at most workers - 1) finish and are recorded normally; (5) final flush, one
  `BLOCKED url=... status=... reason=...` line, exit 2. Also stop after 3 consecutive errors (counted in the order
  pages complete; soft statuses neither count nor reset). A retry cut short by the halt records the attempt that was
  already sent ('error'), never "not started". SIGTERM/Ctrl-C: halt, let in-flight requests finish (drain at most
  30 s; the crawl socket timeout is 15 s per operation; a second Ctrl-C stops waiting, and process exit still waits
  for those sockets), flush, 'interrupted'. The BLOCKED line and exit 2 survive a failed final flush, a locked
  journal or a Ctrl-C/SIGTERM after the block. Resume only when the user reruns (24 h cooldown, `--after-block` to
  override; crawl() itself also refuses within the cooldown unless `after_block=True`). Never bypass bot protection.
  Resume from crawl_state.
- Local FinanceDatabase summaries are Yahoo text: licence tier gray-private.

## Official annual reports (layer-2 evidence)
Common to `sync-sec`, `sync-cninfo`, `sync-edinet`, `sync-dart`:
- Only the business section is kept, as a text file under `data/docs/<source>/...`; `documents` holds metadata
  (doc_id `<source>:<native id>:<doc id>:business`, form, filing/report dates, url, raw sha256/bytes, text sha256/chars,
  extractor, extract note, snapshot). The full primary document is not stored. A short excerpt goes to `descriptions`
  under the adapter's own source_id. Identifier crosswalk rows: `sec_cik`, `cninfo_orgid`, `edinet_code`,
  `dart_corp_code`.
- One polite client per run, one rate budget per source (lock files `data/locks/<budget>.lock`: `sec.gov`,
  `cninfo`, `edinet`, `dart`), no DuckDB connection during network I/O (short session per batch), incremental
  (an already processed filing is not downloaded again unless `--refresh`).
- The first HTTP 403/429 or challenge stops the whole run: cooldown marker `data/cooldown/<command>.json` first, then
  the flush; exit 2; 24 h cooldown per command unless `--after-block`. 5 consecutive errors stop the run. SIGTERM /
  Ctrl-C flush and exit 130. Never bypass bot protection. A 403/429 is decided from the status line alone: its body
  is never read, so a stalled error body can neither delay the stop nor be retried.
- Timeouts (http.Client): every request ATTEMPT has a total wall-clock deadline from open to the last body byte
  (default 120 s; set per call: CNINFO PDFs 300 s and the query POST 60 s, EDINET/DART lists 60 s, EDINET/DART
  documents and code lists 300 s, SEC primary documents 600 s), besides the per-socket-operation timeout (timeout_s).
  Bodies are read in chunks with the socket timeout lowered to the time left, so a trickling or stalled server ends
  at the deadline (seen live: an OpenDART document.xml held 10+ minutes). Either limit raises http.RequestTimeout
  (not a block): 'deadline' when the total deadline passed, 'stall' when a body read got nothing within timeout_s.
  A GET/HEAD attempt that received no body byte is retried once; after partial body data, or for a POST, it is
  never retried. (A connection error or socket timeout before the response headers, within the deadline, is retried
  as before: max 2, backoff.) The item is recorded crawl_state 'error' with note `deadline;<s>s;received:<bytes>` or
  `stall;<s>s;received:<bytes>` (EDINET/DART; CNINFO/SEC: 'network: RequestTimeout: ...') and the run goes on
  (the consecutive-error stop still applies).
- Screening (layer 2) reads the newest filing per company from any of these sources; a full business section beats
  a summary only for the same fiscal year and when its text file exists. Language is detected by script
  (zh / ja / ko / en) and excerpt keywords follow it; CJK keywords match as substrings with optional spaces. A full
  CNINFO report is read together with its same-year summary (summary first).
- `sync-mops` (Taiwan, no key) follows the same rules: crosswalk id type `mops_co_id`, lock file `mops`, cooldown
  marker `data/cooldown/sync-mops.json`; the MOPS security page and TWSE's F5 rejection page stop it at once too,
  and its default mode reads no annual report (see MOPS / TWSE below).

### CNINFO 巨潮资讯 (`sync-cninfo`, source `cninfo_annual_report`)
- Endpoints: `GET www.cninfo.com.cn/new/data/szse_stock.json` (code -> orgId), `POST
  www.cninfo.com.cn/new/hisAnnouncement/query` (annual-report announcements, category `category_ndbg_szsh`), `GET
  static.cninfo.com.cn/<adjunctUrl>` (the PDF). Fixtures: tests/fixtures/cninfo_*.
- SSE / SZSE universe lines map by 6-digit code; A + B share lines of one orgId are fetched once.
- Latest fiscal year: `<year>年年度报告摘要` for `--kind summary` (default, small PDF; falls back to the full report
  when there is no summary or its business section cannot be extracted), `<year>年年度报告` for `--kind full` (the
  summary when that year has no full report: flag 'summary_only', counted apart from 'fallback_full'). English
  versions, cancelled copies and correction notices are ignored; the latest revision of a year wins.
- Accepted titles carry any company-name / short-name prefix (`中国移动：2025年年度报告摘要`, `建设银行2025年度报告`):
  `<year>年年度报告` / `<year>年度报告` / `<year>年年报` (full), the same + `摘要`, `<year>年度业绩公告（年度报告摘要）`,
  `<year>年度业绩报告` (summary); the year may be in Chinese numerals (`二零二四`, `二〇二五`, `二0二五`). H-share /
  overseas-regulatory copies, a subsidiary's report filed by the parent (`中国平安：平安银行股份有限公司…`), 业绩快报 /
  业绩预告 and correction notices are not annual reports.
- Newest-year guard: when an annual-report-like title of a NEWER year than every parsed one cannot be parsed
  (e.g. `XX2025年年度报告及摘要`, or a colon title naming the company's own full name), nothing is selected (never an
  older year silently): crawl_state 'no_annual_report', note `newest_year_unparsed:<year>`.
- Staleness: a report whose fiscal year is behind the newest one that must be out (mainland deadline 30 April: after
  30 April of year Y it is Y-1, before it Y-2) is kept with flag `stale:<years>` in its extract note.
- Summary then full: a summary whose text came only from the management-discussion fallback (`fallback:mda_opening`,
  typical of banks) is kept (text file + row) and the full report is still downloaded for a real business section
  (one extra 9-11 MB PDF per such bank); both texts are kept. If the full report fails (HTTP error, timeout,
  network error, or no extractable section) the summary stands.
- Fast mode (default; measured 2026-09-26: the old per-company path ran 0.28 companies/s, ETA 5.2 h for 5,181
  A-shares, because every company needed its own query and queries + PDFs shared one sequential 1 req/s limiter):
  - **Rate change (needs the user's explicit OK before the first live run):** the old rule was 1 request/second on
    one rate key shared by www. and static.; fast mode keeps 1 req/s on www.cninfo.com.cn (listing + queries) and
    adds up to 4 PDF starts/s on static.cninfo.com.cn (3 parallel connections by default), i.e. up to 5 requests/s
    at CNINFO in total. `--workers 1` gives at most 1 PDF connection; `--per-company` restores the old 1 req/s.
  - Bulk listing: the query works without a stock (`stock=''`, `seDate=<since>~<today>`, `column=szse` covers SSE
    too; 30 rows per page, each with secCode / orgId / title / adjunctUrl / adjunctSize / announcementTime; Beijing
    rows are unverified). Paged on rate key `cninfo` (>= 1 s apart), each page saved raw
    (`list-p<NNNN>-<since>-<until>[-<plate>]`), one `list_pass` snapshot per pass (status `partial` when not
    complete; note `stop=...;windows=N;pages=P;rows=R` plus `day_over_cap:` / `repaired:` / `gap:` entries; the raw
    manifest lists every window, the repairs, the gaps, the days over the cap and the split checks).
  - **Page cap (live 2026-09-26):** CNINFO serves at most 100 pages = 3,000 rows per query. The 13-month query
    2025-08-26~2026-09-26 reported totalAnnouncement 11,403 (totalpages 380), but page 101 was byte-identical to
    page 1 (stop `duplicate_page` after 3,000 rows, reaching back only to 2026-04-28); the run then fell back to
    per-company queries for all 5,170 companies at ~0.84 companies/s. Fixture:
    tests/fixtures/cninfo_bulk_page_cap_2026-09-26.json (page hashes, rows per day of pages 1-100). Page 101 is
    never requested now (stop `page_cap`).
  - Adaptive date windows: the range is read in windows whose page 1 reports at most 2,800 rows (a margin under
    3,000 for rows added while paging); a window reporting more is split in half by date (newer half first; its
    page 1 is a one-request probe), down to single days. A single day is always paged: above 3,000 rows it stops
    at page 100 and is recorded `day_over_cap:<date>` (possible around 30 April; the live peak was 1,424 rows on
    2026-04-29). Expected cost for the 13-month window: ~392 requests (384 pages in 9 leaf windows + 8 split probes), measured on
    a SYNTHETIC day distribution (the live pages 1-100 for 2026-04-28 onwards, earlier days shaped like the 1,817
    per-company responses of the live run, scaled to 11,403 rows; peak 1,424/day, no day over the cap), not live;
    it assumes no gap (below for what one costs).
  - Split check: the split assumes `seDate` `a~b` is inclusive at China-date granularity (children `[a, mid]` and
    `[mid+1, b]`; live probe 2026-09-27: `2026-04-28~2026-04-28` 1,224 + `2026-04-29~2026-04-29` 1,424 =
    `2026-04-28~2026-04-29` 2,648, every row dated inside its window).
    Once both halves of a split are read, their page-1 totals must agree with the window's own (at most 3 fewer,
    at most max(30, 1%) more for rows published meanwhile), and the leaves' totals with the whole-range probe;
    otherwise the pass is unusable (`split_totals:<a>~<b>:<children>_vs_<parent>`, WARNING): an exclusive or
    shifted end would otherwise lose or double-count boundary days silently.
  - Window: default 13 months back, but never later than 1 January of (the fiscal year that must be out by today,
    30 April deadline) + 1, i.e. 1 January of last year until 30 April and 1 January of this year from 1 May.
    Plain 13 months would start after that from early February to 30 April and leave every company that has not
    yet published its new report unsettled. `--since` later than that floor logs a WARNING.
  - Completeness, per window: CNINFO's `totalpages` is floor(totalAnnouncement / 30), not the page count (live:
    total 67 -> totalpages 2, 3 real pages); the page count is ceil(totalAnnouncement of page 1 / 30). Stops on
    hasMore=false or a page shorter than 30 rows; an empty page or a page of already-seen rows ends it too
    (incomplete when more pages are expected); the page cap (100) and a guard at page count + 3; rows seen on an
    earlier page are dropped (the listing can shift while paging). A window that ends normally is complete only
    when it saw at least as many distinct rows as its page 1 reported (fewer means rows were lost at a page
    boundary, since announcementTime has day granularity), else `rows_missing:<n>_of_<total>`. For the pass: sum
    over windows of distinct rows vs sum of window totals (`list_rows`, `list_total_reported`, `list_shortfall`).
  - Repairs before a gap is recorded: a window with rows missing (or an early empty / duplicate page) is paged
    once more and the two passes' rows are united (complete when the union reaches the retry's page-1 total;
    same-day order is not stable, so a second pass usually catches the lost row). A retry whose page 1 reports
    fewer rows than the first pass knew of (a blank or throttled reply answers total 0; a withdrawal) is a failed
    repair (`retry_total_dropped:<retry>_vs_<first>`, the window stays a gap). A day over the cap is re-listed per
    exchange with the query's `plate` filter (`sh`, `sz`, `bj`; live probe 2026-09-27: 2026-04-29 sh 367 + sz 1,006 + bj 51 = 1,424, each plate only its own
    market's codes): accepted
    only when every plate stays under the cap, holds only its own market's codes, is complete, and the plate
    totals add up to at least the day's total (no downward slack: rows no honoured plate returns would be lost
    silently past page 100; at most max(30, 1%) more); a server that ignores `plate` costs 1 request and the day
    stays a gap. A repaired window keeps its first pass's page-1 total for the split and whole-range checks (a
    repair's total is newer by a whole pass, up to ~100 requests, and rows published meanwhile on a busy day would
    fail the check); the repair's total only decides the window's own completeness. Summary `list_repairs`
    (`<se_date>:retry|plate`).
  - Blank whole-range probe: page 1 of the whole range without rows (a blank or throttled reply looks like
    `announcements: null, totalAnnouncement: 0`, and a single-window pass has no split check) is asked once more;
    no rows twice makes the pass unusable (`stop=empty_range`, error `empty_range:<since>~<until>`, raw windows
    role `empty_probe`).
  - Gaps: a window still incomplete after its repair is a gap; the rest of the listing is still used. A company
    is affected by a gap only when a report of its newest listed year Y (or a newer one) could be published inside
    it, i.e. the gap ends on or after 1 January Y+1 (a company with no row in the listing is affected by every
    gap); affected companies get their per-company query outside the fallback cap (`gap_queries_planned`), all
    others settle from the listing. This test is by date only: a gap in the reporting season (January-April)
    affects nearly every company whose newest report is last year's, so one unrepaired gap costs one query per
    non-current company (~3,900 on the first full run, ~65 min); current companies are skipped without a request
    (a revision inside the gap is then missed: `skipped_current_in_gap`). An HTTP error, bad JSON, a network
    error, the 2,000-request guard, a failed split check or a whole range without rows twice make the whole pass
    unusable (below).
  - Selection is the per-company one: rows are grouped by orgId (plus secCode) and go through the same
    select_annual_reports. The window settles a company when its newest annual-report-like year Y (parsed or not)
    is complete inside it (since <= 1 January Y+1) and no gap can hold one of its rows (above): every report of
    year Y is then in the listing, so the choice is identical. Companies not settled (nothing in the window, or only an older year) get the per-company query
    (`cninfo`), at most 300 per run in market-cap order, companies whose crawl_state already says
    `no_annual_report` last; when the listing has no Beijing (4/8/92xxxx) row at all, queued Beijing companies are
    queried outside the cap. The rest are counted `no_annual_report_in_window` (split `skipped_not_in_listing` /
    `skipped_older_year_only`; no crawl_state row, codes in the summary); more than 5% of the queue left unchecked
    makes the run `partial` with a WARNING line. An unusable listing (HTTP error, bad JSON, network error, the
    request guard, the split check, an empty whole range) is not used at all, not even for companies it saw (a revision or the full report may be on a
    missed page): every company that is not current (pre-query skip) gets its per-company query, no cap.
  - Pre-query skip: before any per-company query (fast mode, also when the listing is skipped or unusable), a
    company whose stored section extracted by the current extractor version is of the newest fiscal year that can
    exist (last year, on any day: after 30 April it is also the expected year; before, the expected year is the
    one before, but last year's report may already be out, so only a stored last-year report counts) is counted
    `skipped_current` without a request (no crawl_state row). `--kind full` counts only stored full reports;
    `--refresh` and `--codes` (an explicit recheck: a revision such as '2025年年度报告（更正后）' is picked up)
    disable it. Companies settled by the listing still go through their listing rows (no request, and a later
    revision of the current year is seen there); a current company outside the listing misses such a revision
    until `--refresh` or `--codes`.
  - Small runs skip the listing: with `--codes`, or a queue of at most 400 companies (the listing costs ~390-420
    requests), every company gets its per-company query on `cninfo` (without `--codes`: unless current) and its
    PDFs go to the worker pool.
  - PDFs on a separate limiter: rate key `cninfo-static` (static.cninfo.com.cn), starts >= 0.25 s apart and <= 4
    per rolling second, downloaded concurrently by a worker pool (`--workers`, default 3, 1..4; daemon threads);
    text extraction is serialised by a process-wide lock (PyMuPDF is not thread-safe; ~0.08 s per PDF). The
    summary-then-full fallback stays inside one company task. Workers never touch DuckDB; the main thread owns the
    queue, the per-company fallback queries (collecting finished PDF results between them), the batch, the
    counters, the short flush sessions and every stop decision. Reports already extracted (same adjunctUrl; failed
    ones with the current extractor) are settled before any PDF task is submitted; the listing still runs.
  - A 200 response for a PDF URL that is not a PDF (no `%PDF` in the first 1,024 bytes): an HTML / JSON / text
    page is a soft block (WAF / rate-limit page) and stops the run exactly like a 403/429 (reason
    `non_pdf_body`); other bytes (e.g. empty) are crawl_state `error` note `pdf_not_pdf` (counts toward the
    consecutive-error stop, retried by a later run), never a failed extraction. Both paths.
  - First 403/429/challenge on either key: the two http.Clients share one halt Event (no new start anywhere), the
    worker that saw it writes the cooldown marker, in-flight PDFs drain (at most 60 s; then they are abandoned and
    the process exits without waiting for them, up to 3 sockets closed by the OS at exit), flush, one
    `[cninfo] BLOCKED url=... status=... reason=...` line on stderr, exit 2; also when the run crashes while
    stopping (journal `blocked`). SIGTERM / Ctrl-C: halt, drain, flush, `interrupted` (130).
  - Progress `[cninfo] n/N {counts} companies/s=X.XX` (every company handled, also skipped / already done ones:
    it overstates the download rate on a resume); the summary adds list_windows, list_splits, list_pages,
    list_rows, list_total_reported, list_shortfall, list_usable, days_over_cap, list_repairs, list_gaps,
    list_split_mismatches, list_s, list_skipped, skipped_current, skipped_current_in_gap, gap_queries_planned, fallback_queries, workers, mean_pdf_kb, companies_per_s, fetched_companies,
    fetched_companies_per_s (companies with a PDF downloaded, over total_s including the stock list and listing).
    A run showing list_complete=false or many fallback_queries does not have the fast-mode throughput.
- `--per-company`: the old path (one query per company, then its PDFs, everything on rate key `cninfo`,
  1 request/second, sequential). PDFs are size-capped. No key is needed.
- Tier **official-private**: the exchange-designated disclosure site gives no redistribution grant and the exchanges
  assert exclusive rights over securities information. Personal use; keep text local.

### EDINET (`sync-edinet`, source `edinet_yuho`)
- Needs the user's own free EDINET API v2 `Subscription-Key` (`data/edinet_api_key` or `JEVSCREEN_EDINET_API_KEY`);
  missing -> refused before any request.
- Code list (Edinetcode.zip, cp932) maps Japanese lines (TSE, NAG, FSE, SAPSE) by 証券コード (local code + '0') to
  EDINET codes; the daily document lists are scanned newest first (final days cached under `data/raw/edinet/lists/`)
  for each company's newest 有価証券報告書 (docTypeCode 120); `type=5` CSV gives 事業の内容
  (DescriptionOfBusinessTextBlock). 事業の内容 is short and structural, so 経営方針、経営環境及び対処すべき課題等
  (BusinessPolicyBusinessEnvironmentIssuesToAddressEtcTextBlock, capped at 8,000 chars) is appended under the heading
  line `【経営方針、経営環境及び対処すべき課題等】`; extract note `blocks:business[,policy]`, extractor `edinet-v2`
  (edinet-v1 rows are re-extracted once, re-downloading the zip). The minimum length (80 chars) and the short
  description apply to 事業の内容 alone: the policy block never makes a too-short section pass and never enters the
  description.
- 1 request/second (the EDINET terms forbid heavy short-interval access). A rejected key (StatusCode 401, also
  inside HTTP 200) or 429 stops the run.
- Tier **official-private**: site content is under the Public Data License 1.0 (attribution). Only the EDINET
  code list and 事業の内容 may be republished, through the open data pack with its attribution (docs/OPEN_PACK.md);
  the 経営方針 block and everything else stays local.

### OpenDART (`sync-dart`, source `dart_business_report`)
- Needs the user's own free OpenDART `crtfc_key` (`data/opendart_api_key` or `JEVSCREEN_OPENDART_API_KEY`);
  missing -> refused before any request. Quota ~20,000 calls/day; 1 OpenDART call per company (list.json) in the
  default web mode.
- Measured 2026-09-26: OpenDART answers small calls fast (list.json 739 bytes in 1.4 s) but throttles large
  downloads to ~9.5 KB/s per connection (corpCode.xml hit a 300 s deadline after 2.9 MB; a document.xml got 573 KB in
  60 s). The DART website is not throttled (main.do 112 KB in 2.0 s, a viewer.do section 4.4 KB in 0.9 s).
- corpCode.xml maps KRX lines by stock_code to corp_code. Before downloading it, the newest raw copy an earlier run
  saved (`data/raw/dart_business_report/<YYYY-MM-DD>/dart_corpcode-*.zip`, day folder <= 7 days old, UTC) that
  parses is reused (snapshot note `reused_raw:<path>`, summary `corpcode_reused`); corrupt copies are skipped. Only
  without one is it downloaded (~3.6 MB, deadline 900 s). A list (reused or downloaded) with < 50,000 corp codes or
  < 2,000 stock codes is suspect (live 2026-09-26: 119,447 / 3,994): a suspect copy is not reused; a suspect
  download stops the run (`corpcode_error`, `corpcode_suspect:rows=..;stock_codes=..`) before any snapshot or
  identifier change, so a partial file can never delete the `dart_corp_code` rows of the lines it misses. The CLI
  does not yet pass `--refresh` through as a forced corpCode download (`reuse_corpcode=False`) or expose `mode`
  (cli.py was being edited elsewhere); until then delete the raw copy to force a download.
- list.json (pblntf_detail_ty A001) finds the newest 사업보고서 (the original preferred over a correction of the same
  period).
- Section text, default mode `web`: `https://dart.fss.or.kr/dsaf001/main.do?rcpNo=<rcept_no>` embeds the table of
  contents as JS nodes (`node1` chapter, `node2`/`node3` items; fields text, dcmNo, eleId, offset, length, dtd, in any
  order). Under the chapter `II. 사업의 내용` the items `사업의 개요` and `주요 제품 및 서비스` (`주요 제품, 서비스 등`)
  are found by title without numbering or spaces (`1. 사업의 개요`, `1.사업의 개요`, `1 사업의 개요`, `가. 사업의 개요`,
  `1. (금융업) 사업의 개요`; several candidates -> the first, noted `multiple_overview:<n>` / `multiple_products:<n>`)
  and fetched from `https://dart.fss.or.kr/report/viewer.do?rcpNo=..&dcmNo=..&eleId=..
  &offset=..&length=..&dtd=..`; HTML -> text keeps paragraph breaks and writes one line per table row (cells joined
  by ` | `, block tags inside a cell a space, nested tables inside-out; the leading title dropped, also when broken
  by `<BR>`). A 200 section page must look like a section (a `<P class='section-N'>` element or the item's title in
  its first lines); otherwise (an HTTP 200 error / throttling page) it is crawl_state `error`
  `section_unverified;bytes:..;sha:..;title:..` with no documents row, retried next run. Stored text = 사업의 개요 + `\n\n【주요 제품 및 서비스】\n` + products (capped at 8,000 chars); the
  minimum length (80 chars) and the short description use 사업의 개요 alone. Without a 사업의 개요 item, the whole
  chapter is fetched when it has a range (capped at 20,000 chars, note `fallback:chapter`). A failed products fetch
  keeps the overview (`products_http_<n>` / `products_deadline` / `products_stall` / `products_network:...` /
  `products_unverified`). Extractor `dart-web-v1` (earlier `dart-v1/xml` rows are re-extracted once); a result
  missing a part for a temporary reason (failed products fetch, truncated body) is stored under
  `dart-web-v1/partial` and fetched again by the next run (no `--refresh` needed). The skip test compares the
  extractor, so switching between modes `web` and `zip` re-extracts every company once. Notes like
  `web;overview;products`; raw sha256/bytes of the fetched section HTML (the HTML itself is not stored); filing
  snapshots of web documents record rate key `dart-web`, auth `none`. A main.do page without TOC nodes is
  crawl_state `error` note `no_toc;bytes:..;sha:..;title:..`; HTTP errors `main_http_<n>` / `section_http_<n>`
  (3xx with `;location:..`). 3 consecutive website refusals (main.do without TOC or 3xx, viewer.do 3xx or
  unverified; reset by a verified section) stop the run like a block (`access_refused`, cooldown marker); DART's
  real refusal page has not been seen yet (validate from these notes on the first live run). 5 consecutive
  `section_too_short` results stop the run (`consecutive_errors`).
- Mode `zip` (`sync(mode='zip')`) keeps the OpenDART document.xml path ('II. 사업의 내용 / 1. 사업의 개요' from the
  DART XML, extractor `dart-v1/xml`, deadline 300 s); impractical at the measured throttle.
- Rates: OpenDART 2 requests/second (rate key `dart`, budget lock `dart`); the website >= 1 s between request
  starts (rate key `dart-web`, budget lock `dart-web` taken before `dart`, deadline 60 s per page; the CLI holds
  and reports busy (exit 4) for `dart` only, so another process holding `dart-web` is a plain error, before any
  request). Per company in web mode: list.json +
  main.do + 2 viewer.do = 4 requests, ~5-6 s. Status '020' (quota) and '101' (improper access) stop the run like a
  block; key/IP errors and maintenance stop it too. HTTP 403/429 or a challenge on the website is a block: cooldown
  marker (URL of the website page), run stopped.
- Tier **official-private**: no explicit redistribution grant. Personal use; keep text local.

### MOPS / TWSE (`sync-mops`, sources `mops_annual_report` and `mops_basic`)
- Taiwan (TWSE / TPEx lines). No API key. Lines whose TradingView symbol is a 4-digit company code (`[1-9]ddd`,
  KY companies included) map to the MOPS code (`identifiers` `mops_co_id`, method `symbol`); REITs (`01001T`), ETFs
  and 6-digit codes are not queued (summary `non_company_code`). ~958 Taiwan lines >= $200M (651 TWSE, 307 TPEx),
  of which ~953 companies are queued (646 TWSE, 307 TPEx; the other 5 are REITs).
- Paths compared live 2026-09-27 (19 research requests + smoke tests, no block, captcha or challenge at >= 1.2 s
  between requests):
  - TWSE OpenAPI `t187ap03_L` (official-open, 1,095 TWSE companies, 1.3 MB in 17 s) and the TPEx equivalent have no
    business text: not used.
  - MOPS basic data 主要經營業務: `POST https://mops.twse.com.tw/mops/api/t05st03` `{"companyId": "<code>"}` (the JSON
    API behind the new MOPS site; ~5 KB, ~0.5 s; TWSE and TPEx alike). The field is short and often only keywords
    ('PCB', '金融控股公司業'; TSMC ~100 chars) and the API joins the filer's line breaks with '、' ('積體電、路'): an
    official L1 hint, not L2 evidence. The classic page `mopsov.twse.com.tw/mops/web/ajax_t05st03` has the same
    field with clean breaks, but mopsov's robots.txt disallows everyone but bingbot: not used.
  - Annual report 股東會年報 (L2): the new MOPS page 年報及股東會相關資料 itself posts to TWSE's e-document server
    `doc.twse.com.tw/server-java/t57sb01`, the only official source of the report. **Its robots.txt is
    `User-agent: * / Disallow: /`**: fetch slowly, on demand or in one yearly pass, never in parallel bursts.
    (mops.twse.com.tw and openapi.twse.com.tw have no robots.txt.)
- Default and opt-in: `sync-mops` with no flags runs mode `basic` (the MOPS API, no robots.txt). Mode `annual`
  crawls doc.twse.com.tw, whose robots.txt disallows crawlers, so it runs only for named companies (`--codes`) or
  with an explicit `--full-pass` (all ~953 companies: the one slow yearly pass); `--mode annual` alone (even with
  `--limit`) is refused before any request with an explanation (exit 1). The library matches:
  `mops.sync(mode="annual")` without `codes` or `full_pass=True` raises ValueError. Whether bulk passes on this
  host are acceptable at all is still the owner's call; until then keep to `--codes` / on-demand `fetch_company`.
- Mode `annual` (`--mode annual`), per company 3 requests on doc.twse.com.tw (>= 1.5 s between starts, rate key
  `doc.twse.com.tw`, budget lock `mops`):
  1. `GET t57sb01?step=1&colorchg=1&co_id=<code>&year=<ROC meeting year>&mtype=F` (Big5 HTML, ~8 KB): the current ROC
     year (AD - 1911), else the previous one (January-May, before the meeting's report is filed). The annual report
     is the row whose file is `<AD fiscal year>_<code>_<meeting date>F04.pdf` and whose description names 年報
     (`股東會年報(尚未適用永續揭露準則)` today; the English copy `FE4` and `年報前十大股東相互間關係表` `F17` are not
     used): newest fiscal year, then newest upload. `查無所需資料` = no documents that year. The listing HTML is
     saved raw per company (aux batch).
  2. `POST t57sb01` `step=9&kind=F&co_id=<code>&filename=<file>` -> a 503-byte page linking a temporary copy
     `/pdf/<file stem>_<YYYYMMDD_HHMMSS>.pdf` made for this request (the link cannot be guessed; always 1 request).
  3. `GET` that PDF (deadline 300 s, cap 128 MB). Measured: 2.2-7.0 MB per report; 0.25-0.76 MB/s.
  Section: PyMuPDF text (pypdf fallback), running headers and page numbers dropped, paragraphs re-flowed (helpers of
  sources/cninfo.py), control characters dropped, Kangxi / CJK-radical code points mapped to the ideographs ('⼿'
  -> '手', '⻑' -> '長': seen live in a KY company's report; they would break keyword matching). Start = a line that
  is exactly `業務內容` with optional numbering (`一、`, `5.1`, `(一)`, `肆、`; TOC rows carry page numbers and never
  match). End = the first of `市場及產銷概況`, the next chapter (`伍、`, `第X章`), the next item of the start's own
  numbering (`二、` after `一、`; `5.2` after `5.1`, a CJK title required so '5.7%' is not an end), or 60,000
  chars. The first candidate with >= 200 chars wins (a TOC entry ends at its next TOC row); a candidate followed by
  >= 4 TOC rows in page order (dot leaders / a dash, or a numbered heading, then a page number) is skipped, while
  table rows such as `晶圓 85` / `合計 100` never count as TOC rows (note `toc_skipped:N` when nothing is found).
  Without one, the longest chapter `營運概況` up to the next chapter (note `fallback:chapter`, 30,000 chars; a
  financial holding company's `(1)業務內容` layout lands here). No text layer -> `extract_failed` `no_text_layer`. Stored text = the whole 業務內容 item
  (業務範圍, 營業比重, products, 產業概況, R&D, business plans). Extractor `mops-ar-v2` (`/partial` when the PDF hit the
  cap; v1 mistook integer share tables for a TOC). documents: doc_id `mops_annual_report:<code>:<file stem>:business`, form `股東會年報`, accession = file name,
  filing_date = upload date, report_date = 12-31 of the fiscal year (assumed), url = the listing page. Short
  description (`descriptions`, lang `zh`) from the first real paragraphs (business-registration items such as
  `C306010成衣業` and paragraphs under 30 chars skipped).
- Incremental: a company whose stored document is the fiscal year before today's (the newest that can exist) is
  skipped **without any request** (`skipped_without_request`), unless that document is an `extract_failed` row
  without text (then the listing is fetched again, and the PDF when file or extractor changed); otherwise the listing is fetched and the company is
  skipped when file and extractor are unchanged. `--refresh` re-downloads (also for a re-uploaded correction).
- Mode `basic` (the default): 1 POST per company to the t05st03 API (>= 2 s between starts) -> `descriptions`
  source `mops_basic` (snapshot = the aux batch holding the raw JSON); code 200 without the field (or hidden) ->
  `no_basic`; a refusal / throttle message in the JSON `message` (`查詢過於頻繁`, ...) stops the run at once; any
  other `code != 200` is `error` and counts as an unverified answer (3 in a row stop the run with the cooldown).
  Companies that already have a `mops_basic` description are skipped unless `--refresh`.
- Stops: http.Blocked (403/429/Cloudflare challenge) at once; the MOPS security page (`FOR SECURITY REASONS` /
  `因為安全性考量` / `查詢過於頻繁`) and the F5 BIG-IP ASM rejection page TWSE hosts sit behind (HTTP 200, title
  `Request Rejected`, `The requested URL was rejected` / `Your support ID is`) at once as `access_refused` with the
  cooldown marker; 3 consecutive unverified answers (a listing
  that is neither rows nor `查無所需資料`, a step-9 page without link, a 'PDF' that is not one, any 3xx; notes carry
  bytes / sha / page title / Location) the same way; 5 consecutive errors (`consecutive_errors`). MOPS's real refusal
  page on these hosts has not been seen yet.
- Smoke test 2026-09-27 (temporary home, 5 companies 2330 / 2881 / 4958 KY / 5274 TPEx / 4438): 5 of 5 `ok`,
  15 requests, 23.7 MB, 110 s (~22 s per company, dominated by the PDF download); sections 5,979-21,805 chars
  (2881 financial holding: `fallback:chapter`, capped). Basic mode: 5 requests, 26 KB, 9 s. Expected full pass for
  the ~953 companies >= $200M: 3 requests and ~4.7 MB each, ~2.5 h at 0.76 MB/s to ~6 h at 0.25 MB/s (~4.5 GB
  downloaded, only the text kept); later runs need no request until the next fiscal year's reports.
  Re-check on the committed code (same day): 6488 TPEx `ok` (3 requests, 1.7 MB, 6 s, 142 pages, 11,272 chars),
  2317 `--mode basic` `ok` (1 request, 5 KB), and a re-run of 6488 made 0 requests (`skipped_without_request`).
- `fetch_annual_report(client, code)` / `fetch_company(cfg, code)` return one company's section without touching the
  database (on-demand L2 text for the few Taiwan L1 passes of a screen). `fetch_company` keeps the sync's cooldown
  discipline: within 24 h of a recorded `sync-mops` block it raises `CooldownActive` before any request (unless
  `after_block=True`), and a block or refusal page it meets writes the `sync-mops` cooldown marker before it
  propagates. It holds the `mops` budget lock (`Busy` beside a running sync).
- Tier **official-private** (both sources): issuer-authored reports / TWSE-operated site, no redistribution grant.
  Personal use; keep text local; never in the open data pack.

### BSE India (`sync-bse`, source `bse_annual_report`)
- Personal use only (tier **official-private**): issuer filings hosted by the exchange; no redistribution grant
  found and no BSE terms-of-use page located (checked 2026-09-27), so nothing is redistributed. Text stays under `data/docs/bse/<scrip code>/`; `documents` holds
  metadata, `descriptions` a ~1,200-char excerpt (lang `en`). No key needed.
- Path chosen (live probe 2026-09-26/27): per company one listing call
  `api.bseindia.com/BseIndiaAPI/api/AnnualReport_New/w?scripcode=<code>` (4-12 KB, 0.5-1.4 s) and one PDF from
  `www.bseindia.com/xml-data/corpfiling/AttachHis/<uuid>.pdf` (the report as filed, often notice + report + financial
  statements: 1.1-17.4 MB, 61-678 pages, 0.5-4.7 s). Nothing smaller carries the company's own words: the scrip list
  has no description, BRSR XBRL covers only the top 1,000 companies with a one-line activity, and ranged reads of
  single pages would need tens of requests per page at >= 1 s each. Both hosts need `Referer` / `Origin`
  `https://www.bseindia.com`; there is no robots.txt (the SPA answers every path).
- Mapping: universe lines on `BSE` / `NSE` with an `IN...` (or no) ISIN -> the Equity rows of
  `ListofScripData` (5,048 active scrips, 1.8 MB) by ISIN; a BSE line without an ISIN hit falls back to symbol =
  scrip id or a numeric symbol = scrip code. `identifiers` id_type `bse_scrip_code`. TradingView's `BSE` is Bombay; a
  `BSE` line with a CN ISIN is Beijing and belongs to `sync-cninfo`. On 2026-09-27, ~4,580 of 5,392 Indian universe
  companies mapped; the 814 unmapped are mostly NSE-only (Emerge SME) companies without a BSE scrip. The scrip list a
  run saved is reused for 3 days (snapshot note `reused_raw:<path>`); a download with fewer than 3,000 rows stops
  the run (`scrip_list_suspect`) before identifiers change.
- Report choice: the highest `Year` label (BSE's fiscal-year label: FY 2025-26 -> 2026); in a year a `Revised` row
  beats the original. `report_date` stays NULL (the fiscal year end is not given; the label is in the extract note
  `fy_label:<year>`); `filing_date` = the revision / authorise time. `stale:<year>` when the label is behind the one
  that must be out *assuming* a 31 March fiscal year (AGMs by 30 September) and the filing is over 365 days old (so
  the few June / September / December year-end companies, foreign-group subsidiaries, are not flagged while their
  next report is not due; how BSE labels such years was not checked live). A company whose stored section already
  has the newest possible label (this year from April), or was filed less than 270 days ago whatever its label, is
  skipped before any request (`skipped_current`, unless `--refresh`); a report URL already processed is not
  downloaded again. With `report_date` NULL, Layer 2 dates these reports by `filing_date`.
- Section (extractor `bse-v2/pymupdf`; v1 failures are retried once under v2): the MD&A chapter (SEBI LODR Reg. 34 makes it mandatory), found through the
  PDF outline when it names it (verified within +-2 pages) or by its heading in a page's head blocks (first 10
  blocks + blocks in the top 30% of the page; split headings, `Annexure B` prefixes; contents pages and
  directors'-report pointers such as `forms part of` / `is included in this Annual Report` / `is enclosed as Annexure
  C` / `furnished separately` ignored; a section divider listing two or more other chapters is tried last, and a
  heading page whose range holds under 300 chars gives way to the next heading page); it ends at the next outline entry or statutory heading (Corporate
  Governance, BRSR, Auditor's report, Annexure, ...; a block already printed on an earlier MD&A page, i.e. a
  section tab such as `Financial Statements` or `Board's Report`, does not end it), at most 25 pages, and on its
  first page the previous
  chapter's blocks are dropped. A business sub-heading (`Company Overview`, `Business Review`, `Our Business`,
  `Segment-wise performance`, ...) found after the first 1,500 chars is moved to the front with 4,000 chars of the
  opening kept after it (`mda_part:business_first`; large companies open with the economy), else the chapter is
  kept as is (`mda_part:opening`). A front-matter `About the Company` / `Who We Are` / `<Name> at a Glance` page is
  put first (`overview:outline|heading`, <= 5,000 chars); pages about the report, the chairman / board, ESG /
  sustainability or the year, directors' report pages and a heading-scan `Business Overview` item do not count, and
  a page with under 150 chars of text (a numbers-only `at a Glance`) gives way to the next one. Without an MD&A: the overview alone, else the notes'
  `Company information` paragraph (`fallback:corporate_information`), else the directors' report `State of the
  Company's affairs` (`fallback:directors_report`), else `extract_failed` `no_section`. Blocks without enough
  letters (tables) and short blocks repeated on 3+ pages (running headers, digits masked) are dropped. Cap 40,000
  chars; minimum 300 (120 for a fallback). Only the pages needed are parsed (`pages:<read>/<total>`); no OCR
  (`no_text_layer`).
- Limits and failures: PDFs capped at 64 MB (`too_large:>64MB`, recorded, not retried under the same extractor),
  300 s deadline each. HTTP 403/429, a Cloudflare-style challenge (`http.challenge_reason` knows no Akamai
  signature; an Akamai denial arrives as a 403 or as a page below) and the first soft block stop the run at once
  (cooldown marker `sync-bse`, exit 2, 24 h cooldown): a listing redirect (`redirect_refusal`; BSE sends refused API
  calls to a members page), an HTML page instead of the listing JSON (`non_json_page`) and an HTML page instead of
  a PDF (`non_pdf_body`). The one exception is a page whose `<title>` names BSE with no denial / challenge wording
  (the SPA's answer to a dead link): `error` `pdf_html_page`. Broken JSON (`list_bad_json`) and other non-PDF bytes
  (`pdf_not_pdf`) are ordinary errors; 5 consecutive errors stop the run.
- Rates: one client, >= 1 s between request starts per host (`api.bseindia.com`, `www.bseindia.com`; budget lock
  `bse`); listings on the main thread, PDFs + parsing on `--workers N` threads (default 2, 1..3; PyMuPDF serialised).
  Measured 2026-09-27 on 5 companies (HDFC Bank, TCS, Persistent, Vallabh Steels, Gconnect): 11 requests incl. the
  scrip list, 43.7 MB, 12.9 s end to end, 2.1 s per company with 2 workers (PDF mean 8.4 MB, parse 0.2-1.3 s);
  a 2-company `--codes` rerun reused the scrip list: 4 requests, 3.3 s per company. A screen's few hundred companies
  take roughly 10-20 minutes and 2-4 GB of transient downloads (PDFs are not kept).
- Layer 2 reads `bse_annual_report` like the other official sources (`screen.OFFICIAL_DOC_SOURCES` `("BSE", "en")`,
  native id `bse_scrip_code` -> `documents.cik`; `coverage` counts it as `official_document`). With `report_date`
  NULL the fiscal year is taken from `filing_date`.

### On-demand fetch during a screen (`jevscreen.ondemand`)

After a screen's first report, the layer-2 companies that read only a profile get their newest annual report from the
official source of their market, through the adapters above and under their own rules: one child process per source
running that adapter's `sync(codes=...)` under its rate-budget lock (another process using the source: skipped, no
request), its polite limiter, its cooldown marker and stop on the first block (only that source stops), its
consecutive-error stop and size / time caps. Nothing is requested without the source's prerequisite (SEC contact
email, OpenDART key, a PDF reader for CNINFO / BSE / MOPS, a recorded `consent set mops-annual yes` for MOPS) or
within 24 h of a block (marker file or `runs` journal). Per run at most 8 MOPS, 20 DART and 40 BSE companies. Recent
failures are not retried for a while (no report 14 days, extraction failed or unsupported form 30 days, network error
1 day; an extraction that failed only for want of a PDF reader, or with an older extractor, is retried);
`fetch-docs --retry-failed` overrides. The fetch has a wall-clock budget (screen 120 s, quickstart 150 s, fetch-docs
300 s): at the deadline each child gets SIGTERM and flushes, SIGKILL 20 s later as a last resort. Rows land in `documents`,
`crawl_state`, `snapshots` and `runs` exactly as the source's own sync writes them (official-private, kept local).
EDINET is not fetched on demand. Every company still on a profile gets a reason code for the report
(`gaps.l2_doc_unavailable`); fetched text is fact, the L2 label inference, the reason a gap.

### API keys
- Keys are read at call time only (config.edinet_api_key() / opendart_api_key()), never committed, printed, logged,
  journaled or stored in snapshots: keys travel in query strings, so every recorded URL, error text, run note and
  cooldown marker shows `<edinet-api-key>` / `<opendart-api-key>`.

## Automatic translation (local model)
- `jevscreen keywords` / `screen` translate a non-English idea (CJK / kana / Hangul, or mostly non-Latin letters)
  with Qwen3.5-4B from the local Hugging Face cache,
  offline (`HF_HUB_OFFLINE=1`, `TRANSFORMERS_OFFLINE=1`): no download, no paid call. Output (`idea_en`, keywords per
  language) is cached under `data/keywords/<sha256(idea)[:16]>.json`; invalid model output is cached for one hour
  (`<key>.failed.json`) so repeated screens do not re-run a failing generation. Unavailable model -> the idea is used
  verbatim with a warning. `idea_en` enters the paid Jev question text: regenerating it (`--refresh`, a new
  `PROMPT_VERSION` or model, deleting the cache) re-prices the L1/L2 answers of translated ideas.

## Calibration (sieve)
- One file per idea: `data/sieves/<idea_key>.json` (`jevscreen.sieve/1`; the key is the keyword-cache key,
  sha256(idea.strip())[:16]). It is the single source of truth and the user's AI may draft or edit it (facets,
  target_terms, should_pass / should_fail examples with `source: "sieve"`). `facets` = {category, target, mechanism,
  type?}; `type` is one of `technology` (the target is what the thing is for: 'AI agents', 'humanoid robot joints'),
  `geography` (the target is a place: 'Southeast Asia', or a customer segment in a place: 'consumers in Indonesia')
  or `customer` (a customer segment: 'small businesses', 'Chinese EV makers'). It picks the rule templates; the AI
  drafting the sieve should set it. Without it the type is detected conservatively: geography only when the target
  is a place, never from a demonym used as an adjective ('traditional Chinese medicine makers' is a customer
  segment, 'data centers in the United States' is technology). It is written atomically (tmp file +
  `os.replace`) under a version check: a writer that read an older version is refused (请重新读取). `screen` loads it
  automatically (`--sieve auto|none|PATH`); a reworded idea gets a new key, and screen names the closest existing
  sieve (character-bigram Jaccard >= 0.6) so it can be passed with `--sieve PATH`. `jevscreen sieve check` / `sieve
  show` inspect it for free.
- Pins are judgments, not evidence. A card answer (`source: "card"`) pins the company: a yes is always listed and
  scored with the user's label (a yes ranked below the top max_out is still listed, with its real rank and marked
  在截线外), a no is removed from the ranking and listed under 你排除的 with its evidence (never silently dropped). A
  pinned company is asked again (recheck card) only when its filing changed, not when only the excerpt did. Each row says where its verdict came from: `verdict_source` evidence | user | evidence+user; a
  yes the filing does not support reads 用户判断（年报未写明）. Rows that enter the top only because others were removed
  are flagged `backfill` (递补，未经确认). Pinned and should_pass / should_fail companies are always read by L2, even
  when L1 missed them (the report says L1 漏掉了 X). should_pass / should_fail examples are checks, never pins.
- Only library rule texts enter a Jev question: the fixed English sentences of `calib.RULES`, chosen by the answers'
  reason letters (c-k) and filled with the sieve's facets. The user's free text never does. `validate_rule_text`
  keeps universe company names, tickers of >= 4 characters (the run's own keyword terms, e.g. SAML, excepted) and
  30-character card quotes out of them; `screen` enforces it on the sieve's rules before anything is sent. A rule is
  adopted only after a trial (about 20 companies x 2 reads in different packets, a few tenths of a cent) agrees
  better with the answers and knocks out no yes answer, no should_pass company and at most 2 anchors; a trial in
  which some company got no answer adopts nothing, and only a rule set that passed together is adopted (never the
  passing singles of a rejected combination). The agreement is in-sample (用你的回答算的) and is labelled so. Nothing
  numeric is fitted.
- Scope answers (`sieve.scope_answers`, written by `jevscreen decide` and by the idea-wording defaults of
  quickstart `--facets`): the human's answer to a question about a KIND of company (a buyer, an upstream parts
  maker, hardware sold to operators, a holding, a company that only uses the target, business outside the target
  place, an excerpt naming only the broad category), never about one company. They never enter `sieve.rules` or the
  L2 question. They are enforced in the ranking step over the facet labels (facet layer: cheap Jev choice questions
  over the same L2 text, `screen_results` layers `facet_role` / `facet_scope` / `facet_geo`, results.json `facets`)
  at p >= 0.60 (a question is asked only at p >= 0.70): 不要 removes that kind (excerpt-only: moves it to the end),
  directly and reversibly, every removal listed with its reason (推断) and one undo token; an answer is tied to the
  sieve's facets (`facets_sha`) and goes inert when they change. Idea-wording defaults (`source: idea_wording`) must
  quote the idea's own words and are shown with their undo token. 精调 rules that contradict a scope answer are not
  adopted.
- The user's AI's review (`data/sieves/<idea_key>.agent.json`, `jevscreen judge`): the AI reads the same excerpt
  the system read (the deck is a local file in the run folder; personal use, covered like the rest of the run's
  outputs) and answers yes / no / unsure per company citing sentence numbers. Its calls are a separate layer below
  the human's: never `user_verdict`, never a re-score, never an unverified company listed, only on the evidence it
  read (`evidence_sha`). Strong disagreements and unsure meanings go to the human as optional questions; weak ones
  apply with the reason shown. Precedence in the ranking: scope answers, then the AI's calls, then the human's pins
  (a human pin wins over both). A new version made from answers is `rank_only`: the stored answers re-ranked, no Jev
  call, $0 (`params.rank_only`, `change_kind` scope | agent | decide | reapply).
- Repeated reads: L2 items whose read-0 P(explicit)+P(partial) lies in 0.30-0.75 get `--reads K` (default 3) reads
  in total, each a separate cache entry (`Question.read` salts the item key only when > 0, so read-0 keys and the
  whole cache stay valid) with reshuffled packets; the label is the argmax of the MEAN probabilities. Rows with a mean
  of 0.40-0.60 are marked 边缘. `jev_items.read_index` records which read an answer is. Compare two configurations only
  on averaged reads: one fresh draw per item is noise. `--reads 1` is the single-read screen.
- `--from-run RUN_ID|OUT_DIR|results.json` never touches L1: the base run's stored L1 answers (`screen_results` layer l1) are loaded, its
  params are the defaults (options given override them), and companies absent from it do not pass L1 (counted in the
  notes). Only L2 inputs whose text or question changed, plus band reads, are paid for.
- Keyword learning is local and free: a run's terms that appear in >= 5% of a source's filings become weak
  (down-weighted when choosing the keyword paragraph, never deleted); local vocabulary is mined from the filings of
  yes companies behind precision gates (yes-doc support, background df, lift against L1 core/adjacent, not a company
  or product name, not in a rejected company's excerpt) and must pass a peer preview (no yes company loses its
  keyword paragraph). Keyword changes affect L2 excerpts only, never L1. The background df per source is cached in
  `data/calib/df-<source_id>.json`, keyed by the document count, the newest fetch time and the zh variant table.
- Costs: cards, the free re-ranking, keyword learning and `sieve check` are $0; a rule trial about $0.002; an apply
  screen about $0.02 after a rule change, under $0.002 for pins / keywords only; L1 $0. `answer --apply-budget`
  (default $0.05) caps trials + the apply screen together: a higher estimate of the planned reads stops before any
  paid call (exit 5, the answers stay saved; the suggested budget covers the extra reads too). The estimate line
  names the planned trial reads (约), the most the boundary re-reads and the control read can add, and the apply
  screen's upper bound including the should_pass extra reads; the clients stop at the cap, and a trial that runs
  out of budget for its extra reads adopts nothing (the rule stays untried).
- Licence: cards.md / cards.json and answers.json quote annual reports (official-private) and gray-private company
  descriptions: personal use only, like report.md; they stay local.
- v1.1 (fixes from the live accuracy test of 2026-09-26):
  - Keyword mining: a mined term must be a whole word (no start inside a katakana / Hangul word, no word-final Han
    start such as 化, no Chinese term across 的 / 和, no Han start glued to the same left character in 2/3 of its
    hits: 限管理, クセス制御, 置和管理 are refused), found at >= 3 distinct yes companies (all of them when fewer),
    and an acronym only when the run seeded it or it has >= 4 letters and is not a generic one (SQL, VPN, POS, HTTP,
    regulators, filing types). Each new term is tried alone on the yes / should_pass companies and dropped when it
    costs one of them its keyword paragraph or an old term of its excerpt; the peer preview refuses the same.
    Moving a yes company's excerpt off a term the change makes weak is not 'worse' (that is what down-weighting is
    for). Learned adds (log source `mined` / `replacement`) are never seeds: only the model's and the user's terms
    and target_terms let an acronym through. Mined adds from before these gates (log without `gate` 2) are re-gated
    at the next answer (acronym, word fragment over the run's texts of that language or, without them, the static
    checks, fewer than 3 yes companies) and removed with a log entry (`action: remove`, `source: regate`, `why`);
    the console shows them as -term. An add without a mined log entry is the user's own and is never re-gated.
  - Seeds that appear in no filing of their source (df 0, typical for the model's ja / ko terms) get replacement
    terms from the L2 texts of the yes companies of that language (answered yes / should_pass, or verified with
    p̄ >= 0.8), behind the same gates, only with >= 3 such companies, at most 5 per run (log source `replacement`).
  - zh keywords match Simplified and Traditional (MOPS) text through a built-in character table
    (`zhvariants`, no dependency; one-to-many characters such as 复 複 復, 系 係, 于 於, 历 曆 are grouped and every
    group folds to one Simplified form); the background df folds both to Simplified and its cache is keyed by the
    table version too. Only characters are bridged, not vocabulary (软件 / 軟體, 信息 / 資訊, 身份 / 身分 do not
    match each other).
  - Rule trials: read noise never vetoes a rule. A would-be casualty on the boundary (p̄ 0.40-0.60 or a read spread
    >= 0.15) is re-read twice more first; a remaining casualty is read noise only when the question without the new
    rule reads it below 0.5 as well AND the rule did not push it clearly lower (trial p̄ >= control p̄ - max(0.15,
    2 x the read sd at the control p̄)): a rule that takes a yes company from 0.55 to 0.02 still vetoes. The control
    is one fresh read of the pool at read indices 900/901, shared by the trials: never the base run's read indices,
    so it is not the base run's own answers replayed from the item cache. Noise companies are listed as 读数噪声 and
    are neutral in the agreement. A casualty whose re-reads or control cannot be read (budget, provider) is a gap:
    the trial is incomplete, nothing is adopted and its rules stay untried (a tight budget never adopts what a
    larger one would veto).
  - The deck is seeded by the idea and the L2 inputs (evidence_sha), not the run id: a cached rerun gives the same
    deck.
  - A should_pass company that comes out below its level (or below its base-run label) gets 2 more reads and is
    flagged in the row, the report's 示例检查 and the console; it is never silently lost. It is judged on the label
    the row shows: a profile-only company is capped at partial first and wanted at most at partial, so it is never
    re-read for a level it cannot reach, and its flag says 仅简介，最多算相关.
  - Chips: g (只有大类) on every deck, with a generic rule text without facets; role chips h 买方/客户, i 只持股,
    j 上游零件/设备, k 卖硬件给运营方 map to library rules. Templates follow the facets' type (facets.type, else
    detected: geography / customer segment / technology), so a geography target reads "offered in Southeast Asia".
    A company named exactly like a region (Asia) does not block a geography facet. No template names a product
    kind (cards, chips, terminals) as insufficient, so a chip idea is never told that chip sellers do not count.
    The cards.md legend says what g means on that deck (g不面向<target> on a geography / customer deck).
  - A label read from a company profile alone is at most partial and is marked 仅简介.
  - `--from-run` takes a run id, its output directory or results.json; a dry run from a run sizes the band reads by
    that run's real read-0 band (never below the usual 28%) and says so.

- Author fields (idea_en, seed_terms, should_pass / should_fail with a stored `resolved`, notes), the shells filter
  before L1 and `jevscreen why`: see [SIEVE.md](SIEVE.md), [SHELLS.md](SHELLS.md) and [WHY.md](WHY.md).

## Licence tiers
- Every row → snapshot → source → tier (provenance.py). Gray-private data and anything derived from it never ships
  in a public artefact.
- Taiwan (`sync-mops`): `mops_annual_report` and `mops_basic` are official-private, under the same rules as the
  official-private sources below.
- official-open: `sec_tickers`. official-private: `sec_filing_text`, `cninfo_annual_report`, `edinet_yuho`,
  `dart_business_report`, `mops_annual_report`, `mops_basic`, `bse_annual_report` (personal use; the verbatim text
  and anything derived from it, including labels, lists, screens and pages, stay local, except the EDINET code list
  and 事業の内容 that the open pack ships under its allowlist, see "Open data pack"; short excerpts are sent to the AI
  service to be read, which is not publishing). gray-private: TradingView scanner and profiles, FinanceDatabase
  summaries. Shareable: the code, the user's idea, and outputs built only from the open data pack.

## Keys and consent (agent-first operation)
- Keys live in the data folder (git-ignored), one value per file, mode 0600, written only by
  `jevscreen keys set NAME` (hidden prompt; `--stdin` only when stdin is not a terminal). Names and files:
  `typesafe` -> `typesafe_api_key`, `openrouter` -> `openrouter_api_key`, `vercel` -> `vercel_api_key` (the Jev keys:
  Jev via TypeSafe's official API, OpenRouter or Vercel AI Gateway, same price; one is enough), `sec-email` ->
  `sec_user_agent`, `edinet` -> `edinet_api_key`, `opendart` -> `opendart_api_key`. An environment variable wins over the file. No command prints a key; `keys check` and
  `doctor` report presence, source, file mode and shape only, and `doctor` reads no key file at all (stat only)
  unless `--check-jev` is given.
- `doctor --check-jev` is the only network access of these commands: one free GET to the active Jev provider (no
  model call): `https://openrouter.ai/api/v1/key`, `https://ai-gateway.vercel.sh/v1/credits` or
  `https://api.typesafe.ai/v1/models`; the key is redacted from any error text.
- Gray-private sources need a recorded human answer: `jevscreen consent set gray-sources yes|no` writes
  `data/consent.json` (current value, UTC timestamp, statement, append-only history). No record, an unreadable file
  or an unknown source id all mean no (`consent.require_gray_sources` raises `ConsentRequired`). `refresh-universe`,
  `crawl-descriptions` and `import-fd` check it before any request or file read and exit 1 with status
  `consent_required` without a `yes`. Official sources never need it. An agent records only what the human said;
  see AGENTS.md.

## Open data pack
- `jevscreen pack build` exports only `pack.PACK_SOURCES` (SEC ticker list; EDINET code list and 事業の内容 under
  PDL 1.0 with attribution); every file is checked by `pack.assert_redistributable` (never gray-private; a
  non-official-open tier only with an explicit allowlist grant). No SEC 10-K/20-F text, CNINFO, DART, TradingView or
  FinanceDatabase data, no Jev labels. A build aborts if a configured secret appears in its output.
- `jevscreen pack pull` verifies size + sha256 of every file before importing; imported rows keep their original
  source_id, snapshots have kind `pack:<table>` and note `pack-imported tag=...`; local documents and mappings are
  never overwritten. 403/429 stops the pull (cooldown marker `pack pull`). Details: docs/OPEN_PACK.md.

## Quickstart and the result page
- `jevscreen quickstart` (jevscreen/quickstart.py) runs its download steps through `ops.run_networked` under the
  standard command names (`refresh-universe`, `fetch-fd`, `import-fd`, and `crawl-descriptions` for the optional
  profile fill the human says yes to after the first result): the same consent check, 24 h cooldown after
  a block, rate-budget lock, `runs` journal and block marker as the plain commands, so a block met inside quickstart
  stops the plain command too. It never passes `--after-block`. A network failure without a block is not retried
  for 15 minutes unless `--retry`. Annual reports come only from its fetch step (below): the on-demand children of
  "On-demand fetch during a screen", each under its adapter's own lock, rate limiter, cooldown marker and
  stop-on-first-block (SEC, CNINFO, BSE; MOPS only after the `mops-annual` consent; DART only with an OpenDART key).
- FinanceDatabase bulk file: `equities.bz2` (about 15 MB, one request) from `raw.githubusercontent.com`, then
  `cdn.jsdelivr.net` (90 s each), conditional on the stored ETag / Last-Modified. A host that refuses (403/429,
  challenge) is recorded in `<home>/raw/financedatabase/mirror-blocks.json` even when the other mirror delivers, and
  is not asked again for 24 h; when every mirror refuses or is cooling down, `fetch-fd` is blocked (its own 24 h
  marker). Raw kept under
  `<home>/raw/financedatabase/<date>/equities-<sha12>.bz2` (snapshot kind `fd_equities_bz2`, journaled as
  `fetch-fd`). Imported with snapshot kind `fd_summaries_bz2`; source_id stays `financedatabase_local`
  (gray-private). The file has no security-type column: preferred, warrant, unit and rights lines are skipped by
  ticker ending (`-P?`, `-PR`, `.PR`, `_P`, `-W`, `-WT`, `-U`, `-R`; a Thai `-R` NVDR is kept). Stale deletion
  removes only rows of an earlier bulk import, never rows of the DuckDB import. `--fd-file PATH` imports a manual
  download. Both need a recorded `yes` for gray-sources before any request or file read.
- Consent statement version 4 (`consent.STATEMENTS`) says it in the owner's casual tone, still truthfully: the stock
  list and profiles come from TradingView and Yahoo, a gray area whose terms don't allow automated bulk use, with
  occasional rate limits or short blocks; the data stays on the user's computer for their own research and is not
  shared; annual reports downloaded from official sites (SEC, CNINFO, BSE, ...) are for their own use too; profiles
  and report excerpts are sent to Jev (the AI service) to read. The Chinese answer words are 可以 (yes) / 不要 (no).
  Version 3 said the same formally (and named OpenRouter as the AI service); version 2 added the AI-service
  sentence. The record keeps the version, the language and the exact text
  shown. An older `yes` stays valid for the plain commands; quickstart asks once more for the current version.
  Migration note for an existing install: the owner runs `jevscreen consent set gray-sources yes` once.
- Canary: after the human approves the budget and before the estimate, quickstart sends one paid Jev request
  (a fixed one-item question about an invented company, budget $0.001, typically about $0.00005, run id
  `canary-<date>`), so an account without credit (HTTP 402) is found before any real spend. It counts against the
  approval.
- On-demand annual reports: quickstart's `fetch` step (after the screen, before the page) runs the same fetch and
  update pass as `screen --fetch-docs auto` ("On-demand fetch during a screen" above: `ondemand_cli.run_fetch`, one
  child per source under that source's own rules), with a 150 s budget and the update capped at min($0.05, the
  approval's rest); the update's cost counts against the approval. SIGTERM stops it (no update; a resumed worker
  fetches again and the update then reads the stored reports). There is no other on-demand fetch path.
- The job file (`<home>/quickstart/<idea_key>.json`) and the result page are scanned for every configured secret
  before they are written; a hit refuses the write. Neither ever holds a key or the SEC name / e-mail.
- Result page (`page.html`, `<home>/pages/<idea_key>.html`): personal use only, like everything derived from
  gray-private data; it carries TradingView / Yahoo-derived text and verbatim annual-report excerpts and says so in
  its banner. It makes no network request (strict Content-Security-Policy, no external resource) and is never
  uploaded or shared by jev-screen.
  - One page per idea: the prerequisites checklist, the live progress and the results share the stable page. Its
    status part (`live`, jevscreen.pagestatus) comes from the quickstart job file and local files only (consent
    answers, key presence by stat, cooldown markers, doctor's local checks): no network request, no wait on the
    store, no key value (the page is scanned for every configured secret before each write, and a hit means no
    write). The worker rewrites it atomically (tmp + replace, under the page lock that write_page's stable copy also
    takes, so a status tick never puts back older results); it reloads itself (meta refresh, 3 s) only while work
    runs or waits for an answer. No card button and no answer bar: cards are a CLI tool for the user's AI.
  - Evidence must match the claim. The idea's words (page.idea_terms: the run's excerpt terms in every language
    and the sieve's learned keywords minus the weak ones and generic words such as supplier / 系统 / 核心; CJK
    phrases also as their 2-character pieces) are looked for in all the text the AI read for the row (the run
    folder's l2_inputs.jsonl, same document by evidence_sha: the overview and the context pieces), not only in the
    stored excerpt. When the stored excerpt has none of them but another piece does, that piece (cut around the
    first hit) is the quote. Only when none of the text has one is the quote labelled 年报摘录未提到（缺口） /
    "does not mention it (gap)" (简介摘录… / "profile excerpt…" for a profile), and the row is marked borderline
    (badge `no_mention`, the "Borderline" filter). The words named as missing are only words the check looked for
    (never a weak keyword). When no word is in the excerpt's script (Chinese words only and an English profile, an
    English idea and a Chinese filing) there is nothing to check: no gap label. The verdict itself is unchanged (it
    is the AI's inference and says so).
  - The user's call is marked in every view (the cards, the top-10 table, the plain-text list, quickstart `top` and
    the chat lines): a row listed only because the user said yes reads 按你的判断（AI 没从原文确认） / "your call (the
    AI did not confirm it from the text)", never 年报原文; it is not marked borderline, but its quote still says
    when it does not name the idea.
  - One language per page: a zh page is fully Chinese, an en page fully English (labels, sources, forms, dates,
    money, countries, the plain-text list); no language toggle.
  - Names: on a Chinese page an A share shows its CNINFO short name (简称, from the newest stock list in the store,
    joined through identifiers.cninfo_orgid and the line's own code: one org_id carries the A and the B share, 南玻A
    000012 and 南玻B 200012; the org_id alone only when the list has no row with the code, then its A-share name),
    a Taiwan line its MOPS 公司簡稱 (from the basic-data payloads sync-mops stored), any other company the user's
    agent's translation with the original name small below it (else the English name); an English page shows the
    English name only. Share-class tails ("Class A", "ADR") are dropped.
  - Translations (jevscreen.translations) are made by the user's own AI agent, stored apart from the evidence
    (table `translations`, provenance 'agent translation', key sha256 of the text + target language) and never
    replace it: the page labels them "AI 翻译 / AI translation" (a translated excerpt "AI 翻译（不是原文）", never
    "事实") and keeps the verbatim original one tap away; a translated name keeps the original name beside it in
    every place it shows (list rows, cards, the top table, the unverified / excluded lists, the chat text); an
    untranslated text shows the original marked "原文（未翻译） / original (not translated)". A text the agent
    marks keep_original (and a name kept in Latin letters) is stored as itself: resolved, shown as the original.
    A page built while the store stays busy never replaces an existing page, and a new one carries the idea
    page's translations; the export then refuses (store_busy) rather than export already translated texts. Translations are
    derived from personal-use text and stay local like it (never in the open pack). A "what it does" line that
    only repeats the company name is replaced by the excerpt's first sentence.
  - Internal labels never reach the reader: core / adjacent / explicit / partial are shown as 核心 / 相邻 / 明确 /
    相关, the mean fit probability as "AI 判断符合的把握 96%"; the first-read core score only with `?debug` in the
    page URL. Filing forms are plain words (annual_report_summary -> 年报摘要 / annual report summary; 10-K stays
    10-K); an unknown internal form id is left out, as is a missing source.
  - The page also carries its ranked list as plain text in a `<noscript>` block (`jevscreen page --text` prints the
    same), so an agent can read the result without running JavaScript.

## Proposed, not applied: a clearer "explicit" criterion for layer 2

The first novice run (liquid-cooling thermal management for battery energy storage) listed 11 companies and none as
"explicit", although one excerpt says in so many words that the company supplies cooling for 电化学储能系统. The
current criterion ("explicitly states that the company offers or operates a product or service that directly
implements the idea") reads "core supplier" ideas as "the main business must be this". A change of the paid L2
question is not made silently: it changes every L2 cache key (each run pays its L2 once more, about $0.002-0.02), the
`l2_question_sha` the sieves record, and the calibrated rules. Proposal for review:

- explicit: "The text names what the idea is about (the product or service, or a plain synonym) together with the
  application or customer the idea names, as something the company sells, supplies or operates now. It may be one of
  several product lines; how large the line is decides the rank, not this label."
- partial: "The text names only one side (the product without the idea's application, or the application without
  the product), or describes a part, material, enabler, small activity or plan."
- Keep contradicted / insufficient as they are.

Before adopting: an A/B on averaged reads (`--reads 3`) over at least three saved ideas with answered cards, the
agreement with the human's answers reported in-sample, and a note in the run's report that the question changed.
