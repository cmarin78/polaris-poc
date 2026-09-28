#!/usr/bin/env python3
"""
polaris POC — Intranet portal.

Endpoints:
  GET  /                home (links to directory, files, grafana, customer)
  GET  /directory       list of employees from pol_intranet DB
  GET  /files           list of files from the user's MinIO bucket
  GET  /healthz         JSON status

Auth flow (OIDC against Keycloak):
  - User hits any page -> if no session, redirect to /login
  - /login -> 302 to Keycloak authorize endpoint
  - Keycloak authenticates, redirects back to /callback?code=...
  - /callback exchanges code for tokens (id_token, access_token, refresh_token)
  - id_token is stored in encrypted session; the `groups` field drives the
    user's bucket choice in /files (mapped from Keycloak group to a
    MinIO bucket via OIDC + MinIO IAM).
  - access_token is sent to MinIO STS AssumeRoleWithWebIdentity when the
    user wants to read S3.

S3 access (the IAM layer):
  - User logs in via Keycloak -> Keycloak issues a JWT with `groups` claim.
  - /files calls POST /api/v1/sts/assume-role-with-web-identity on MinIO,
    passing the JWT (with minio-oidc-client-id as audience).
  - MinIO validates the JWT against Keycloak's JWKS, extracts the `groups`
    claim, maps it to a MinIO IAM policy (configured server-side), and
    returns temporary STS credentials (AccessKey, SecretKey, SessionToken,
    Expiration).
  - /files uses boto3 with the STS creds to ListObjects in the user's
    permitted bucket.
  - If the user has no mapping (no groups match a policy), MinIO returns
    403 AccessDenied and /files shows a deny message.
"""
import os
import json
import logging
import time
import secrets
from urllib.parse import urlencode, urlparse, parse_qs

import boto3
import psycopg2
import psycopg2.extras
from botocore.client import Config
from botocore.exceptions import ClientError
from flask import Flask, redirect, request, session, jsonify, render_template_string, url_for

logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s %(levelname)s %(name)s %(message)s')
log = logging.getLogger("intranet")

# ===== Configuration =====
KEYCLOAK_ISSUER = os.environ.get("KEYCLOAK_ISSUER", "http://keycloak:8080/realms/polaris")
KEYCLOAK_CLIENT_ID = os.environ.get("KEYCLOAK_CLIENT_ID", "pol-intranet")
KEYCLOAK_CLIENT_SECRET = os.environ.get("KEYCLOAK_CLIENT_SECRET", "")  # public client
KEYCLOAK_DISCOVERY_URL = f"{KEYCLOAK_ISSUER}/.well-known/openid-configuration"
MINIO_ENDPOINT = os.environ.get("MINIO_ENDPOINT", "http://minio:9000")
MINIO_STS_ENDPOINT = f"{MINIO_ENDPOINT}/"
POSTGRES_HOST = os.environ.get("POSTGRES_HOST", "postgres")
POSTGRES_PORT = int(os.environ.get("POSTGRES_PORT", "5432"))
POSTGRES_DB = os.environ.get("POSTGRES_DB", "pol_intranet")
POSTGRES_USER = os.environ.get("POSTGRES_USER", "polaris_admin")
POSTGRES_PASSWORD = os.environ.get("POSTGRES_PASSWORD", "polaris")
PUBLIC_URL = os.environ.get("PUBLIC_URL", "http://intranet.polaris.ts.net")

# Map from Keycloak group -> MinIO bucket (one per user-role)
GROUP_TO_BUCKET = {
    "pol-employees": "pol-data-employees",
    "pol-ops": "pol-data-ops",
    "pol-admin": "pol-data-employees",  # admin sees employees bucket too; full power via ACL
    "pol-customer-tenant-acme": "pol-data-acme",
    "pol-customer-tenant-brightside": "pol-data-brightside",
    "pol-partners-northwind": "pol-data-partners",
}
# Priority order: first match wins
GROUP_PRIORITY = [
    "pol-admin",
    "pol-customer-tenant-acme",
    "pol-customer-tenant-brightside",
    "pol-partners-northwind",
    "pol-ops",
    "pol-employees",
]

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET", secrets.token_hex(32))

# Cache Keycloak discovery document
_kc_discovery = None


def _kc_get_discovery():
    """Lazy-fetch the OIDC discovery doc from Keycloak."""
    global _kc_discovery
    if _kc_discovery is None:
        import urllib.request
        with urllib.request.urlopen(KEYCLOAK_DISCOVERY_URL, timeout=5) as r:
            _kc_discovery = json.loads(r.read())
    return _kc_discovery


