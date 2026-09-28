#!/usr/bin/env python3
"""
Capture screenshots of the 3 portals for the public repo.

Reaches the portal via a localhost port-forward (e.g., intranet on 13000),
follows the OIDC code+PKCE flow, and screenshots the post-login page.

Uses chromium's --host-rules=MAP keycloak 192.168.32.4 to make the browser
resolve `keycloak` to the docker network IP of the Keycloak container.
That container's port 8080 is reachable from the host (the docker bridge
routes it).

Run with: python3 tests/capture_screenshots.py
"""
import asyncio
import subprocess
from pathlib import Path

from playwright.async_api import async_playwright

INTRANET = "http://127.0.0.1:13000"
GRAFANA = "http://127.0.0.1:13001"
CUSTOMER = "http://127.0.0.1:13002"
KEYCLOAK_DOCKER_IP = "192.168.32.4"

OUT_DIR = Path(__file__).resolve().parent.parent / "docs" / "screenshots"
OUT_DIR.mkdir(parents=True, exist_ok=True)


async def login(ctx, portal_url, username, password):
    """Drive the portal's OIDC flow. Returns the page after login completes."""
    page = await ctx.new_page()
    await page.goto(f"{portal_url}/login", wait_until="networkidle", timeout=20000)
    # After /login, the browser is on the Keycloak auth URL.
    if "/protocol/openid-connect/auth" not in page.url:
        # Maybe we ended up on a callback error page.
        body = (await page.evaluate("() => document.body.innerText"))[:300]
        raise RuntimeError(f"unexpected URL after /login: {page.url} -- {body}")
    await page.wait_for_selector("input[name='username']", timeout=15000)
    await page.fill("input[name='username']", username)
    await page.fill("input[name='password']", password)
    await page.click("input[name='login']")
    # Wait for the redirect chain to complete and end up on the portal's home.
    await page.wait_for_load_state("networkidle", timeout=20000)
    return page


async def shoot(page, name):
    path = OUT_DIR / f"{name}.png"
    await page.screenshot(path=str(path), full_page=True)
    print(f"  saved {path.name} ({path.stat().st_size:,} B)")


async def close_page(page):
    if page and not page.is_closed():
        try:
            await page.close()
        except Exception:
            pass


async def main():
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=[f"--host-rules=MAP keycloak {KEYCLOAK_DOCKER_IP}"],
        )
        ctx = await browser.new_context(viewport={"width": 1280, "height": 800})

        # === 01 INTRANET — alice ===
        print("\n[01] intranet /directory as alice (employee)")
        page = None
        try:
            page = await login(ctx, INTRANET, "alice", "polaris")
            await page.click("text=directory")
            await page.wait_for_load_state("networkidle", timeout=10000)
            print(f"  landed: {page.url}")
            await shoot(page, "01-intranet-directory-alice")
        except Exception as e:
            print(f"  failed: {e}")
        await close_page(page)

        # === 02 GRAFANA — carol (Admin role) ===
        # Clear cookies first so carol can fully log in (the Keycloak
        # SSO session for alice from [01] would otherwise pre-fill
        # carol's username away).
        # Grafana's generic_oauth endpoint handles the full PKCE-style
        # auth code flow when given an empty session, so we go there
        # directly to skip the SPA-rendered "Sign in with Keycloak" button.
        print("\n[02] grafana home as carol (pol-admin → Admin)")
        page = None
        try:
            await ctx.clear_cookies()
            page = await ctx.new_page()
            # Hit the OAuth endpoint directly — Grafana 302s to Keycloak.
            await page.goto(f"{GRAFANA}/login/generic_oauth", wait_until="networkidle", timeout=20000)
            await page.wait_for_selector("input[name='username']", timeout=15000)
            await page.fill("input[name='username']", "carol")
            await page.fill("input[name='password']", "polaris")
            await page.click("input[name='login']")
            await page.wait_for_load_state("networkidle", timeout=20000)
            print(f"  landed: {page.url}")
            await shoot(page, "02-grafana-home-carol-admin")
        except Exception as e:
            print(f"  failed: {e}")
        await close_page(page)

        # === 03-05 CUSTOMER — alice-acme (home + data + files) ===
        # Clear Keycloak cookies first so the realm SSO session from
        # the [01] intranet login as alice doesn't make Keycloak show
        # only a password field with alice pre-filled (we need the full
        # login form so the test can enter alice-acme's credentials).
        # We also wipe 127.0.0.1 session cookies to drop any leftover
        # Flask sessions from previous portals.
        print("\n[03-05] customer alice-acme (home + data + files)")
        page = None
        try:
            await ctx.clear_cookies()
            page = await login(ctx, CUSTOMER, "alice-acme", "polaris")
            print(f"  [03] landed: {page.url}")
            await shoot(page, "03-customer-home-alice-acme")

            await page.goto(f"{CUSTOMER}/data", wait_until="networkidle", timeout=10000)
            print(f"  [04] landed: {page.url}")
            await shoot(page, "04-customer-data-alice-acme-acme-only")

            await page.goto(f"{CUSTOMER}/files", wait_until="networkidle", timeout=10000)
            print(f"  [05] landed: {page.url}")
            await shoot(page, "05-customer-files-alice-acme")
        except Exception as e:
            print(f"  failed: {e}")
        await close_page(page)

        # === 06 CUSTOMER — alice (no tenant group → 403) ===
        # Use a fresh browser context so the alice-acme session from
        # 03-05 doesn't bleed into this login (same Flask session cookie).
        print("\n[06] customer /data as alice (no tenant → 403)")
        page = None
        try:
            ctx2 = await browser.new_context(viewport={"width": 1280, "height": 800})
            page = await login(ctx2, CUSTOMER, "alice", "polaris")
            await page.goto(f"{CUSTOMER}/data", wait_until="networkidle", timeout=10000)
            print(f"  landed: {page.url}")
            await shoot(page, "06-customer-data-alice-403-no-tenant")
            await ctx2.close()
        except Exception as e:
            print(f"  failed: {e}")
        await close_page(page)

        await ctx.close()
        await browser.close()
        print("\nDone.")


if __name__ == "__main__":
    asyncio.run(main())