# Polaris POC — Design

**Status**: shipped (POC complete)
**Date**: 2026-09-28
**Author**: Cesar Marin
**Working dir**: `/home/cmarin78/Documents/Projects/MiniMax/Headscale/polaris/`

## 0. Naming

- **Organization / customer tenant root**: `Polaris` — fantasy name,
  nothing from the real company / product names.
- **B2B customer**: `Acme Industries`, `Brightside Health` (fantasy).
- **External partner**: `Northwind Consulting` (fantasy).

## 1. Goal and non-goals

### Goal

Demonstrate that replacing OpenVPN with **Tailscale/Headscale +
Keycloak (OIDC) + kind + Postgres** delivers:

1. **Per-role granularity via the IDP** — Keycloak groups →
   Tailnet tags → ACLs in `acl/policy.hujson` → access to portals /
   services.
2. **Three portals** with disjoint access domains:
   - **intranet** for employees
   - **grafana** for ops/SRE
   - **customer portal** for B2B customers + partners (multi-tenant)
3. **EKS simulation** via kind, **RDS simulation** via Postgres in
   docker — all reproducible from a clean host.
4. **Onboarding of external users** without touching the control plane.

### Non-goals

- No real production migration.
- No use of real product / company names.
- No high availability, no autoscaling (single-node kind is enough).
- No real cloud provisioning — everything runs in local docker.

## 2. Top-level architecture

```
                                   Internet
                                      │
                                      ▼
                          ┌─────────────────────┐
                          │   Reverse proxy     │
                          │   (traefik :8080)   │ ← shared by all 3 portals
                          └──────────┬──────────┘
                                     │
        ┌────────────────────────────┼────────────────────────────┐
        │                            │                            │
        ▼                            ▼                            ▼
┌──────────────┐            ┌──────────────┐            ┌──────────────┐
│   kind       │            │   kind       │            │   kind       │
│   cluster    │            │   cluster    │            │   cluster    │
│  "polaris-   │            │  "polaris-   │            │  "polaris-   │
│  eks-sim"    │            │  eks-sim"    │            │  eks-sim"    │
│              │            │              │            │              │
│ ns:          │            │ ns:          │            │ ns:          │
│  pol-        │            │  pol-        │            │  pol-        │
│  intranet    │            │  grafana     │            │  customer    │
│              │            │              │            │              │
│ ┌──────────┐ │            │ ┌──────────┐ │            │ ┌──────────┐ │
│ │ intranet │ │            │ │ grafana  │ │            │ │ customer │ │
│ │ Flask+JS │ │            │ │ 11.x OSS │ │            │ │ portal   │ │
│ │ :3000    │ │            │ │ :3000    │ │            │ │ :3000    │ │
│ └──────────┘ │            │ └──────────┘ │            │ └──────────┘ │
└──────────────┘            └──────────────┘            └──────────────┘
                │
                ▼
        ┌──────────────────────────┐
        │  Tailscale/Headscale     │
        │  control plane           │
        │  MagicDNS:               │
        │   polaris.ts.net         │
        └──────────────────────────┘
                │
   ┌────────────┼─────────────┐
   ▼            ▼             ▼
┌─────────┐ ┌──────────┐ ┌─────────┐
│Keycloak │ │ Postgres │ │  Per-   │
│ :8081   │ │ (RDS sim)│ │  user   │
│         │ │  :5433   │ │ devices │
│ OIDC    │ │          │ │         │
│ issuer  │ │ pol_int  │ │employees│
│ pol-*   │ │ pol_graf │ │ ops,    │
│ clients │ │ pol_cust │ │ B2B     │
└─────────┘ └──────────┘ └─────────┘
```

- **kind** runs a Kubernetes cluster with 1 server + 1 agent node.
  Five namespaces (`pol-intranet`, `pol-grafana`, `pol-customer`,
  `pol-storage`, `pol-system`) run the portals as Deployments. In
  production each pod runs a Tailscale sidecar; in the POC the
  portals reach MinIO and Postgres via the docker hostnames (the
  kind nodes joined the docker-compose network).
- **Headscale** (self-hosted) is the Tailscale control plane; the
  sidecar tags determine what each pod can see.
- **Keycloak** is the IDP. OIDC issuer for all 3 portals; also
  issuer for the tailscale-tag-sync (claims → tags).
- **Postgres** runs as a docker container, one database per portal.
- **Single reverse proxy** (Traefik) on the host, routes by hostname
  (`intranet.polaris.ts.net`, `grafana.polaris.ts.net`,
  `customer.polaris.ts.net`).

