# AGENTS.md: installing and running jev-screen for a human

You are an AI coding agent. The human most likely told you, as the [README](README.md) suggests:

> Install jev-screen from https://github.com/YuanTong-Wu/jev-screen and follow AGENTS.md. My idea: ...

They want **jev-screen** set up and one idea screened: an investment idea in, a ranked list of companies with
evidence quotes and a result page out. Keep their idea word for word; if they gave none, ask for it before you
install anything. Assume the human is not technical, is short on patience, and will notice every mistake.

What the human will be asked, in **one** message (the same list as the README, so nothing surprises them):

1. an OK on two sources whose terms restrict this use (TradingView's stock list, Yahoo profiles via
   FinanceDatabase; the statement also says that annual reports from official sites are for their own use only);
2. a spending cap for this idea: $1 unless they say otherwise (expected about $0.30, paid from their own credit at
   their Jev provider);
3. when no Jev key is set yet: which account they have (TypeSafe's official API, OpenRouter or Vercel AI Gateway:
   Jev via any of them, same price), then that key, typed into a hidden box, never into the chat.

Other questions come only if something changes: the estimate exceeds the cap, the budget runs out, the English
sentence changes its meaning, or the Jev provider reports no credit. Optional questions may come with the result, each
only when it helps this idea and labelled optional: filling missing company profiles of the idea's market (free,
about 10 minutes for China, then a re-rank of a few cents), an SEC contact name and email (only when a US company is
in the top 10) and Taiwan annual reports from MOPS (only when a Taiwan company is in the top 10). Before the install the human may also meet two system steps: on a Mac, the first `git` can open a
box offering the command line developer tools (they click Install, a few minutes); and when neither `uv` nor a
Python >= 3.10 exists, they run one command that installs uv (Step 1 below).

Follow the steps **in order**. Every step ends with a **Verify** line: do not move on until it holds.
Machine-readable schemas for every command below: [docs/AGENT_API.md](docs/AGENT_API.md); every command and flag:
[docs/REFERENCE.md](docs/REFERENCE.md).

Speak to the human in their language, in short plain sentences. Never paste raw JSON at them; summarise it.

## Fast path: `jevscreen quickstart` (use this first)

