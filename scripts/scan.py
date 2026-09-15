#!/usr/bin/env python3
"""
Audit-contest + new-protocol monitor.

Scans Immunefi, Code4rena, Sherlock, Cantina and CodeHawks for open audit
contests, and DefiLlama for newly-listed DeFi protocols >= $1M TVL.
Sends a Telegram alert immediately for anything new since the last run,
and a consolidated daily digest once per UTC day (after DIGEST_HOUR_UTC).

State (state.json, committed back to the repo by the workflow) is what
makes "new" detection possible across runs.

Known limitation: Code4rena / Sherlock / CodeHawks / Immunefi are scraped
via a generic heuristic (look for a Next.js __NEXT_DATA__ blob, then find
dict entries whose keys look like a contest listing). This was written
without the ability to hit those sites live to verify exact field names —
expect it to need a tuning pass against real output from the first run.
Cantina and DefiLlama use documented JSON APIs and should be reliable
from the start.
"""
import json
import os
import re
import sys
import urllib.request
import urllib.error
from datetime import datetime, timezone

STATE_PATH = os.path.join(os.path.dirname(__file__), "..", "state.json")
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
DIGEST_HOUR_UTC = int(os.environ.get("DIGEST_HOUR_UTC", "8"))
UA = "Mozilla/5.0 (compatible; AuditMonitorBot/1.0; +https://github.com/amin00216/audits)"

OPEN_STATUSES = {"live", "active", "upcoming", "open", "ongoing"}
CLOSED_STATUSES = {
    "judging", "evaluating", "finished", "closed", "ended", "completed",
    "review", "mitigation",
}


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


NEXT_DATA_RE = re.compile(
    r'<script id="__NEXT_DATA__" type="application/json"[^>]*>(.*?)</script>',
    re.DOTALL,
)


def extract_next_data(html):
    if not html:
        return None
    m = NEXT_DATA_RE.search(html)
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except Exception as e:
        print(f"[warn] __NEXT_DATA__ parse failed: {e}", file=sys.stderr)
        return None


NAME_KEYS = {"title", "name", "projectname", "protocolname", "slug"}
PRIZE_KEYS = {
    "prize", "prizepool", "totalprize", "rewardpool", "reward",
    "totalrewards", "maxrewards", "totalprizepool", "usdprizepool",
}
DATE_KEYS = {
    "enddate", "deadline", "contestend", "endtime", "end", "endsat",
    "submissionclosedate", "endtimestamp",
}
CHAIN_KEYS = {"chain", "blockchain", "chains", "network"}
STATUS_KEYS = {"status", "state", "phase"}


def _lower_keys(d):
    return {k.lower(): k for k in d.keys()}


def find_candidate_listings(node, found=None, depth=0, max_depth=14):
    """Best-effort recursive walk of an unknown JSON tree (e.g. a Next.js
    __NEXT_DATA__ payload) to find dict entries that look like contest
    listing cards, based on key-name heuristics rather than an exact
    schema (which couldn't be verified against live data here)."""
    if found is None:
        found = []
    if depth > max_depth:
        return found
    if isinstance(node, dict):
        lk = _lower_keys(node)
        has_name = bool(lk.keys() & NAME_KEYS)
        has_signal = bool(
            (lk.keys() & PRIZE_KEYS) or (lk.keys() & DATE_KEYS) or (lk.keys() & STATUS_KEYS)
        )
        if has_name and has_signal:
            found.append(node)
        for v in node.values():
            find_candidate_listings(v, found, depth + 1, max_depth)
    elif isinstance(node, list):
        for item in node:
            find_candidate_listings(item, found, depth + 1, max_depth)
    return found


def normalize_listing(raw, platform, base_url=""):
    if not isinstance(raw, dict):
        return None
    lk = _lower_keys(raw)

    def pick(keys):
        for k in keys:
            if k in lk:
                return raw[lk[k]]
        return None

    name = pick(NAME_KEYS) or "?"
    prize = pick(PRIZE_KEYS)
    end = pick(DATE_KEYS)
    chain = pick(CHAIN_KEYS)
    status = pick(STATUS_KEYS)
    slug = pick({"slug", "id", "_id"}) or name
    return {
        "platform": platform,
        "name": str(name)[:120],
        "prize": prize,
        "chain": chain if isinstance(chain, str) else (", ".join(chain) if isinstance(chain, list) else chain),
        "end": end,
        "status": str(status).lower() if status is not None else None,
        "id": f"{platform}:{slug}",
        "url": base_url,
    }


def is_open(listing):
    status = listing.get("status")
    if status is None:
        return None  # unknown — treated as "keep, can't confirm closed"
    if any(s in status for s in CLOSED_STATUSES):
        return False
    if any(s in status for s in OPEN_STATUSES):
        return True
    return None


