#!/usr/bin/env python3
"""One-off diagnostic: render each JS-heavy target with a real browser and
record every XHR/fetch JSON response it makes, so the real backing API
endpoints can be identified and called directly in scan.py instead of
scraping rendered HTML on every run. Not part of the recurring monitor —
invoked manually via the Diagnose workflow.
"""
import asyncio
import json
import urllib.request

from playwright.async_api import async_playwright

TARGETS = {}

API_TARGETS = {
    "sherlock_bug_bounties_api": "https://audits.sherlock.xyz/api/bug-bounties?page=1&per_page=20",
}

# Interactive search tests: type into each platform's real search box (URL
# query params don't filter — confirmed via prior diagnostic run) and see
# if the result list actually narrows.
SEARCH_TESTS = {
    "immunefi_interactive_lombard": {
        "url": "https://immunefi.com/bug-bounty/",
        "input_selector": "input[type='search'], input[placeholder*='earch' i]",
        "query": "Lombard",
    },
    "hackenproof_interactive_cetus": {
        "url": "https://hackenproof.com/programs",
        "input_selector": "input[type='search'], input[placeholder*='earch' i]",
        "query": "Cetus",
    },
}


async def search_test(browser, name, cfg):
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
            await page.wait_for_timeout(2500)
            result["text_after_search"] = await page.inner_text("body")
        else:
            # report what search-like elements DO exist, to help find the right selector
            candidates = await page.locator("input").evaluate_all(
                "els => els.map(e => ({type: e.type, placeholder: e.placeholder, name: e.name, id: e.id}))"
            )
            result["all_inputs_on_page"] = candidates
    except Exception as e:
        result["error"] = str(e)
    await page.close()
    return result


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


async def main():
    results = {}
    for name, url in API_TARGETS.items():
        print(f"[diagnose] fetching {name} ({url})")
        results[name] = fetch_api(url)

    if TARGETS or SEARCH_TESTS:
        async with async_playwright() as p:
            browser = await p.chromium.launch()
            for name, url in TARGETS.items():
                print(f"[diagnose] visiting {name} ({url})")
                results[name] = await diagnose_one(browser, name, url)
            for name, cfg in SEARCH_TESTS.items():
                print(f"[diagnose] search test {name} ({cfg['url']} -> '{cfg['query']}')")
                results[name] = await search_test(browser, name, cfg)
            await browser.close()

    with open("diagnose-output.json", "w") as f:
        json.dump(results, f, indent=2)
    print("[diagnose] wrote diagnose-output.json")


if __name__ == "__main__":
    asyncio.run(main())
