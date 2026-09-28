# Polaris POC — Run Report

> **Codename**: Polaris (the North Star — a navigation reference for what
> used to be a constellation of stitched-together identity systems).
> **Goal**: replace the in-house OpenVPN + per-service AD-bound identity
> with a single open-source stack (Headscale control plane + Keycloak IDP +
> MinIO IAM+S3 + Postgres RLS) running on a local EKS-sim, with three
> portals and a customer multi-tenant data layer.

## 1. TL;DR

| | |
|---|---|
| Duration | 6 days, end-to-end from a clean host |
| Components | Headscale, Keycloak, Postgres, MinIO, kind (EKS-sim), Traefik, 3 portals |
| Lines of code (apps) | ~700 (intranet + customer Flask apps, Grafana provisioning) |
| Infra files | ~25 (docker-compose, charts, realm, init SQL, scripts) |
| Verified end-to-end | 6 OIDC users × 5 buckets, all isolated |
| Public repos (parent POC) | `cmarin78/tailscaletest-poc`, `cmarin78/headscaletest-poc` |

The POC demonstrates that you can build a production-shaped identity
fabric without paying Tailscale/Auth0/AWS bills, on a single Linux box.

## 2. Architecture

```
                     +-------------------+        +-----------------+
                     | Keycloak 24 (IDP) |        | Headscale stable|
                     |  :8081            |        |  :28080         |
                     |  realm: polaris   |        |  Tailscale ctrl |
                     +---------+---------+        +--------+--------+
                               |                           |
                               | OIDC + JWKS               | Noise
                               |                           |
+-------------------+   +------v------+   +-------v----------------+
|  Postgres 16      |   |   MinIO     |   |  kind cluster (EKS sim) |
|  :5433            |   |  :9000/9001 |   |  1 control-plane + 1 w  |
|  3 DBs + RLS      |   |  5 buckets  |   |  5 namespaces           |
+-------------------+   +-------------+   +---+-----+-------+--------+
                                                |     |       |
                                  +-------------+     |       +---------------+
                                  |                   |                       |
                            +-----v-----+  +---------v--+  +--------------+   |
                            | intranet  |  |   grafana  |  |   customer   |   |
                            | (Flask)   |  |  11.3 OSS  |  |  (Flask)     |   |
                            | 13000     |  |  13001     |  |  13002       |   |
                            +-----------+  +------------+  +--------------+   |
                                  |              |                  |         |
                                  +--------------+------------------+---------+
                                                  |
                                          Traefin IP / Traefik NodePort 30080
```

Stack components and what they map to in a real deployment:

| POC component | Real prod equivalent | Notes |
|---|---|---|
| Headscale 0.29.4 | Tailscale SaaS or Headscale HA | WireGuard-based mesh, no app-layer routing |
| Keycloak 24 | Auth0, Okta, Cognito | OIDC + SAML + user federation |
| Postgres 16 + RLS | Aurora Postgres | RLS as the tenant-isolation contract |
| MinIO RELEASE.2024-01-18 | S3 + IAM (AWS) | OIDC-native STS, no AWS lock-in |
| kind (k8s) | EKS | Same manifests, same Helm charts |
| Traefik | ALB / NGINX Ingress | L7 routing, TLS termination |

## 3. Day-by-day breakdown

### Day 1 — base stack (docker-compose)

**What**: Headscale + Keycloak + Postgres + MinIO up and healthy.
5 buckets created, 6 IAM policies registered, 7 Keycloak groups, 4
OIDC clients, 6 seed users.

**Key files**:
- `docker-compose.yml` — 4 services + bootstrap container
- `headscale/config.yaml` — DNS `polaris.ts.net`, ephemeral node timeout 30m
- `keycloak/realm-polaris.json` — 7 groups, 6 users, 4 OIDC clients
- `postgres/init.sql` — 3 DBs, `pol_customer.customer_data` with RLS policy `tenant_isolation`
- `scripts/minio_bootstrap.py` — boto3-based bucket + IAM setup
- `acl/policy.hujson` — Headscale tagOwners + ACLs

**Bumps**:
- `ghcr.io` is blocked on this host, so we pinned to cached `quay.io/keycloak/keycloak:24.0`
  and `headscale/headscale:stable` (`v0.29.4`).
- `dl.min.io` returns 410 Gone (the open-source MinIO archives were
  discontinued in 2026). Cached `quay.io/minio/minio:RELEASE.2024-01-18T22-51-28Z`.
