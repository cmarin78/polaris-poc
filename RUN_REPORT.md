# Polaris POC — Run Report

> **Codename**: Polaris (the North Star — a navigation reference for what
> used to be a constellation of stitched-together identity systems).
> **Goal**: validate replacing an in-house OpenVPN + per-service
> AD-bound identity with a single open-source stack (Headscale control
> plane + Keycloak IDP + MinIO IAM+S3 + Postgres RLS) running on a
> local EKS-sim, with three portals and a customer multi-tenant data
> layer.

## 1. TL;DR

|                                          |                                                                                  |
| ---------------------------------------- | -------------------------------------------------------------------------------- |
| Components                               | Headscale, Keycloak, Postgres, MinIO, kind (EKS-sim), Traefik, 3 portals         |
| Lines of code (apps)                     | ~700 (intranet + customer Flask apps, Grafana provisioning)                      |
| Infra files                              | ~25 (docker-compose, charts, realm JSON, init SQL, scripts)                      |
| OIDC users verified                      | 6 (3 internal employees + 3 external customers across 3 tenants)                 |
| Isolation verified                       | 6 users × 5 buckets; RLS multi-tenant; denial when no tenant group              |
| Public repos                             | `cmarin78/polaris-poc`, `cmarin78/tailscaletest-poc`, `cmarin78/headscaletest-poc` |

The POC demonstrates that a production-shaped identity fabric can be
built without paying Tailscale / Auth0 / AWS bills, on a single Linux
host.

See **[`docs/TOPOLOGY.md`](docs/TOPOLOGY.md)** for the architecture
diagram, the tailnet explanation, the Headscale policy walked through
line by line, and the access matrix. See
**[`docs/REPRODUCIBILITY.md`](docs/REPRODUCIBILITY.md)** for the
step-by-step commands, expected outputs, and explanations that bring
the POC up from a clean host.

## 2. What this POC simulates

The POC exercises a single integrated identity fabric against the
following legacy pattern:

| Legacy stack                                                      | Replacement the POC validates                                          |
| ----------------------------------------------------------------- | --------------------------------------------------------------------- |
| Site-to-site OpenVPN                                              | WireGuard mesh via Tailscale clients, control plane in Headscale      |
| Per-app Active Directory SSO with NTLM/Kerberos dance              | One Keycloak realm, OIDC issued once, every portal speaks OIDC        |
| Per-application S3 + per-app IAM roles, separate AWS accounts      | One MinIO instance, OIDC-direct STS, IAM mapped from Keycloak groups  |
| DB-per-tenant Postgres (one database per customer)                 | Shared schema + RLS via `SET LOCAL app.current_tenant_id`            |
| Manual "this employee can see this app" spreadsheets               | Tag-driven ACLs in Headscale + Keycloak `groups` claim driving IAM    |

The POC runs the whole stack on a single Linux box. It exercises the
end-to-end flow with six real users (three employees and three
external customers, one per tenant) and proves that the layering
holds: a user without the right Keycloak group cannot reach the
portal, cannot read rows they should not see, and cannot list S3
objects outside their tenant.

## 3. Task list (in execution order)

These are the tasks that were performed to bring the POC up. The
order matters; each task is a prerequisite for the next. The list is
flat by design — there is no project timeline framing here. See
`docs/REPRODUCIBILITY.md` for the commands and outputs of each task.

1. **Stand up the base docker-compose stack** (Headscale, Keycloak,
   Postgres, MinIO). Wait for all four services to be healthy.
2. **Run the MinIO bootstrap container** — creates the five buckets
   (`pol-data-employees`, `pol-data-ops`, `pol-data-acme`,
   `pol-data-brightside`, `pol-data-partners`) and six IAM policies
   that map each Keycloak `groups` claim to its bucket. Configures
   MinIO OIDC against Keycloak's `polaris` realm.
