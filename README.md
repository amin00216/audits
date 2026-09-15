# Audit & Bounty Monitor

Scans Immunefi, Code4rena, Sherlock, Cantina and CodeHawks for open audit
contests, and DefiLlama for newly-listed DeFi protocols with >= $1M TVL.
Posts to a Telegram group: an immediate alert the moment something new
shows up, plus one consolidated daily digest.

Runs entirely inside GitHub Actions (`.github/workflows/audit-monitor.yml`)
on a `*/30 * * * *` schedule, so it isn't dependent on any external
session and has normal outbound internet access.

## One-time setup

In this repo's **Settings → Secrets and variables → Actions**, add:

- `TELEGRAM_BOT_TOKEN` — the bot token from @BotFather. **Paste it directly
  into the GitHub secret field — do not commit it to any file.**
- `TELEGRAM_CHAT_ID` — the numeric group chat id (usually negative, e.g.
  `-100123456789`). If you skip this, the first run will try to resolve it
  automatically from `getUpdates` (requires the bot to already be a member
  of the group and at least one message to have been sent there since it
  joined) and print the resolved id in the run log — copy that into the
  secret afterward so future runs don't depend on `getUpdates` still
  having the message in its buffer.

Then run the workflow once manually (Actions tab → Audit & Bounty Monitor
→ Run workflow) to confirm it posts correctly, or just wait for the next
`*/30 * * * *` tick.

## How "new" detection works

`state.json` at the repo root tracks every contest/protocol id the script
has already seen, and is committed back by the workflow after each run.
The **first ever run only seeds this state** — it will not blast every
currently-open contest as "new"; only entries the script hasn't seen
before will trigger an alert from then on.

## Known limitation

Code4rena, Sherlock, CodeHawks and Immunefi are scraped with a generic
heuristic (look for a Next.js `__NEXT_DATA__` payload embedded in the
page, then pattern-match dict entries that look like a contest listing).
This was written without the ability to hit those sites live to verify
exact field names, so it may need a tuning pass — check the Action run
logs for `[warn]` lines naming which source failed to parse, and the
daily digest will also name any source it couldn't read. Cantina
(`cantina.xyz/api/v0/opportunities`) and DefiLlama (`api.llama.fi/protocols`)
use documented JSON APIs and should be reliable from the start.
