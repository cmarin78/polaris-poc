# Polaris — corporate identity fabric on OSS

A proof of concept that simulates replacing an in-house OpenVPN +
per-service Active Directory identity setup with a single open-source
stack:

- **Headscale** (self-hosted Tailscale control plane) for the network mesh
- **Keycloak 24** for OIDC issuance
- **MinIO** (S3-compatible, IAM + OIDC) for object storage
- **Postgres 16 + RLS** for relational data with row-level tenant isolation
- **kind** (Kubernetes-in-Docker, EKS sim) for the workload layer
- **3 portals**: intranet, Grafana, customer — all OIDC against Keycloak

Status: **done**. End-to-end verified with 6 users, 5 buckets, 3 tenants.

See **[RUN_REPORT.md](RUN_REPORT.md)** for the narrative (flat task
list, end-to-end test with six figures, findings, and risks). See
**[`docs/TOPOLOGY.md`](docs/TOPOLOGY.md)** for the topology and
architecture diagrams, the tailnet explanation, the Headscale policy
walked through line by line, and the access matrix. See
**[`docs/REPRODUCIBILITY.md`](docs/REPRODUCIBILITY.md)** for the
step-by-step commands and expected outputs that bring the POC up from
a clean host. **[`docs/design.md`](docs/design.md)** has the
architecture source of truth.

## TL;DR

| Piece             | Implementation                                          | Why                                                                          |
| ----------------- | ------------------------------------------------------- | ---------------------------------------------------------------------------- |
| IDP               | Keycloak 24                                             | OIDC standard, OSS, no vendor lock                                            |
| EKS sim           | kind (k8s in Docker)                                    | reuses real k8s manifests + Helm charts                                       |
| RDS sim           | Postgres 16, multi-DB, RLS                              | one RLS policy per tenant, ops-cheap                                          |
| S3 + IAM sim      | MinIO with OIDC against Keycloak                        | lightweight, OIDC first-class, native STS                                     |
| Network mesh      | Headscale 0.29.4                                        | full ownership of the control plane                                           |
| Portal intranet   | Flask + OIDC + STS proxy to MinIO                       | end-to-end SSO + IAM                                                         |
| Portal Grafana    | Grafana 11.3 + generic OIDC + JMESPath role mapping     | pol-admin → Admin, pol-ops → Editor                                          |
| Portal customer   | Flask multi-tenant, tenant via Keycloak groups          | SQL RLS + bucket IAM, two-layer defense                                      |

## Architecture (one line)

```
traefik :8080 → kind cluster (5 namespaces)
                → each pod talks Headscale-registered services only
                → Keycloak OIDC issuer for portals + MinIO (STS)
                → Postgres for relational data
                → MinIO for object storage
```

Four layers of authorization (all must fail for a leak):

| Layer        | What it controls                                | Mechanism                                                            |
| ------------ | ----------------------------------------------- | -------------------------------------------------------------------- |
| Network      | which user → which pod                          | Headscale ACL v2, tag-based                                          |
| Identity     | which user → which app/portal                   | Keycloak `groups` claim                                              |
| Data (DB)    | which user → which row                          | Postgres RLS via `SET LOCAL app.current_tenant_id`                   |
| Data (S3)    | which user → which bucket/object                | MinIO IAM policy mapped from the `groups` claim                      |

## Quickstart

```bash
git clone https://github.com/cmarin78/polaris-poc
cd polaris-poc

# 1. Start base stack
docker compose -f docker-compose.yml up -d
docker compose -f docker-compose.yml run --rm minio-bootstrap

# 2. Start kind cluster and connect to the docker-compose network
kind create cluster --name polaris-eks-sim --config kind/cluster.yaml
docker network connect polaris_polaris_default polaris-eks-sim-control-plane
docker network connect polaris_polaris_default polaris-eks-sim-worker

# 3. Build + load portal images
docker build -t polaris-intranet:latest apps/intranet/
docker build -t polaris-grafana:latest  apps/grafana/
docker build -t polaris-customer:latest apps/customer/
kind load docker-image polaris-intranet:latest --name polaris-eks-sim
kind load docker-image polaris-grafana:latest  --name polaris-eks-sim
kind load docker-image polaris-customer:latest --name polaris-eks-sim

# 4. Apply manifests
kubectl --context=kind-polaris-eks-sim apply -f kind/traefik.yaml
kubectl --context=kind-polaris-eks-sim apply -f charts/intranet/deployment.yaml
kubectl --context=kind-polaris-eks-sim apply -f charts/grafana/deployment.yaml
kubectl --context=kind-polaris-eks-sim apply -f charts/customer/deployment.yaml

# 5. Apply Headscale policy (tagOwners + ACLs)
# See acl/policy.hujson; apply via `headscale policy set --file ...`
```

