#!/usr/bin/env python3
"""
Audit-contest + bug-bounty-program monitor.

Sends a Telegram alert immediately when something with a real, verifiable
"landed" signal appears — a new open audit contest (Immunefi, Code4rena,
Sherlock, Cantina, CodeHawks) or a newly-launched ongoing bug bounty
program (HackerOne, HackenProof) — plus a consolidated status digest
every DIGEST_INTERVAL_HOURS (default 4).

DefiLlama's newly-≥$1M-TVL-protocol scan (scan_defillama) still runs and
seen_protocols is still maintained, but deliberately does NOT drive any
message: DefiLlama has no launch-date field, so "new" there only ever
meant "just crossed our TVL floor", which mislabeled a ~5-year-old
protocol (Allbridge Classic, 2026-09-15) as freshly landed. Left in
place so a better "new" heuristic can reuse it later without
re-bootstrapping the whole tracked set.

State (state.json, committed back to the repo by the workflow) is what
makes "new" detection possible across runs.

Source techniques (verified against live output on 2026-09-15, see
diagnose.py / diagnose-output.json in git history):
  - Cantina, DefiLlama: documented JSON APIs, fetched directly.
  - Sherlock: audits.sherlock.xyz/api/contests — clean undocumented but
    stable-looking JSON API, fetched directly.
  - Immunefi, Code4rena, CodeHawks: no usable API found (client-rendered
    Next.js App Router apps with no discrete listing XHR). Rendered with
    Playwright and parsed from the visible text via a line-pattern parser
    tailored to each site's card layout. More fragile than a real API —
    if a site's layout changes, this needs a re-tune (the digest will
    name any source that fails to parse rather than silently going
    quiet).
"""
import json
import os
import re
import sys
import urllib.request
from datetime import datetime, timezone

from playwright.sync_api import sync_playwright

STATE_PATH = os.path.join(os.path.dirname(__file__), "..", "state.json")
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
DIGEST_INTERVAL_HOURS = float(os.environ.get("DIGEST_INTERVAL_HOURS", "4"))
UA = "Mozilla/5.0 (compatible; AuditMonitorBot/1.0; +https://github.com/amin00216/audits)"

OPEN_STATUSES = {"live", "active", "upcoming", "open", "ongoing", "starting"}
CLOSED_STATUSES = {
    "judging", "evaluating", "finished", "closed", "ended", "completed",
    "review", "mitigation", "submissions closed", "report in progress",
}


def is_open_status(status):
    if not status:
        return None
    s = status.lower()
    if any(c in s for c in CLOSED_STATUSES):
        return False
    if any(o in s for o in OPEN_STATUSES):
        return True
    return None


