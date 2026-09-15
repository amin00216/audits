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

TARGETS = {
    # investigating whether new DeFi protocols already have a bug bounty
    # registered (separate concern from the audit-*contest* scanning above)
    "immunefi_bounties": "https://immunefi.com/bug-bounty/",
    "hackenproof_programs": "https://hackenproof.com/programs",
    "cantina_bounties": "https://cantina.xyz/bounties",
    "sherlock_bounties": "https://audits.sherlock.xyz/bug-bounties",
}

API_TARGETS = {}


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

    if TARGETS:
        async with async_playwright() as p:
            browser = await p.chromium.launch()
            for name, url in TARGETS.items():
                print(f"[diagnose] visiting {name} ({url})")
                results[name] = await diagnose_one(browser, name, url)
            await browser.close()

    with open("diagnose-output.json", "w") as f:
        json.dump(results, f, indent=2)
    print("[diagnose] wrote diagnose-output.json")


if __name__ == "__main__":
    asyncio.run(main())
