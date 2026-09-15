# Audit & Bounty Monitor

Scans Immunefi, Code4rena, Sherlock, Cantina and CodeHawks for open audit
contests, and DefiLlama for newly-listed DeFi protocols with >= $1M TVL.
Each newly-detected protocol is also automatically checked against
Immunefi's bug-bounty list (verified reliable — see below); the alert
says explicitly whether one was found rather than just flagging TVL and
leaving the check entirely manual. Posts to a Telegram group: an
immediate alert the moment something new shows up, plus a consolidated
status update every 4 hours.

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

## Bug-bounty cross-check

DefiLlama has no "listed at" field, so a newly-detected protocol is just
whatever wasn't in `state.json` last run. Whether it already has a bug
bounty is a separate question, checked against:

- **Immunefi — automated.** `check_immunefi_bounty()` in `scan.py` drives
  the real search box at `immunefi.com/bug-bounty/` (its `?search=`
  URL param does nothing — confirmed not to filter — so this types into
  the actual input and reads "View N Bounties" back). Verified against
  three known-positive names and one nonsense name before shipping; a
  page-load failure reports as "couldn't check" (❓), never silently as
  "not found".
- **HackenProof — not automated.** Its search box *does* filter, but
  returned zero results for a name confirmed present in the unfiltered
  list (a false negative) during testing — not trustworthy enough to
  report a verdict, so the alert just links to it for a manual look.
- **Cantina, Sherlock — not automated.** Cantina's bounty page
  (`cantina.xyz/bounties`, now branded `cantina.security`) never reached
  network-idle in testing (constant analytics polling). Sherlock's
  `/bug-bounties` page renders individual program links but no bulk
  listing or API was found. Both just get a manual-check link in the
  alert.

## Known limitation

Sherlock's contest API, Cantina's competition API and DefiLlama use
documented/stable JSON APIs, fetched directly — reliable by construction.
Immunefi, Code4rena and CodeHawks have no public contest-listing API
(confirmed via `diagnose.py` against live traffic): they're rendered with
Playwright and parsed from the visible page text with a line-pattern
parser tuned to each site's current card layout. That's more fragile than
a real API — if one of those sites redesigns its contest page, its parser
will likely start returning zero listings. The status update names any
source that fails to parse rather than silently going quiet; if that
happens, edit `scripts/diagnose.py`'s `TARGETS`/`API_TARGETS`/
`SEARCH_TESTS` dicts and push to re-run the diagnostic workflow, see what
the page looks like now, then re-tune the matching function in `scan.py`.
