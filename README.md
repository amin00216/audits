# Audit & Bounty Monitor

Posts to a Telegram group whenever something with a real, verifiable
**"landed" signal** appears:

- **New open audit contests** — Immunefi, Code4rena, Sherlock, Cantina,
  CodeHawks
- **Newly-launched ongoing bug bounty programs** — HackerOne, HackenProof
  (separate from the time-boxed contests above — see "Newest bounty
  programs" below)

An immediate alert fires the moment either happens, plus a consolidated
status update every 4 hours.

**DefiLlama's newly-≥$1M-TVL-protocol scan runs but doesn't message
anything** (as of 2026-09-15) — see "Why protocol-TVL tracking was
removed from alerting" below for why.

**Links**: every open contest alert includes a real, contest-specific URL
(not just the platform's generic listing page — see "Real contest links"
below).

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

`state.json` at the repo root tracks every id the script has already
seen (contests, bounty programs, and — though it no longer drives any
message — DefiLlama protocols too), and is committed back by the
workflow after each run. The **first ever run only seeds this state** —
it will not blast every currently-open contest as "new"; only entries
the script hasn't seen before will trigger an alert from then on. Each
of the three tracked categories has its **own independent bootstrap
flag** (`bootstrapped`, `bounty_programs_bootstrapped`) — reusing one
flag for a category added later caused a real bug once (see "Newest
bounty programs" below).

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

## Why protocol-TVL tracking was removed from alerting

`scan_defillama()` still runs every cycle and `seen_protocols` is still
maintained, but as of 2026-09-15 it drives **no Telegram message at
all** — not the immediate alert, not the digest.

The original design flagged any DefiLlama protocol newly crossing $1M
TVL as "new", cross-checked it against Immunefi's bug-bounty list
(`check_immunefi_bounty()` — still in `scan.py`, drives the real search
box at `immunefi.com/bug-bounty/` since its `?search=` URL param is a
no-op, confirmed accurate against 3 positive + 1 negative test case),
and alerted either way. In practice this meant "new" only ever meant
"just crossed our TVL floor" — DefiLlama has no launch-date field — and
it mislabeled **Allbridge Classic**, live since July 2021, as a freshly
landed protocol the moment its TVL ticked from just under to just over
$1M. Since the whole point of this monitor is catching genuinely new
things, a floor-crossing heuristic that flags 5-year-old protocols isn't
good enough to message on.

The code is kept (not deleted) so a better "new" heuristic — e.g. cross-
referencing a protocol's actual first-seen date via a source that has
one — can reuse `seen_protocols`'s history later. If reactivated, the
same per-protocol Immunefi/HackenProof/Cantina/Sherlock bounty cross-
check described in earlier versions of this README applies.

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
  list has no visible launch-date field to sort by (only "last updated").
  Cantina's bounty page never reached network-idle in testing (constant
  analytics polling). Sherlock's `/bug-bounties` page renders individual
  program links but no bulk listing or API was found.

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