## 3. Components and services

### 3.1 Identity — Keycloak (24.x)

| Realm    | Clients                                          | Groups                                                                                          | Roles                         |
| -------- | ------------------------------------------------ | ----------------------------------------------------------------------------------------------- | ----------------------------- |
| `polaris`| `pol-intranet`, `pol-grafana`, `pol-customer`, `pol-minio` | `pol-employees`, `pol-eng`, `pol-ops`, `pol-admin`, `pol-customer-tenant-acme`, `pol-customer-tenant-brightside`, `pol-partners-northwind` | per-client `read`, `write`, `admin` |

**User provisioning flow**:

1. Admin opens the Keycloak Admin Console (`http://localhost:8081`).
2. Creates the user with username `first.last@pol-intranet` (or
   `ext.last@partner-northwind`).
3. Assigns the user to one or more groups.
4. For each group, Keycloak emits a `groups` claim in the JWT with the
   group name.

**OIDC → Tailscale tag mapping** (admin-scripted for the POC):

```
keycloak group                 →  tailnet tag
pol-employees                  →  tag:pol-employees
pol-eng                        →  tag:pol-eng
pol-ops                        →  tag:pol-ops
pol-admin                      →  tag:pol-admin
pol-customer-tenant-acme       →  tag:pol-customer-acme
pol-customer-tenant-brightside →  tag:pol-customer-brightside
pol-partners-northwind         →  tag:pol-partners-northwind
```

The POC uses a manual sync (`headscale nodes tag --identifier=<email>
--tags=tag:<g>`). `tsidp` (Tailscale's IDP proxy for automatic tag
sync) is a follow-up.

### 3.2 Network — Headscale + tailscale sidecars

Tags per namespace (production target; the POC uses docker hostnames
inside the cluster):

| Namespace         | Tailscale tag                       | MagicDNS suffix                | TCP access                       |
| ----------------- | ----------------------------------- | ------------------------------ | -------------------------------- |
| `pol-intranet`    | `tag:pol-intranet`                  | `intranet.polaris.ts.net`      | `:3000` (web)                    |
| `pol-grafana`     | `tag:pol-grafana`                   | `grafana.polaris.ts.net`       | `:3000` (web)                    |
| `pol-customer`    | `tag:pol-customer-{tenant}`         | `customer.polaris.ts.net`      | `:3000` (web), `:5432` (admin)   |
| `pol-storage`     | `tag:pol-minio`                     | `minio.polaris.ts.net`         | `:9000` (S3), `:9001` (console)  |

ACL policy (`acl/policy.hujson`, full file reproduced in
[`TOPOLOGY.md`](TOPOLOGY.md) §4):

```jsonc
{
  "tagOwners": {
    "tag:pol-intranet":              ["user:polaris@"],
    "tag:pol-grafana":               ["user:polaris@"],
    "tag:pol-customer-acme":         ["user:polaris@"],
    "tag:pol-customer-brightside":   ["user:polaris@"],
    "tag:pol-partners-northwind":    ["user:polaris@"]
  },
  "acls": [
    { "action": "accept",
      "src":    ["tag:pol-employees"],
      "dst":    ["tag:pol-intranet:80", "tag:pol-grafana:80"] },
    { "action": "accept",
      "src":    ["tag:pol-ops"],
      "dst":    ["tag:pol-grafana:80"] },
    { "action": "accept",
      "src":    ["tag:pol-customer-acme"],
      "dst":    ["tag:pol-customer-acme:80", "tag:pol-customer-acme:5432"] },
    { "action": "accept",
      "src":    ["tag:pol-partners-northwind"],
      "dst":    ["tag:pol-customer-partners:80"] },
    { "action": "accept",
      "src":    ["tag:pol-admin"],
      "dst":    ["tag:*"] }
  ]
}
```

### 3.3 Compute — kind cluster

Cluster: `polaris-eks-sim`. 1 control-plane + 1 agent (sufficient for
the POC). Traefik ingress controller. Five namespaces + per-namespace
RBAC.

Image: `kindest/node:v1.30.0` (cached locally; the k3s-tools image that an
alternative tool would need is blocked on this host).

### 3.4 Data — Postgres

| DB             | Owner             | Users                  | Notes                          |
| -------------- | ----------------- | ---------------------- | ------------------------------ |
| `pol_intranet` | `pol_intranet_app`| `pol_intranet_ro`, `pol_intranet_rw` | directory, wiki, announcements |
| `pol_grafana`  | `grafana`         | (default)              | metadata, dashboards           |
| `pol_customer` | `pol_customer_app`| one user per tenant    | tenant data                    |

