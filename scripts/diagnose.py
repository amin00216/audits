#!/usr/bin/env python3
"""One-off diagnostic tool for reverse-engineering a site's real data
source or interactive search behavior, using a real browser on the
Actions runner (which has normal internet access, unlike the session
that develops this repo). Not part of the recurring monitor — invoked
manually via the Diagnose workflow by editing the dicts below and
pushing.

Usage patterns (fill in whichever dict you need, leave the rest empty):
  TARGETS       -- render a URL, capture every XHR/fetch JSON call it
                   makes plus the final rendered visible text. Use this
                   first to find a real API endpoint.
  API_TARGETS   -- fetch a URL directly (no browser) and record the
                   JSON/text response. Use once you have a candidate API
                   URL to verify its shape.
  SEARCH_TESTS  -- type a query into a specific input on a page and
                   capture the rendered text afterward, to test whether
                   an interactive search filters results (many sites
                   don't support filtering via URL query params).
"""
import asyncio
import json
import sys
import urllib.error
import urllib.request

from playwright.async_api import async_playwright

TARGETS = {}
API_TARGETS = {}
SEARCH_TESTS = {}

GRAPHQL_CAPTURE_TARGETS = {}


async def capture_graphql(browser, name, url, url_filter):
    page = await browser.new_page()
    captured = []

    async def on_response(resp):
        if url_filter not in resp.url:
            return
        try:
            req = resp.request
            entry = {"url": resp.url, "status": resp.status, "method": req.method}
            if req.method == "POST":
                entry["post_data"] = req.post_data
            try:
                entry["response_body"] = (await resp.text())[:4000]
            except Exception as e:
                entry["response_body_error"] = str(e)
            captured.append(entry)
        except Exception as e:
            captured.append({"error": str(e)})

    page.on("response", lambda r: asyncio.ensure_future(on_response(r)))
    try:
        await page.goto(url, wait_until="networkidle", timeout=45000)
        await page.wait_for_timeout(4000)
        await page.mouse.wheel(0, 2000)
        await page.wait_for_timeout(4000)
    except Exception as e:
        captured.append({"nav_error": str(e)})
    await page.close()
    return captured

# One-off: verify find_link_for_name() resolves real per-contest URLs
# against live Code4rena/CodeHawks data (using scan.py's own sync-API
# implementation, so this tests the exact code that ships). Verified
# 2026-09-15: all 4 test names resolved to exactly the right contest
# URL. Flip back to True (and see run_link_match_test() below) to
# re-verify after changing the matching logic.
LINK_MATCH_TEST = False


async def diagnose_one(browser, name, url):
    page = await browser.new_page()
    calls = []

    def on_response(resp):
        try:
            ct = resp.headers.get("content-type", "")
            rt = resp.request.resource_type
            if "json" in ct or rt in ("xhr", "fetch"):
                calls.append({"url": resp.url, "status": resp.status, "type": rt, "content_type": ct})
        except Exception as e:
            calls.append({"error": f"listener error: {e}"})

    page.on("response", on_response)
    text = None
    try:
        await page.goto(url, wait_until="networkidle", timeout=45000)
        await page.wait_for_timeout(3000)
        text = await page.inner_text("body")
    except Exception as e:
        calls.append({"error": f"navigation error: {e}"})
    await page.close()
    return {"calls": calls, "rendered_text_len": len(text) if text else None, "rendered_text": text}


def fetch_post_json(url, body_obj, headers=None):
    data = json.dumps(body_obj).encode()
    req = urllib.request.Request(url, data=data, method="POST", headers={
        "User-Agent": "Mozilla/5.0 (compatible; AuditMonitorBot/1.0)",
        "Content-Type": "application/json",
        "Accept": "application/json",
        **(headers or {}),
    })
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            try:
                return {"status": resp.status, "json": json.loads(body)}
            except Exception:
                return {"status": resp.status, "text": body[:3000]}
    except urllib.error.HTTPError as e:
        return {"status": e.code, "error_body": e.read().decode("utf-8", errors="replace")[:2000]}
    except Exception as e:
        return {"error": str(e)}


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

POST_API_TARGETS = {
    "hackerone_discovery_direct": {
        "url": "https://hackerone.com/graphql",
        "body": {
            "operationName": "DiscoveryQuery",
            "variables": {
                "size": 10, "from": 0, "query": {},
                "filter": {"bool": {"filter": [{"bool": {
                    "must_not": {"term": {"team_type": "Engagements::Assessment"}},
                    "should": [{"term": {"offers_bounties": True}}],
                }}, None]}},
                "sort": [{"field": "launched_at", "direction": "DESC"}],
                "post_filters": {"my_programs": False, "bookmarked": False, "campaign_teams": False},
                "product_area": "opportunity_discovery", "product_feature": "search",
            },
            "query": HACKERONE_DISCOVERY_QUERY,
        },
    },
}


