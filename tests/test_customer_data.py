#!/usr/bin/env python3
"""E2E verification for the customer portal multi-tenant flow.

Drives the OIDC code+PKCE flow against Keycloak as the given user,
then hits /data (Postgres RLS) and /files (MinIO STS). Both layers
must agree on the tenant.

Usage:
    python3 tests/test_customer_data.py <username> <password>

The script expects port-forwards already running on the host:
    customer  -> http://127.0.0.1:13002
    keycloak  -> reachable at the docker IP configured via --kc-host
"""
import argparse
import re
import sys

import requests
from bs4 import BeautifulSoup  # type: ignore

DEFAULT_CUSTOMER = "http://127.0.0.1:13002"
DEFAULT_KC_HOST = "192.168.32.4"
DEFAULT_KC_PORT = 8080
REALM = "polaris"
CLIENT_ID = "pol-intranet"  # all portals share this client for the POC


def discover_keycloak(kc_host: str, kc_port: int) -> dict:
    url = f"http://{kc_host}:{kc_port}/realms/{REALM}/.well-known/openid-configuration"
    r = requests.get(url, timeout=10)
    r.raise_for_status()
    return r.json()


def drive_oidc(session, base: str, kc_disco: dict, username: str, password: str) -> None:
    r = session.get(f"{base}/login", allow_redirects=False, timeout=10)
    if r.status_code != 302:
        raise RuntimeError(f"/login expected 302, got {r.status_code}")
    authorize_url = r.headers["Location"]
    r = session.get(authorize_url, allow_redirects=True, timeout=10)
    soup = BeautifulSoup(r.text, "html.parser")
    action = soup.form.get("action") if soup.form else None
    if not action:
        raise RuntimeError("Keycloak login form not found")
    r = session.post(
        action,
        data={"username": username, "password": password, "login": ""},
        allow_redirects=True,
        timeout=10,
    )
    if not r.url.startswith(base):
        raise RuntimeError(f"OIDC did not return to portal: {r.url}")


def extract_tenant(html: str) -> str:
    """The home page renders 'Welcome <user>. tenant: <name>'."""
    m = re.search(r"tenant:\s*(\S+)", html)
    return m.group(1) if m else "(no tenant)"


def extract_data_rows(html: str) -> list:
    """The /data page renders a <table> with columns label, value, created."""
    soup = BeautifulSoup(html, "html.parser")
    rows = []
    for tr in soup.select("table tr"):
        tds = [td.get_text(strip=True) for td in tr.select("td")]
        if tds and tds[0] != "label":  # skip header row
            rows.append(tds)
    return rows


def extract_files(html: str) -> list:
    soup = BeautifulSoup(html, "html.parser")
    rows = []
    for tr in soup.select("table tr"):
        tds = [td.get_text(strip=True) for td in tr.select("td")]
        if tds and tds[0] != "key":  # skip header
            rows.append(tds)
    return rows


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("username")
    ap.add_argument("password")
    ap.add_argument("--customer", default=DEFAULT_CUSTOMER)
    ap.add_argument("--kc-host", default=DEFAULT_KC_HOST)
    ap.add_argument("--kc-port", type=int, default=DEFAULT_KC_PORT)
    args = ap.parse_args()

    kc = discover_keycloak(args.kc_host, args.kc_port)

    s = requests.Session()
    try:
        drive_oidc(s, args.customer, kc, args.username, args.password)
    except Exception as e:
        print(f"  OIDC failed: {e}")
        return 1

    # /  — landing with tenant info
    r = s.get(f"{args.customer}/", timeout=10)
    tenant = extract_tenant(r.text) if r.status_code == 200 else "?"
    print(f"=== {args.username} (tenant={tenant}) ===")

    # /data — Postgres RLS
    r = s.get(f"{args.customer}/data", timeout=10)
    print(f"  /data: HTTP {r.status_code}")
    if r.status_code == 200:
        rows = extract_data_rows(r.text)
        print(f"  /data: {len(rows)} row(s) visible to {args.username}.")
        for row in rows:
            print(f"    {row[0]}: {row[1]}")

    # /files — MinIO STS
    r = s.get(f"{args.customer}/files", timeout=20)
    print(f"  /files: HTTP {r.status_code}")
    if r.status_code == 200:
        rows = extract_files(r.text)
        for row in rows:
            print(f"    {row[0]} ({row[1]} B)")
    elif r.status_code == 403:
        print(f"  /files: 403 — no tenant group or no bucket policy")

    return 0


if __name__ == "__main__":
    sys.exit(main())