Postgres is not exposed on the tailnet for the POC; the
customer-tagged pods reach it via the docker-compose network instead.

### 3.5 Portals

#### 3.5.1 Intranet (`pol-intranet`)

- Stack: Python Flask + server-side templates.
- Endpoints:
  - `GET /`         — home with logo and links to Grafana + Customer Portal
  - `GET /directory` — table of employees
  - `GET /files`    — list of files in the user's MinIO bucket
  - `GET /healthz`  — JSON status
- Auth: OIDC against Keycloak (client `pol-intranet`).
- Authorization: any user in `pol-employees` can read.

#### 3.5.2 Grafana (`pol-grafana`)

- Stack: Grafana 11.3 OSS.
- Auth: OIDC against Keycloak (client `pol-grafana`).
- Role mapping in Grafana config:
  - `pol-admin` → Grafana Admin
  - `pol-ops`   → Editor
  - other groups → Viewer (read-only)
- Datasources preconfigured:
  - Postgres `pol_grafana` (metadata, dashboards)
- Dashboards seeded: time-series demo of `polaris_metrics` (service ×
  region × minute).

#### 3.5.3 Customer Portal (`pol-customer`)

- Stack: Flask with multi-tenant rendering.
- Endpoints:
  - `GET /`         — landing with tenant info (from `groups` claim)
  - `GET /data`     — tabular view of the tenant's rows
  - `GET /files`    — list of files in the tenant's S3 bucket (proxy to MinIO)
- Auth: OIDC against Keycloak (client `pol-customer`).
- Tenant resolution: the `groups` claim is matched against
  `pol-customer-tenant-{name}` to resolve the tenant name; that name
  is then translated to a `tenant_id` used in
  `SET LOCAL app.current_tenant_id`.
- Isolation layers:
  - Headscale ACL separates the customer tags by tenant
  - SQL: `WHERE tenant_id = current_setting('app.current_tenant_id')`
    so a bug in the portal cannot leak cross-tenant rows
  - MinIO IAM policies: the bucket policy + STS temporary credentials
    limit which files each tenant can list or upload

### 3.6 Storage — MinIO as S3 sim with OIDC against Keycloak

MinIO chosen over LocalStack because:

- Single binary, ~150 MB image (LocalStack ~1.5 GB and 30+ s to start)
- Native OIDC support since RELEASE.2022-12-02 (Keycloak is first-class)
- Same API as S3 (`s3://`, `aws s3 cp`, `boto3` unchanged)
- STS-style temporary credentials based on the Keycloak JWT

```
+-------------------------------------------+
|  MinIO  (minio/minio:RELEASE.2024-...)    |
|                                           |
|  OIDC config:                             |
|    issuer    = http://keycloak:8080/realms/polaris
|    client_id = pol-minio                  |
|    claim_name = groups                    |
|                                           |
|  Policies (MinIO IAM):                    |
|    pol-employees-rw                       |
|    pol-ops-rw                             |
|    pol-admin-rw                           |
|    pol-customer-acme-rw                   |
|    pol-customer-brightside-rw             |
|    pol-partners-northwind-rw              |
|                                           |
|  Group claim → policy mapping:            |
|    pol-employees                → pol-employees-rw
|    pol-ops                      → pol-ops-rw
|    pol-admin                    → pol-admin-rw
|    pol-customer-tenant-acme     → pol-customer-acme-rw
|    pol-customer-tenant-brightside → pol-customer-brightside-rw
|    pol-partners-northwind       → pol-partners-northwind-rw
+-------------------------------------------+
```

**End-to-end flow**:

1. User opens `https://intranet.polaris.ts.net/files`.
2. Portal detects that the endpoint needs S3 credentials and starts
   the OIDC flow against Keycloak (already authenticated if SSO is
   active).
3. Portal calls MinIO `AssumeRoleWithWebIdentity` with the Keycloak JWT
   (`http://keycloak:8080/realms/polaris` as issuer).
4. MinIO validates the JWT against Keycloak, extracts the `groups`
   claim, maps to a MinIO policy per the table above, and returns
   temporary access/secret keys (STS, default TTL 1 h).
5. Portal uses the STS credentials to list / upload
   (`aws s3 ls s3://pol-data-employees/`).
6. Every S3 request is evaluated by both the bucket policy and the IAM
   policy in MinIO — if either denies, the request is rejected.

**Why this split**:

- The Tailscale ACL decides whether the request reaches MinIO (network
  layer).
