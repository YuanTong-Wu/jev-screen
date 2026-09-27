# Release checklist

What had to change before jev-screen could be published, and what to do on every later release. Inventory taken
on branch `chore/release-scrub` from base commit `7c80218`. Every row is done unless its action says otherwise.

## Gate (every release)

1. `python3 -m unittest discover -s tests` passes (it includes `tests/test_release_check.py`, which scans the tree).
2. `python3 tools/release_check.py` prints `release_check: clean`.
3. Publish from a **fresh orphan squash** (e.g. `git checkout --orphan release && git commit`); this is mandatory.
   Author and commit it with a no-reply address **and a neutral name** (e.g.
   `git -c user.name="jev-screen contributors" -c user.email=<id>+<login>@users.noreply.github.com commit`).
   The private history still holds the original third-party fixtures (removed ones *and* earlier versions of
   fixtures that were overwritten in place, e.g. the real TradingView rows in `tv_scan_usd.json`), old home paths
   in `config.py`, and the owner's name and e-mail in every commit. Purging only the paths that `--git-history`
   lists (e.g. `git filter-repo --invert-paths`) is **not** enough. `python3 tools/release_check.py --git-history`
   on the published clone must be clean: it checks author/committer e-mails and names (against the global git
   `user.name` and this machine's user name) and runs every check on every blob reachable from any ref.
4. New test fixtures: synthetic or hand-made only, declared in `tests/fixtures/MANIFEST.json` with their origin.
   Never a saved page, PDF, filing or API response from a real source.
5. No `data/`, DuckDB file, key file, `sec_user_agent`, screen report or keyword cache in the tree
   (`.gitignore` covers them; the scan refuses them by name).

## Inventory

Categories: **3P** third-party page or document dump, **PII** personal data, **SEC** secret or secret path,
**GRAY** gray-private data, **LIC** licence/metadata, **STALE** reference to a removed item.

| File | Issue | Action |
|---|---|---|
| `src/jevscreen/config.py` | SEC/PII: hard-coded fallback to the old private workspace's OpenRouter key file and FinanceDatabase DuckDB (absolute home paths, old-project folder names) | Removed. Key: env `OPENROUTER_API_KEY`, then file named by `JEVSCREEN_OPENROUTER_KEY_FILE`, then `<JEVSCREEN_HOME>/openrouter_api_key`. FD file: `--fd-duckdb`, `JEVSCREEN_FD_DUCKDB`, `<JEVSCREEN_HOME>/financedatabase.duckdb`. Documented in README and SECURITY.md; tested in `tests/test_config.py` |
| `src/jevscreen/sources/financedatabase_local.py`, `tests/test_financedatabase_local.py` | PII: references to the old project's DuckDB and a dated dump in docstrings | Reworded |
| `src/jevscreen/sources/dart.py` | STALE: docstring named the removed real DART capture and its issuer | Points at the synthetic fixtures |
| `tests/fixtures/tv_scan_usd.json`, `tv_scan_local.json` | 3P/GRAY: TradingView scanner responses (real companies, prices, fundamentals) | Replaced by invented companies with the same shapes and edge cases; generator `tools/synthetic_fixtures/tradingview.py`. The A-share row uses the unlisted `SSE:609999` (the first cut used `600999`, a live code) and the symbol-page company is "Okuzan Motor" (the first cut reused a Toyota-group brand). Other tickers copy each exchange's format and are not checked against live listings (generator docstring); `ASX:RDG` and `HKEX:990` may be live codes: **open**, rename if confirmed |
| `tests/fixtures/tv_profile_TSE-7203.html` | 3P/GRAY: saved TradingView symbol page (real company description) | Replaced by `tv_profile_TSE-9901.html` (invented "Okuzan Motor"); tests in `test_tradingview_profiles.py` and `test_crawl_stream_rules.py` updated |
| `tests/fixtures/profile_graph.html`, `profile_no_financial_product.html`, `profile_challenge.html` | GRAY domain named in fixtures | Kept: minimal hand-made pages (a few lines, one invented sentence); declared `hand-made` |
| `tests/fixtures/dart_web_main_20260310002820.html`, `dart_web_section_overview_…`, `dart_web_section_products_…` | 3P: DART viewer pages of a real issuer's business report | Replaced by `…20260310009990.html` for an invented filer (same TOC idioms and HTML edge cases); generator `tools/synthetic_fixtures/dart_web.py` |
| `tests/fixtures/sec_doc_EGAN_10-K.htm.gz`, `sec_doc_NICE_20-F.htm.gz` | 3P: full EDGAR filings (issuer-authored text, 500 KB); the 20-F contained an investor-relations e-mail | Replaced by small invented 10-K / 20-F (`QKNW`, `QCXL`) with the structures the extractor needs; generator `tools/synthetic_fixtures/sec.py` |
| `tests/fixtures/sec_submissions_{DSGX,EGAN,NICE}.json.gz`, `sec_company_tickers_exchange.json.gz`, `sec_picks.json` | 3P: EDGAR submissions histories and the 10k-row tickers file (175 KB) | Replaced by invented CIKs/tickers (`QKNW`, `QCXL`, `QFRT`); same generator |
| `tests/fixtures/cninfo_300386_2025_summary.pdf`, `cninfo_300386_2025_full_p1-40.pdf` | 3P: real CNINFO annual-report PDFs (860 KB), with company contact e-mails | Replaced by text-only PyMuPDF renders for invented issuer 309386 (37 KB / 98 KB) that take the same extraction path; declared `transformed`. They embed a Droid Sans Fallback subset (Apache-2.0): credited in DATA_LICENSES.md, licence text shipped as `LICENSES/Apache-2.0.txt` (s.4(a)) |
| `tests/fixtures/cninfo_60xxxx_2025_*_pages.txt` (10 files) | 3P: page texts of real bank / insurer / telecom / energy annual reports, with investor-relations contacts | Replaced by `cninfo_6092xx_…` (unlisted codes, not one-digit variants of the source codes): page layout, headings, section numbers, running headers, page numbers and wrapping kept, every other word filler, names and contacts removed; every decimal, percent, thousands-separated and 3+-digit figure has its digits permuted (checked against the source: none survives at its position; report years 2010-2039, months, days and 1-2 digit counts kept); issuer-named test names neutralised; same end notes in every test |
| `tests/fixtures/cninfo_query_300386.json`, `cninfo_stock_list_trimmed.json`, `cninfo_bulk_listing_page.json`, `cninfo_bulk_page_cap_2026-09-26.json` | 3P: CNINFO API responses with real codes, names, orgIds, announcement ids | Replaced with invented values in the same shapes |
| `tests/fixtures/jev_response_*.json` | PII: `_source` notes carried old-workspace paths | Notes reworded; content is the project's own Jev answers, no issuer text; declared `own-output` |
| `tests/fixtures/jev_calibration.json` | Aggregates from the owner's Jev journals | Kept: counts and means only, no text; declared `own-output` |
| `tests/fixtures/dart_*.zip/json/xml`, `edinet_*` | (checked) | Already hand-made with invented filers (테스트전자, テスト精機, E999xx); declared `hand-made` |
| `tests/test_cninfo.py`, `test_cninfo_fast.py`, `test_screen.py`, `test_sec_edgar.py`, `test_dart.py`, `test_tradingview_*.py`, `test_crawl_stream_rules.py` | Tests pinned to the real fixtures' content | Rewritten against the synthetic fixtures, same assertions and edge cases |
| Test code (e.g. `test_cninfo.py` title cases, `BRK.B`, `TSE:7203`) | Real company names, tickers, ISINs and announcement titles as literals | Kept: short factual identifiers, not copyrightable content; no page text |
| `tests/test_*` e-mails and keys | Fake values (`@example.org`, `@example.invalid`, `sk-or-v1-FAKE-…`) | Kept; the scan recognises them as fake |
| `.gitignore` | Missing patterns for key files, `.env.*`, editor/OS files, virtualenvs | Extended; `data/`, `*.duckdb`, `.secrets/` were already ignored |
| `LICENSE` | Missing | Added: MIT, "jev-screen contributors" |
| `DATA_LICENSES.md` | Missing | Added: per-source tiers, what is and is never redistributed, third-party material in the repo |
| `SECURITY.md` | Missing | Added: private reporting, how secrets are read and redacted, the release scan |
| `tests/fixtures/MANIFEST.json` | No record of fixture origins | Added; `tools/release_check.py` refuses undeclared fixtures |
| `tools/release_check.py` | No automated gate | Added (see below); `tests/test_release_check.py` runs it on the repository |
| git history and commit metadata | Original fixtures (removed and overwritten in place), old home paths and the owner's personal name and e-mail live on in history | **Open**: publish a fresh squashed history with a no-reply author (gate step 3) |
| `data/` (git-ignored) | Owner's DuckDB, raw payloads, keys, SEC User-Agent | Never published; the scan refuses any tracked file under it |
| `src/jevscreen/jev.py` key-missing error | Says only "set OPENROUTER_API_KEY or the key file"; names neither the path nor `JEVSCREEN_OPENROUTER_KEY_FILE` | Fixed at the wave-1 integration: a missing key raises `JevUnavailable(cfg.openrouter_key_hint())` (names the place checked, never a value); `jevscreen keys` / `doctor` read the same single file (`Config.openrouter_key_files()`) |
| Calibration work (`src/jevscreen/calib.py`, `tests/fixtures/screen_reads1_golden.json`, `tests/test_calib.py`) | Golden embedded a regulator-template sentence from real A-share summaries; test_calib paired real names with report-style text | Fixed at the wave-1 integration: the seed sentence is invented and the golden regenerated (old code and `reads=1` agree byte for byte); the golden is declared in MANIFEST.json. The first pass renamed only the two rows with explicit text; a review found real issuers still carrying generated report text (synthetic run: Rackspace, HubSpot, SailPoint, Box, Microsoft, two A-share code/name pairs; `test_candidate_rules`: invented quotes for HubSpot, Rackspace, DocuSign) and real pins in `test_jev.py`. All renamed to invented issuers (unused codes `SSE:609292`, `SZSE:309161`), same card order; the `test_jev.py` read-0 digests were recomputed on 7c80218 for invented issuers. The rule-trial tests keep bare keys on the placeholder exchange `X:` with `"<KEY> text"` bodies |
| `tests/test_screen.py` `JA_YUHO_FLAT` | Labelled synthetic, but a sentence-by-sentence paraphrase of a real issuer's 事業の内容 (same product lineup and note numbers, one 58-character clause verbatim) | Fixed at the wave-1 integration: rewritten from scratch for invented "Sorano"; only the statutory headings, item markers and the 将来に関する事項 disclaimer match the old text (longest other shared span: the 22-character statutory opening). The old text stays in private history (gate step 3) |

## `tools/release_check.py`

    python3 tools/release_check.py [ROOT] [--json] [--git-history] [--max-kb 256] [--max-binary-kb 128]

Scans `git ls-files` (tracked and untracked-not-ignored) or, outside git, a walk that skips `data/`, `.secrets/`
and caches. It also looks inside `.gz` and `.zip` members and PDF text/metadata. Findings are redacted (first
characters only). Categories: `email`, `home-path` (including the running machine's home directory and user
name, read at run time), `old-project`, `api-key`, `secret-file`, `large-file`, `fixture`, `gray-domain`,
`history` (`--git-history`: commit e-mails and names, plus the checks above on every blob in any ref that is not
in the current tree; any earlier or removed fixture version is a finding). A line containing `release-check: ignore` is skipped. Exit 0 clean, 1 findings, 2 usage error.

## Migration note for the maintainer's own machine (blocking before master moves to wave 1)

**Do this before fast-forwarding master to the wave-1 integration, and only after any running live test has
finished.** The old fallback paths are gone, and on the maintainer's machine the OpenRouter key and the
FinanceDatabase DuckDB exist only at those old paths: without this step `screen` / `answer` lose Jev (exit 6,
`JevUnavailable`) and `import-fd` finds no DB. Do not reintroduce a hard-coded path.

1. Key: `jevscreen keys set openrouter` (hidden prompt; writes `data/openrouter_api_key`, mode 0600), or export
   `JEVSCREEN_OPENROUTER_KEY_FILE` pointing at the existing key file (then it is the only file read), or export
   `OPENROUTER_API_KEY`.
2. FinanceDatabase: symlink or copy the DuckDB to `data/financedatabase.duckdb`, or export `JEVSCREEN_FD_DUCKDB`.
3. Consent: `refresh-universe`, `crawl-descriptions` and `import-fd` now refuse to run (exit 1,
   `consent_required`) without a recorded answer. The maintainer records it once, in person:
   `jevscreen consent set gray-sources yes`.
4. Check: `jevscreen doctor --json` shows `key_openrouter` and `consent_gray_sources` ok (add `--check-jev` for one
   free key check against OpenRouter).