3. **Create the kind cluster** (`polaris-eks-sim`) with one
   control-plane and one worker. Join both nodes to the
   `polaris_polaris_default` docker network so pods inside the
   cluster can resolve `keycloak`, `minio`, `postgres`, `headscale`
   by hostname.
4. **Build the three portal images** (`polaris-intranet`,
   `polaris-grafana`, `polaris-customer`) on the host.
5. **Load the images into the kind cluster** via
   `kind load docker-image` so the pods can pull them.
6. **Apply Traefik ingress** first, then the three portal
   deployments. Each portal gets a Deployment, a ClusterIP Service,
   an Ingress (host-based routing), and a NodePort Service for the
   test browser.
7. **Apply the Headscale ACL policy** from `acl/policy.hujson` —
   declares `tagOwners`, the per-tag ACLs, and the SSH rules. Without
   this step Headscale defaults to permissive.
8. **Register the nodes and tag them** so the Headscale mesh
   contains the four pods (intranet, grafana, customer, minio).
9. **Run the end-to-end verification** — for each of the six users,
   log in via OIDC, exercise the relevant endpoint, and confirm the
   expected result (rows visible for tenant users; 403 for employees
   without a tenant; correct Grafana role for admins).
10. **Capture the six screenshots** that document the working POC
    (intranet directory, Grafana home as Admin, customer home/data/
    files for tenant acme, customer 403 for an employee without a
    tenant). The capture script is
    `tests/capture_screenshots.py`.

## 4. Setup details (per task)

### 4.1 Base stack (Task 1)

Four services on the host: Headscale (control plane, listens on
`28080`), Keycloak (IDP, `8081`), Postgres (RDBMS, `5433`), MinIO
(S3+IAM, `9000` for the S3 API and `9001` for the console).

The Keycloak realm (`polaris`) is exported from `keycloak/realm-polaris.json`
with seven groups, six users, and four OIDC clients (`pol-intranet`,
`pol-grafana`, `pol-customer`, `pol-minio`). The MinIO bootstrap
container (`scripts/minio_bootstrap.py`) is a `python:3.12-slim` image
that uses `boto3` for bucket creation and raw HTTP (sigv4-signed
`PUT /minio/admin/v3/add-canned-policy?name=<name>`) for IAM policy
upload — there is no `mc` CLI image and the AWS IAM `PutUserPolicy`
endpoint is not supported by MinIO.

### 4.2 Cluster and portals (Tasks 3–6)

`kind` cluster with 5 namespaces (`pol-intranet`, `pol-grafana`,
`pol-customer`, `pol-storage`, `pol-system`). Traefik ingress
controller runs as a DaemonSet with a NodePort Service for host
access. The cluster is configured without `extraPortMappings` because
kind refused to start on this host with the host-port mappings; ingress
is reached through the Traefik NodePort or `kubectl port-forward`.

The portal pods each have a tailscale sidecar in the POC design but
in this run the portals reach MinIO and Postgres via the docker
hostnames (because the kind nodes joined the docker-compose
network); the tailnet is the production path and is documented in
`docs/TOPOLOGY.md` §3.

### 4.3 Headscale policy (Task 7)

The policy file (`acl/policy.hujson`) declares ten `tagOwners` and
the matching `acls` + `ssh` rules. The full file is reproduced and
walked through in [`docs/TOPOLOGY.md`](docs/TOPOLOGY.md) §4. Two
Headscale-specific quirks are documented in the policy comments:
the `user:polaris@` literal (the trailing `@` is the validation
token) and the absence of `autogroup:admin` /
`autogroup:nonroot` (Headscale does not implement those).

### 4.4 Verification matrix (Task 9)