- MinIO IAM decides which bucket / key the user can see (object layer).
- Two independent layers — if one fails, the other still contains.

### 3.7 Bucket layout

| Bucket                 | Owner       | Tenant policy                          | Notes                            |
| ---------------------- | ----------- | -------------------------------------- | -------------------------------- |
| `pol-data-employees`   | `pol-admin` | readwrite by `pol-employees`           | wiki uploads, announcements      |
| `pol-data-ops`         | `pol-admin` | admin by `pol-ops`                     | log dumps, k8s manifests, runbooks |
| `pol-data-acme`        | `pol-admin` | readwrite by `pol-customer-tenant-acme`| Acme tenant data                 |
| `pol-data-brightside`  | `pol-admin` | readwrite by `pol-customer-tenant-brightside` | Brightside tenant data     |
| `pol-data-partners`    | `pol-admin` | readwrite by `pol-partners-northwind`  | shared partner data              |

Each bucket has a seed object (`README.md` with tenant info) to verify
that isolation works during the walkthrough.

## 4. Onboarding flows

### 4.1 Internal employee (e.g. `alice@polaris.example`)

```
1. Admin opens Keycloak admin console
2. Creates user alice@polaris.example, temporary password
3. Assigns groups: pol-employees + pol-eng
4. Admin runs:
   $ headscale nodes tag --identifier=alice@... \
       --tags='tag:pol-employees,tag:pol-eng'
5. Alice opens intranet.polaris.ts.net → OIDC against pol-intranet
   → already authenticated, sees the directory
```

### 4.2 B2B customer (`alice-acme`)

```
1. Admin creates the user in Keycloak with group pol-customer-tenant-acme
2. Admin creates a preauth key tagged tag:pol-customer-acme
   $ headscale preauthkeys create --user polaris \
       --reusable --expiration 168h \
       --tags tag:pol-customer-acme
3. User downloads tailscale + registers with the key
4. MagicDNS resolves customer.polaris.ts.net → 100.x.y.z (sidecar)
5. User opens customer.polaris.ts.net → OIDC against pol-customer
   → claim groups=[pol-customer-tenant-acme]
   → portal filters and shows only Acme data
```

### 4.3 Partner (`carol-northwind`)

Same as the B2B flow but with `tag:pol-partners-northwind`. More
limited access: read-only on a pre-approved set of endpoints.

## 5. Tag matrix (summary)

| Tag                          | Who                          | What they can reach                                  |
| ---------------------------- | ---------------------------- | ---------------------------------------------------- |
| `tag:pol-employees`          | employees in `pol-employees` | intranet:80, grafana:80, minio:9000 (sts), bucket `pol-data-employees` (rw) |
| `tag:pol-eng`                | engineers                    | intranet:80, kind bastion:22                          |
| `tag:pol-ops`                | SRE / ops                    | grafana:80 (write), intranet:80, kind bastion:22, minio:9000, bucket `pol-data-ops` (admin) |
| `tag:pol-admin`              | admins (1–2 people)          | everything (including minio:9001 console)            |
| `tag:pol-customer-acme`      | Acme users                   | customer portal Acme, DB Acme, minio:9000, bucket `pol-data-acme` (rw) |
| `tag:pol-customer-brightside`| Brightside users             | customer portal Brightside, DB Brightside, minio:9000, bucket `pol-data-brightside` (rw) |
| `tag:pol-partners-northwind` | Northwind users              | customer portal Partners, minio:9000, bucket `pol-data-partners` (rw) |
| `tag:pol-minio`              | MinIO pod only               | exposes the S3 API on the tailnet                     |

Default-deny: any tag not listed is denied everywhere.

## 6. File map