Each task is described in detail in
[`docs/REPRODUCIBILITY.md`](docs/REPRODUCIBILITY.md).

## Verified end-to-end

| user             | groups                              | /data (Postgres)               | /files (MinIO)               | Grafana role |
| ---------------- | ----------------------------------- | ------------------------------ | ---------------------------- | ------------ |
| alice            | pol-employees, pol-eng              | 403 (no tenant)                | 403 (no bucket policy)       | Viewer       |
| bob              | pol-employees, pol-ops              | 403                            | 403                          | Editor       |
| carol            | pol-admin, pol-employees            | 403                            | 403                          | Admin        |
| alice-acme       | pol-customer-tenant-acme            | 2 ACME rows                    | pol-data-acme                | Viewer       |
| bob-brightside   | pol-customer-tenant-brightside      | 2 BRIGHT rows                  | pol-data-brightside          | Viewer       |
| carol-northwind  | pol-partners-northwind              | 1 partner row                  | pol-data-partners            | Viewer       |

Negative cases:

- `alice-acme` requesting `pol-data-ops` → `AccessDenied`.
- `alice` (employee) on customer portal → `403 no tenant group`.

## Decisions

| Question                                                    | Choice                                                | Reasoning                                                      |
| ----------------------------------------------------------- | ----------------------------------------------------- | -------------------------------------------------------------- |
| Sync Keycloak groups to Headscale tags?                     | manual `headscale nodes tag` for POC                  | webhook automation is straightforward but out of scope         |
| Schema-per-tenant or shared schema in Postgres?             | shared schema + RLS via `SET LOCAL`                   | cheaper ops, one migration applies to all                      |
| Customer portal API-first or HTML-only?                     | HTML-only                                             | API contract is a separate workstream                          |
| Prometheus?                                                 | skipped (Grafana + Postgres datasource only)          | metric scope is small; Grafana queries are enough              |
| LocalStack or MinIO for S3+IAM?                             | MinIO with OIDC                                       | lighter, OIDC first-class, native STS                          |

## Repo layout

```
polaris/
├── README.md                   ← this file
├── RUN_REPORT.md               ← task list + E2E test (6 figures) + findings
├── RUN_REPORT.docx             ← same content, .docx for sharing
├── docker-compose.yml          ← 4 services + bootstrap
├── .env.example                ← credentials template (real .env is gitignored)
├── acl/
│   └── policy.hujson           ← Headscale tagOwners + ACLs + SSH
├── headscale/
│   └── config.yaml             ← DNS polaris.ts.net, sqlite, ephemeral
├── keycloak/
│   └── realm-polaris.json      ← 7 groups, 6 users, 4 OIDC clients
├── postgres/
│   └── init.sql                ← 3 DBs, RLS policy tenant_isolation
├── minio/
│   └── policies.json           ← reference doc for IAM policies
├── scripts/
│   ├── Dockerfile.minio-bootstrap
│   └── minio_bootstrap.py      ← boto3 + raw HTTP admin API
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
│   ├── design.md               ← architecture source of truth
│   ├── TOPOLOGY.md             ← diagrams + access matrix + policy walkthrough
│   ├── REPRODUCIBILITY.md      ← step-by-step commands + outputs + explanations
│   ├── diagrams/{architecture,iam-flow,idp-flow,tag-matrix}.mmd
│   └── screenshots/             ← 6 PNGs: intranet, Grafana, customer (×4)
├── kind/
│   ├── cluster.yaml            ← kind cluster config
│   ├── traefik.yaml            ← Traefik ingress
│   └── test-pod.yaml           ← connectivity test
└── tests/                       ← E2E scripts (see RUN_REPORT §A)
```

## Risks

See [RUN_REPORT §9](RUN_REPORT.md#9-known-risks-at-poc-close). Nine
risks identified; R7 (MinIO community archives discontinued) and R8
(audience mapper required per OIDC client) hit during the build and
were mitigated. R9 (boto3 STS rejects empty RoleArn) required
switching to raw HTTP for the STS call.

## Out of scope

- TLS certificates (cert-manager + Let's Encrypt in prod)
- Service mesh (Linkerd / Istio)
- Backup / disaster recovery
- Secrets management (Vault, External Secrets Operator)
- CI/CD (ArgoCD / Flux)
- Observability stack beyond Grafana + Postgres queries
- Production-grade multi-tenant OIDC federation
- Cost analysis at scale

## License

[MIT](LICENSE). Use, modify, and redistribute freely. See `LICENSE` at the
root of this repository for the full text.
