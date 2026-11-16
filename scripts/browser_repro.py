"""Drive the live web app in headless Chromium: sign in, send a chat message, report errors.

    python scripts/browser_repro.py --url https://... --email ... --password ...
"""

import argparse
import sys

from playwright.sync_api import sync_playwright


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", required=True)
    ap.add_argument("--email", required=True)
    ap.add_argument("--password", required=True)
    ap.add_argument("--message", default="Where is my laptop? It hasn't arrived.")
    ap.add_argument("--screenshot", default="browser_repro.png")
    args = ap.parse_args()

    problems: list[str] = []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.on("pageerror", lambda e: problems.append(f"pageerror: {e}"))
        page.on(
            "console",
            lambda m: problems.append(f"console.{m.type}: {m.text}")
            if m.type in ("error", "warning")
            else None,
        )
        page.goto(args.url)
        page.get_by_label("Email").fill(args.email)
        page.get_by_label("Password").fill(args.password)
        page.get_by_role("button", name="Continue").click()
        page.get_by_label("Message").wait_for(timeout=30_000)
        page.get_by_label("Message").fill(args.message)
        page.get_by_role("button", name="Send").click()
        # Poll instead of waiting on specific elements, so a crash is still captured.
        body = ""
        for second in range(150):
            page.wait_for_timeout(1_000)
            body = page.inner_text("body")
            if second in (1, 5):
                print(f"t+{second}s body length={len(body.strip())}")
            if page.get_by_role("status").count() == 0 and second > 3:
                break
        page.screenshot(path=args.screenshot, full_page=True)
        browser.close()

    print("BODY TEXT (first 1500 chars):")
    print(body[:1500])
    print("\nPROBLEMS:")
    for line in problems or ["none"]:
        print(" ", line)
    return 1 if not body.strip() or any(x.startswith("pageerror") for x in problems) else 0


if __name__ == "__main__":
    sys.exit(main())