def fetch_api(url):
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (compatible; AuditMonitorBot/1.0)",
        "Accept": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            try:
                return {"status": resp.status, "json": json.loads(body)}
            except Exception:
                return {"status": resp.status, "text": body[:5000]}
    except Exception as e:
        return {"error": str(e)}


async def search_test(browser, name, cfg):
    """cfg: {url, input_selector, query}. Fills the matched input and
    reports the rendered text afterward, plus which/how many inputs
    matched the selector (helps pick the right one if it's ambiguous)."""
    page = await browser.new_page()
    result = {}
    try:
        await page.goto(cfg["url"], wait_until="networkidle", timeout=45000)
        await page.wait_for_timeout(1500)
        locator = page.locator(cfg["input_selector"]).first
        count = await locator.count()
        result["input_found"] = count > 0
        if count > 0:
            await locator.click()
            await locator.fill(cfg["query"])
            if cfg.get("press_enter"):
                await locator.press("Enter")
            await page.wait_for_timeout(2500)
            result["text_after_search"] = await page.inner_text("body")
        else:
            candidates = await page.locator("input").evaluate_all(
                "els => els.map((e,i) => ({i, type: e.type, placeholder: e.placeholder, "
                "name: e.name, id: e.id, ariaLabel: e.getAttribute('aria-label')}))"
            )
            result["all_inputs_on_page"] = candidates
    except Exception as e:
        result["error"] = str(e)
    await page.close()
    return result


def run_scanner_test():
    import os
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import scan
    out = {}
    for fn in (scan.scan_hackerone_newest, scan.scan_hackenproof_newest):
        listings, err = fn()
        out[fn.__name__] = {"error": err, "count": len(listings), "sample": listings[:8]}
    if scan._BROWSER:
        scan._BROWSER.close()
    return out


def run_link_match_test():
    import os
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import scan  # noqa: reuses the shipped, sync-API implementation as-is
    out = {}
    for platform, url, path_hint, names in [
        ("code4rena", "https://code4rena.com/audits", "/audits/", ["Rujira", "Monetrix", "K2"]),
        ("codehawks", "https://codehawks.cyfrin.io", "/c/", ["BattleChain Confidence Pools"]),
    ]:
        text, links = scan.get_rendered_text_and_links(url)
        out[platform] = {
            "num_links": len(links),
            "matches": {name: scan.find_link_for_name(links, name, path_hint=path_hint) for name in names},
            "sample_links": links[:15],
        }
    if scan._BROWSER:
        scan._BROWSER.close()
    return out


async def main():
    results = {}
    for name, url in API_TARGETS.items():
        print(f"[diagnose] fetching {name} ({url})")
        results[name] = fetch_api(url)

    for name, cfg in POST_API_TARGETS.items():
        print(f"[diagnose] POSTing {name} ({cfg['url']})")
        results[name] = fetch_post_json(cfg["url"], cfg["body"], cfg.get("headers"))

    if TARGETS or SEARCH_TESTS or GRAPHQL_CAPTURE_TARGETS:
        async with async_playwright() as p:
            browser = await p.chromium.launch()
            for name, url in TARGETS.items():
                print(f"[diagnose] visiting {name} ({url})")
                results[name] = await diagnose_one(browser, name, url)
            for name, cfg in SEARCH_TESTS.items():
                print(f"[diagnose] search test {name} ({cfg['url']} -> '{cfg['query']}')")
                results[name] = await search_test(browser, name, cfg)
            for name, url in GRAPHQL_CAPTURE_TARGETS.items():
                print(f"[diagnose] capturing graphql for {name} ({url})")
                results[name] = await capture_graphql(browser, name, url, "graphql")
            await browser.close()

    with open("diagnose-output.json", "w") as f:
        json.dump(results, f, indent=2)
    print("[diagnose] wrote diagnose-output.json")


SCANNER_TEST = True

if __name__ == "__main__":
    # Anything using scan.py's sync Playwright API must run outside the
    # asyncio event loop main() uses below, or Playwright's sync API
    # raises ("Sync API inside asyncio loop").
    sync_results = {}
    if LINK_MATCH_TEST:
        print("[diagnose] running link-match test against scan.py's real functions")
        sync_results["link_match_test"] = run_link_match_test()
    if SCANNER_TEST:
        print("[diagnose] running scanner test against scan.py's real functions")
        sync_results["scanner_test"] = run_scanner_test()
    if sync_results:
        with open("diagnose-output.json", "w") as f:
            json.dump(sync_results, f, indent=2)
        print("[diagnose] wrote diagnose-output.json (sync tests only)")
    else:
        asyncio.run(main())