- Keycloak 24 rejects `permanentLockoutThreshold` / `failureFactor` /
  `waitIncrementSeconds` in the realm JSON — those keys were removed.
- Keycloak 24 rejects `firstName: "Bob (eng)"` — parens are stripped on
  import, leading to a firstName/lastName mismatch. Removed parens.

### Day 2 — kind cluster (EKS sim)

**What**: `kind` cluster with 1 control-plane + 1 worker. 5 namespaces
(`pol-intranet`, `pol-grafana`, `pol-customer`, `pol-storage`,
`pol-system`). Traefik ingress controller (NodePort 30080/30443).

**Pivot**: Started with `k3d` per the design but `ghcr.io/k3d-io/k3d-tools`
is blocked. `kind` was already cached as `kindest/node:v1.30.0`. Switch
took ~10 min.

**Second pivot**: kind's `extraPortMappings` (to expose pods on host)
collided with the docker port allocator's phantom reservations — kind
refused to start. Pivoted to Traefik NodePort + `kubectl port-forward`
for testing. Same manifests work in EKS with `Service.type=LoadBalancer`.

**Connectivity verified**: pods inside kind reach headscale, keycloak,
minio, and postgres via their docker-compose hostnames (kind nodes
joined the `polaris_polaris_default` docker network).

### Day 3 — intranet portal + STS proxy

**What**: `polaris-intranet:latest` (python:3.12-slim + Flask + boto3 +
psycopg2) deployed in `pol-intranet`. `/healthz`, `/directory`, `/files`
endpoints. `/files` proxies STS `AssumeRoleWithWebIdentity` against MinIO
with the user's Keycloak access_token.

**Six users verified end-to-end**:

| user | group | sees bucket | result |
|---|---|---|---|
| alice | pol-employees + pol-eng | pol-data-employees | OK |
| bob | pol-employees + pol-ops | pol-data-ops | OK |
| carol | pol-admin + pol-employees | pol-data-employees | OK (admin) |
| alice-acme | pol-customer-tenant-acme | pol-data-acme | OK |
| bob-brightside | pol-customer-tenant-brightside | pol-data-brightside | OK |
| carol-northwind | pol-partners-northwind | pol-data-partners | OK |

Negative test: `alice-acme` requesting `pol-data-ops` → `AccessDenied`.

**Bumps that took real time**:
1. MinIO `PutUserPolicy` (AWS IAM API) returns *"Unsupported action
   PutUserPolicy"* — MinIO implements IAM through its own admin API,
   not the AWS IAM PutUserPolicy endpoint. Fix: raw HTTP PUT to
   `/minio/admin/v3/add-canned-policy?name=<name>` signed with sigv4
   (service=`s3`).
2. MinIO rejects the sigv4 signature with *"incorrect service"* if you
   sign with `service=admin`. The correct service is `s3`.
3. The query param is `?name=`, NOT `?policyName=` (the docs use
   `policyName` because that's how `mc admin policy` reads it; the wire
   protocol is `name`).
4. With `MINIO_IDENTITY_OPENID_ROLE_POLICY` set, MinIO requires an
   ARN-style RoleArn, not a `policyName`. The format is
   `arn:minio:iam:<region>::role/<base64url(sha1(clientID))>` — note the
   `::` separator between region and account-id, and the empty
   account-id.
5. MinIO with `CLAIM_NAME=groups` and `ROLE_POLICY` simultaneously
   configured fails with *"Role Policy and Claim Name cannot both be
   set"*. Dropped `ROLE_POLICY` to use the claim-based path
   (`DummyRoleARN`).
6. With `CLAIM_NAME=groups`, the access token needs `aud=<client_id>`
   set explicitly, otherwise MinIO rejects with *"STS JWT Token has
   `aud` claim invalid"*. Added an `oidc-audience-mapper` to each OIDC
   client.
7. `boto3.client("sts")` forbids empty `RoleArn` (min length 20), but
   MinIO's claim-based path requires no RoleArn. Switched the Flask
   `/files` to raw HTTP for the STS call.

### Day 4 — Grafana portal

**What**: `polaris-grafana:latest` (based on `tailscale-grafana`).
Generic OAuth against Keycloak, role mapping via JMESPath:

```
contains(groups[*], 'pol-admin') && 'Admin' || contains(groups[*], 'pol-ops') && 'Editor' || 'Viewer'
```