def _pick_bucket(groups):
    """Pick the MinIO bucket the user is allowed to see based on groups."""
    for g in GROUP_PRIORITY:
        if g in groups:
            return GROUP_TO_BUCKET[g], g
    return None, None


# ===== Routes =====

@app.route("/")
def index():
    user = session.get("user")
    return render_template_string(INDEX_HTML, user=user)


@app.route("/healthz")
def healthz():
    return jsonify(service="intranet", status="ok",
                   keycloak_reachable=_kc_reachable(),
                   postgres_reachable=_pg_reachable())


@app.route("/login")
def login():
    state = secrets.token_urlsafe(16)
    session["oauth_state"] = state
    # Use the request's actual host (works for both the production
    # hostname intranet.polaris.ts.net and for local port-forwarded
    # testing on http://localhost:13000).
    redirect_uri = f"{request.url_root.rstrip('/')}/callback"
    session["redirect_uri"] = redirect_uri
    discovery = _kc_get_discovery()
    params = {
        "response_type": "code",
        "client_id": KEYCLOAK_CLIENT_ID,
        "redirect_uri": redirect_uri,
        "scope": "openid email",
        "state": state,
        # Force re-auth at Keycloak so SSO from another portal doesn't
        # silently bridge users across pods (see customer app.py).
        "prompt":        "login",
    }
    # PKCE so the redirect_uri stays simple and Keycloak is happy.
    import hashlib, base64
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(64)).rstrip(b"=").decode()
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()
    ).rstrip(b"=").decode()
    session["code_verifier"] = verifier
    params["code_challenge"] = challenge
    params["code_challenge_method"] = "S256"
    return redirect(f"{discovery['authorization_endpoint']}?{urlencode(params)}")