One command family takes the human from nothing to a ranked list with evidence in their browser. The human
answers **one** round of questions (two only if the cost estimate goes over what they approved). Machine-readable
output: [docs/AGENT_API.md](docs/AGENT_API.md#jevscreen-quickstart).

**0. Before you install, say this** (in the human's language; both versions are also in every `quickstart` JSON as
`before_you_start_zh` / `before_you_start_en`):

> 开始前说明：①会问你可不可以用两个数据源（TradingView 的股票清单、Yahoo 的公司简介）：它们的条款限制程序取用，只能你个人研究用、不能分享；②要一个能付费用 Jev（读简介和年报的 AI 服务）的账号：TypeSafe 官方 API、OpenRouter 或 Vercel AI Gateway 任选一家，价格一样。第一次一般要先充值（最低充值额通常是几美元，另有支付手续费，以各家付款页为准），需要能付美元的卡；这次筛选只从余额里扣约 $0.3（约 ¥2），剩下的留着下次用；③第一次约 15–25 分钟（含注册账号）。Mac 第一次用可能弹出「安装命令行开发者工具」，点安装即可（几分钟）。你只需要回答一轮问题。

> Before we start: (1) I will ask if you are OK with two data sources (TradingView's stock list, Yahoo company
> profiles) whose terms restrict automated use: for your personal research only, never shared; (2) you need an account
> that pays for Jev (the AI service that reads profiles and annual reports): TypeSafe's official API, OpenRouter or
> Vercel AI Gateway, whichever you have; the price is the same. The first top-up usually has a minimum (a few dollars,
> plus a payment fee; see the provider's payment page) and needs a card that pays in US dollars; this screen uses
> about $0.30 of it and the rest stays for later. (3) About 15–25 minutes the first time, including the sign-up. On a
> Mac, a box may offer to install the command line developer tools: click Install (a few minutes). You answer one
> round of questions.

**1. Install** (no admin rights, no Homebrew, no system Python needed):

```bash
git clone https://github.com/YuanTong-Wu/jev-screen jev-screen && cd jev-screen
uv venv --python 3.12 .venv && uv pip install -e '.[pdf]'      # if uv is missing and python3 --version >= 3.10:
                                                               #   python3 -m venv .venv && .venv/bin/pip install -e '.[pdf]'
export PATH="$PWD/.venv/bin:$PATH"                             # in EVERY new shell, from the jev-screen folder
```

Every command the JSON hands you (`next_command`, `poll_command`, `agent_try`, `record_answer_commands`, the page's
answer line) starts with a bare `jevscreen`. If your tool starts each command in a fresh shell (most agent harnesses
do: `cd`, `activate` and exported variables are lost between calls), begin every call with
`cd <the jev-screen folder> && export PATH="$PWD/.venv/bin:$PATH" && ...`, or replace the leading `jevscreen` with
the absolute path `<the jev-screen folder>/.venv/bin/jevscreen`. A `command not found` (exit 127, no JSON) means the
PATH was lost, not that anything failed.

Only when both uv and a Python >= 3.10 are missing, ask the human to install uv (one command from
https://docs.astral.sh/uv/) or Python from python.org.

**2. Start** with the human's idea, word for word, and `--json`:

```bash
jevscreen quickstart "<the human's idea>" --json
```

It returns in seconds and never uses the network itself. Trust the `status` field (exit codes in the table below).

**3. The loop.**

- `needs_agent` (exit 11): the idea is not English. Write **one English sentence** (at most 400 characters, no
  company names or tickers that are not in the idea) and rerun the same command with `--idea-en '<sentence>'` right
  away. This comes first because the human sees that sentence in the spend question. Ordinary words are fine even
  when a company happens to be called that ("Core suppliers of ...", "Immersion cooling ..."), and so is the English
  name of a company the idea itself names in Chinese (特斯拉 → Tesla). If the sentence names a company, the same
  status comes back with `idea_en_problems`, `suggested_idea_en` (the sentence without the example names, or null
  when the name carries the phrase: then write a new sentence) and `rerun_command` (the full command, with
  `--approve-budget` when the human already approved): check the suggestion reads well, fix it if not, and run it.
  On a first run this can come back while you poll (about 30 s into the downloads, which go on). If the flagged word
  is an ordinary word and you only lower-case it (e.g. "Harmonic" → "harmonic") or swap Core / Main / Key / Leading /
  Major / Primary, the human's approval stands and you do **not** ask again; tell them the English changed in one
  line (`text_<lang>` says it). Any other sentence (a company name dropped or replaced) comes back as
  `reprice_idea_en` (below): ask it.
- `needs_human` (exit 10): read `intro_<lang>` to the human, then ask **every** `pending` item in one message:
  - `consent_gray_sources`: ask `question_<lang>` word for word, followed by `note_<lang>` (who receives the text
    sent to Jev: the active provider, and the company in between if any); run only the one command of
    `record_answer_commands` that matches their answer (`answer_words` lists the clear yes / no replies, step 5).
    Never answer it yourself.
  - `approve_budget`: ask `question_<lang>` word for word (it shows the English idea and the dollar cap). After an
    explicit yes, rerun `next_command` plus the item's `rerun_with` (e.g. `--approve-budget 1`). A number above $1
    needs their explicit yes to that number. The number is the **total** cap for this idea, not an extra amount.
    With `kind: "over"` the question also offers narrower `alternatives` (each fits the cap): when the human picks
    one, rerun `next_command` plus that alternative's `flags` (e.g. `--countries US`); it is estimated again.
  - `key_jev`: with `provider: null` (no Jev key set yet) it carries the one account question: read `text_<lang>`
    (TypeSafe's official API, OpenRouter or Vercel AI Gateway, same price, each with its sign-up link and key step)
    and let the human name the account they have (none yet: OpenRouter is the quickest). Take the matching entry of
    `choices` (`provider` typesafe | openrouter | vercel). With `provider` set (its key was rejected: `rejected:
    true`, or the human chose it and its key is missing) the item itself carries that provider's `agent_try` etc.,
    and `choices` lists the other providers: if the human now wants another account, run that entry instead (the
    last Jev key set is the one used; `clear_command`, `jevscreen keys clear <provider>`, forgets the old one). Only
    when JEVSCREEN_JEV_PROVIDER pins the provider are there no `choices`. Ask them to say
    when the key is ready (a new account takes 5–10 minutes). **Only then** run `agent_try` (`jevscreen keys set
    <provider> --dialog`): a hidden input box opens on their screen for up to 100 s and the key goes straight to the
    key file. If they saved the key in a file themselves, run `agent_try_file` with that path instead (`jevscreen
    keys set <provider> --from-file <path>`: only the location is recorded, you never read the file; the file must
    hold only the key, one line (OpenRouter keys start with `sk-or-`), and anything else is refused with a plain
    message). Never export `TYPESAFE_API_KEY`, `OPENROUTER_API_KEY`, `AI_GATEWAY_API_KEY` or a `JEVSCREEN_*_KEY_FILE`
    for this: a worker already running would not see it.
    If the box times out (exit 1, `timed_out: true`), run `retry_command` when they say they are ready; if it fails
    otherwise (exit 1), give them `human_command` (an absolute path) to run in their own terminal. Never ask for the
    key in chat and never put it on a command line. After the key is stored, rerun `next_command` or poll with
    `jevscreen quickstart --status --key K --wait 100 --json`: either one continues the work.
  - `reprice_idea_en`: the English sentence would change its meaning (the agent's new `--idea-en`, or the sieve's
    `idea_en`); ask `question_<lang>` (it names the cap). After a yes rerun `next_command` plus `rerun_with` (the
    new `--idea-en` and `--approve-budget` together); after a no rerun `next_command` plus `decline_with` (the old
    sentence), which drops the question and keeps the old English. With `old_refused: true` the old sentence
    cannot be used (`decline_with` is null): after a no, write another sentence (`on_no_en`). It can also come back
    with a finished result (the status is then `needs_human`, the result fields are still there).
  Rerun `next_command` **right after** recording a yes to consent (with `--approve-budget` too if they already said
  yes to the budget), before you deal with the key: that starts the free downloads (about 1 minute) while the
  human is still making the key. From then on the JSON carries `poll_command` whenever the worker runs, even while
  a key or an answer is still open: poll it between your messages.
- `running` (exit 0): run `poll_command` (`jevscreen quickstart --status --key K --wait 100 --json`) in a loop; each
  call returns within 110 s. About once a minute, tell the human `progress.text_<lang>` in one line. Never rerun the
  front command while the status is `running`.
- `done` / `partial` (exit 0): relay `text_<lang>` (the counts, the idea's total time and cost, the page, and what
  changed in this version) and the `top` rows as a short list in chat (translate `one_line` into the human's
  language when `one_line_needs_translation` is true). Say whether the page opened (`page_opened`); if not, give
  them `page_uri`. There is one page per idea (`page`, `<home>/pages/<key>.html`): it always shows the newest
  version, so the human only refreshes it. The worker already fetched missing annual reports and updated the result
  (`fetch`: "Annual reports fetched during the screen" below).
  - **Translate the page, right after that message** (free, no question to the human): the page is in one language
    (`lang`), but profiles, annual-report excerpts and company names often arrive in another. When
    `translation_pending` > 0, run `translation.export_command` (a JSON list of at most 60 items, the most
    important first), write each item's `translation` into the page's language yourself (faithful, no summary, keep
    numbers and product names; a `name` is the company's usual short name without Co., Ltd., or the name
    unchanged when it has none, like Zscaler; `"keep_original": true` on any other text that has no translation;
    `null` only when unsure), keep every other field, and run `translation.import_command`. Repeat while its
    `translation_pending` is above 0 and the last round imported something (each round the same two commands;
    usually 1–4 rounds, at most 6). Then tell the human in one line to refresh the page.
    Translations are marked "AI 翻译 / AI translation" on the page with the verbatim original one tap away, and are
    reused by later runs. Never edit the page file or the evidence yourself.
  - If `pending` holds `fill_descriptions` (optional), ask its `question_<lang>` word for word **in the same
    message**, together with any `fetch.questions` (each also optional and only there when it helps a company in
    the top 10). Yes: rerun `next_command` plus `rerun_with` (`--fill-descriptions yes`); the status is `running`
    for about the minutes it names, poll it, the page updates itself. No: rerun `next_command` plus `decline_with`
    (it is not asked again). No answer is fine too.
  - Offer the three `next_steps` in one line each.
- `budget_exhausted` (exit 5): the page shows what was checked. Ask the pending `approve_budget` top-up question
  (`kind: "topup"`) before anything else; after a yes, rerun `next_command` plus its `rerun_with` (the new total).
  The next run continues where the old one stopped: answers already paid for come from the cache for $0.
- `ai_unavailable` (exit 6): 402 means no credit at the Jev provider (the human tops up at the link in `text_<lang>`,
  then says "done": rerun the front command); 401/403 means the key was rejected (the `key_jev` item comes back for
  that provider; on Vercel a 403 usually means no AI Gateway credits were bought yet).
- `blocked` (exit 2): a provider refused us. Tell the human `text_<lang>`; wait until `retry_after`. Never use
  `--after-block`, a proxy or another User-Agent to get around it (hard rule 6).
- `failed` (exit 1) / `store_busy` (exit 3): tell the human `text_<lang>` in one sentence; `next_command` resumes
  (with `--retry` only after `retry_after`). A `store_busy` with a `poll_command` means another idea is being
  screened and this one is queued with its answers saved: poll it (it starts when the other one finishes) or rerun
  `next_command` later. If company profiles cannot be downloaded at all (also as `blocked`), the text gives the
  manual download: the human saves the file and you rerun with `--fd-file <path>`; that needs no request.
- `declined` (exit 12): stop. Do not ask again unless the human says they changed their mind.

| status | exit | what you do |
|---|---|---|
| `running` | 0 | poll with `--status --wait`, relay progress about once a minute |
| `done` / `partial` | 0 | relay the result and `top` in chat |
| `failed` | 1 | tell the human; `next_command` after `retry_after` |
| `blocked` | 2 | wait (24 h cooldown); no override |
| `store_busy` | 3 | another jev-screen task uses the database; poll (`poll_command`) or rerun later |
| `budget_exhausted` | 5 | ask the top-up question; rerun with its `rerun_with` after a yes |
| `ai_unavailable` | 6 | 402: human tops up; 401/403: new key |
| `needs_human` | 10 | ask all pending human items in one message |
| `needs_agent` | 11 | write `--idea-en` and rerun at once |
| `declined` | 12 | stop |

A usage error (a typo in the flags) exits 2 **with no JSON**: that is a typo, not a block.

**4. After the first result.** The same idea again returns the finished result for $0. A new idea costs about
$0.25 and 4 minutes with no downloads. On the page the human can answer a few cards and copy one line
(`jevscreen answer "1a 2h 3c" --deck deck-...`) to you: run it as given (about $0.01–0.03); it updates the same page
(tell the human to refresh it), and `quickstart --status` then reports that newest version. A new version can bring
new foreign texts: when `quickstart --status --json` shows `translation_pending` > 0 again, do the translation
rounds again (translations made before are reused). The page, the `--text` list, `why`, `answer` and `cards` speak
the human's language (`--lang zh|en`, default: the language of the idea's quickstart job). If the human asks why a
company is missing, run `jevscreen why <name or ticker> --run <run_id> --json` (free; Step 9).
Profile gap fills are asked by quickstart itself (`fill_descriptions` above); run a plain
`jevscreen crawl-descriptions --countries CN --min-mcap 1e9` only when the human asks for it.

The steps below are the manual path (doctor, keys, consent, data, screen) for when you need finer control.

## Hard rules (read first)

**Never:**

1. Read, print, `cat`, `grep`, copy, summarise or echo a key or the SEC name/email: not the files
   `data/typesafe_api_key`, `data/openrouter_api_key`, `data/vercel_api_key`, `data/edinet_api_key`,
   `data/opendart_api_key`, `data/sec_user_agent`, not the variables `TYPESAFE_API_KEY`, `OPENROUTER_API_KEY`,
   `AI_GATEWAY_API_KEY`, `JEVSCREEN_*_API_KEY`, `JEVSCREEN_SEC_USER_AGENT`, not any `.secrets/` folder.
   Use `jevscreen keys check` to learn whether a key exists; it never shows values.
2. Put a key on a command line (`--key abc`, `echo abc | ...`, `export X=abc`): it lands in shell history and your
   transcript. The human types keys themselves into a hidden prompt (step 4).
3. Ask the human to paste a key into the chat. If they do it anyway: do not repeat it, do not store it from the chat,
   tell them it is now in the chat history and that they may want to replace it (revoke and create a new one) later.
4. Record consent the human did not give (step 5). Silence, "whatever", "just make it work" are **not** a yes.
5. Spend more than **$1** on Jev without the human's explicit yes for the new amount (step 7; with quickstart:
   never pass `--approve-budget` above what the human said yes to). The $1 cap is this file's rule, not the
   program's: `jevscreen screen` on its own defaults to `--budget 3` and `--min-mcap 2e8`
   ($3, companies from $200M), so always pass `--budget` and `--min-mcap` on every screen, dry run or not.
6. Work around a provider block: no `--after-block`, no proxies, no other User-Agent, no retries in a loop, no
   "slower but continue". A block (exit 2) ends that command for 24 hours unless the human says otherwise. This
   forbids switching network or identity to get around a block; it does not forbid the human's normal network
   setup (including a VPN or proxy they always use), so never refuse to work because of it.
7. Run two copies of the same sync or crawl, or kill a running one with `kill -9`. (Ctrl-C / SIGTERM are fine.)
8. Commit, upload, share or publish anything under `data/` (database, raw pages, reports, screens). Gray-private
   and official-private data is **personal use only**; outputs derived from it too.
9. Delete `data/` or the database to "fix" something. Ask the human first.
10. Edit `data/consent.json` or a key file by hand. Use the commands.

**Ask the human first** (and wait for the answer) before:

- creating any account or key (TypeSafe, OpenRouter, Vercel, EDINET, OpenDART) and before running `jevscreen keys
  set`;
- sending their name and email to the SEC (`keys set sec-email`, needed only for US annual reports);
- recording consent for gray-private sources (`consent set gray-sources ...`), with the question in step 5;
- any paid screen, and any budget above $1;
- any command marked "long" below (it can run for hours), and `--after-block` after a block;
- turning on Japan (EDINET) or Korea (DART) annual reports (they need extra free keys; off by default);
- turning on Taiwan annual-report downloads (`consent set mops-annual ...`): ask only when a screen's `questions[]`
  includes `mops_annual`, with its question word for word.

## Step 1. Install

```bash
python3 --version                      # must be 3.10 or newer
git clone https://github.com/YuanTong-Wu/jev-screen jev-screen && cd jev-screen
python3 -m venv .venv && export PATH="$PWD/.venv/bin:$PATH"   # repeat the export in every fresh shell (see above)
python3 -m pip install -e '.[pdf]'     # duckdb, plus pypdf to read China/India/Taiwan annual-report PDFs
```

The `[pdf]` extra installs `pypdf` (BSD licence), which is enough (`doctor`'s `dep_pymupdf` check is then `ok`).
PyMuPDF (`pip install pymupdf`) is optional: it reads Chinese PDF layouts better, but it is AGPL-3.0 licensed, so
install it only when the human agrees.

If Python is older than 3.10, stop and ask the human to install a newer Python (do not try `sudo`).

**Verify:** `jevscreen --help` exits 0 and lists `doctor`, `keys`, `consent`, `screen`.

## Step 2. Create the store

```bash
jevscreen init
```

**Verify:** the JSON output has `"status": "ok"`.

## Step 3. Ask the doctor

```bash
jevscreen doctor --json
```

Read `ok`, `next_command`, `ask_human` and each check's `status` / `fix_command` / `ask_human`. The rule of thumb:

- `fail` blocks a real screen. Fix it, starting with `next_command`.
- `warn` does not block. Mention it to the human in one line, fix it later if they want.
- A check with `"ask_human": true` needs the human's decision before you run its `fix_command`.
- `skip` needs nothing (for example EDINET/DART keys, which are off by default).
- `next_command` and every `fix_command` are complete commands: run them exactly as written, never edit or fill
  them in. None of them ever records consent.
- A check that needs a yes/no from the human (gray-private sources) has `fix_command: null`, a `human_question`
  and `record_answer_commands`. Ask the question word for word (step 5), then run only the one command that matches
  the human's own answer.
- `next_command` is `null` when nothing can be run without the human, and always after the human said no to
  gray-private sources: then stop (step 5), do not ask again.

Run `jevscreen doctor --json` again after every fix.

**Verify:** you know which checks fail and which of them need the human.

## Step 4. Keys (the human types them)

One **Jev** key is needed for a first screen (it pays for the AI reads). Jev via TypeSafe's official API, OpenRouter
or Vercel AI Gateway, same price (US$0.042 per million input tokens, output free): use whichever account the human
has. When `doctor --json` fails `jev_provider` with no key at all, ask its `human_question` (or
`human_question_zh`) word for word: which account they have, with the sign-up link and key step of each. Then:

| Account | Key page | Command | Model id sent |
|---|---|---|---|
| TypeSafe official API (sign-ups are sometimes paused) | https://console.typesafe.ai/keys | `jevscreen keys set typesafe` | `jev-1.13.0` (pinned) |
| OpenRouter | https://openrouter.ai/settings/keys | `jevscreen keys set openrouter` | `typesafe/jev-1.13` |
| Vercel AI Gateway (buy AI Gateway credits first; Jev may not be in the free tier) | Vercel dashboard, AI Gateway, API Keys | `jevscreen keys set vercel` | `typesafe-ai/jev` (no version) |

Tell the human, in plain words, to create the key (a spending limit of a few dollars on the key is a good idea where
the provider has one), then run this in their own terminal and paste the key when it asks; nothing will show while
they paste: `<jev-screen folder>/.venv/bin/jevscreen keys set <provider>`.

Replace `<jev-screen folder>` with the absolute path before you tell them (a bare `jevscreen` is not on the PATH of
their terminal). Better still, run `jevscreen keys set <provider> --dialog` yourself: a hidden box opens on their
screen (macOS / Linux desktop), and its failure JSON carries the absolute `human_command`.

If your harness gives the human no terminal, they can pipe the key from a file they created themselves:
`jevscreen keys set <provider> --stdin < /path/they/chose` (then they delete that file). You never see the value.

The last Jev key set is the one used (the choice is saved in `data/jev_provider`, so it survives fresh shells and
detached workers). `jevscreen keys use <provider>` switches without a new key; `jevscreen keys clear <provider>`
removes a key and forgets that choice. Only when several keys exist and no choice was ever saved (keys from
environment variables, or an install older than this) is the first of openrouter, typesafe, vercel used.
`JEVSCREEN_JEV_PROVIDER` still overrides everything, but it is lost between your calls, so prefer `keys use`.
Answers are cached per provider (never mixed): after a switch, the first screen of an idea reads and pays again. When a
`keys set` / `keys use` switches provider, its JSON carries `provider_switch` with `notice_zh` / `notice_en` (what was
already paid through the old provider, and `switch_back_command`): tell the human in one line.

Optional, only when the human asks for that data and agrees:

| Name | What for | Default |
|---|---|---|
| `sec-email` | US annual reports (`sync-sec`); their `Name email@example.com` is sent to sec.gov as required by the SEC | ask |
| `edinet` | Japanese annual reports (`sync-edinet`), free key | off |
| `opendart` | Korean annual reports (`sync-dart`), free key | off |

**Verify:** `jevscreen keys check <provider>` exits 0 and shows `"configured": true, "shape_ok": true`, and
`jevscreen doctor --json` shows `jev_provider` ok with that provider. Optionally `jevscreen doctor --check-jev --json`
(one free request to that provider, no paid call): the `jev` check is `ok`. OpenRouter reports the key's own spending
limit (not the account balance), Vercel the AI Gateway credit balance, TypeSafe no balance at all; a `fail` with HTTP
402 means the account has no credit, and only the human can add it.

## Step 5. Consent for gray-private sources

The universe (the list of stocks) and most business descriptions come from **gray-private** sources:
TradingView pages and FinanceDatabase (Yahoo) text. They are public, but TradingView's terms forbid automated use of
its data and the Yahoo text's terms restrict reuse. Ask exactly (statement version 4):

> "Quick heads-up: the stock list and company profiles come from TradingView and Yahoo. That's a gray area — their
> terms don't actually allow automated bulk use, and you may occasionally get rate-limited or briefly blocked. So this
> data stays on your computer for your own research; don't share it. Annual reports downloaded from official sites
> (SEC, CNINFO, BSE, ...) are the same: for your own use. Company profiles and report excerpts are sent to Jev (the AI
> service) to read. OK? (yes / no)"

In Chinese, ask exactly `human_question_zh` from `jevscreen doctor --json` and record with `--lang zh`.

Right after the statement, say `recipient_note` / `recipient_note_zh` from the `consent_gray_sources` check (who
receives the text sent to Jev for the active provider; quickstart's item calls it `note_<lang>`).

Then record **their** answer. A clear yes (可以, 同意, 好, 好的, 行, 没问题, yes, OK, sure, agree) is `yes`; a clear no
(不要, 不同意, 不行, 不可以, 算了, no) is `no`. The full lists are `answer_words` in the check and the quickstart item.
Anything else, or a yes with a condition ("好，但是……"), is not an answer: ask again.

```bash
jevscreen consent set gray-sources yes --lang en    # or: no; --lang zh when you asked in Chinese
```

If the answer is no: do not run `refresh-universe`, `crawl-descriptions` or `import-fd`. Tell the human screening
needs a universe, which only these sources provide (the open data pack has no stock list); stop here.

**Verify:** `jevscreen consent show` shows the state you recorded, with a `recorded_at` time.

## Step 6. Data (only with consent = yes)

| Command | Time | Needs |
|---|---|---|
| `jevscreen refresh-universe` | about 1 minute | consent yes |
| `jevscreen import-fd` | about 1 minute | consent yes; a local FinanceDatabase DuckDB (skip if doctor/README says it is not there) |
| `jevscreen crawl-descriptions` | **long** (hours for the whole world) | consent yes, ask the human; can run in the background, resumes where it stopped |
| `jevscreen sync-sec --limit 200` | minutes | `sec-email` key; optional, gives stronger layer-2 evidence for US companies |

These three commands refuse to start without a recorded `yes` (exit 1, JSON `"status": "consent_required"`): go
back to step 5 and ask the human; never record the answer yourself.

Run one networked command at a time per source. Exit codes: `2` blocked or in cooldown (stop, tell the human, no
`--after-block`), `3` database busy (another jevscreen process is writing: wait and retry later), `4` the same
command is already running elsewhere (do not start a second copy).

**Verify:** `jevscreen doctor --json` shows `universe` as `ok` (or `warn` only for age) and `descriptions` not `fail`.

## Step 7. First screen: dry run, then at most $1

First-run defaults: companies with market cap **>= $1B**, budget **$1**, dry run first. These are not the program's
defaults (`--budget 3`, `--min-mcap 2e8`): always pass `--budget` and `--min-mcap` explicitly, on the dry run and on
the paid run. Write the idea in the human's words (any language works; it is translated locally when the optional
model is present).

```bash
jevscreen screen "<the human's idea>" --min-mcap 1e9 --budget 1 --dry-run
```

Tell the human the `total estimate:` line (dollars and time). Then:

- estimate **<= $1**: ask "OK to spend up to $1?"; on yes run the same command **without** `--dry-run`;
- estimate **> $1**: offer to narrow it (for example `--countries US` or `--min-mcap 5e9`), or ask for an explicit
  budget ("OK to spend up to $X?"), and use exactly that number in `--budget`.

The budget is hard: the run stops before spending more. Exit `5` means the budget ran out (a partial report is still
written): ask before raising it. Exit `6` means Jev is unavailable (missing or rejected key, or HTTP 402: no credit
left, the human must add some): run `jevscreen doctor --check-jev --json`. Exit `4` means another screen is running.

**Verify:** exit code 0 and a folder `data/screens/<timestamp>-<idea>/` with `report.md`. Summarise the top rows and
the "unverified" list for the human; say which companies had official annual-report evidence and which only a
profile; point them to `report.md` for the details. Remind them: personal use only, do not share the list.
If any of the top rows is marked ST / *ST (the console prints 「注意：交易所风险警示（ST）…」), say so in one plain
sentence: the exchange flags financial trouble; *ST means possible delisting.

**Step 7a (conditional, silent).** Only when the dry run's `results.json` shows `keywords.status` other than
`generated` / `sieve` / `agent` for a non-English idea (the local model is missing), write a draft
`{"idea": "<the exact idea>", "idea_en": "<English rendering>", "seed_terms": {"en": [...], "zh": [...]}}` and run
`jevscreen sieve set --idea "<the exact idea>" --from draft.json --json`, then the dry run again. Do not ask the
human. `"ok": true` is enough; warnings never block; after two failed attempts run the screen with `--sieve none`.
Never put a company name in idea_en. Details: [docs/SIEVE.md](docs/SIEVE.md).

**Annual reports fetched during the screen.** After the first report is written, `screen` fetches the newest annual
report of first-round companies that have none stored (free, official sites, at most 2 minutes) and updates the same
report (at most $0.05, inside the budget you agreed). So:

- (a) Run `screen` in the background or with a timeout of at least 10 minutes. Never kill it because it looks idle:
  it prints a line every 15 s. The first report exists after about 4 minutes, the updated one about 2-3 minutes later.
- (b) Tell the human in one sentence that up to 2–3 extra free minutes are spent fetching official annual reports;
  do not ask.
- (c) Relay `layers.fetch.summary_zh` (or `summary_en`) from `results.json`, not the per-company rows.
- (d) A `blocked` or `source_paused` source inside a screen's fetch is already handled (`agent_action: "none"`, the
  source pauses itself for 24 hours). Tell the human the one sentence; do not run any sync or `--after-block`.
  `jevscreen fetch-docs` exit 2 follows hard rule 6.
- (e) Ask the `questions[]` of `layers.fetch` (in `results.json`, `fetch-docs --json`, or quickstart's
  `fetch.questions`: there only when a top-10 company needs it, and labelled optional) word for word, and only when
  present: `sec_email` (then the human runs `jevscreen keys set
  sec-email`; if they say no, record it with `jevscreen consent set sec-email-ask no` and the question stops),
  `mops_annual` (then record their own answer with the matching `record_answer_commands` entry). After a **yes**,
  once the key or the consent is recorded, run the question's `then_command` (`jevscreen fetch-docs <run>`): that
  fetches those companies' annual reports and updates the report and the page (free fetch plus an update of at
  most $0.05, within the $1 rule). Rerunning quickstart does not do it (it returns the finished result as is).
- (f) Companies left for later (`deferred_time` / `deferred_cap`), or reports stored but not read yet
  (`layers.fetch.next_command` set): `jevscreen fetch-docs latest` (free fetch plus an update of at most $0.05, within
  the $1 rule: no new question needed). `--fetch-docs off` skips the fetch.

## Step 8. When something goes wrong

1. Run `jevscreen doctor --json` and follow `next_command`.
2. `jevscreen status` shows row counts and the last runs; `jevscreen coverage` shows data gaps.
3. Tell the human what failed in one sentence, what you propose, and ask before anything costly, long or
   destructive.

## Step 8a. Optional: checks the human cares about (after the first result)

Offer once: 「要不要我帮你核对几家你觉得应该在的公司？」 On yes, turn the names into tickers yourself and run
`jevscreen sieve add should_pass TICKER --run <run_id> --json` for each. They are read and reported on the next run
(`jevscreen screen --from-run <run_id> --budget 0.05`, ask first: it is a paid run), never listed just because they
were named. Pins (`jevscreen sieve pin TICKER yes|no --run <run_id>`) only on the human's explicit yes.

## Step 9. "Why is X (not) in the list?"

1. Turn the name into an exchange ticker yourself (2330, TWSE:2330, 7203, NYSE:ABC); the store knows official names
   but not nicknames such as 台积电.
2. Run `jevscreen why <TICKER> --run <run_id> --json` (the run id the screen printed). It is free and read-only.
3. Tell the human `plain_zh` (or `plain_en`), then the facts and gaps in one or two sentences. Keep fact (what the
   data says), inference (the model's judgement) and gap (what is missing) apart.
4. Offer `changes[]` in plain words with their cost text. Run a change's `steps` only when every step you run has
   `ask_human: false`, or the human said yes to that change. Never run a pin without the human's explicit yes.
5. `status: "partial_files_only"` means a screen or sync holds the database: the answer is still right for the
   pre-L1 stages and step 1; ask again later for step-2 details. `status: "choose_run"`: ask which idea, or pass
   `--run`. Details: [docs/WHY.md](docs/WHY.md).
