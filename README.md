# Audit & Bounty Monitor

Scans Immunefi, Code4rena, Sherlock, Cantina and CodeHawks for open audit
contests, DefiLlama for newly-listed DeFi protocols with >= $1M TVL, and
HackerOne + HackenProof for newly-*launched* ongoing bug bounty programs
(separate from the time-boxed contests above — see "Newest bounty
programs" below). Each newly-detected protocol is also automatically
checked against Immunefi's bug-bounty list (verified reliable — see
below); the alert says explicitly whether one was found rather than just
flagging TVL and leaving the check entirely manual. Posts to a Telegram
group: an immediate alert the moment something new shows up, plus a
consolidated status update every 4 hours.

**Links**: every open contest alert includes a real, contest-specific URL
(not just the platform's generic listing page — see "Real contest links"
below). Protocol alerts deliberately do *not* link to the DefiLlama
protocol page, since that's just a TVL listing, not a bounty program or
contest, and linking it would misrepresent what it is.

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

## Real contest links

Cantina's API returns a competition id, so its link is built directly:
`cantina.xyz/competitions/<id>`. Immunefi, Code4rena and CodeHawks have no
per-item URL in their scraped text (see "Known limitation" below) — for
those, `get_rendered_text_and_links()` also captures every `<a href>` on
the page, and `find_link_for_name()` matches each listing's display name
back to its real link (exact match preferred, substring match as
fallback), scoped first to a path hint like `/audits/` to avoid matching
nav links. Falls back to the platform's generic listing page only if no
match is found. Verified against live data (`LINK_MATCH_TEST` in
`diagnose.py`): all 4 test names resolved to exactly the right contest
URL.

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

## Newest bounty programs

Tracked separately from the "new protocol has a bounty?" cross-check
above — this scans bounty platforms directly for recently *launched*
programs, catching them even if the underlying protocol never shows up
on DefiLlama (or is below the $1M TVL floor):

- **HackerOne — automated, reliable.** `scan_hackerone_newest()` POSTs
  directly to `hackerone.com/graphql` (`DiscoveryQuery`, no auth or
  browser needed — confirmed via `diagnose.py`), sorted by real
  `launched_at DESC`. This is the cleanest source in the whole repo.
- **HackenProof — automated, best-effort.** No API exists, but
  `hackenproof.com/programs`'s default (unsorted) order already puts the
  most recently started programs first — confirmed live: consecutive
  `Started date:` values were strictly descending. Reuses the same
  status-word-anchored text parser proven on Code4rena. No clean
  per-program URL was found, so its alerts link to the listing page, not
  the specific program.
- **Immunefi, Cantina, Sherlock — not included here.** Immunefi's bounty
  list has no visible launch-date field to sort by (only "last updated");
  it's still covered by the reactive per-protocol check above. Cantina
  and Sherlock have the same automation gaps described under "Bug-bounty
  cross-check".

Tracked with its own `seen_bounty_programs` / `bounty_programs_bootstrapped`
state (deliberately separate from the contests/protocols `bootstrapped`
flag — reusing that one caused a real bug: this feature's first-ever run
saw an already-`True` flag from older history and blasted all 25
pre-existing programs as "new" in one alert. Fixed by giving it an
independent bootstrap flag.).

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