| user             | groups                              | /data (Postgres)               | /files (MinIO)               | Grafana role |
| ---------------- | ----------------------------------- | ------------------------------ | ---------------------------- | ------------ |
| alice            | pol-employees, pol-eng              | 403 (no tenant)                | 403 (no bucket policy)       | Viewer       |
| bob              | pol-employees, pol-ops              | 403                            | 403                          | Editor       |
| carol            | pol-admin, pol-employees            | 403                            | 403                          | Admin        |
| alice-acme       | pol-customer-tenant-acme            | 2 ACME rows                    | pol-data-acme                | Viewer       |
| bob-brightside   | pol-customer-tenant-brightside      | 2 BRIGHT rows                  | pol-data-brightside          | Viewer       |
| carol-northwind  | pol-partners-northwind              | 1 partner row                  | pol-data-partners            | Viewer       |

Negative cases:

- `alice-acme` requesting `pol-data-ops` → STS 200 + IAM AccessDenied.
- `alice` (employee) on customer `/data` → 403 "no tenant group".
- `bob` (no `pol-customer-tenant-*` group) on customer `/files` → 403.

## 5. End-to-end test (with figures)

The validation runs through `tests/capture_screenshots.py` (Playwright
headless + chromium with `--host-rules=MAP keycloak 192.168.32.4` so
the browser can reach the Keycloak container over the docker bridge
from the test host). The cookie jar is cleared between portal
captures so the realm-level Keycloak SSO session from one user's
intranet login does not auto-bridge them into another pod when we
later log in as a different user.

Each figure below is one screenshot from `docs/screenshots/`, with
an explanation of what it proves.

### 5.1 Intranet — directory as `alice`

![Employee directory rendered for alice](docs/screenshots/01-intranet-directory-alice.png)

**Figure 1 — `alice` views the employee directory.** This proves the
OIDC code+PKCE flow against Keycloak works end-to-end and that the
`/directory` endpoint queries the `pol_intranet` database with the
`polaris_admin` role (the owner, no RLS). Alice is an internal
employee in the `pol-eng` group; the page shows the five seeded
people (Ada, Linus, Grace, Barbara, Sara) with their department,
location, and role. This endpoint is an internal HR resource, so it
has no tenant group requirement — any employee-tagged user can read
it.

### 5.2 Grafana — post-OAuth home as `carol` (Admin role)

![Grafana home for carol](docs/screenshots/02-grafana-home-carol-admin.png)

**Figure 2 — `carol` authenticated via OIDC in Grafana.** The avatar
in the top right confirms the Grafana generic-OAuth flow completed
the code exchange against Keycloak and that the Grafana session is
now bound to `carol`. The home shows "Home" in the sidebar (no
dashboards yet) but the Admin role is applied via the JMESPath in
`GF_AUTH_GENERIC_OAUTH_ROLE_ATTRIBUTE_PATH` —
`contains(groups[*], 'pol-admin') && 'Admin'`. A `Viewer` or `Editor`
would see the same home but with fewer permissions at `/admin`.

### 5.3 Customer portal — home as `alice-acme`

![Customer portal home as alice-acme](docs/screenshots/03-customer-home-alice-acme.png)

**Figure 3 — `alice-acme` views the customer portal with her tenant.**
The welcome line shows explicitly "Welcome alice-acme. tenant:
acme", which proves the group→tenant mapping worked: the
`pol-customer-tenant-acme` group was translated to tenant name
`acme` via the portal's internal table, and that name was used to
resolve the `tenant_id` that then feeds the `SET LOCAL
app.current_tenant_id` on every request. The two cards ("/data"
and "/files") point at the endpoints where the two layers of defense
(RLS in Postgres and IAM in MinIO) are exercised.

### 5.4 Customer portal — `/data` filtered by RLS

![alice-acme sees only acme rows](docs/screenshots/04-customer-data-alice-acme-acme-only.png)

