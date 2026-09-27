# jev-screen

**English** · [中文说明](README.zh-CN.md)

**jev-screen turns an investment idea, written in any language, into a ranked list of listed companies worldwide
whose annual report, or a public company profile, says they do it, with the quote that says so.**

It runs on your computer. An AI model reads the text for you, and you pay OpenRouter a few tens of cents per idea
for it. The list is where your research starts; it is not investment advice.

**What to expect, measured:** before any calibration, about 1 in 4 of the top 40 was clearly right and about 1 in 3
clearly wrong. Supply-chain and Southeast Asia ideas did worse ([details](#accuracy-and-limits)). Treat the list as a
first-pass shortlist, and read the rows marked **annual report** first. They are far more reliable than the rows
marked **profile only**.

## 30-second example

You tell your AI agent:

> Find companies that sell identity and access control for enterprise AI agents.

About 15 minutes later on a first run (about 4 minutes for later ideas), it answers in chat and a page opens in your
browser. The companies below are placeholders, because real rows quote filings and profiles that are for personal use
only. The count, cost and time are typical first-run values.

```text
Done: 34 companies listed (21 confirmed by annual report, 13 by profile only). Cost $0.31, took 12 min.
Results page: opened in your browser. For your personal research only; please do not share.

1. Company A (NASDAQ:AAAA, US) - identity security for human and machine accounts; Clearly fits, annual report
2. Company B (TSE:0000, JP) - privileged-access management software; Clearly fits, annual report
3. Company C (SZSE:000000, CN) - zero-trust access gateway for enterprises; Related, profile only
...
```

On the page, every row carries its evidence, and four kinds of statement are kept apart:

| Label | What it is |
|---|---|
| **Fact** | The passage from the company's latest annual report (with the filing date and a link to the filing), or its profile when no report is available |
| **Inference** | The AI's verdict: **Clearly fits** (`explicit`: the text says it plainly) or **Related** (`partial`: related, or only part of the business) |
| **Gap** | What is missing: no annual report, a stale filing, or no description at all |
| **Your call** | Your answers on the calibration cards (below) |

Below the list you find the companies that passed the first read but could not be confirmed (**unverified**), the
gaps, and up to 8 cards. The page is a local HTML file with a Chinese / English switch, and opening it makes no
network requests.

**How it works, in five steps.**

1. It builds a local list of every primary listed stock worldwide (one main listing per company) with its market cap.
   Blank-check shells (SPACs) are dropped, and A shares under an exchange risk warning (ST / \*ST) are flagged.
2. The AI reads each company's profile once and asks: does its current business match the idea?
3. For the companies that pass, jev-screen fetches the newest official annual report it can get (free, from official
   sites; [which markets](#which-markets-get-an-annual-report-check)), and the AI checks short excerpts from it.
4. It ranks the companies. A label confirmed by an annual report counts twice as much as the same label read from a
   profile, and the model's confidence adds to that.
5. You get the page and the cards, plus `jevscreen why` to answer "why is X (not) in the list?".

### About Jev, the model

The AI is **Jev** (`typesafe/jev-1.13`), a model published on OpenRouter under the name `typesafe`. The project is
named after it.

- **Who serves it:** the [model page](https://openrouter.ai/typesafe/jev-1.13) on OpenRouter names the provider and
  links that provider's data policy.
- **Why this model:** it answers multiple-choice questions with a probability for each option, and it is cheap. A
  first read of about 20,000 company profiles cost about $0.71. The probabilities let jev-screen rank by confidence
  and re-read the companies near the borderline.
- **Who gets your money:** you pay OpenRouter directly with your own key. The code sends no referral, affiliate or
  app id with its requests.
- **Privacy:** if your idea is sensitive, first check OpenRouter's
  [privacy settings](https://openrouter.ai/settings/privacy). They control whether your prompts may be logged, and
  whether providers that train on prompts may be used.

## Start in about 15 minutes with your AI

**You need:**

- **An AI coding agent** that can run commands on your computer, such as Claude Code, Codex or Cursor. It usually
  needs its own paid plan, which is not part of jev-screen.
- **macOS or Linux.** Windows is not supported yet: it is untested, and the hidden key box does not exist there. WSL
  (Linux on Windows) may work but is untested too.
- **An internet connection** that reaches github.com and raw.githubusercontent.com (or cdn.jsdelivr.net),
  openrouter.ai, tradingview.com and the official filing sites. From mainland China, several of these are often slow
  or unreachable. Your usual network setup is fine. What the tool and your agent never do is switch network or
  identity to get around a site that has blocked them.
- **An OpenRouter account with credit.** The first top-up has a minimum, usually a few dollars, plus a payment fee;
  check the payment page on openrouter.ai for current terms. It needs a card that pays in US dollars, or another
  method OpenRouter accepts. That top-up is your first real outlay; each idea then takes about $0.25–0.30 of it.
- **On a Mac,** the first `git` command may open a box that offers to install the command line developer tools.
  Click Install; it takes a few minutes. If your computer has neither `uv` nor Python 3.10 or newer, the agent asks
  you to run one command that installs `uv`, which brings its own Python. jev-screen itself needs no admin rights.

**Tell your agent:**

> Install jev-screen from https://github.com/YuanTong-Wu/jev-screen and follow AGENTS.md. My idea: \<your idea\>.

The agent installs it and then asks you **one round of questions**, all in one message:

1. **Consent to two data sources whose terms restrict this use.** The stock list and market caps come from
   TradingView, and the company profiles come from Yahoo (via FinanceDatabase). TradingView's terms do not allow
   automated use of its data, including algorithmic screening, and the Yahoo text's terms restrict reuse. There is
   no free alternative, so jev-screen uses them anyway, and the risk is yours: a site may block your access, for
   example. The annual reports it fetches from official sites (SEC, CNINFO, BSE India) are personal-use only too.
   All of this stays on your computer, apart from short excerpts sent to the AI to be read
   ([what leaves your computer](#what-leaves-your-computer)). You agree not to share the data or the results. If you
   say no, it stops, because there is no screen without a stock list.
2. **A spending cap, $1 by default.** The agent shows you the English sentence the AI will be asked about and the
   expected cost (about $0.30). The cap is hard: the run stops before it would spend more. Anything above $1 needs
   your explicit yes to that number.
3. **An OpenRouter key.** Create one at [openrouter.ai](https://openrouter.ai/settings/keys). A hidden input box
   opens on your screen for the key, so it never passes through the chat; never paste it there. Giving the key a
   spending limit of a few dollars is a good idea.

After that:

1. Free downloads, about 1 minute. They start while you are still creating the key.
2. A test that the key can pay, about $0.0001.
3. The AI reads, about 2 minutes.
4. Annual reports are fetched, usually about 1 minute and at most 2–3 minutes (free), then the companies whose
   evidence changed are checked again within your cap (at most $0.05).
5. The page opens.

The first time takes about 15–25 minutes, including the OpenRouter sign-up. The agent tells you the progress about
once a minute.

Other questions come only if something changes: the estimate goes over your cap, the budget runs out, the English
sentence changes its meaning, or OpenRouter reports no credit. Optional questions may come with the result, only
when they help your idea (just say no if you like; the result stays):

- **Fill missing company profiles.** When many companies of your idea's market have no profile (often the case for
  China), the AI could not read them. Filling them is free and runs in the background (about 10 minutes for China);
  then the AI reads only the new ones and re-ranks (a few cents, within your cap). The same page updates.

- **An SEC contact for US annual reports** (only when a US company is in the top 10). The SEC asks everyone who downloads filings to include a name and email
  with each request. There is no account; they go only to sec.gov, and a separate address is fine. If you decline,
  US companies are judged from their profiles only, which is less accurate.
- **Taiwan annual reports from MOPS** (only when a Taiwan company is in the top 10). This is off by default, because that site's robots.txt says it does not want
  automated downloads.

[AGENTS.md](AGENTS.md) is written for your AI agent (it is English and technical): let the agent follow it. Every
command and flag is in [docs/REFERENCE.md](docs/REFERENCE.md).

## What it costs

jev-screen itself is free (MIT). It has no server and no account. Downloads and annual reports are free. The Jev
model is paid from **your** OpenRouter credit.

| What | Money | Basis | Time |
|---|---|---|---|
| First idea with `quickstart`: companies from $1B market cap, about 10,000 of them | about $0.30 | estimate (the program expects $0.20–0.40) | 15–25 min the first time, including downloads and sign-up |
| Every further idea | about $0.25 | estimate | about 4 min, plus up to 2–3 min fetching annual reports |
| The same idea again through `quickstart`, nothing changed | $0 | measured (answers are cached) | seconds; a cached `screen` rerun takes about 1 min |
| Answering the cards and re-ranking | about $0.01–0.03 | measured $0.002–0.027 per idea; capped at $0.05 by default | about 1 min |
| A wider screen: about 20,000 companies from $200M (`screen --min-mcap 2e8`) | about $0.71 | measured, on a store with more profiles than a new install | about 4 min of AI reads |
| Test that the key can pay | about $0.0001 | measured | seconds |

Outside jev-screen, you also pay your AI agent's plan and the first OpenRouter top-up (see above).

Every paid request is logged locally with its cost. An answer already paid for is not paid for again. The one
exception is a rare resend after an interrupted request, which costs under 1 cent and stays within your cap. A dry
run (`--dry-run`) prints the estimate without spending anything. Running `jevscreen screen` by hand without flags
uses the program's own defaults: a $3 cap and companies from $200M (about $0.71). Agents always pass `--budget` and
`--min-mcap`.

## Which markets get an annual-report check

| Check | Markets |
|---|---|
| **Annual report, automatic** | China A shares (CNINFO); India (BSE, up to 40 companies per run) |
| **Annual report, if you allow it** | US and companies filing 10-K / 20-F with the SEC (needs your SEC contact); Korea (your own free OpenDART key: `keys set opendart`; up to 20 per run); Taiwan (a separate yes for MOPS; up to 8 per run, slowly) |
| **Japan** | The open data pack, once its first release is published (about 1,600 companies so far, growing daily), or `sync-edinet` with your own free EDINET key |
| **Profile only** | Hong Kong, Europe, UK, Canada, Australia, Southeast Asia and everywhere else, unless the company also files with the SEC |

China, India and Taiwan reports are PDFs. The standard install includes a PDF reader for them (`pypdf`).

## Data and licences

The code is MIT. **The data is not**: each source keeps its own terms, and the MIT licence grants nothing over that
data. The full per-source table is in [DATA_LICENSES.md](DATA_LICENSES.md).

| Data | Source | Tier | When |
|---|---|---|---|
| Stock list, market caps | TradingView scanner | gray-private | after your consent; about 1 minute |
| Company profiles | FinanceDatabase bulk file `equities.bz2` (Yahoo text, 15 MB, from GitHub raw or jsDelivr) | gray-private | after your consent; about 1 minute |
| More profiles, optional | TradingView symbol pages (`crawl-descriptions`) | gray-private | only when you agree; long |
| Annual reports, on demand | SEC EDGAR (US, needs your SEC contact), CNINFO (China A shares), BSE (India), OpenDART (Korea, your key), MOPS (Taiwan, asked separately) | official-private | only for the companies a screen shortlists, capped per run |
| Annual reports, bulk, optional | the same sources plus EDINET (Japan, your free key) through the `sync-*` commands | official-private | only when you run them |
| Open data pack | SEC ticker list; EDINET code list and 事業の内容 (Japan) | official-open / PDL 1.0 | automatic, no key |

The tiers mean:

- **gray-private:** the data is public, but the site's terms restrict reuse or automated use (TradingView forbids
  algorithmic use of its data). It is personal use only, it needs your recorded consent, and it is never
  redistributed, by this project or by you. "Gray" names that conflict with the terms. It is not a legal opinion.
- **official-private:** official filings without a redistribution grant. They are personal use only: the full text
  stays on your machine, and only short excerpts are sent to the AI to be read.

### What leaves your computer

| What | Goes to |
|---|---|
| Your idea as you wrote it, its English sentence, the rule sentences from your card answers, company profile text and short annual-report excerpts | OpenRouter and the provider that serves Jev |
| Download requests. The filing sites see which companies are requested. | TradingView; GitHub or jsDelivr; the official filing sites (SEC, CNINFO, BSE, DART, MOPS); GitHub for the open data pack |
| Your SEC name and email | sec.gov only, with those downloads |

Nothing goes to this project, and there is no telemetry. Keep the lists, reports and pages you make to yourself,
because they are built from personal-use data. Nothing here is legal advice. When in doubt, keep the data on your
own machine.

## Accuracy and limits

The test used 4 ideas. For each one, the top 40 rows (160 in all) were labelled with one of three verdicts:

- **Right:** the evidence directly shows the company belongs.
- **Edge:** related, but the evidence is thin, the business too broad or the geography off.
- **Wrong:** the wrong role, an unrelated business, or only a shareholding.

"After cards" means after one round of 6–7 card answers.

| Idea | Right / edge / wrong | After cards |
|---|---|---|
| Enterprise AI-agent identity and access control | 18 / 13 / 9 | 21 / 14 / 5 |
| GLP-1 peptide contract manufacturing (CDMO) and delivery devices | 11 / 23 / 6 | 14 / 22 / 4 |
| Humanoid-robot joint reducers | 10 / 11 / 19 | 12 / 13 / 15 |
| Southeast Asian digital payments and e-wallets | 5 / 16 / 19 | 5 / 23 / 12 |
| **Total (160 rows)** | **44 / 63 / 53** | **52 / 72 / 36** |

**How this was measured, and what differs for you:**

- **When and what was run:** the runs took place on 2026-09-26 with calibration cards v1. This release ships v1.1,
  whose fixes have not been measured again.
- **The screen setup:** each base screen was the wider one (about 20,000 companies from $200M). It ran on a store
  with bulk-synced annual-report text (about 55% of companies from $1B, from SEC, CNINFO, EDINET and DART) and
  crawled profiles.
- **Your first install has less evidence.** It starts from $1B, with FinanceDatabase profiles (about 69% of companies
  from $1B) and annual reports fetched on demand. US reports need your SEC contact, Japan uses the open data pack, and
  Korea needs an OpenDART key. Expect more rows marked profile only than in this test.
- **Who judged the rows:** the AI agents that ran the trials labelled each row from its evidence. For the reducer and
  GLP-1 ideas they used the full evidence during the run. For the AI-agent and Southeast Asia ideas the rows were
  labelled afterwards from excerpts cut to about 260 characters, so strictness may differ between ideas. The project
  owner checked one company by hand. The labelling was not independent and not blind, and the test covers only 4
  ideas. Read it as a direction, not a benchmark.

What the test shows:

- **Annual-report rows are far more reliable than profile-only rows.** US rows backed by a 10-K were almost all right.
  The wrong Clearly fits rows the test noted, such as a petrol retailer in the payments test, were read from
  profiles only.
- **Strong** when the idea is a product category with its own standard words, and the companies file annual reports
  the system can read. The AI-agent security idea ended with 21 right and 5 wrong.
- **Weak on supply-chain ideas**, where it confuses roles. It took buyers, makers of generic parts, servo or motor
  makers, and mere shareholders for suppliers. The reducer idea still had 15 wrong out of 40 after the cards.
- **Weak on ideas defined by geography.** Companies in Indonesia, Vietnam, Thailand, Malaysia and the Philippines
  have profiles only here. The Southeast Asia idea never got past 5 right, and it took smart-card and POS hardware
  makers for payment operators.
- **Coverage:** a new install has profiles for about 69% of companies from $1B. Companies with no description are
  listed as a gap, not screened. Annual-report text comes from the on-demand fetch for shortlisted companies and, for
  Japan, from the open data pack.
- **Noise:** the model's reads vary. The same input moves by about 0.04 in probability on average, and a rerun can
  swap about 2 of the top 40 near the boundary. Companies near the boundary are read 3 times, and the reads are
  averaged.
- **Cards fix what you answer, not everything.** Wrong rows fell from 53 to 36, but right rows only rose from 44 to
  52, because a removed row is often replaced by an equally weak one.
- It does not value companies, look at prices or tell you what to buy. The rank reflects how strong the evidence is,
  not investment merit.

## Next time, stopping, and removing it

- **A new idea:** open your AI agent in the jev-screen folder and say "Screen with jev-screen: \<new idea\>". It costs
  about $0.25 and takes about 4 minutes, with no new downloads.
- **Old results:** `jevscreen page latest --open` opens the newest page. Each run's page is in its own folder under
  `data/screens/`.
- **Stopping midway:** ask your agent to stop, or press Ctrl-C when you run `screen` in your own terminal. Answers
  already paid for are kept, a rerun continues from there, and your cap still holds.
- **Disk space:** the quickstart path usually needs well under 1 GB. A store with every bulk sync done is about 2 GB.
- **Removing it:** delete the jev-screen folder (your data, keys and consent records are all inside its `data/`), and
  delete the key on [openrouter.ai](https://openrouter.ai/settings/keys).

## Commands

Your agent runs these for you. `quickstart`, `doctor`, `why`, `answer`, `fetch-docs`, `page`, `cards`, `coverage` and
`sieve` take `--json`. The other commands print their own summary and reject the flag. The main flags are listed in
[docs/REFERENCE.md](docs/REFERENCE.md), and `jevscreen <command> --help` lists them all. JSON formats are in
[docs/AGENT_API.md](docs/AGENT_API.md).

| Command | What it does | Cost |
|---|---|---|
| `jevscreen quickstart "<idea>" --json` | The whole path from an idea to the page, with one round of questions | within the cap you approve |
| `jevscreen doctor --json` | What is set up, what is missing, and the next command | free |
| `jevscreen keys set openrouter --dialog` / `keys check` | Store a key from a hidden box on your screen; the key is never shown. In your own terminal: `keys set NAME` | free |
| `jevscreen consent set gray-sources yes\|no` | Record your answer on the restricted sources | free |
| `jevscreen screen "<idea>" --min-mcap 1e9 --budget 1 [--dry-run]` | A screen with every setting (countries, market cap, reads) | paid, hard budget |
| `jevscreen page latest --open` | Rebuild and open the result page | free |
| `jevscreen why <ticker> --run <run_id>` | Why a company is or is not in the list, and what would change that | free |
| `jevscreen answer "1a 2h 3c" --deck <deck>` | Apply your card answers and re-rank | about $0.01–0.03 |
| `jevscreen fetch-docs latest` | Fetch missing annual reports for a run's shortlist and update it | free fetch, at most $0.05 |
| `jevscreen sieve add should_pass <ticker> --run <run_id>` | Add a company you expect; the next run checks it and reports on it | free |
| `jevscreen pack pull` | Import the open data pack | free |
| `jevscreen status` / `coverage` | Row counts; data gaps by region, source and licence | free |
| `jevscreen refresh-universe`, `crawl-descriptions`, `sync-sec` / `-cninfo` / `-edinet` / `-dart` / `-mops` / `-bse` | Manual data syncs | free; some take hours |
| `jevscreen import-fd` | Import profiles from a local FinanceDatabase DuckDB file. This is not needed with quickstart, which downloads the profiles itself | free |

## Calibration cards

An AI reading text makes the same few kinds of mistake over and over: it takes a buyer for a supplier, matches a
word that means something else, or reads a one-line plan as a business. Only you know where your idea's borders
are. So after each run the page shows up to 8 **cards**. Each card is a company near the border, with a short quote
from its evidence (the passage that best matches your idea's keywords).

On the page you click **keep** or **drop**, plus a reason if you like (for example "a buyer, not a supplier"). The
page then builds one line such as `jevscreen answer "1a 2h 3c" --deck deck-...` for you to copy to your agent. It
costs about $0.01–0.03. Your answers are remembered for this idea, and the list is re-ranked without reading the
profiles again. The next run of the same idea uses them too. In the test above, cards cut the wrong rows from 53 to
36; they cannot invent right rows that the data does not show.

The mechanics are in [docs/DATA_RULES.md](docs/DATA_RULES.md) under "Calibration": the reason letters a–k, how
answers become rules, and keyword learning. The accepted answer forms are in the `answer` row of
[docs/REFERENCE.md](docs/REFERENCE.md#commands). What your AI may write into an idea's settings file is in
[docs/SIEVE.md](docs/SIEVE.md).

## Open data pack

The open data pack is a free daily pack, published as GitHub Releases of this repository (`pack-YYYY-MM-DD`). It holds
only data whose licence allows redistribution:

- the SEC ticker list;
- for Japan, the EDINET code list plus the 事業の内容 (business description) section of each company's latest
  有価証券報告書 (annual securities report), under the Public Data License 1.0 with attribution. So far that is about
  1,600 companies, and the number grows daily.

Once the first pack is published, `quickstart` pulls it automatically, so Japanese companies can be checked against
their own filings without an EDINET key. Until then, Japan uses profiles.

`jevscreen pack pull` imports the pack by hand, and every file is checked against its sha256. The pack contains
nothing from TradingView or Yahoo and no other annual-report text.

One caveat is still open: it is not confirmed that the EDINET licence covers issuer-written 事業の内容 text. The pack
says so. Details are in [docs/OPEN_PACK.md](docs/OPEN_PACK.md).

## FAQ

**Is this investment advice?** No. It finds companies whose annual reports or profiles match your idea, and it shows
the evidence. It does not value them, time them or recommend them. Check the filings yourself before any decision.

**What leaves my computer, and who can see it?** See [the table above](#what-leaves-your-computer). In short:

- Your idea, its English sentence, your rule sentences, profile text and short excerpts go to OpenRouter and the
  provider that serves Jev.
- Download requests go to the data sites.
- Your SEC contact goes only to sec.gov.

Keys are stored in `data/`, readable only by your user account, and are never printed.

**Can I share the list or the page?** Not the ones built from the default sources, because they come from
personal-use data. You can share the code, your idea, and anything built only from the open data pack. The same rule
is in [DATA_LICENSES.md](DATA_LICENSES.md) and [docs/DATA_RULES.md](docs/DATA_RULES.md).

**Why is company X missing (or there)?** Ask your agent. It runs `jevscreen why X --run <run_id>` (free) and tells you
in one sentence where X stopped (market cap, no description, the first read, the annual-report check or the ranking)
and what would change that. If you know X belongs, add it as a check (`sieve add should_pass`) or answer its card.

**Can I use it without TradingView and Yahoo data?** No. There is no free, licence-clean list of the world's stocks
with business descriptions that it could use instead, and the open data pack covers only US tickers and Japan. If
you say no to those sources, jev-screen stops and does not ask again.

## Licence

- **Code and documentation:** MIT ([LICENSE](LICENSE)).
- **Data:** each source's own terms ([DATA_LICENSES.md](DATA_LICENSES.md)).
- **Test PDFs:** two of them embed an Apache-2.0 font subset ([LICENSES/Apache-2.0.txt](LICENSES/Apache-2.0.txt)).
- **Security:** report leaks and vulnerabilities privately ([SECURITY.md](SECURITY.md)).
- **Contributing:** before opening a pull request or publishing a fork, run `python3 tools/release_check.py`. It must
  print `release_check: clean`.