```
polaris/
├── README.md                        quickstart + TL;DR
├── RUN_REPORT.md                    task list + E2E test (6 figures) + findings
├── RUN_REPORT.docx                  same content, .docx for sharing
├── docker-compose.yml               keycloak + postgres + headscale + minio + bootstrap
├── .env.example                     credentials template
├── acl/
│   └── policy.hujson                tagOwners + ACLs + SSH
├── headscale/
│   └── config.yaml                  control plane config
├── keycloak/
│   └── realm-polaris.json           preconfigured realm
├── postgres/
│   └── init.sql                     schemas + seed data + RLS policy
├── minio/
│   └── policies.json                reference doc for the IAM policies
├── scripts/
│   ├── Dockerfile.minio-bootstrap
│   └── minio_bootstrap.py           boto3 + raw HTTP admin API
├── apps/
│   ├── intranet/{app.py,Dockerfile}
│   ├── grafana/{Dockerfile,grafana.ini,provisioning/datasources/}
│   └── customer/{app.py,Dockerfile}
├── charts/
│   ├── traefik.yaml                 Traefik DaemonSet + NodePort
│   ├── intranet/deployment.yaml
│   ├── grafana/deployment.yaml
│   └── customer/deployment.yaml
├── docs/
│   ├── design.md                    ← this file
│   ├── TOPOLOGY.md                  diagrams + access matrix + policy walkthrough
│   ├── REPRODUCIBILITY.md           step-by-step commands + outputs + explanations
│   └── diagrams/
│       ├── architecture.mmd
│       ├── idp-flow.mmd
│       ├── tag-matrix.mmd
│       └── iam-flow.mmd
├── kind/
│   ├── cluster.yaml                 kind cluster config
│   ├── traefik.yaml                 Traefik ingress
│   └── test-pod.yaml                connectivity test
└── tests/                           E2E scripts (see RUN_REPORT §A)
```

## 7. Risks and open questions

### 7.1 Risks

| #  | Risk                                                                                                | Mitigation                                                                                              |
| -- | --------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------- |
| R1 | kind + tailscale sidecar: each pod needs `/dev/net/tun` and NET_ADMIN                               | use `tailscale/k8s-operator` or official chart; for POC, init container with hostPath                |
| R2 | Keycloak `groups` claim reaches the control plane but the mapping to tags is manual                  | start with manual sync (`headscale nodes tag`); `tsidp` as follow-up                                  |
| R3 | Customer data isolation: SQL injection or a bug in the portal leaks cross-tenant data              | Headscale ACL + `SET LOCAL app.current_tenant_id` on every query                                       |
| R4 | MagicDNS suffix collision with other POCs running in parallel                                       | use `polaris.ts.net` and stop the other POCs first                                                     |
| R5 | kind consumes a lot of RAM (~4 GB) along with Keycloak + Postgres + Headscale + MinIO               | use `--memory=2G` per container, total ~10 GB; document the 16 GB minimum on the host                 |
| R6 | Tailscale sidecars inside kind pods cannot see the host's `/dev/net/tun` by default                  | install kind with the right feature flags, or use `tailscale/k8s-operator`                              |
| R7 | MinIO OIDC + Keycloak: the `groups` claim can arrive with the realm path prefix (`/pol-employees`) and break the policy mapping | configure `claim_name = groups` AND empty `claim_prefix` in MinIO; verify with a test user  |
| R8 | STS credentials have a TTL (default 1 h); re-auth SSO should be transparent but sometimes is not   | use session cookies + OIDC refresh token in the portal; TTL bumped to 12 h for the POC                |
| R9 | Bucket policy + IAM policy = two places to define permissions; can diverge                          | IAM policies are the source of truth; bucket policies only restrict specific paths (default deny if IAM denies) |

### 7.2 Resolved decisions

| #   | Question                                              | Decision                                          | Why                                                                                |
| --- | ----------------------------------------------------- | ------------------------------------------------- | ---------------------------------------------------------------------------------- |
| 1   | OIDC vs manual sync for Keycloak → Tailscale tags     | manual sync via `headscale nodes tag`             | ~30 lines of Python; `tsidp` is a follow-up                                        |
| 2   | Schema-per-tenant vs RLS in Postgres                  | RLS with `SET LOCAL app.tenant`                   | standard SQL, easier isolation tests; schema-per-tenant adds migration complexity  |
| 3   | Customer portal REST vs HTML                          | HTML-only for the POC                             | REST + API key per tenant added later when the UI flow is validated                 |
| 4   | Monitoring skeleton                                   | skip Prometheus, Grafana with Postgres datasource | no need for alerting in the POC; Grafana has its own DB metadata                    |
| 5   | IAM + S3 sim                                          | MinIO with OIDC against Keycloak                  | lightweight, OIDC first-class, STS-style creds; LocalStack is overkill              |

## 8. Replication plan

The flat, ordered task list is in
[`REPRODUCIBILITY.md`](REPRODUCIBILITY.md). Each task lists the
exact command(s), the expected output, and an explanation. The same
tasks are bundled in `bin/bootstrap.sh` (one-shot) and `Makefile`
(thin wrapper).

## 9. How to approve / change

If something does not add up, the fastest path is:

- **Edit this file** (sections 2, 3, and 5 are the ones most likely
  to change).
- Restart the review cycle.

Once signed off, the design is treated as the source of truth for any
follow-up work.