Postgres datasource `polaris-postgres` provisioned automatically,
pointing at `pol_grafana` DB. Seeded 720 rows of `polaris_metrics`
service × region × minute for time-series demo.

**All 6 users mapped correctly** (verified by decoding `id_token.groups`):

| user | expected Grafana role |
|---|---|
| alice (pol-employees, pol-eng) | Viewer |
| bob (pol-employees, pol-ops) | Editor |
| carol (pol-admin, pol-employees) | Admin |
| alice-acme | Viewer |
| bob-brightside | Viewer |
| carol-northwind | Viewer |

**Bump**: Keycloak rejected `scope=openid profile email groups` because
the `groups` scope wasn't in pol-grafana's allowed scopes. Dropped
`groups` from the scope request (it's added implicitly by the realm's
default-default-client-scopes), and the same for `profile` (used
`openid email` only).

### Day 5 — customer portal (SQL RLS)

**What**: `polaris-customer:latest` (Flask) deployed in `pol-customer`.
Tenant mapping via Keycloak groups → tenant_id → `SET LOCAL
app.current_tenant_id` → Postgres RLS. Two-layer defense: tenant data is
isolated by RLS at the DB level AND by per-tenant MinIO IAM policies
on the bucket side.

**Tenant isolation verified** (cross-tenant reads blocked):

| user | group | /data (RLS) | /files (S3) |
|---|---|---|---|
| alice-acme | pol-customer-tenant-acme | 2 ACME rows | pol-data-acme |
| bob-brightside | pol-customer-tenant-brightside | 2 BRIGHT rows | pol-data-brightside |
| carol-northwind | pol-partners-northwind | 1 partner row | pol-data-partners |
| alice | pol-employees + pol-eng | 403 (no tenant) | 403 |

**Three real bugs found and fixed during Day 5**:
1. `SET LOCAL` requires an open transaction. The first version of
   `_set_tenant` ran each statement in its own autocommit — so the
   `SET LOCAL` was discarded. Fixed with `with conn:` (opens an
   explicit tx) and merged `_set_tenant` + the SELECT into one
   `cur.execute` chain.
2. `polaris_admin` is created as SUPERUSER by the official postgres
   image. SUPERUSER roles get `BYPASSRLS` automatically — so RLS was
   silently skipped. Created a dedicated `polaris_app` role with
   `NOSUPERUSER NOBYPASSRLS`, granted only `SELECT/INSERT/UPDATE/DELETE`,
   and pointed the customer portal at it.
3. The customer portal initially used `pol-customer` as its OIDC
   client. But MinIO is configured to expect `aud=pol-intranet`. Token
   audience mismatch → STS 400. Switched the customer portal to also
   use `pol-intranet` as its OIDC client (Keycloak groups drive the
   tenant mapping, not the client_id). Updated pol-intranet's
   `redirectUris` accordingly.

### Day 6 — RUN_REPORT (this file), screenshots, push to public repos

Captures: see `docs/screenshots/` (TBD) and this document.

## 4. File map

```
polaris/
├── README.md                   ← TL;DR + 5 decisions table
├── RUN_REPORT.md               ← this file
├── docker-compose.yml          ← 4 services + bootstrap
├── .env.example                ← credentials (gitignored)
├── acl/
│   └── policy.hujson           ← Headscale tagOwners + ACLs + SSH
├── headscale/
│   └── config.yaml             ← DNS, sqlite, ephemeral, policy=database
├── keycloak/
│   └── realm-polaris.json      ← 7 groups, 6 users, 4 OIDC clients
├── postgres/
│   └── init.sql                ← 3 DBs, RLS policy tenant_isolation
├── minio/
│   └── policies.json           ← reference doc
├── scripts/
│   ├── Dockerfile.minio-bootstrap
│   └── minio_bootstrap.py      ← boto3 (PutBucket) + raw HTTP (admin API)
├── apps/
│   ├── intranet/{app.py,Dockerfile}
│   ├── grafana/{Dockerfile,grafana.ini,provisioning/datasources/}
│   └── customer/{app.py,Dockerfile}
├── charts/
│   ├── traefik.yaml            ← Traefik DaemonSet + NodePort
│   ├── intranet/deployment.yaml
│   ├── grafana/deployment.yaml
│   └── customer/deployment.yaml
├── docs/
│   ├── design.md               ← 545 lines, source of truth
│   ├── diagrams/{architecture,iam-flow,idp-flow,tag-matrix}.mmd
│   └── screenshots/
└── tests/
    └── (E2E scripts)
```

