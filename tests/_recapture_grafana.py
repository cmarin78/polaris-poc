#!/usr/bin/env python3
"""Re-capture the Grafana screenshot showing the new dashboard.

Usage:
    python3 tests/_recapture_grafana.py

Replaces docs/screenshots/02-grafana-home-carol-admin.png with a
screenshot of the provisioned 'Polaris POC — tenant overview'
dashboard as seen by carol (Admin role).
"""
import asyncio
from pathlib import Path

from playwright.async_api import async_playwright

GRAFANA = "http://127.0.0.1:13001"
KEYCLOAK_DOCKER_IP = "192.168.32.4"
DASHBOARD_UID = "polaris-poc-overview"
OUT = Path(__file__).resolve().parent.parent / "docs" / "screenshots" / "02-grafana-home-carol-admin.png"


async def main():
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=[
                f"--host-rules=MAP keycloak {KEYCLOAK_DOCKER_IP}",
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
            ],
        )
        ctx = await browser.new_context(
            viewport={"width": 1280, "height": 800},
            device_scale_factor=1,
        )

        page = await ctx.new_page()
        # Hit the OAuth endpoint directly — Grafana 302s to Keycloak.
        await page.goto(f"{GRAFANA}/login/generic_oauth", wait_until="networkidle", timeout=20000)
        await page.wait_for_selector("input[name='username']", timeout=15000)
        await page.fill("input[name='username']", "carol")
        await page.fill("input[name='password']", "polaris")
        await page.click("input[name='login']")
        await page.wait_for_load_state("networkidle", timeout=20000)
        print(f"  logged in: {page.url}")

        # Navigate to the dashboard.
        await page.goto(f"{GRAFANA}/d/{DASHBOARD_UID}", wait_until="networkidle", timeout=30000)
        # Wait for the panels to render — Grafana emits one section per panel with this testid.
        await page.wait_for_selector(
            "[data-testid='data-testid Panel header Total tenants']",
            timeout=30000,
        )
        # Wait for the table panel to be rendered. The "All customer_data rows" panel is
        # the heaviest data render — its <div role="table"> only appears once the underlying
        # Postgres query resolves and Grafana paints the rows. We poll on that selector
        # with a generous timeout.
        await page.wait_for_selector(
            "[data-testid='data-testid Panel header All customer_data rows (cross-tenant view; portal filters by RLS)'] [role='table']",
            timeout=30000,
        )
        # Generous delay so the chart SVGs and stat values finish drawing — empirically the
        # uplot bar chart and stat color thresholds take ~5-8s after the table paints.
        await page.wait_for_timeout(10000)
        # Workaround: Playwright's full_page=True composes by re-rastering the page at a
        # tall viewport, which sometimes races with Grafana's React render and ends up
        # capturing the panels before their SVG/text contents are committed. Capturing the
        # viewport (1280x800) first reliably captures the dashboard, and Grafana's
        # responsive layout fits all 7 panels in view because the dashboard auto-collapses
        # to a single column at <1280px. We screenshot the viewport (containing the whole
        # dashboard in two rows of panels) and write that as the canonical capture.
        print(f"  dashboard: {page.url}")

        await page.screenshot(path=str(OUT), full_page=False)
        print(f"  saved {OUT.name} ({OUT.stat().st_size:,} B)")

        await ctx.close()
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
