#!/usr/bin/env python3
"""One-off diagnostic: render each JS-heavy target with a real browser and
record every XHR/fetch JSON response it makes, so the real backing API
endpoints can be identified and called directly in scan.py instead of
scraping rendered HTML on every run. Not part of the recurring monitor —
invoked manually via the Diagnose workflow.
"""
import asyncio
import json

from playwright.async_api import async_playwright

TARGETS = {
    "immunefi": "https://immunefi.com/audit-competition/",
    "code4rena": "https://code4rena.com/audits",
    "sherlock": "https://audits.sherlock.xyz/contests",
    "codehawks": "https://codehawks.cyfrin.io",
}


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
    try:
        await page.goto(url, wait_until="networkidle", timeout=45000)
        await page.wait_for_timeout(3000)
        text_len = len(await page.inner_text("body"))
    except Exception as e:
        calls.append({"error": f"navigation error: {e}"})
        text_len = None
    await page.close()
    return {"calls": calls, "rendered_text_len": text_len}


async def main():
    results = {}
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