@app.route("/callback")
def callback():
    if request.args.get("error"):
        return f"OIDC error: {request.args['error']} - {request.args.get('error_description','')}", 400
    if request.args.get("state") != session.pop("oauth_state", None):
        return "state mismatch", 400
    code = request.args.get("code")
    if not code:
        return "missing code", 400
    verifier = session.pop("code_verifier", None)
    redirect_uri = session.pop("redirect_uri", url_for("callback", _external=True))

    discovery = _kc_get_discovery()
    # Exchange code for tokens
    import urllib.request
    payload = {
        "grant_type": "authorization_code",
        "code": code,
        "client_id": KEYCLOAK_CLIENT_ID,
        "redirect_uri": redirect_uri,
    }
    if verifier:
        payload["code_verifier"] = verifier
    data = urlencode(payload).encode()
    token_req = urllib.request.Request(
        discovery["token_endpoint"], data=data, method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    with urllib.request.urlopen(token_req, timeout=5) as r:
        tokens = json.loads(r.read())

    # Decode the id_token claims without signature verification (POC only).
    # In production use python-jose with Keycloak's JWKS.
    import base64
    payload_b64 = tokens["id_token"].split(".")[1]
    # Pad base64url
    payload_b64 += "=" * (-len(payload_b64) % 4)
    claims = json.loads(base64.urlsafe_b64decode(payload_b64))

    session["user"] = {
        "preferred_username": claims.get("preferred_username"),
        "email": claims.get("email"),
        "name": claims.get("name"),
        "groups": claims.get("groups", []),
    }
    session["access_token"] = tokens["access_token"]
    session["id_token_raw"] = tokens["id_token"]
    return redirect(url_for("index"))


@app.route("/logout")
def logout():
    session.clear()
    discovery = _kc_get_discovery()
    return redirect(f"{discovery.get('end_session_endpoint', '')}?client_id={KEYCLOAK_CLIENT_ID}")


@app.route("/directory")
def directory():
    user = session.get("user")
    if not user:
        return redirect(url_for("login"))
    rows = _fetch_directory()
    return render_template_string(DIRECTORY_HTML, user=user, rows=rows)


@app.route("/files")
def files():
    """List files from the user's MinIO bucket via STS."""
    user = session.get("user")
    if not user:
        return redirect(url_for("login"))
    groups = user.get("groups", [])
    bucket, matched_group = _pick_bucket(groups)
    if not bucket:
        return render_template_string(
            FILES_HTML, user=user, error=(
                f"No MinIO bucket mapped for groups: {groups}. "
                "Your Keycloak group must be one of: " +
                ", ".join(GROUP_TO_BUCKET.keys())
            ),
            bucket=None, files=[]
        )

    # Step 1: assume role with web identity (JWT) to get STS creds
    access_token = session.get("access_token")
    if not access_token:
        return render_template_string(FILES_HTML, user=user,
                                       error="session expired, please log in again",
                                       bucket=bucket, files=[])

    try:
        sts = boto3.client(
            "sts",
            endpoint_url=MINIO_STS_ENDPOINT,
            aws_access_key_id="",  # not used for STS web identity
            aws_secret_access_key="",
            config=Config(signature_version="s3v4"),
            region_name="us-east-1",
        )
        # AssumeRoleWithWebIdentity requires an ARN; MinIO accepts
        # any string and uses the JWT claims to map to a policy.
        resp = sts.assume_role_with_web_identity(
            RoleArn="arn:aws:iam::polaris:role/polaris-oidc",
            RoleSessionName=f"{user['preferred_username']}-{int(time.time())}",
            WebIdentityToken=access_token,
            DurationSeconds=3600,
        )
        creds = resp["Credentials"]
    except ClientError as e:
        log.error("STS error: %s", e)
        return render_template_string(
            FILES_HTML, user=user,
            error=f"MinIO STS denied: {e.response.get('Error', {}).get('Code', '')} - "
                  f"{e.response.get('Error', {}).get('Message', '')}",
            bucket=bucket, files=[]
        )

    # Step 2: use STS creds to ListObjects in the user's bucket
    s3 = boto3.client(
        "s3",
        endpoint_url=MINIO_ENDPOINT,
        aws_access_key_id=creds["AccessKeyId"],
        aws_secret_access_key=creds["SecretAccessKey"],
        aws_session_token=creds["SessionToken"],
        config=Config(signature_version="s3v4"),
        region_name="us-east-1",
    )
    try:
        listing = s3.list_objects_v2(Bucket=bucket)
        objects = listing.get("Contents", [])
    except ClientError as e:
        log.error("S3 error: %s", e)
        return render_template_string(
            FILES_HTML, user=user,
            error=f"S3 ListObjects denied: {e.response.get('Error', {}).get('Code', '')}",
            bucket=bucket, files=[]
        )

    return render_template_string(FILES_HTML, user=user,
                                   bucket=bucket,
                                   matched_group=matched_group,
                                   files=objects,
                                   error=None)


# ===== Helpers =====

def _kc_reachable():
    try:
        _kc_get_discovery()
        return True
    except Exception:
        return False


def _pg_reachable():
    try:
        conn = psycopg2.connect(host=POSTGRES_HOST, port=POSTGRES_PORT,
                                dbname=POSTGRES_DB, user=POSTGRES_USER,
                                password=POSTGRES_PASSWORD, connect_timeout=3)
        conn.close()
        return True
    except Exception:
        return False


def _fetch_directory():
    """Read pol_intranet.employees."""
    try:
        conn = psycopg2.connect(host=POSTGRES_HOST, port=POSTGRES_PORT,
                                dbname=POSTGRES_DB, user=POSTGRES_USER,
                                password=POSTGRES_PASSWORD,
                                connect_timeout=5)
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT email, display_name, department, location, role "
                    "FROM employees ORDER BY id")
        rows = cur.fetchall()
        conn.close()
        return rows
    except Exception as e:
        log.error("DB error: %s", e)
        return []


# ===== Templates =====

INDEX_HTML = """
<!doctype html><html><head><meta charset="utf-8"><title>Polaris intranet</title>
<style>
body{font-family:-apple-system,Segoe UI,sans-serif;background:#1e1e2e;color:#cdd6f4;
     margin:0;padding:24px;max-width:900px;margin:auto;}
h1{color:#f9e2af;}
h2{color:#89b4fa;border-bottom:1px solid #45475a;padding-bottom:4px;}
a{color:#89b4fa;text-decoration:none;}
a:hover{text-decoration:underline;}
.box{background:#11111b;border:1px solid #313244;border-radius:6px;padding:16px;
     margin:16px 0;}
.kw{color:#f9e2af;}
.ok{color:#a6e3a1;}
.bad{color:#f38ba8;}
small{color:#6c7086;}
</style></head><body>
<h1>Polaris intranet</h1>
{% if user %}
<p>Hi <span class="kw">{{ user.name or user.preferred_username }}</span> &mdash;
   logged in via OIDC. Your groups:
   {% for g in user.groups %}<span class="ok">{{ g }}</span>{% if not loop.last %}, {% endif %}{% endfor %}</p>
<div class="box">
  <h2>Quick links</h2>
  <ul>
    <li><a href="/directory">Employee directory</a> &mdash; from <code>pol_intranet</code></li>
    <li><a href="/files">My files</a> &mdash; from your MinIO bucket via STS</li>
    <li><a href="http://grafana.polaris.ts.net" target="_blank">Grafana</a> &mdash; ops dashboards (Day 4)</li>
    <li><a href="http://customer.polaris.ts.net" target="_blank">Customer portal</a> &mdash; if you have customer tags</li>
  </ul>
</div>
<p><a href="/logout">log out</a></p>
{% else %}
<p>You are not logged in. <a href="/login">log in</a> to see your stuff.</p>
<p><small>This portal authenticates against Keycloak via OIDC. Try
<code>alice / polaris</code> (employee), <code>bob / polaris</code> (ops), or
<code>carol / polaris</code> (admin).</small></p>
{% endif %}
</body></html>
"""

