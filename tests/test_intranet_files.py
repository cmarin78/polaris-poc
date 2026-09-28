#!/usr/bin/env python3
"""E2E verification for the intranet portal.

Drives the OIDC code+PKCE flow against Keycloak as the given user,
then hits /directory and /files and prints the response summary.

Usage:
    python3 tests/test_intranet_files.py <username> <password>

The script expects port-forwards already running on the host:
    intranet  -> http://127.0.0.1:13000
    keycloak  -> reachable at the docker IP configured via --kc-host
"""
import argparse
import sys
from urllib.parse import urlencode, urlparse, parse_qs
import re

import requests
from bs4 import BeautifulSoup  # type: ignore

DEFAULT_INTRANET = "http://127.0.0.1:13000"
DEFAULT_KC_HOST = "192.168.32.4"  # docker bridge IP for the keycloak container
DEFAULT_KC_PORT = 8080
REALM = "polaris"
CLIENT_ID = "pol-intranet"


def discover_keycloak(kc_host: str, kc_port: int) -> dict:
    url = f"http://{kc_host}:{kc_port}/realms/{REALM}/.well-known/openid-configuration"
    r = requests.get(url, timeout=10)
    r.raise_for_status()
    return r.json()


def drive_oidc(session, intranet_base: str, kc_disco: dict, username: str, password: str,
               kc_host: str, kc_port: int) -> None:
    """Drive the intranet OIDC flow end-to-end and leave the cookies in `session`.

    The portal's /login redirects to http://keycloak:8080/... — the
    `keycloak` hostname only resolves inside the docker-compose
    network. Since the test runs on the host, we rewrite the
    redirect target to point at the docker bridge IP (kc_host /
    kc_port) before following it.
    """
    # 1. Hit /login -> 302 to Keycloak authorize
    r = session.get(f"{intranet_base}/login", allow_redirects=False, timeout=10)
    if r.status_code != 302:
        raise RuntimeError(f"/login expected 302, got {r.status_code}")
    authorize_url = r.headers["Location"]
    # Rewrite `keycloak:8080` -> `kc_host:kc_port` for host-side reachability.
    authorize_url = authorize_url.replace("keycloak:8080", f"{kc_host}:{kc_port}")
    # Same rewrite applies to the form action we'll get back from Keycloak.
    kc_reachable = f"{kc_host}:{kc_port}"

    # 2. Submit credentials at Keycloak
    r = session.get(authorize_url, allow_redirects=True, timeout=10)
    soup = BeautifulSoup(r.text, "html.parser")
    action = soup.form.get("action") if soup.form else None
    if not action:
        raise RuntimeError("Keycloak login form not found")
    # Keycloak posts to its own /login-actions/authenticate — same host rewrite.
    action = action.replace("keycloak:8080", kc_reachable)
    r = session.post(
        action,
        data={"username": username, "password": password, "login": ""},
        allow_redirects=True,
        timeout=10,
    )
    # Following the redirect chain ends up back on the intranet host (callback).
    # If we land on a non-intranet host, something went wrong.
    if not r.url.startswith(intranet_base):
        raise RuntimeError(f"OIDC did not return to intranet: {r.url}")


def list_employees(html: str) -> list:
    soup = BeautifulSoup(html, "html.parser")
    rows = []
    for tr in soup.select("table tr"):
        tds = [td.get_text(strip=True) for td in tr.select("td")]
        if tds:
            rows.append(tds)
    return rows


def list_files(html: str) -> list:
    # The /files page lists bucket objects in a <table>. Each row is one object.
    soup = BeautifulSoup(html, "html.parser")
    rows = []
    for tr in soup.select("table tr"):
        tds = [td.get_text(strip=True) for td in tr.select("td")]
        if tds:
            rows.append(tds)
    return rows


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("username")
    ap.add_argument("password")
    ap.add_argument("--intranet", default=DEFAULT_INTRANET)
    ap.add_argument("--kc-host", default=DEFAULT_KC_HOST)
    ap.add_argument("--kc-port", type=int, default=DEFAULT_KC_PORT)
    args = ap.parse_args()

    kc = discover_keycloak(args.kc_host, args.kc_port)
    print(f"=== {args.username} (intranet) ===")

    s = requests.Session()
    try:
        drive_oidc(s, args.intranet, kc, args.username, args.password,
                   args.kc_host, args.kc_port)
    except Exception as e:
        print(f"  OIDC failed: {e}")
        return 1

    # /directory is an employee-only resource. Anyone in pol-employees can read.
    r = s.get(f"{args.intranet}/directory", timeout=10)
    if r.status_code != 200:
        print(f"  /directory: HTTP {r.status_code}")
    else:
        rows = list_employees(r.text)
        print(f"  /directory: HTTP 200 ({len(rows)} employees)")

    # /files uses MinIO STS against the user's groups. Without a bucket policy,
    # this is 403 for employees with no per-bucket mapping.
    r = s.get(f"{args.intranet}/files", timeout=20)
    print(f"  /files:    HTTP {r.status_code}")
    if r.status_code == 200:
        rows = list_files(r.text)
        for row in rows:
            print(f"    - {row[0]} ({row[1]} B)")
    elif r.status_code == 403:
        print(f"  (no bucket policy maps to this user's groups — deny by default)")

    return 0


if __name__ == "__main__":
    sys.exit(main())