**Figure 4 — `/data` shows only the rows of the acme tenant.** This
is the strongest single piece of evidence for tenant isolation at
the SQL layer. The `customer_data` table contains rows for three
tenants (acme, brightside, partners); the `tenant_isolation` RLS
policy rejects every read where `tenant_id !=
current_setting('app.current_tenant_id')`. The page shows two rows
(`contract-id ACME-2026-001` and `monthly-revenue $4.2M`) and
exposes the SQL it ran below — including `SET LOCAL
app.current_tenant_id = '1'`. If `bob-brightside` opened the same
URL he would see only the brightside rows; `carol-northwind` would
see only partners; none can read another's row even if they know
the row id.

### 5.5 Customer portal — `/files` with STS against MinIO

![alice-acme lists pol-data-acme via STS](docs/screenshots/05-customer-files-alice-acme.png)

**Figure 5 — STS works end-to-end for `alice-acme`.** The portal
takes the user's Keycloak access-token JWT (with
`groups=[pol-customer-tenant-acme]`) and posts it to MinIO's
`/api/v1/sts/assume-role-with-webidentity` endpoint. MinIO validates
the JWT against Keycloak's JWKS, extracts the `groups` claim, maps
it to the IAM policy `pol-customer-acme`, and returns temporary
credentials. The page shows "STS OK — temporary access key …" and
below that the contents of the `pol-data-acme` bucket
(`README.md`, 285 B). If `alice-acme` tried to list
`pol-data-brightside` MinIO would return 403 AccessDenied by the IAM
policy server-side, even though the STS exchange itself succeeded —
defense in depth (Postgres RLS + MinIO IAM).

### 5.6 Customer portal — `/data` denied to a user without a tenant

![alice (employee) gets 403](docs/screenshots/06-customer-data-alice-403-no-tenant.png)

**Figure 6 — `alice` (employee with no tenant group) receives 403.**
This is the "deny by default" proof. Alice belongs to `pol-employees`
and `pol-eng`; neither group appears in the customer portal's
group→tenant dictionary, so `_user_tenant()` returns `None` and the
endpoint returns
`403: no tenant group in your JWT (groups=['pol-employees', 'pol-eng'])`.
The response exposes the JWT groups explicitly so it is obvious to
the reader that the decision is made at the application layer by
looking at the groups, not in the database. An internal employee
cannot see tenant data even though they passed Keycloak — the
portal filters before touching the table.

## 6. Findings that were not in the design

These are the things the build uncovered that were not in
`docs/design.md` and required real changes to make the POC work.

1. **MinIO does not implement AWS IAM `PutUserPolicy`.** It has its
   own admin API. Once we knew that the rest was direct, but the
   documentation does not say it.
2. **Roles `SUPERUSER` bypass RLS in Postgres** — easy to miss.
   `FORCE ROW LEVEL SECURITY` only applies when the user is not a
   superuser.
3. **boto3 enforces a minimum `RoleArn` length of 20** — but
   MinIO's claim-based STS path does not want a `RoleArn`. The STS
   call has to be raw HTTP.
4. **The `groups` scope in Keycloak is a custom client scope**, not a
   default-allowed scope on public clients. Requesting it explicitly
   in `scope=` is rejected unless the client has it in
   `defaultClientScopes` or `optionalClientScopes`.
5. **Headscale v2 requires every referenced tag to be declared in
   `tagOwners`**, even when the tag maps from an OIDC claim rather
   than a preauth key. (Pre-auth ACLs would have caught this, but the
   POC uses database mode.)
6. **Flask session cookies signed with the same key across pods
   decrypt on both pods.** Both portals shipped with the same
   `FLASK_SECRET` placeholder, so the intranet's signed cookie
   decrypted on the customer pod — the customer then rendered
   `Welcome alice.` even though alice had never logged into the
   customer. Fixed with per-pod secrets
   (`polaris-poc-intranet-secret-CHANGE_ME` vs
   `polaris-poc-customer-secret-CHANGE_ME`).
