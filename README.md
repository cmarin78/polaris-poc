# Polaris — POC de fase 2 (reemplazo OpenVPN + IDP + EKS sim + 3 portales + IAM/S3)

**Estado**: en diseño. Revisar [`docs/design.md`](docs/design.md) antes de codear.

## TL;DR

Expande el POC base (`headscaletest-poc`) con:

| Pieza | Implementación | Por qué |
| --- | --- | --- |
| IDP para granularidad de roles | Keycloak 24 (self-hosted, docker) | OIDC estándar, open-source, sin vendor lock |
| Simulación EKS | k3d (k3s en docker, 1 server + 1 agent) | Arranca en 10s, soporta Ingress + NetworkPolicy |
| Simulación RDS | Postgres 16 (docker, single instance, multi-DB) | Suficiente para POC; replica de RDS en prod usaría IAM + VPC |
| Simulación S3 + IAM | **MinIO** (S3-compatible) con OIDC contra Keycloak | lightweight, OIDC first-class, STS-style creds |
| Portal intranet | Flask + OIDC + endpoint `/files` con STS proxy | Demo end-to-end de SSO + IAM |
| Portal Grafana | Grafana 10.x oficial + OIDC | Role mapping pol-admin/ops/employees |
| Portal customer | Flask multi-tenant, tenant via Keycloak `groups` claim | Aislamiento por tag + SQL RLS + bucket IAM |

## Arquitectura en una línea

```
traefik :8080 → k3d cluster (3 namespaces: intranet/grafana/customer + 1 storage con MinIO)
                → cada pod con tailscale sidecar → Headscale central
                → Keycloak como OIDC issuer para portales + MinIO (STS)
                → Postgres para datos relacionales
```

Dos capas de autorización:

| Capa | Qué controla | Mecanismo |
| --- | --- | --- |
| **Red** | ¿puede llegar al endpoint? | Tailscale ACL (`acl/policy.hujson`) + tag del sidecar |
| **Objeto** | ¿qué bucket/key puede ver? | MinIO IAM policy (mapped desde Keycloak `groups` claim via OIDC) |

Si una falla, la otra todavía contiene.

## Estructura

```
polaris/
├── README.md                   este archivo
└── docs/
    ├── design.md               propuesta completa (revisar)
    └── diagrams/
        ├── architecture.mmd    top-level components + connections
        ├── idp-flow.mmd        secuencia Keycloak → Headscale → portal
        ├── tag-matrix.mmd      Keycloak groups → tags → ACLs
        └── iam-flow.mmd        secuencia MinIO STS + Keycloak OIDC (S3 access)
```

## Próximo paso

1. Revisar `docs/design.md` (sobre todo las secciones 3.6 IAM+S3, 5 tag matrix, 7.1 riesgos).
2. Las 5 decisiones de sección 7.2 ya están cerradas:
   - Sync Keycloak→Tailscale tags: **manual** (`headscale nodes tag`)
   - Postgres multi-tenant: **RLS con `SET LOCAL app.tenant`**
   - Customer portal API: **solo HTML** para POC
   - Monitoring skeleton: **skip Prometheus**
   - IAM/S3 sim: **MinIO con OIDC**
3. Cuando esté firmado, pasar a fase 3 (build) con plan día-por-día
   en sección 8 del design.