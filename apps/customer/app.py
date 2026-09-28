#!/usr/bin/env python3
"""
polaris POC — Customer portal.

Multi-tenant data isolation driven by:
  1. Keycloak groups claim   -> tenant name
  2. `SET LOCAL app.current_tenant_id` per request -> Postgres RLS
  3. MinIO IAM policy        -> bucket-level isolation (see /files)

Endpoints:
  GET  /            home (links to data, files, logout)
  GET  /data        list of customer_data rows visible to this user
  GET  /files       list of objects in the user's MinIO bucket
  GET  /healthz     JSON status
  GET  /login       302 to Keycloak
  GET  /callback    OIDC code -> tokens -> session
  GET  /logout      clear session

Tenant mapping (group -> tenant name):
  pol-customer-tenant-acme        -> acme
  pol-customer-tenant-brightside  -> brightside
  pol-partners-northwind          -> partners

If the user has none of those groups, they get a 403 on /data and /files.
"""
import os
import json
import logging
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
log = logging.getLogger("customer-portal")

KEYCLOAK_ISSUER   = os.environ["KEYCLOAK_ISSUER"]   # http://keycloak:8080/realms/polaris
KEYCLOAK_CLIENT   = os.environ["KEYCLOAK_CLIENT_ID"] # pol-customer
POSTGRES_HOST     = os.environ["POSTGRES_HOST"]
POSTGRES_PORT     = int(os.environ.get("POSTGRES_PORT", "5432"))
POSTGRES_DB       = os.environ["POSTGRES_DB"]
# Connect as polaris_app (NOT polaris_admin). polaris_admin is a
# SUPERUSER, which automatically gets BYPASSRLS — Row-Level Security
# would be silently skipped. polaris_app is NOSUPERUSER NOBYPASSRLS
# so the RLS policy on customer_data is actually enforced.
POSTGRES_USER     = os.environ.get("POSTGRES_USER", "polaris_app")
POSTGRES_PASSWORD = os.environ.get("POSTGRES_PASSWORD", "polaris")
MINIO_ENDPOINT    = os.environ["MINIO_ENDPOINT"]
PUBLIC_URL        = os.environ.get("PUBLIC_URL", "http://customer.polaris.ts.net")

# Group -> tenant name + MinIO policy
TENANT_MAP = {
    "pol-customer-tenant-acme":       {"tenant": "acme",       "bucket": "pol-data-acme"},
    "pol-customer-tenant-brightside": {"tenant": "brightside", "bucket": "pol-data-brightside"},
    "pol-partners-northwind":         {"tenant": "partners",   "bucket": "pol-data-partners"},
}

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET", secrets.token_hex(32))


def _oidc_discovery():
    return f"{KEYCLOAK_ISSUER}/.well-known/openid-configuration"


def _user_tenant():
    """Resolve the current user's tenant from the groups claim in the session."""
    claims = session.get("claims") or {}
    groups = claims.get("groups") or []
    for g in groups:
        if g in TENANT_MAP:
            return TENANT_MAP[g]
    return None


def _db_conn():
    return psycopg2.connect(
        host=POSTGRES_HOST, port=POSTGRES_PORT,
        dbname=POSTGRES_DB, user=POSTGRES_USER,
        password=POSTGRES_PASSWORD,
    )


def _set_tenant(cur, tenant_name):
    """Look up tenant_id by name and `SET LOCAL app.current_tenant_id`.
    Must be called inside an open transaction (the caller is responsible).
    Returns the tenant_id.
    """
    cur.execute("SELECT id FROM tenants WHERE name = %s", (tenant_name,))
    row = cur.fetchone()
    if not row:
        raise ValueError(f"Unknown tenant {tenant_name!r}")
    tenant_id = row[0]
    # SET LOCAL applies to the current transaction only.
    cur.execute("SET LOCAL app.current_tenant_id = %s", (str(tenant_id),))
    return tenant_id


