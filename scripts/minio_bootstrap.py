#!/usr/bin/env python3
"""
polaris POC — MinIO bootstrap script.

Creates 5 buckets (one per portal tenant) and uploads a seed README to
each. Uses boto3 because the MinIO community archives are no longer
served (dl.min.io returns 410 Gone as of 2026) so the `mc` client
binary can't be downloaded fresh.

Idempotent: re-running is safe.
"""
import os
import sys
import time
import json
import boto3
import botocore
import datetime as _dt
import hashlib as _hashlib
import hmac as _hmac
import urllib.parse as _urlparse
import urllib.request as _urlreq
from botocore.client import Config
from botocore.exceptions import ClientError

endpoint = os.environ["MINIO_ENDPOINT"]
user = os.environ["MINIO_ROOT_USER"]
password = os.environ["MINIO_ROOT_PASSWORD"]

buckets = [
    ("pol-data-employees",   "Employees workspace. Read-write for group pol-employees."),
    ("pol-data-ops",         "Ops dumps, logs, runbooks. Admin for group pol-ops."),
    ("pol-data-acme",        "Customer tenant Acme Industries. Read-write for group pol-customer-tenant-acme."),
    ("pol-data-brightside",  "Customer tenant Brightside Health. Read-write for group pol-customer-tenant-brightside."),
    ("pol-data-partners",    "Partner Northwind Consulting. Read-only for group pol-partners-northwind."),
]

# IAM policies — policy names match Keycloak group names so MinIO's
# CLAIM_NAME=groups mapping picks them up automatically (MinIO looks up
# the policy by the exact claim value, no custom mapping needed).
policies = {
    "pol-employees": {
        "Version": "2012-10-17",
        "Statement": [{
            "Effect": "Allow",
            "Action": [
                "s3:GetBucketLocation", "s3:ListBucket",
                "s3:GetObject", "s3:PutObject", "s3:DeleteObject",
                "s3:GetObjectVersion"
            ],
            "Resource": [
                "arn:aws:s3:::pol-data-employees",
                "arn:aws:s3:::pol-data-employees/*"
            ]
        }]
    },
    "pol-ops": {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Action": ["s3:*"],
                "Resource": [
                    "arn:aws:s3:::pol-data-ops",
                    "arn:aws:s3:::pol-data-ops/*"
                ]
            },
            {
                "Effect": "Allow",
                "Action": ["s3:ListBucket"],
                "Resource": ["arn:aws:s3:::*"]
            }
        ]
    },
    "pol-admin": {
        "Version": "2012-10-17",
        "Statement": [{
            "Effect": "Allow",
            "Action": ["s3:*"],
            "Resource": ["arn:aws:s3:::*"]
        }]
    },
    "pol-customer-tenant-acme": {
        "Version": "2012-10-17",
        "Statement": [{
            "Effect": "Allow",
            "Action": ["s3:GetBucketLocation", "s3:ListBucket",
                       "s3:GetObject", "s3:PutObject", "s3:DeleteObject"],
            "Resource": ["arn:aws:s3:::pol-data-acme",
                         "arn:aws:s3:::pol-data-acme/*"]
        }]
    },
    "pol-customer-tenant-brightside": {
        "Version": "2012-10-17",
        "Statement": [{
            "Effect": "Allow",
            "Action": ["s3:GetBucketLocation", "s3:ListBucket",
                       "s3:GetObject", "s3:PutObject", "s3:DeleteObject"],
            "Resource": ["arn:aws:s3:::pol-data-brightside",
                         "arn:aws:s3:::pol-data-brightside/*"]
        }]
    },
    "pol-partners-northwind": {
        "Version": "2012-10-17",
        "Statement": [{
            "Effect": "Allow",
            "Action": ["s3:GetBucketLocation", "s3:ListBucket", "s3:GetObject"],
            "Resource": ["arn:aws:s3:::pol-data-partners",
                         "arn:aws:s3:::pol-data-partners/*"]
        }]
    },
}

# wait for MinIO to be reachable (the healthcheck is on the host, this is
# for the in-container check)
s3 = None
for attempt in range(30):
    try:
        s3 = boto3.client(
            "s3",
            endpoint_url=endpoint,
            aws_access_key_id=user,
            aws_secret_access_key=password,
            config=Config(signature_version="s3v4"),
        )
        s3.list_buckets()
        break
    except ClientError as e:
        print(f"[{attempt + 1}/30] waiting for MinIO: {e}", file=sys.stderr)
        time.sleep(2)

if s3 is None:
    print("ERROR: MinIO never became reachable", file=sys.stderr)
    sys.exit(1)

print("MinIO reachable. Proceeding with bucket + policy setup.")

# Apply IAM policies via boto3 (MinIO uses the same PutUserPolicy API as S3)
# We attach each policy to the root user (uid=0). When a user logs in via
# OIDC, MinIO maps their `groups` claim to a MinIO policy by exact match
# (configured server-side via MINIO_IDENTITY_OPENID_CLAIM_NAME=groups +
# MINIO_IDENTITY_OPENID_CLAIM_PREFIX="").
print("Applying IAM policies...")
# NOTE: MinIO does NOT support the AWS-standard PutUserPolicy IAM API.
# Policy definition goes through the MinIO admin API instead:
#   PUT /minio/admin/v3/add-canned-policy?policyName=<name>
# with the JSON policy in the request body, signed with AWS sigv4 against
# the "admin" service. boto3 has no built-in client for this so we sign
# the request manually.