7. **Keycloak SSO bridges users at the realm level, not per client.**
   When alice authenticated against the intranet, Keycloak held a
   session for her in the polaris realm. The customer pod then
   redirected to Keycloak and Keycloak auto-logged her in (with
   `prompt=login` even pre-filling the username field so only the
   password was visible). The customer pod ended up authenticated as
   alice. Fixed with `prompt=login` on `/login` plus
   `ctx.clear_cookies()` between portal captures. The production
   answer is per-portal OIDC clients — flagged as TODO.
8. **Grafana's `appUrl` is baked into the Docker image**, so setting
   `GF_AUTH_GENERIC_OAUTH_AUTH_URL` does not change the
   `redirect_uri` Grafana sends to Keycloak. Without
   `GF_SERVER_ROOT_URL=http://127.0.0.1:13001`, Grafana would
   302-redirect to `http://grafana.polaris.ts.net/login/generic_oauth`,
   which does not resolve on the test host.
9. **Grafana's `generic_oauth` does not implement PKCE**, so any
   client in Keycloak with `pkce.code.challenge.method` enforced
   rejects the authorize call with `Missing parameter:
   code_challenge_method`. Workaround: leave PKCE off for the
   pol-grafana client only. The intranet and customer pods were
   updated to send PKCE.

## 7. Reproduction

The full reproduction sequence is in
[`docs/REPRODUCIBILITY.md`](docs/REPRODUCIBILITY.md) — eleven flat
tasks with the exact commands, expected outputs, and explanations
for each. The same tasks are also bundled in `bin/bootstrap.sh`
(one-shot) and `Makefile` (thin wrapper around it).

The one-shot entry point is `make bootstrap` from the repo root.

## 8. Decisions taken

| Question                                                    | Choice                                                | Reasoning                                                      |
| ----------------------------------------------------------- | ----------------------------------------------------- | -------------------------------------------------------------- |
| Sync Keycloak groups to Headscale tags?                     | manual `headscale nodes tag` for the POC             | webhook automation is straightforward but out of scope         |
| Schema-per-tenant or shared schema in Postgres?             | shared schema + RLS via `SET LOCAL`                   | cheaper ops, one migration applies to all                      |
| Customer portal API-first or HTML-only?                     | HTML-only                                             | API contract is a separate workstream                          |
| Prometheus?                                                 | skipped (Grafana + Postgres datasource only)          | metric scope is small; Grafana queries are enough              |
| LocalStack or MinIO for S3+IAM?                             | MinIO with OIDC                                       | lighter, OIDC first-class via `MINIO_IDENTITY_OPENID_*`, native STS |

## 9. Known risks at POC close

| id | Risk                                                  | Mitigation                                              | Status      |
| -- | ----------------------------------------------------- | ------------------------------------------------------- | ----------- |
| R1 | Headscale single instance (no HA)                     | run two, peer them                                      | out of scope |
| R2 | keycloak_db backed by Postgres → SPOF                 | external DB                                             | out of scope |
| R3 | pgAdmin not deployed                                  | —                                                       | out of scope |
| R4 | bootstrap boto3 has hardcoded credentials             | use Vault / SOPS                                        | out of scope |
| R5 | Resource-owner password grant used by all portals     | switch to auth-code flow                                | planned     |
| R6 | No MFA on Keycloak                                    | enable WebAuthn in prod                                 | planned     |
| R7 | MinIO community archives discontinued                 | pin to cached `RELEASE.2024-01-18`                      | mitigated   |
| R8 | MinIO OIDC aud claim requires per-client audience mapper | added in realm JSON + admin API                      | fixed       |
| R9 | boto3 STS rejects empty RoleArn                       | raw HTTP for STS                                        | fixed       |

## 10. Out of scope

- TLS certificates (in prod: cert-manager + Let's Encrypt)
- Service mesh (Linkerd / Istio)
- Backup / disaster recovery
- Secrets management (Vault, External Secrets Operator)
- CI/CD (ArgoCD / Flux)
- Observability stack (Prometheus + Loki + Tempo)
- Production-grade multi-tenant OIDC federation
- Cost analysis at scale

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