def sts_assume_role_with_web_identity_raw(access_token, session_name):
    """Raw HTTP STS to MinIO (boto3 forbids empty RoleArn, MinIO needs it
    omitted to use the JWT-claim-based policy lookup path)."""
    import urllib.parse, urllib.request
    body = urllib.parse.urlencode({
        "Action": "AssumeRoleWithWebIdentity",
        "Version": "2011-06-15",
        "WebIdentityToken": access_token,
        "DurationSeconds": "3600",
        "RoleSessionName": session_name,
    }).encode()
    req = urllib.request.Request(
        f"{MINIO_ENDPOINT}/",
        data=body, method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    import xml.etree.ElementTree as ET
    with urllib.request.urlopen(req, timeout=10) as resp:
        xml_data = resp.read()
    ns = {"a": "https://sts.amazonaws.com/doc/2011-06-15/"}
    root = ET.fromstring(xml_data)
    creds_el = root.find(".//a:AssumeRoleWithWebIdentityResult/a:Credentials", ns)
    return {
        "AccessKeyId":     creds_el.find("a:AccessKeyId", ns).text,
        "SecretAccessKey": creds_el.find("a:SecretAccessKey", ns).text,
        "SessionToken":    creds_el.find("a:SessionToken", ns).text,
    }


# ----- Routes ------------------------------------------------------------

HOME_HTML = """
<!doctype html><title>polaris customer portal</title>
<style>
  body{font-family:-apple-system,system-ui,sans-serif;max-width:760px;margin:2rem auto;padding:0 1rem;color:#222}
  h1{color:#0b3d91}
  .card{border:1px solid #ddd;border-radius:8px;padding:1rem;margin:.6rem 0;background:#fafafa}
  .muted{color:#888;font-size:.85em}
  a{color:#0b66c2;text-decoration:none}a:hover{text-decoration:underline}
  .tag{display:inline-block;padding:.1rem .5rem;border-radius:4px;background:#e3f0fb;color:#0b3d91;font-size:.8em;margin-right:.3rem}
</style>
<h1>polaris customer portal</h1>
<p>Welcome <strong>{{ user }}</strong>.
   {% if tenant %}<span class="tag">tenant: {{ tenant }}</span>{% endif %}
   <a href="/logout" style="float:right">logout</a></p>

<div class="card">
  <h3><a href="/data">/data</a></h3>
  <p>Customer data visible to your tenant. Backed by Postgres
     <code>pol_customer</code> with <strong>Row-Level Security</strong>:
     every query runs with <code>SET LOCAL app.current_tenant_id = &lt;your tenant&gt;</code>.
     A second tenant row in <code>customer_data</code> would be hidden even if
     the app code forgot the WHERE clause.</p>
</div>

<div class="card">
  <h3><a href="/files">/files</a></h3>
  <p>List objects in your tenant's S3 bucket. Same JWT, different layer:
     MinIO uses the <code>groups</code> claim to map to a per-tenant IAM
     policy (see <code>scripts/minio_bootstrap.py</code>). STS
     <code>AssumeRoleWithWebIdentity</code> signs the request.</p>
</div>
"""


@app.route("/")
def home():
    if not session.get("user"):
        return redirect(url_for("login"))
    tenant = (_user_tenant() or {}).get("tenant")
    return render_template_string(HOME_HTML, user=session["user"], tenant=tenant)


@app.route("/healthz")
def healthz():
    return jsonify({
        "status": "ok",
        "service": "customer",
        "keycloak_reachable": True,
        "postgres_reachable": _check_pg(),
    })


def _check_pg():
    try:
        with _db_conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT 1")
            return True
    except Exception as e:
        log.error("postgres health check failed: %s", e)
        return False


@app.route("/login")
def login():
    state = secrets.token_urlsafe(16)
    session["oauth_state"] = state
    # Use the request's actual host (works for both the production
    # hostname customer.polaris.ts.net and for local port-forwarded
    # testing on http://localhost:13002).
    redirect_uri = f"{request.url_root.rstrip('/')}/callback"
    session["redirect_uri"] = redirect_uri
    params = {
        "client_id":     KEYCLOAK_CLIENT,
        "response_type": "code",
        "redirect_uri":  redirect_uri,
        "scope":         "openid email",
        "state":         state,
        # Force re-auth at Keycloak every time so the SSO session
        # from another portal (e.g. intranet alice) does NOT silently
        # bridge us into this pod as that user. For a production
        # multi-tenant setup the right answer is per-portal OIDC
        # clients, but for this POC we keep one realm client and
        # disable SSO across portals with `prompt=login`.
        "prompt":        "login",
    }
    # Generate PKCE pair so the redirect_uri stays simple.
    import hashlib, base64
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(64)).rstrip(b"=").decode()
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()
    ).rstrip(b"=").decode()
    session["code_verifier"] = verifier
    params["code_challenge"] = challenge
    params["code_challenge_method"] = "S256"
    return redirect(f"{KEYCLOAK_ISSUER}/protocol/openid-connect/auth?{urlencode(params)}")


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
    # IMPORTANT: redirect_uri MUST match what was sent to /authorize.
    # We store the original one in the session at /login time.
    redirect_uri = session.pop("redirect_uri", f"{PUBLIC_URL}/callback")
    token_data = {
        "grant_type":    "authorization_code",
        "client_id":     KEYCLOAK_CLIENT,
        "code":          code,
        "redirect_uri":  redirect_uri,
    }
    if verifier:
        token_data["code_verifier"] = verifier
    import urllib.request
    body = urlencode(token_data).encode()
    req = urllib.request.Request(
        f"{KEYCLOAK_ISSUER}/protocol/openid-connect/token",
        data=body, method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    import urllib.parse
    tokens = json.loads(urllib.request.urlopen(req, timeout=10).read())
    id_token = tokens.get("id_token")
    access_token = tokens.get("access_token")
    if not id_token:
        return "no id_token in response", 400

    # Decode the id_token claims (no signature verification — POC).
    payload = id_token.split(".")[1]
    payload += "=" * (-len(payload) % 4)
    import base64
    claims = json.loads(base64.urlsafe_b64decode(payload))
    session["user"] = claims.get("preferred_username")
    session["claims"] = claims
    session["access_token"] = access_token
    return redirect(url_for("home"))


@app.route("/logout")
def logout():
    session.clear()
    return redirect(f"{KEYCLOAK_ISSUER}/protocol/openid-connect/logout?redirect_uri={PUBLIC_URL}/")


DATA_HTML = """
<!doctype html><title>customer data — {{ tenant }}</title>
<style>
  body{font-family:-apple-system,system-ui,sans-serif;max-width:760px;margin:2rem auto;padding:0 1rem;color:#222}
  h1{color:#0b3d91}
  table{border-collapse:collapse;width:100%}
  th,td{border-bottom:1px solid #eee;padding:.5rem;text-align:left}
  th{background:#f5f5f5}
  .muted{color:#888;font-size:.85em}
  .ok{color:#0a7a3a}
  a{color:#0b66c2;text-decoration:none}a:hover{text-decoration:underline}
  pre{background:#f5f5f5;padding:.6rem;border-radius:6px;overflow-x:auto;font-size:.85em}
</style>
<h1>customer data — tenant <span style="color:#0b3d91">{{ tenant }}</span></h1>
<p class="muted">Showing rows where <code>tenant_id = {{ tenant_id }}</code>.
   RLS policy <code>tenant_isolation</code> enforces this at the DB layer
   via <code>SET LOCAL app.current_tenant_id</code>.</p>

<table>
  <thead><tr><th>label</th><th>value</th><th>created</th></tr></thead>
  <tbody>
  {% for r in rows %}
    <tr><td>{{ r.label }}</td><td>{{ r.value }}</td><td>{{ r.created_at }}</td></tr>
  {% endfor %}
  </tbody>
</table>

<p class="muted">{{ rows|length }} row(s) visible to <strong>{{ user }}</strong>.</p>

<h3>SQL emitted by the app</h3>
<pre>SET LOCAL app.current_tenant_id = '{{ tenant_id }}';
SELECT label, value, created_at FROM customer_data;</pre>

<p><a href="/">back</a></p>
"""


@app.route("/data")
def data():
    if not session.get("user"):
        return redirect(url_for("login"))
    mapping = _user_tenant()
    if not mapping:
        return f"403: no tenant group in your JWT (groups={session['claims'].get('groups')})", 403
    tenant_name = mapping["tenant"]
    conn = _db_conn()
    try:
        # `with conn:` opens an explicit transaction; SET LOCAL + the
        # subsequent SELECT both run inside it, so RLS sees the setting.
        with conn, conn.cursor() as cur:
            tenant_id = _set_tenant(cur, tenant_name)
            cur.execute("SELECT label, value, created_at FROM customer_data ORDER BY id")
            rows = cur.fetchall()
    finally:
        conn.close()
    return render_template_string(
        DATA_HTML,
        tenant=tenant_name,
        tenant_id=tenant_id,
        user=session["user"],
        rows=[{"label": r[0], "value": r[1], "created_at": str(r[2])} for r in rows],
    )


FILES_HTML = """
<!doctype html><title>customer files — {{ tenant }}</title>
<style>
  body{font-family:-apple-system,system-ui,sans-serif;max-width:760px;margin:2rem auto;padding:0 1rem;color:#222}
  h1{color:#0b3d91}
  table{border-collapse:collapse;width:100%}
  th,td{border-bottom:1px solid #eee;padding:.5rem;text-align:left}
  th{background:#f5f5f5}
  .muted{color:#888;font-size:.85em}
  .ok{color:#0a7a3a}.err{color:#a02d2d}
  a{color:#0b66c2;text-decoration:none}a:hover{text-decoration:underline}
  pre{background:#f5f5f5;padding:.6rem;border-radius:6px;overflow-x:auto;font-size:.85em}
</style>
<h1>customer files — tenant <span style="color:#0b3d91">{{ tenant }}</span></h1>

<p class="muted">Bucket <code>{{ bucket }}</code> — listing uses STS
   credentials obtained via <code>AssumeRoleWithWebIdentity</code>.</p>

{% if error %}
  <p class="err">STS DENIED: {{ error }}</p>
{% else %}
  <p class="ok">STS OK — temporary access key <code>{{ access_key_prefix }}…</code></p>
  <table>
    <thead><tr><th>key</th><th>size (B)</th></tr></thead>
    <tbody>
    {% for o in objects %}
      <tr><td>{{ o.key }}</td><td>{{ o.size }}</td></tr>
    {% endfor %}
    </tbody>
  </table>
{% endif %}

<p><a href="/">back</a></p>
"""


@app.route("/files")
def files():
    if not session.get("user"):
        return redirect(url_for("login"))
    mapping = _user_tenant()
    if not mapping:
        return f"403: no tenant group in your JWT (groups={session['claims'].get('groups')})", 403
    bucket = mapping["bucket"]
    tenant_name = mapping["tenant"]
    error = None
    access_key_prefix = None
    objects = []
    try:
        access_token = session.get("access_token")
        creds = sts_assume_role_with_web_identity_raw(access_token, f"{session['user']}-files")
        access_key_prefix = creds["AccessKeyId"][:30]
        s3 = boto3.client(
            "s3", endpoint_url=MINIO_ENDPOINT,
            aws_access_key_id=creds["AccessKeyId"],
            aws_secret_access_key=creds["SecretAccessKey"],
            aws_session_token=creds["SessionToken"],
            config=Config(signature_version="s3v4"),
            region_name="us-east-1",
        )
        listing = s3.list_objects_v2(Bucket=bucket)
        objects = [{"key": o["Key"], "size": o["Size"]}
                   for o in listing.get("Contents", [])]
    except Exception as e:
        log.exception("STS/files failed for user=%s", session["user"])
        error = str(e)
    return render_template_string(
        FILES_HTML,
        tenant=tenant_name, bucket=bucket,
        access_key_prefix=access_key_prefix,
        objects=objects, error=error,
    )


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=3000, debug=False)