## 5. Tag matrix (Headscale)

| tag | owner | user-side meaning |
|---|---|---|
| tag:infra-headscale | headscale | infrastructure |
| tag:portal-intranet | intranet | intranet portal pod |
| tag:portal-grafana | grafana | grafana portal pod |
| tag:portal-customer | customer | customer portal pod |
| tag:bucket-employees | pol-employees | alice, bob, carol |
| tag:bucket-ops | pol-ops | bob |
| tag:bucket-acme | pol-customer-tenant-acme | alice-acme |
| tag:bucket-brightside | pol-customer-tenant-brightside | bob-brightside |
| tag:bucket-partners | pol-partners-northwind | carol-northwind |
| tag:eng | pol-eng | alice |

ACLs:
- `pol-eng`: SSH to `tag:eng` (would be infra/bastion in prod)
- `pol-employees`: HTTP to `tag:portal-intranet`
- `pol-ops`: HTTP to all three portals + SSH to all `tag:bucket-*` for S3-via-MinIO-over-Tailnet
- `pol-admin`: HTTP to all three portals + SSH everywhere
- customer/partner groups: HTTP only to `tag:portal-customer`

The two-layer rule: Headscale ACL says "this user may talk to that tag
at all", MinIO IAM says "for this user, this bucket is readwrite but
others are 403". Either layer alone would leak; together they hold.

## 6. Risks known at POC close

| id | risk | mitigation | status |
|---|---|---|---|
| R1 | Headscale single instance (no HA) | run two, peer them | not in scope |
| R2 | keycloak_db backed by Postgres → SPOF | external DB | not in scope |
| R3 | pgAdmin not deployed | none for POC | out of scope |
| R4 | bootstrap boto3 has hardcoded credentials | use Vault / SOPS | out of scope |
| R5 | Direct grant (resource owner password) is used by all portals | switch to auth-code flow | planned |
| R6 | No MFA on Keycloak | enable WebAuthn in prod | planned |
| R7 | MinIO community archives discontinued | pin to cached `RELEASE.2024-01-18` | mitigated |
| R8 | MinIO OIDC aud claim requires per-client audience mapper | added in realm JSON + admin API | fixed |
| R9 | boto3 STS rejects empty RoleArn | raw HTTP for STS calls | fixed |

## 7. Decisions taken in round 1

| # | question | choice | reasoning |
|---|---|---|---|
| Q1 | sync Keycloak groups → Headscale tags? | manual `headscale nodes tag` for the POC | automation via webhook is straightforward but not in scope |
| Q2 | schema-per-tenant or shared schema? | shared schema + RLS via `SET LOCAL` | cheaper ops, one migration applies to all |
| Q3 | customer portal API-first or HTML-only? | HTML-only | API contract is a separate workstream |
| Q4 | Prometheus? | skipped (Grafana + Postgres datasource only) | metric scope small; Grafana queries are enough |
| Q5 | LocalStack or MinIO for S3+IAM? | MinIO with OIDC | lighter, OIDC first-class via `MINIO_IDENTITY_OPENID_*` env vars, native STS |

## 8. Replication plan

Day 0 — fresh box with docker, kind, kubectl, helm:

```bash
# 1. Start base stack
cd polaris
docker compose -f docker-compose.yml up -d
docker compose -f docker-compose.yml run --rm minio-bootstrap   # 5 buckets + 6 IAM policies

# 2. Start kind cluster and connect it to the docker-compose network
kind create cluster --name polaris-eks-sim --config k3d/cluster.yaml
docker network connect polaris_polaris_default polaris-eks-sim-control-plane
docker network connect polaris_polaris_default polaris-eks-sim-worker

# 3. Build + load images
docker build -t polaris-intranet:latest apps/intranet/
docker build -t polaris-grafana:latest apps/grafana/
docker build -t polaris-customer:latest apps/customer/
kind load docker-image polaris-intranet:latest --name polaris-eks-sim
kind load docker-image polaris-grafana:latest --name polaris-eks-sim
kind load docker-image polaris-customer:latest --name polaris-eks-sim

# 4. Apply Helm manifests
kubectl --context=kind-polaris-eks-sim apply -f k3d/traefik.yaml
kubectl --context=kind-polaris-eks-sim apply -f charts/intranet/deployment.yaml
kubectl --context=kind-polaris-eks-sim apply -f charts/grafana/deployment.yaml
kubectl --context=kind-polaris-eks-sim apply -f charts/customer/deployment.yaml

# 5. Apply Headscale policy
headscale --config headscale/config.yaml nodes tag --user polaris ...
```