DIRECTORY_HTML = """
<!doctype html><html><head><meta charset="utf-8"><title>Polaris directory</title>
<style>
body{font-family:-apple-system,Segoe UI,sans-serif;background:#1e1e2e;color:#cdd6f4;
     margin:0;padding:24px;max-width:1100px;margin:auto;}
h1{color:#f9e2af;}h2{color:#89b4fa;}
table{border-collapse:collapse;width:100%;margin-top:12px;}
th,td{border:1px solid #313244;padding:8px 12px;text-align:left;}
th{background:#313244;color:#f9e2af;}
tr:nth-child(even){background:#181825;}
.kw{color:#f9e2af;}.bad{color:#f38ba8;}
a{color:#89b4fa;}
</style></head><body>
<h1>Employee directory</h1>
<p>Showing {{ rows|length }} employees from <code>pol_intranet.employees</code>.</p>
<table>
<thead><tr><th>Email</th><th>Name</th><th>Department</th><th>Location</th><th>Role</th></tr></thead>
<tbody>
{% for r in rows %}
<tr><td>{{ r.email }}</td><td>{{ r.display_name }}</td><td>{{ r.department }}</td>
    <td>{{ r.location or "" }}</td><td>{{ r.role }}</td></tr>
{% else %}
<tr><td colspan="5" class="bad">no rows returned (DB may be unreachable)</td></tr>
{% endfor %}
</tbody></table>
<p><a href="/">&larr; back to home</a></p>
</body></html>
"""

FILES_HTML = """
<!doctype html><html><head><meta charset="utf-8"><title>Polaris files</title>
<style>
body{font-family:-apple-system,Segoe UI,sans-serif;background:#1e1e2e;color:#cdd6f4;
     margin:0;padding:24px;max-width:1100px;margin:auto;}
h1{color:#f9e2af;}h2{color:#89b4fa;}
.box{background:#11111b;border:1px solid #313244;border-radius:6px;padding:16px;
     margin:16px 0;}
.kw{color:#f9e2af;}.ok{color:#a6e3a1;}.bad{color:#f38ba8;}
table{border-collapse:collapse;width:100%;margin-top:12px;}
th,td{border:1px solid #313244;padding:8px 12px;text-align:left;}
th{background:#313244;color:#f9e2af;}
tr:nth-child(even){background:#181825;}
a{color:#89b4fa;}
small{color:#6c7086;}
</style></head><body>
<h1>My files (MinIO via STS)</h1>
{% if error %}
<div class="box bad"><strong>denied:</strong> {{ error }}</div>
{% endif %}
{% if bucket %}
<div class="box">
  <p>bucket: <span class="kw">{{ bucket }}</span>
  {% if matched_group %}
  &mdash; mapped from your group <span class="ok">{{ matched_group }}</span>
  {% endif %}
  </p>
  <p><small>The browser-to-MinIO flow is: <span class="kw">your Keycloak JWT</span>
  &rarr; <span class="kw">MinIO STS AssumeRoleWithWebIdentity</span>
  &rarr; <span class="kw">temporary STS creds</span>
  &rarr; <span class="kw">ListObjects on your bucket</span>.</small></p>
</div>
{% if files %}
<table>
<thead><tr><th>Key</th><th>Size (B)</th><th>Last modified</th></tr></thead>
<tbody>
{% for f in files %}
<tr><td>{{ f.Key }}</td><td>{{ f.Size }}</td>
    <td>{{ f.LastModified }}</td></tr>
{% endfor %}
</tbody></table>
{% else %}
<p><small>(bucket is empty or you have read-only access and the bucket has no objects yet)</small></p>
{% endif %}
{% endif %}
<p><a href="/">&larr; back to home</a></p>
</body></html>
"""


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 3000))
    app.run(host="0.0.0.0", port=port, debug=False)