# ---------------------------------------------------------------- sources --

def scan_cantina():
    platform = "Cantina"
    data = get_json("https://cantina.xyz/api/v0/opportunities")
    if data is None:
        return [], f"{platform}: fetch/parse failed (down, blocked, or shape changed)"
    comps = []
    try:
        comps = data.get("groups", {}).get("currentCompetitions", []) or []
    except AttributeError:
        comps = []
    if not comps:
        comps = find_candidate_listings(data)
    listings = [l for l in (normalize_listing(c, platform, "https://cantina.xyz/competitions") for c in comps) if l]
    return listings, None


def scan_next_data_site(url, platform):
    html = http_get(url)
    if html is None:
        return [], f"{platform}: fetch failed (blocked or unreachable) — {url}"
    data = extract_next_data(html)
    if data is None:
        return [], f"{platform}: page returned no usable data (JS-rendered, no __NEXT_DATA__ found) — manual check needed at {url}"
    candidates = find_candidate_listings(data)
    if not candidates:
        return [], f"{platform}: __NEXT_DATA__ found but no listing-shaped entries — manual check needed at {url}"
    listings = [l for l in (normalize_listing(c, platform, url) for c in candidates) if l]
    return listings, None


def scan_code4rena():
    return scan_next_data_site("https://code4rena.com/audits", "Code4rena")


def scan_sherlock():
    return scan_next_data_site("https://audits.sherlock.xyz/contests", "Sherlock")


def scan_codehawks():
    return scan_next_data_site("https://codehawks.cyfrin.io", "CodeHawks")


def scan_immunefi():
    return scan_next_data_site("https://immunefi.com/audit-competition/", "Immunefi")


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


# ------------------------------------------------------------------ state --

def load_state():
    if os.path.exists(STATE_PATH):
        try:
            with open(STATE_PATH) as f:
                return json.load(f)
        except Exception:
            pass
    return {"seen_contests": [], "seen_protocols": [], "last_digest_date": None, "bootstrapped": False}


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
    return " — ".join(bits)


def fmt_protocol(p):
    tvl = p.get("tvl")
    tvl_s = f"${tvl/1e6:.1f}M" if tvl else "?"
    return f"<b>{p['name']}</b> — {p.get('category','?')} — {tvl_s} TVL — {p.get('chain','?')}"


# ------------------------------------------------------------------- main --

def main():
    resolve_chat_id_if_needed()
    state = load_state()
    seen_contests = set(state.get("seen_contests", []))
    seen_protocols = set(state.get("seen_protocols", []))

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

    # keep contests that are confirmed open, or whose status we couldn't
    # determine (better to surface a maybe than silently drop it)
    open_contests = [c for c in all_contests if is_open(c) is not False]

    bootstrapped = state.get("bootstrapped", False)

    new_contests = [c for c in open_contests if c["id"] not in seen_contests]
    new_protocols = [p for p in protocols if p["id"] not in seen_protocols]

    if not bootstrapped:
        print("[info] First run — bootstrapping state without alerting.")
        new_contests = []
        new_protocols = []
        state["bootstrapped"] = True

    if new_contests or new_protocols:
        lines = ["\U0001F195 <b>New audit activity detected</b>"]
        for c in new_contests:
            lines.append("• " + fmt_contest(c))
        for p in new_protocols:
            lines.append("• \U0001F9EA " + fmt_protocol(p) + " (check for a bug bounty)")
        send_telegram("\n".join(lines))

    now = datetime.now(timezone.utc)
    today = now.date().isoformat()
    should_digest = now.hour >= DIGEST_HOUR_UTC and state.get("last_digest_date") != today
    if should_digest:
        lines = [f"\U0001F4CB <b>Daily audit/bounty digest — {today}</b>", ""]
        lines.append(f"<u>Open contests ({len(open_contests)})</u>")
        if open_contests:
            for c in open_contests:
                lines.append("• " + fmt_contest(c))
        else:
            lines.append("none found")
        lines.append("")
        top_protocols = sorted(protocols, key=lambda x: -(x.get("tvl") or 0))[:5]
        lines.append(f"<u>Newest protocols ≥$1M TVL (top 5 of {len(protocols)} tracked)</u>")
        for p in top_protocols:
            lines.append("• " + fmt_protocol(p))
        if errors:
            lines.append("")
            lines.append("<u>Sources that failed to parse</u>")
            for e in errors:
                lines.append("• " + e)
        send_telegram("\n".join(lines))
        state["last_digest_date"] = today

    state["seen_contests"] = sorted(seen_contests | {c["id"] for c in open_contests})
    state["seen_protocols"] = sorted(seen_protocols | {p["id"] for p in protocols})
    save_state(state)

    if errors:
        print("Errors:\n" + "\n".join(errors), file=sys.stderr)


if __name__ == "__main__":
    main()