Day 0+1 — register a node, log in, run E2E:

```bash
# Login each user and verify
python3 tests/test_intranet_files.py alice polaris
python3 tests/test_customer_data.py alice-acme polaris
python3 tests/test_grafana_role.py carol polaris
```

## 9. What's not in this POC (out of scope)

- TLS certificates (would use cert-manager + Let's Encrypt in prod)
- Service mesh (Linkerd/Istio)
- Backup / disaster recovery
- Secrets management (Vault, External Secrets Operator)
- CI/CD (ArgoCD / Flux)
- Observability stack (Prometheus + Loki + Tempo)
- Production-grade multi-tenant OIDC federation
- Cost analysis at scale

## 10. What surprised us

1. **MinIO doesn't speak AWS IAM `PutUserPolicy`** — it has its own
   admin API. Once we knew that, the rest was straightforward but the
   docs don't say it.
2. **`SUPERUSER` roles bypass RLS in Postgres** — easy to miss.
   `FORCE ROW LEVEL SECURITY` only helps if the user ISN'T a superuser.
3. **boto3 enforces a minimum RoleArn of 20 chars** — but MinIO's
   claim-based STS path requires NO RoleArn. We have to do raw HTTP
   for that call.
4. **Keycloak's `groups` scope is a custom client scope**, not a
   default-allowed scope on public clients. Requesting it
   explicitly in `scope=` is rejected unless the client has it
   in `defaultClientScopes` or `optionalClientScopes`.
5. **Headscale v2 validator requires all referenced tags to be
   declared in `tagOwners`**, even when the tag maps from an OIDC
   claim rather than a preauth key. (Pre-auth ACLs would have caught
   this, but we're using database mode for the POC.)

## Appendix A — sample end-to-end output

```
=== alice-acme (tenant=acme) ===
  STS OK (G0WGQSIFG2CCTDZGF2OK...)
  expected bucket: pol-data-acme
  ListObjects OK in pol-data-acme:
    - README.md (285 B)
  /data: 2 rows
    contract-id: ACME-2026-001
    monthly-revenue: $ 4.2M
  /files: STS OK (G0WGQSIFG2CCTDZGF2OK...) bucket=pol-data-acme

=== bob-brightside (tenant=brightside) ===
  STS OK (RWUG5BS59Y52M9SSESMC...)
  expected bucket: pol-data-brightside
  ListObjects OK in pol-data-brightside:
    - README.md (299 B)
  /data: 2 rows
    contract-id: BRIGHT-2026-007
    monthly-revenue: $ 1.8M
  /files: STS OK (RWUG5BS59Y52M9SSESMC...) bucket=pol-data-brightside

=== carol-northwind (tenant=partners) ===
  STS OK (SGBDDDYWWDJD7TG70PG7...)
  expected bucket: pol-data-partners
  ListObjects OK in pol-data-partners:
    - README.md (283 B)
  /data: 1 rows
    partner-tier: gold
  /files: STS OK (SGBDDDYWWDJD7TG70PG7...) bucket=pol-data-partners

=== alice (employee, no tenant group) ===
  /data: HTTP 403
  /files: HTTP 403
```

Negative case:

```
=== alice-acme attempting pol-data-ops ===
  STS OK with access_key 20LN0X4HSBLJFK4ITFUV...
  pol-data-acme: ALLOWED (has 1 objects)
  pol-data-ops: DENIED (AccessDenied: An error occurred (AccessDenied) ...)
```

## Appendix B — references

- MinIO STS source: `minio/cmd/sts-handlers.go`
- MinIO ARN format: `minio/internal/arn/arn.go`
  - `arn:minio:iam:<region>::role/<base64url(sha1(clientID))>`
- Keycloak 24 protocol mappers: `oidc-group-membership-mapper`,
  `oidc-audience-mapper`
- Headscale ACLv2 docs: https://headscale.net/stable/ref/acls/
- Postgres RLS: https://www.postgresql.org/docs/16/ddl-rowsecurity.html