def _sign(key: bytes, msg: str) -> bytes:
    return _hmac.new(key, msg.encode(), _hashlib.sha256).digest()


def _admin_put_policy(name: str, doc: dict) -> bool:
    """PUT a policy via MinIO's admin API. Returns True on success."""
    body = json.dumps(doc).encode("utf-8")
    parsed = _urlparse.urlparse(endpoint)
    host = parsed.netloc
    scheme = parsed.scheme
    region = "us-east-1"
    # MinIO's admin API expects sigv4 with service="s3" (the default for
    # madmin-go as well — see signer.SignV4 with location="").
    service = "s3"
    now = _dt.datetime.utcnow()
    amzdate = now.strftime("%Y%m%dT%H%M%SZ")
    datestamp = now.strftime("%Y%m%d")

    # Sigv4: canonical URI is the path WITHOUT query string; query string
    # is computed and signed separately.
    canonical_uri = "/minio/admin/v3/add-canned-policy"
    canonical_query = f"name={_urlparse.quote(name, safe='')}"
    payload_hash = _hashlib.sha256(body).hexdigest()

    canonical_headers = (
        f"content-length:{len(body)}\n"
        f"host:{host}\n"
        f"x-amz-content-sha256:{payload_hash}\n"
        f"x-amz-date:{amzdate}\n"
    )
    signed_headers = "content-length;host;x-amz-content-sha256;x-amz-date"

    canonical_request = (
        f"PUT\n{canonical_uri}\n{canonical_query}\n{canonical_headers}\n{signed_headers}\n{payload_hash}"
    )
    algorithm = "AWS4-HMAC-SHA256"
    credential_scope = f"{datestamp}/{region}/{service}/aws4_request"
    hashed_canonical_request = _hashlib.sha256(canonical_request.encode()).hexdigest()
    string_to_sign = (
        f"{algorithm}\n{amzdate}\n{credential_scope}\n{hashed_canonical_request}"
    )
    k_date = _sign(("AWS4" + password).encode(), datestamp)
    k_region = _sign(k_date, region)
    k_service = _sign(k_region, service)
    k_signing = _sign(k_service, "aws4_request")
    signature = _hmac.new(k_signing, string_to_sign.encode(), _hashlib.sha256).hexdigest()

    auth = (
        f"{algorithm} Credential={user}/{credential_scope}, "
        f"SignedHeaders={signed_headers}, Signature={signature}"
    )
    headers = {
        "Host": host,
        "Content-Length": str(len(body)),
        "X-Amz-Content-Sha256": payload_hash,
        "X-Amz-Date": amzdate,
        "Authorization": auth,
    }
    url = f"{scheme}://{host}{canonical_uri}?{canonical_query}"
    req = _urlreq.Request(url, data=body, method="PUT", headers=headers)
    try:
        with _urlreq.urlopen(req, timeout=10) as resp:
            return 200 <= resp.status < 300
    except _urlreq.HTTPError as e:
        sys.stderr.write(f"  admin API HTTP {e.code}: {e.read().decode('utf-8', errors='replace')}\n")
        return False


for name, doc in policies.items():
    sys.stderr.write(f"[iam] PUT add-canned-policy({name})...\n")
    sys.stderr.flush()
    if _admin_put_policy(name, doc):
        print(f"  policy applied: {name}")
    else:
        print(f"  policy apply error ({name})", file=sys.stderr)

# Create buckets
existing = {b["Name"] for b in s3.list_buckets()["Buckets"]}
for name, desc in buckets:
    if name in existing:
        print(f"  bucket exists: {name}")
        continue
    s3.create_bucket(Bucket=name)
    print(f"  bucket created: {name}")

# Upload seed README to each bucket
for name, desc in buckets:
    key = "README.md"
    body = (
        f"# {name}\n\n"
        f"{desc}\n\n"
        f"This is a seed object in the bucket. The OIDC identity provider\n"
        f"(Keycloak) drives the per-user access policy.\n\n"
        f"To see the policy mapping, check `polaris/minio/policies.json`\n"
        f"in the repo.\n"
    ).encode("utf-8")
    s3.put_object(Bucket=name, Key=key, Body=body)
    print(f"  uploaded: {name}/{key}")

# Apply IAM policies via PutBucketPolicy (the per-bucket policy is one
# of the two layers; the other is per-user policies mapped from OIDC groups).
# For the POC we use ONLY per-user policies mapped via OIDC claims. Bucket
# policies stay empty (or default-deny via the absent policy attribute).
# Note: s3v4 with no bucket policy means "default deny" since we removed
# the anonymous download policy. Good.

print("\nDone.")
print("Buckets:", ", ".join(name for name, _ in buckets))
print("Next: from a portal pod, log in via Keycloak OIDC, get STS creds,")
print("then `aws s3 ls s3://pol-data-acme/` to verify the policy mapping.")