def http_get(url, headers=None, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": UA, **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except Exception as e:
        print(f"[warn] GET {url} failed: {e}", file=sys.stderr)
        return None


def get_json(url, headers=None):
    text = http_get(url, headers)
    if text is None:
        return None
    try:
        return json.loads(text)
    except Exception as e:
        print(f"[warn] JSON parse failed for {url}: {e}", file=sys.stderr)
        return None


def post_json(url, body_obj, headers=None, timeout=20):
    data = json.dumps(body_obj).encode()
    req = urllib.request.Request(url, data=data, method="POST", headers={
        "User-Agent": UA, "Content-Type": "application/json",
        "Accept": "application/json", **(headers or {}),
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8", errors="replace"))
    except Exception as e:
        print(f"[warn] POST {url} failed: {e}", file=sys.stderr)
        return None


_BROWSER = None


def _get_browser():
    global _BROWSER
    if _BROWSER is None:
        _pw = sync_playwright().start()
        _BROWSER = _pw.chromium.launch()
    return _BROWSER


def get_rendered_text(url):
    """Render `url` with a headless browser and return the visible body
    text, or None on failure. Reuses one browser instance across calls."""
    try:
        page = _get_browser().new_page()
        page.goto(url, wait_until="networkidle", timeout=45000)
        page.wait_for_timeout(2500)
        text = page.inner_text("body")
        page.close()
        return text
    except Exception as e:
        print(f"[warn] render {url} failed: {e}", file=sys.stderr)
        return None


def get_rendered_text_and_links(url):
    """Like get_rendered_text, but also returns every on-page <a href> as
    [{text, href}], so a listing's display name can be matched back to its
    real deep link (the sites scraped here have no clean per-item URL in
    their visible text alone)."""
    try:
        page = _get_browser().new_page()
        page.goto(url, wait_until="networkidle", timeout=45000)
        page.wait_for_timeout(2500)
        text = page.inner_text("body")
        links = page.eval_on_selector_all(
            "a[href]",
            "els => els.map(e => ({text: e.innerText.trim(), href: e.href}))"
            ".filter(l => l.text.length > 0)",
        )
        page.close()
        return text, links
    except Exception as e:
        print(f"[warn] render {url} failed: {e}", file=sys.stderr)
        return None, []


def find_link_for_name(links, name, path_hint=None):
    """Best-effort match of a card's display name back to its href, among
    all links captured on the page. Prefers an exact text match; falls
    back to substring containment. path_hint filters to hrefs containing
    that substring first (e.g. "/audits/"), to avoid matching nav links."""
    name_l = name.strip().lower()
    pools = []
    if path_hint:
        pools.append([l for l in links if path_hint in l["href"]])
    pools.append(links)
    for pool in pools:
        for l in pool:
            if l["text"].strip().lower() == name_l:
                return l["href"]
        for l in pool:
            t = l["text"].strip().lower()
            if t and (t in name_l or name_l in t):
                return l["href"]
    return None


def lines_of(text):
    return [l.strip() for l in text.splitlines() if l.strip()]


IMMUNEFI_SEARCH_SELECTOR = "input[type='search'], input[placeholder*='earch' i]"


def check_immunefi_bounty(protocol_name):
    """Search immunefi.com/bug-bounty/ for `protocol_name` using the site's
    real search box (URL query params don't filter it — confirmed via
    diagnose.py). Returns (True, bounty_url), (False, None) confirmed not
    found, or (None, None) if the check itself failed — treat as unknown,
    never as "not found"."""
    try:
        page = _get_browser().new_page()
        page.goto("https://immunefi.com/bug-bounty/", wait_until="networkidle", timeout=45000)
        page.wait_for_timeout(1500)
        box = page.locator(IMMUNEFI_SEARCH_SELECTOR).first
        if box.count() == 0:
            page.close()
            return None, None
        box.click()
        box.fill(protocol_name)
        page.wait_for_timeout(2500)
        text = page.inner_text("body")
        m = re.search(r"View (\d+) Bounties", text)
        if not m:
            page.close()
            return None, None
        if int(m.group(1)) == 0:
            page.close()
            return False, None
        links = page.eval_on_selector_all(
            "a[href*='/bug-bounty/']",
            "els => els.map(e => ({text: e.innerText.trim(), href: e.href}))"
            ".filter(l => l.text.length > 0)",
        )
        page.close()
        bounty_url = find_link_for_name(links, protocol_name)
        return True, bounty_url
    except Exception as e:
        print(f"[warn] Immunefi bounty check for {protocol_name!r} failed: {e}", file=sys.stderr)
        return None, None


# ---------------------------------------------------------------- sources --

def scan_cantina():
    platform = "Cantina"
    data = get_json("https://cantina.xyz/api/v0/opportunities")
    if data is None:
        return [], f"{platform}: fetch/parse failed (down, blocked, or shape changed)"
    try:
        comps = data.get("groups", {}).get("currentCompetitions", []) or []
    except AttributeError:
        comps = []
    listings = []
    for c in comps:
        if not isinstance(c, dict):
            continue
        name = c.get("title") or c.get("name") or c.get("slug") or "?"
        comp_id = c.get("id") or c.get("slug")
        listings.append({
            "platform": platform,
            "name": str(name)[:120],
            "prize": c.get("prizePool") or c.get("prize"),
            "chain": c.get("chain"),
            "end": c.get("endDate") or c.get("deadline"),
            "status": str(c.get("status", "")).lower() or "live",  # present in currentCompetitions => open
            "id": f"{platform}:{comp_id or name}",
            "url": f"https://cantina.xyz/competitions/{comp_id}" if comp_id else "https://cantina.xyz/competitions",
        })
    return listings, None


def scan_sherlock():
    platform = "Sherlock"
    data = get_json("https://audits.sherlock.xyz/api/contests?order_by_date=false&page=1&per_page=50")
    if not data or "items" not in data:
        return [], f"{platform}: fetch/parse failed (down, blocked, or shape changed)"
    listings = []
    for c in data.get("items", []):
        status = str(c.get("status", "")).lower()
        prize = c.get("prize_pool")
        token = c.get("token")
        prize_s = f"${prize:,}" + (f" {token}" if token else "") if isinstance(prize, (int, float)) else None
        ends_at = c.get("ends_at")
        end_s = datetime.fromtimestamp(ends_at, tz=timezone.utc).strftime("%Y-%m-%d") if ends_at else None
        listings.append({
            "platform": platform,
            "name": str(c.get("title", "?"))[:120],
            "prize": prize_s,
            "chain": None,
            "end": end_s,
            "status": status,
            "id": f"{platform}:{c.get('id')}",
            "url": f"https://audits.sherlock.xyz/contests/{c.get('id')}",
        })
    return listings, None


def scan_defillama(min_tvl=1_000_000):
    data = get_json("https://api.llama.fi/protocols")
    if data is None:
        return [], "DefiLlama: fetch/parse failed"
    protocols = []
    for p in data:
        try:
            tvl = p.get("tvl")
            if tvl is None or tvl < min_tvl:
                continue
            chains = p.get("chains") or []
            protocols.append({
                "platform": "DefiLlama",
                "name": p.get("name"),
                "category": p.get("category"),
                "tvl": tvl,
                "chain": p.get("chain") or (", ".join(chains) if chains else None),
                "id": f"defillama:{p.get('slug') or p.get('name')}",
                "url": f"https://defillama.com/protocol/{p.get('slug', '')}",
            })
        except Exception:
            continue
    return protocols, None


# --- Newest ongoing bug-bounty *programs* (not time-boxed contests) ----
# Separate concern from the audit-contest scanning above: these are
# standing bounty programs, tracked here specifically to catch newly
# *launched* ones as soon as they appear, independent of whether a
# matching DeFi protocol ever shows up on DefiLlama.

HACKERONE_DISCOVERY_QUERY = """query DiscoveryQuery($query: OpportunitiesQuery!, $filter: QueryInput!, $from: Int, $size: Int, $sort: [SortInput!], $post_filters: OpportunitiesFilterInput) {
  me { id __typename }
  opportunities_search(query: $query, filter: $filter, from: $from, size: $size, sort: $sort, post_filters: $post_filters) {
    nodes {
      ... on OpportunityDocument {
        id handle state name launched_at offers_bounties last_updated_at
        currency team_type minimum_bounty_table_value maximum_bounty_table_value
        submission_state
      }
      __typename
    }
    total_count
    __typename
  }
}"""


def scan_hackerone_newest(size=15):
    """HackerOne's public opportunities_search GraphQL query, sorted by
    launched_at DESC — a real, documented-by-observation, no-auth-needed
    API (verified via diagnose.py: works as a plain POST, no browser or
    CSRF token required)."""
    platform = "HackerOne"
    body = {
        "operationName": "DiscoveryQuery",
        "variables": {
            "size": size, "from": 0, "query": {},
            "filter": {"bool": {"filter": [{"bool": {
                "must_not": {"term": {"team_type": "Engagements::Assessment"}},
                "should": [{"term": {"offers_bounties": True}}],
            }}, None]}},
            "sort": [{"field": "launched_at", "direction": "DESC"}],
            "post_filters": {"my_programs": False, "bookmarked": False, "campaign_teams": False},
            "product_area": "opportunity_discovery", "product_feature": "search",
        },
        "query": HACKERONE_DISCOVERY_QUERY,
    }
    data = post_json("https://hackerone.com/graphql", body)
    if not data:
        return [], f"{platform}: fetch/parse failed"
    try:
        nodes = data["data"]["opportunities_search"]["nodes"]
    except (KeyError, TypeError):
        return [], f"{platform}: unexpected response shape"
    programs = []
    for n in nodes:
        handle = n.get("handle")
        min_b, max_b = n.get("minimum_bounty_table_value"), n.get("maximum_bounty_table_value")
        currency = (n.get("currency") or "usd").upper()
        prize = f"{min_b:,}-{max_b:,} {currency}" if min_b is not None and max_b is not None else None
        programs.append({
            "platform": platform,
            "name": n.get("name") or handle,
            "prize": prize,
            "launched_at": n.get("launched_at"),
            "status": (n.get("submission_state") or "").lower(),
            "id": f"{platform}:{handle}",
            "url": f"https://hackerone.com/{handle}" if handle else None,
        })
    return programs, None


HACKENPROOF_STATUS_WORDS = {"live", "paused", "ended", "closed"}
HACKENPROOF_STARTED_RE = re.compile(r"^Started date:\s*(.+)$", re.IGNORECASE)


def scan_hackenproof_newest():
    """hackenproof.com/programs has no discrete listing API (confirmed via
    diagnose.py) but its default (unsorted) order already puts the most
    recently started programs first — verified against live text output,
    where consecutive "Started date:" values were strictly descending."""
    platform = "HackenProof"
    url = "https://hackenproof.com/programs"
    text = get_rendered_text(url)
    if text is None:
        return [], f"{platform}: render failed — {url}"
    ln = lines_of(text)
    programs = []
    for i, line in enumerate(ln):
        if line.lower() not in HACKENPROOF_STATUS_WORDS:
            continue
        name = ln[i + 1] if i + 1 < len(ln) else None
        if not name:
            continue
        started = prize = None
        for fwd in range(2, 30):
            j = i + fwd
            if j >= len(ln):
                break
            m = HACKENPROOF_STARTED_RE.match(ln[j])
            if m:
                started = m.group(1).strip()
            elif started is not None and PRIZE_RE.match(ln[j]):
                prize = ln[j]
                break
        if started is None:
            continue  # not a real card (e.g. matched a stray status word)
        programs.append({
            "platform": platform,
            "name": name[:120],
            "prize": f"up to {prize}" if prize else None,
            "launched_at": started,
            "status": line.lower(),
            "id": f"{platform}:{name}",
            "url": url,  # no clean per-card link found — see README
        })
    if not programs:
        return [], f"{platform}: rendered OK but no listing-shaped entries found — needs a parser re-tune, check {url}"
    return programs, None


# --- Playwright-rendered, text-parsed sources --------------------------

def scan_immunefi():
    platform = "Immunefi"
    url = "https://immunefi.com/audit-competition/"
    text, links = get_rendered_text_and_links(url)
    if text is None:
        return [], f"{platform}: render failed — {url}"
    ln = lines_of(text)
    listings = []
    i = 0
    while i < len(ln):
        if ln[i].startswith("$") and i + 2 < len(ln) and ln[i + 1].lower() == "reward pool":
            prize = ln[i]
            status = ln[i + 2]
            # name is 1-2 lines before the prize (skip "Triaged by Immunefi")
            name = None
            for back in (1, 2, 3):
                j = i - back
                if j < 0:
                    break
                cand = ln[j]
                if cand.lower() in ("triaged by immunefi",) or cand.startswith("$"):
                    continue
                name = cand
                break
            if name:
                item_url = find_link_for_name(links, name, path_hint="/audit-competition/") or url
                listings.append({
                    "platform": platform,
                    "name": name[:120],
                    "prize": prize,
                    "chain": None,
                    "end": ln[i + 3] if i + 3 < len(ln) else None,
                    "status": status.lower(),
                    "id": f"{platform}:{name}",
                    "url": item_url,
                })
            i += 3
        else:
            i += 1
    if not listings:
        return [], f"{platform}: rendered OK but no listing-shaped entries found — needs a parser re-tune, check {url}"
    return listings, None


CODE4RENA_TYPE_LABELS = {"audit", "mitigation review", "bug bounty", "analysis"}
CODE4RENA_STATUS_WORDS = {
    "submissions closed", "completed", "live", "upcoming", "judging",
    "report in progress",
}
DATE_RANGE_RE = re.compile(r"^\d{1,2}\s+[A-Za-z]{3}.*\d{1,2}:\d{2}\s*(AM|PM)?\s*-\s*\d{1,2}\s+[A-Za-z]{3}")
PRIZE_RE = re.compile(r"^\$[\d,]+")


def scan_code4rena():
    platform = "Code4rena"
    url = "https://code4rena.com/audits"
    text, links = get_rendered_text_and_links(url)
    if text is None:
        return [], f"{platform}: render failed — {url}"
    ln = lines_of(text)
    listings = []
    for i, line in enumerate(ln):
        if line.lower() not in CODE4RENA_TYPE_LABELS:
            continue
        name = ln[i + 1] if i + 1 < len(ln) else None
        if not name:
            continue
        # status: nearest preceding recognized status word (within 3 lines)
        status = None
        for back in (1, 2, 3):
            j = i - back
            if j < 0:
                break
            if ln[j].lower() in CODE4RENA_STATUS_WORDS:
                status = ln[j].lower()
                break
        # prize/date: search forward up to ~12 lines
        prize = end = None
        for fwd in range(2, 13):
            j = i + fwd
            if j >= len(ln):
                break
            if end is None and DATE_RANGE_RE.match(ln[j]):
                end = ln[j]
            elif prize is None and PRIZE_RE.match(ln[j]):
                prize = ln[j]
            if prize and end:
                break
        item_url = find_link_for_name(links, name, path_hint="/audits/") or url
        listings.append({
            "platform": platform,
            "name": name[:120],
            "prize": prize,
            "chain": None,
            "end": end,
            "status": status,
            "id": f"{platform}:{name}",
            "url": item_url,
        })
    if not listings:
        return [], f"{platform}: rendered OK but no listing-shaped entries found — needs a parser re-tune, check {url}"
    return listings, None


CODEHAWKS_STATUS_WORDS = {"live", "upcoming", "judging", "ended"}
CODEHAWKS_END_MARKER = "quick actions"


def scan_codehawks():
    platform = "CodeHawks"
    url = "https://codehawks.cyfrin.io"
    text, links = get_rendered_text_and_links(url)
    if text is None:
        return [], f"{platform}: render failed — {url}"
    ln = lines_of(text)
    listings = []
    for i, line in enumerate(ln):
        if line.lower() != CODEHAWKS_END_MARKER:
            continue
        # walk backward from this marker to reconstruct one card's fields
        j = i - 1
        status = None
        # skip an "Ended Xd, Xh ago" line if present
        if j >= 0 and ln[j].lower().startswith("ended") and ln[j].lower() != "ended":
            j -= 1
        if j >= 0 and ln[j].lower() in CODEHAWKS_STATUS_WORDS:
            status = ln[j].lower()
            j -= 1
        currency = ln[j] if j >= 0 and re.match(r"^[A-Z]{2,6}$", ln[j] or "") else None
        if currency:
            j -= 1
        prize = ln[j] if j >= 0 and re.match(r"^[\d,]+(\.\d+)?$", ln[j] or "") else None
        if prize:
            j -= 1
        # skip visibility tag
        if j >= 0 and ln[j] in ("Public", "Private", "Invite-only"):
            j -= 1
        name = ln[j] if j >= 0 else None
        if not name or name.lower() == "kyc rewards":
            if j - 1 >= 0:
                name = ln[j - 1]
        if name:
            item_url = find_link_for_name(links, name, path_hint="/c/") or url
            listings.append({
                "platform": platform,
                "name": name[:120],
                "prize": f"{prize} {currency}" if prize and currency else prize,
                "chain": None,
                "end": None,
                "status": status,
                "id": f"{platform}:{name}",
                "url": item_url,
            })
    if not listings:
        return [], f"{platform}: rendered OK but no listing-shaped entries found — needs a parser re-tune, check {url}"
    return listings, None


# ------------------------------------------------------------------ state --

def load_state():
    if os.path.exists(STATE_PATH):
        try:
            with open(STATE_PATH) as f:
                return json.load(f)
        except Exception:
            pass
    return {
        "seen_contests": [], "seen_protocols": [], "seen_bounty_programs": [],
        "last_digest_at": None, "bootstrapped": False,
    }


def save_state(state):
    with open(STATE_PATH, "w") as f:
        json.dump(state, f, indent=2, sort_keys=True)


# --------------------------------------------------------------- telegram --

def send_telegram(text):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("[warn] TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set — skipping send:\n" + text)
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = json.dumps({
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text[:4000],
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }).encode()
    req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            resp.read()
    except Exception as e:
        print(f"[error] Telegram send failed: {e}", file=sys.stderr)


def resolve_chat_id_if_needed():
    global TELEGRAM_CHAT_ID
    if TELEGRAM_CHAT_ID or not TELEGRAM_TOKEN:
        return
    data = get_json(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates")
    if not data or not data.get("ok"):
        return
    for update in reversed(data.get("result", [])):
        chat = (update.get("message") or update.get("channel_post") or {}).get("chat")
        if chat and chat.get("type") in ("group", "supergroup"):
            TELEGRAM_CHAT_ID = str(chat["id"])
            print(f"[info] Resolved chat_id={TELEGRAM_CHAT_ID} from getUpdates — "
                  f"set this as the TELEGRAM_CHAT_ID secret so future runs don't depend on getUpdates.")
            return


# ---------------------------------------------------------------- format --

def fmt_contest(c):
    bits = [f"<b>{c['name']}</b> ({c['platform']})"]
    if c.get("prize"):
        bits.append(f"\U0001F4B0 {c['prize']}")
    if c.get("chain"):
        bits.append(f"⛓ {c['chain']}")
    if c.get("end"):
        bits.append(f"⏰ ends {c['end']}")
    line = " — ".join(bits)
    if c.get("url"):
        line += f"\n   {c['url']}"
    return line


def fmt_protocol(p, with_bounty_check=False):
    # No link here on purpose — the DefiLlama protocol page isn't a bounty
    # program or contest, just a TVL listing, so it would be misleading to
    # present it as one.
    tvl = p.get("tvl")
    tvl_s = f"${tvl/1e6:.1f}M" if tvl else "?"
    line = f"<b>{p['name']}</b> — {p.get('category','?')} — {tvl_s} TVL — {p.get('chain','?')}"
    if with_bounty_check:
        verdict = p.get("immunefi_bounty")
        if verdict is True:
            line += "\n   ✅ has an Immunefi bounty"
            if p.get("immunefi_bounty_url"):
                line += f"\n   {p['immunefi_bounty_url']}"
        elif verdict is False:
            line += (f"\n   ⚠️ no Immunefi bounty found — check manually: "
                     f"HackenProof (https://hackenproof.com/programs), "
                     f"Cantina (https://cantina.xyz/bounties), "
                     f"Sherlock (https://audits.sherlock.xyz/bug-bounties)")
        else:
            line += "\n   ❓ couldn't check Immunefi (site error) — verify manually"
    return line


def fmt_bounty_program(b):
    bits = [f"<b>{b['name']}</b> ({b['platform']})"]
    if b.get("prize"):
        bits.append(f"\U0001F4B0 {b['prize']}")
    if b.get("launched_at"):
        bits.append(f"\U0001F680 launched {b['launched_at']}")
    line = " — ".join(bits)
    if b.get("url"):
        line += f"\n   {b['url']}"
    return line


# ------------------------------------------------------------------- main --

def main():
    resolve_chat_id_if_needed()
    state = load_state()
    seen_contests = set(state.get("seen_contests", []))
    seen_protocols = set(state.get("seen_protocols", []))
    seen_bounty_programs = set(state.get("seen_bounty_programs", []))

    all_contests = []
    errors = []
    for scan_fn in (scan_immunefi, scan_cantina, scan_code4rena, scan_sherlock, scan_codehawks):
        listings, err = scan_fn()
        if err:
            errors.append(err)
        all_contests.extend(listings)

    protocols, perr = scan_defillama()
    if perr:
        errors.append(perr)

    bounty_programs = []
    for scan_fn in (scan_hackerone_newest, scan_hackenproof_newest):
        listings, err = scan_fn()
        if err:
            errors.append(err)
        bounty_programs.extend(listings)

    # keep contests confirmed open, or whose status couldn't be determined
    # (better to surface a maybe than silently drop it)
    open_contests = [c for c in all_contests if is_open_status(c.get("status")) is not False]

    bootstrapped = state.get("bootstrapped", False)
    # Bounty-program tracking was added after contests/protocols already
    # had real history — it needs its OWN bootstrap flag, or its first
    # run (state.get("bootstrapped") already True from before this
    # existed) treats every pre-existing program as "new" and blasts them
    # all as one alert. (Exactly what happened once, 2026-09-15 — fixed
    # here; that run's stale pending_new_bounty_programs was cleared by
    # hand afterward.)
    bounty_programs_bootstrapped = state.get("bounty_programs_bootstrapped", False)

    new_contests = [c for c in open_contests if c["id"] not in seen_contests]
    new_bounty_programs = [b for b in bounty_programs if b["id"] not in seen_bounty_programs]
    # protocols (from scan_defillama) are deliberately NOT part of
    # alerting or the digest — DefiLlama has no launch-date field, so
    # "new" here only ever meant "just crossed our $1M TVL floor", which
    # can (and did — Allbridge Classic, live ~5 years, 2026-09-15)
    # mislabel a long-established protocol as freshly landed. Messaging
    # is reserved for things with a real "landed" signal: contests and
    # bounty programs. seen_protocols is still maintained below so this
    # can be re-enabled cleanly later with a better "new" heuristic
    # without re-litigating the whole tracked set.

    if not bounty_programs_bootstrapped:
        print("[info] First run of bounty-program tracking — bootstrapping without alerting.")
        new_bounty_programs = []
        state["bounty_programs_bootstrapped"] = True

    if not bootstrapped:
        print("[info] First run — bootstrapping state without alerting.")
        new_contests = []
        state["bootstrapped"] = True

    if _BROWSER is not None:
        try:
            _BROWSER.close()
        except Exception:
            pass

    pending_bounties = {b["id"]: b for b in state.get("pending_new_bounty_programs", [])}
    for b in new_bounty_programs:
        pending_bounties[b["id"]] = b

    if new_contests or new_bounty_programs:
        lines = ["\U0001F195 <b>New audit activity detected</b>"]
        for c in new_contests:
            lines.append("• " + fmt_contest(c))
        for b in new_bounty_programs:
            lines.append("• \U0001F4B0 " + fmt_bounty_program(b))
        send_telegram("\n".join(lines))

    now = datetime.now(timezone.utc)
    last_digest_at = state.get("last_digest_at")
    should_digest = True
    if last_digest_at:
        try:
            elapsed = now - datetime.fromisoformat(last_digest_at)
            should_digest = elapsed.total_seconds() >= DIGEST_INTERVAL_HOURS * 3600
        except Exception:
            should_digest = True
    if should_digest:
        stamp = now.strftime("%Y-%m-%d %H:%M UTC")
        lines = [f"\U0001F4CB <b>Audit/bounty status — {stamp}</b>", ""]
        lines.append(f"<u>Open contests ({len(open_contests)})</u>")
        if open_contests:
            for c in open_contests:
                lines.append("• " + fmt_contest(c))
        else:
            lines.append("none found")
        lines.append("")
        recent_bounties = sorted(
            pending_bounties.values(),
            key=lambda x: x.get("launched_at") or "", reverse=True,
        )
        top5_bounties = recent_bounties[:5]
        lines.append(
            f"<u>Newest bounty programs since last update — top {len(top5_bounties)} of {len(recent_bounties)}</u>"
        )
        if recent_bounties:
            for b in top5_bounties:
                lines.append("• " + fmt_bounty_program(b))
        else:
            lines.append(f"none — {len(bounty_programs)} tracked this run, unchanged")
        if errors:
            lines.append("")
            lines.append("<u>Sources that failed to parse</u>")
            for e in errors:
                lines.append("• " + e)
        send_telegram("\n".join(lines))
        state["last_digest_at"] = now.isoformat()
        pending_bounties = {}  # reset accumulator after reporting

    state["pending_new_bounty_programs"] = list(pending_bounties.values())
    state.pop("pending_new_protocols", None)  # retired — protocols no longer drive any message

    state["seen_contests"] = sorted(seen_contests | {c["id"] for c in open_contests})
    state["seen_protocols"] = sorted(seen_protocols | {p["id"] for p in protocols})
    state["seen_bounty_programs"] = sorted(seen_bounty_programs | {b["id"] for b in bounty_programs})
    save_state(state)

    if errors:
        print("Errors:\n" + "\n".join(errors), file=sys.stderr)


if __name__ == "__main__":
    main()
