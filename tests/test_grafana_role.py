#!/usr/bin/env python3
"""E2E verification for the Grafana role mapping.

Drives the OIDC flow against Grafana's /login/generic_oauth, then
queries the Grafana API to verify the user's role and that the
polaris-postgres datasource is reachable.

Usage:
    python3 tests/test_grafana_role.py <username> <password>

The script expects port-forwards already running on the host:
    grafana   -> http://127.0.0.1:13001
    keycloak  -> reachable at the docker IP configured via --kc-host
"""
import argparse
import re
import sys

import requests
from bs4 import BeautifulSoup  # type: ignore

DEFAULT_GRAFANA = "http://127.0.0.1:13001"
DEFAULT_KC_HOST = "192.168.32.4"
DEFAULT_KC_PORT = 8080
REALM = "polaris"
CLIENT_ID = "pol-grafana"

# Expected role mapping per the JMESPath in GF_AUTH_GENERIC_OAUTH_ROLE_ATTRIBUTE_PATH:
#   contains(groups[*], 'pol-admin') && 'Admin'
#   || contains(groups[*], 'pol-ops') && 'Editor'
#   || 'Viewer'
EXPECTED_ROLE = {
    "alice": "Viewer",
    "bob": "Editor",
    "carol": "Admin",
    "alice-acme": "Viewer",
    "bob-brightside": "Viewer",
    "carol-northwind": "Viewer",
}


def drive_oauth(session, grafana_base: str, username: str, password: str) -> None:
    """Hit /login/generic_oauth -> Keycloak -> /login/generic_oauth?code=... -> session."""
    r = session.get(f"{grafana_base}/login/generic_oauth", allow_redirects=True, timeout=15)
    soup = BeautifulSoup(r.text, "html.parser")
    # If we are on a Keycloak login form, submit credentials.
    if soup.form and "kc-form-login" in (soup.form.get("id") or ""):
        action = soup.form.get("action")
        session.post(
            action,
            data={"username": username, "password": password, "login": ""},
            allow_redirects=True,
            timeout=15,
        )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("username")
    ap.add_argument("password")
    ap.add_argument("--grafana", default=DEFAULT_GRAFANA)
    ap.add_argument("--kc-host", default=DEFAULT_KC_HOST)
    ap.add_argument("--kc-port", type=int, default=DEFAULT_KC_PORT)
    args = ap.parse_args()

    print(f"=== {args.username} ===")
    s = requests.Session()
    try:
        drive_oauth(s, args.grafana, args.username, args.password)
    except Exception as e:
        print(f"  OAuth failed: {e}")
        return 1

    # Whoami: Grafana exposes /api/user when authenticated.
    r = s.get(f"{args.grafana}/api/user", timeout=10)
    if r.status_code != 200:
        print(f"  /api/user: HTTP {r.status_code} (not authenticated?)")
        return 1
    user = r.json()
    actual_role = user.get("orgRole") or user.get("role") or "(unknown)"
    print(f"  Grafana role: {actual_role}")

    expected = EXPECTED_ROLE.get(args.username)
    if expected and actual_role != expected:
        print(f"  WARNING: expected role {expected}, got {actual_role}")
        return 2

    # Datasource health check.
    r = s.get(f"{args.grafana}/api/datasources/name/polaris-postgres/health", timeout=10)
    if r.status_code == 200:
        body = r.json()
        ok = body.get("status") == "OK" or body.get("database") == "OK"
        print(f"  Datasource polaris-postgres reachable: {'yes' if ok else 'no'}")
    else:
        print(f"  Datasource health: HTTP {r.status_code}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
