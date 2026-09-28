# Polaris POC — Diseño de fase 2

**Estado**: proposal (a revisar antes de codear)
**Fecha**: 2026-09-28
**Autor**: Cesar Marin
**Working dir**: `/home/cmarin78/Documents/Projects/MiniMax/Headscale/polaris/`

## 0. Naming

- **Organización / customer tenant root**: `Polaris` — fantasy name, nada de Axial,
  CerberusByte u otros nombres reales.
- **Cliente B2B**: `Acme Industries`, `Brightside Health` (fantasy).
- **Partner externo**: `Northwind Consulting` (fantasy).

## 1. Goal y no-goals

### Goal

Demostrar que el reemplazo de OpenVPN por **Tailscale/Headscale + Keycloak (OIDC)
+ k3d + Postgres** entrega:

1. **Granularidad por rol vía IDP** — grupos en Keycloak → tags en Tailnet →
   ACLs en `acl/policy.hujson` → acceso a portales/servicios.
2. **Tres portales** con dominios de acceso disjuntos:
   - **intranet** para empleados
   - **grafana** para ops/SRE
   - **customer portal** para B2B customers + partners (multi-tenant)
3. **Simulación de EKS** vía k3d, **simulación de RDS** vía Postgres en
   docker — todo reproducible desde cero en una sola máquina.
4. **Onboarding de usuarios externos** sin tocar el plano de control.

### Non-goals

- No migrar producción real.
- No usar nombres reales (Axial, CerberusByte, Helios, etc.).
- No implementar alta disponibilidad ni autoscaling (single-node k3d basta).
- No provisionar infraestructura cloud real — todo corre en docker local.

## 2. Top-level architecture

```
                                   Internet
                                      │
                                      ▼
                          ┌─────────────────────┐
                          │   Reverse proxy     │
                          │   (traefik :8080)   │ ← compartido por los 3 portales
                          └──────────┬──────────┘
                                     │
        ┌────────────────────────────┼────────────────────────────┐
        │                            │                            │
        ▼                            ▼                            ▼
┌──────────────┐            ┌──────────────┐            ┌──────────────┐
│   k3d        │            │   k3d        │            │   k3d        │
│   cluster    │            │   cluster    │            │   cluster    │
│   "polaris"  │            │   "polaris"  │            │   "polaris"  │
│              │            │              │            │              │
│ namespace:   │            │ namespace:   │            │ namespace:   │
│  pol-        │            │  pol-        │            │  pol-        │
│  intranet    │            │  grafana     │            │  customer    │
│              │            │              │            │              │
│ ┌──────────┐ │            │ ┌──────────┐ │            │ ┌──────────┐ │
│ │ intranet │ │            │ │ grafana  │ │            │ │ customer │ │
│ │ Flask+JS │ │            │ │ 10.x     │ │            │ │ portal   │ │
│ │ :3000    │ │            │ │ :3000    │ │            │ │ :3000    │ │
│ └────┬─────┘ │            │ └────┬─────┘ │            │ └────┬─────┘ │
│      │ tailscale sidecar  │      │ tailscale sidecar  │      │ tailscale sidecar
│      │ tag:pol-intranet   │      │ tag:pol-grafana    │      │ tag:pol-customer-{tenant}
└──────┼─────────────────────┴──────┼─────────────────────┴──────┼─────────────────────┘
       │                            │                            │
       └────────────────────────────┼────────────────────────────┘
                                    │
                                    ▼
                  ┌────────────────────────────────────┐
                  │       Tailscale/Headscale         │
                  │       control plane                │
                  │       (MagicDNS suffix:           │
                  │        polaris.ts.net)            │
                  └────────────────┬───────────────────┘
                                   │
       ┌───────────────────────────┼───────────────────────────┐
       │                           │                           │
       ▼                           ▼                           ▼
┌──────────────┐            ┌──────────────┐            ┌──────────────┐
│  Keycloak    │            │   Postgres   │            │  Per-user    │
│  :8081       │            │   (RDS sim)  │            │  sidecar     │
│              │            │   :5432      │            │  devices     │
│ OIDC issuer  │            │              │            │              │
│ pol-intranet │            │ pol_intranet │            │ employees,   │
│ pol-grafana  │            │ pol_grafana  │            │ ops, B2B     │
│ pol-customer │            │ pol_customer │            │ partners     │
└──────────────┘            └──────────────┘            └──────────────┘
```

Texto:

- **k3d** corre un cluster Kubernetes con 1 server + 1 agent node.
  Tres namespaces (`pol-intranet`, `pol-grafana`, `pol-customer`) corren
  los portales como Deployments, cada uno con un sidecar de Tailscale.
- **Headscale** (self-hosted) es el control plane de Tailscale; los tags
  de los sidecars determinan qué pueden ver.
- **Keycloak** es el IDP. OIDC emisor para los 3 portales; emisor
  también para los tailscale-tag-sync (claims → tags).
- **Postgres** corre como docker container, una DB por portal.
- **Reverse proxy único** (traefik) en el host, rutea por hostname
  (`intranet.polaris.ts.net`, `grafana.polaris.ts.net`,
  `customer.polaris.ts.net`).

## 3. Componentes y servicios

### 3.1 Identity — Keycloak (`keycloak:24`)

| Realm | Clients | Groups | Roles |
| --- | --- | --- | --- |
| `polaris` | `pol-intranet`, `pol-grafana`, `pol-customer`, `pol-tailscale` (sync) | `pol-employees`, `pol-eng`, `pol-ops`, `pol-admin`, `pol-sales`, `pol-customer-tenant-acme`, `pol-customer-tenant-brightside`, `pol-partners`, `pol-partners-northwind` | `read`, `write`, `admin` (per-client) |

**Flujo de provisioning de un usuario**:

1. Admin entra al Keycloak Admin Console (`http://localhost:8081`).
2. Crea el usuario con username `first.last@pol-intranet` (o
   `ext.last@partner-northwind`).
3. Asigna el usuario a uno o más `Groups`.
4. Por cada grupo, Keycloak emite un claim `groups` en el JWT con el
   nombre del grupo.

**OIDC → Tailscale tag mapping** (vía `tailscale tag-sync`):

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

Esto se materializa con `tsidp` (Tailscale IDP proxy) o un sync manual
`headscale nodes tag --identifier=<email> --tags=tag:<g>`. Para el POC
arrancamos con **sync manual** vía script de admin; tsidp queda como
nice-to-have para fase 3.

### 3.2 Network — Headscale + tailscale sidecars

Tags por namespace:

| Namespace | Tailscale tag | MagicDNS suffix | Acceso TCP |
| --- | --- | --- | --- |
| `pol-intranet` | `tag:pol-intranet` | `intranet.polaris.ts.net` | `:3000` (web) |
| `pol-grafana` | `tag:pol-grafana` | `grafana.polaris.ts.net` | `:3000` (web) |
| `pol-customer` | `tag:pol-customer-{tenant}` | `customer.polaris.ts.net` | `:3000` (web), `:5432` (admin) |

ACL policy (en `acl/policy.hujson`):

```jsonc
{
  "tagOwners": {
    "tag:pol-intranet":              ["group:pol-admin"],
    "tag:pol-grafana":               ["group:pol-admin"],
    "tag:pol-customer-acme":         ["group:pol-admin"],
    "tag:pol-customer-brightside":   ["group:pol-admin"],
    "tag:pol-partners-northwind":    ["group:pol-admin"]
  },
  "acls": [
    // empleados pueden leer intranet y grafana
    { "action": "accept",
      "src":    ["tag:pol-employees"],
      "dst":    ["tag:pol-intranet:80", "tag:pol-grafana:80"] },

    // ops tiene write en grafana
    { "action": "accept",
      "src":    ["tag:pol-ops"],
      "dst":    ["tag:pol-grafana:80"] },

    // customer-tenant-acme solo ve su portal y su DB
    { "action": "accept",
      "src":    ["tag:pol-customer-acme"],
      "dst":    ["tag:pol-customer-acme:80", "tag:pol-customer-acme:5432"] },

    // partner-northwind solo ve su portal
    { "action": "accept",
      "src":    ["tag:pol-partners-northwind"],
      "dst":    ["tag:pol-customer-partners:80"] },

    // admin todo
    { "action": "accept",
      "src":    ["tag:pol-admin"],
      "dst":    ["tag:*"] }
  ],
  "ssh": [
    // ops puede SSH bastion en k3d nodes para debug
    { "action": "accept",
      "src":    ["tag:pol-ops"],
      "dst":    ["tag:pol-eng:22"],
      "users":  ["root"] }
  ]
}
```

### 3.3 Compute — k3d cluster

Cluster: `polaris-eks-sim`

- 1 server + 1 agent (suficiente para el POC).
- Inicia con `--registry-create` para empujar imágenes locales rápido.
- Traefik ingress controller (built-in en k3d image).
- 3 namespaces + RBAC por namespace.

### 3.4 Data — Postgres

| DB | Owner | Usuarios | Notas |
| --- | --- | --- | --- |
| `pol_intranet` | `pol_intranet_app` | `pol_intranet_ro`, `pol_intranet_rw` | directorio, wiki, anuncios |
| `pol_grafana` | `grafana` | (default) | metadata, dashboards |
| `pol_customer` | `pol_customer_app` | un user por tenant | tenant data |

Postgres no se expone al tailnet salvo para el `tag:pol-customer-{tenant}`
específico (ver ACL arriba). El `intranet` y `grafana` leen de su propia
DB vía service name del cluster, no vía tailnet.

### 3.5 Portales

#### 3.5.1 Intranet (`pol-intranet`)

- Stack: Python Flask + plantilla server-side (o React si se quiere más
  rico, pero Flask alcanza para el POC).
- Endpoints:
  - `GET /` — home con logo Polaris, links a Grafana + Customer Portal
  - `GET /directory` — tabla con empleados (id, email, group, location)
  - `GET /wiki` — markdown renderizado desde `wiki/` en el repo
  - `GET /healthz` — JSON status
- Auth: OIDC contra Keycloak (client `pol-intranet`).
- Authorization: cualquier usuario en `pol-employees` puede leer.

#### 3.5.2 Grafana (`pol-grafana`)

- Stack: Grafana 10.x oficial.
- Auth: OIDC contra Keycloak (client `pol-grafana`).
- Role mapping en Grafana config:
  - `pol-admin` → Grafana Admin
  - `pol-ops` → Editor
  - otros grupos → Viewer (read-only)
- Datasources preconfigurados:
  - Postgres `pol_grafana` (metadata)
  - Prometheus (opcional, si se agrega monitoring stack)
- Dashboards seed: 1 dashboard por namespace (`pol-intranet`,
  `pol-grafana`, `pol-customer`).

#### 3.5.3 Customer Portal (`pol-customer`)

- Stack: Flask con render multi-tenant.
- Endpoints:
  - `GET /` — landing con info del tenant (basado en `groups` claim)
  - `GET /data` — vista tabular de la DB del tenant
  - `GET /files` — lista archivos del bucket S3 del tenant (proxy a MinIO)
  - `POST /api/upload` — sube archivos al bucket (placeholder, no usado en POC)
- Auth: OIDC contra Keycloak (client `pol-customer`).
- Tenant resolution: del claim `groups` se extrae
  `pol-customer-tenant-{name}` y se filtra la query SQL por ese nombre.
- Aislamiento:
  - NetworkPolicy en k3d rechaza tráfico cross-tenant en el namespace
  - ACL en Headscale separa los tags por tenant
  - SQL: WHERE `tenant = current_setting('app.tenant')` para evitar
    data leaks si un usuario cambia de tag
  - MinIO IAM policies: el bucket policy + STS temporal credentials
    emitidos por Keycloak OIDC limitan qué archivos puede listar/subir
    cada tenant

### 3.6 Storage — MinIO como S3 sim con OIDC contra Keycloak

Elegimos **MinIO** sobre LocalStack porque:

- Binario único, imagen ~150 MB (LocalStack pesa ~1.5 GB y arranca 30+ s)
- Soporta OIDC nativo desde RELEASE.2022-12-02 (Keycloak es first-class)
- Misma API que S3 (`s3://`, `aws s3 cp`, `boto3` sin cambios)
- STS-style temporary credentials basadas en el JWT de Keycloak

```
+-------------------------------------------+
|  MinIO  (minio/minio:RELEASE.2024-...)    |
|                                           |
|  OIDC config:                             |
|    issuer = http://keycloak:8081/realms/polaris
|    client_id = pol-minio                  |
|    claim_name = groups                    |
|                                           |
|  Policies (MinIO IAM):                    |
|    pol-data-employees-readwrite           |
|    pol-data-ops-admin                     |
|    pol-data-acme-readwrite                |
|    pol-data-brightside-readwrite          |
|    pol-data-partners-readonly             |
|                                           |
|  Bucket → policy mapping:                 |
|    pol-data-employees  → ...-readwrite    |
|    pol-data-ops        → ...-admin        |
|    pol-data-acme       → ...-readwrite    |
|    pol-data-brightside → ...-readwrite    |
|    pol-data-partners   → ...-readonly     |
|                                           |
|  Group claim → policy mapping:            |
|    pol-employees                → pol-data-employees-readwrite
|    pol-ops                      → pol-data-ops-admin
|    pol-admin                    → (all)
|    pol-customer-tenant-acme     → pol-data-acme-readwrite
|    pol-customer-tenant-brightside → pol-data-brightside-readwrite
|    pol-partners-northwind       → pol-data-partners-readonly
+-------------------------------------------+
```

**Flujo end-to-end**:

1. Usuario abre `https://intranet.polaris.ts.net/files`.
2. Portal detecta que el endpoint requiere credenciales S3 y redirige al
   usuario al flow OIDC contra Keycloak (ya autenticado si SSO activo).
3. Portal pide a MinIO `AssumeRoleWithWebIdentity` con el JWT de Keycloak
   (`https://keycloak:8081/realms/polaris` como issuer).
4. MinIO valida el JWT contra Keycloak, extrae el claim `groups`,
   mapea a MinIO policies según la tabla de arriba, y devuelve
   access/secret key temporales (STS, duran 1 h por default).
5. Portal usa las credenciales STS para listar/subir archivos
   (`aws s3 ls s3://pol-data-employees/`).
6. Cada request S3 es evaluada por el bucket policy + IAM policy en
   MinIO → si la policy no incluye el path, denegado.

**Diagrama**:

```
   user browser                    k3d pod               MinIO            Keycloak
        │                              │                    │                  │
        │ open /files                 │                    │                  │
        ├─────────────────────────────►                    │                  │
        │                              │ redirect to OIDC  │                  │
        │                              ├──────────────────────────────────────►
        │                              │◄─────── JWT (groups) ────────────────
        │                              │ AssumeRoleWithWebIdentity             │
        │                              ├───────────────────►                   │
        │                              │                   validate JWT ──────►│
        │                              │                   resolve groups ───►│
        │                              │◄───── STS creds (1h) ─────────────────
        │                              │                                       │
        │                              │ aws s3 ls (using STS creds)          │
        │                              ├───────────────────►                   │
        │                              │ bucket policy check                  │
        │                              │ IAM policy check (groups→policy)     │
        │                              │◄──── 200 OK or 403 ──────────────────
        │◄────── HTML with file list ──┤                                       │
```

**Tag/ACL additions**:

```jsonc
// acl/policy.hujson — addendum
{
  "tagOwners": {
    "tag:pol-minio": ["group:pol-admin"]   // solo admin puede mover el tag del proxy
  },
  "acls": [
    // todos los sidecars (incluidos los customer) pueden hablarle al MinIO proxy
    // MinIO es el ÚNICO endpoint expuesto al tailnet que NO está particionado
    // por tenant: el aislamiento se hace en MinIO IAM, no en la red.
    { "action": "accept",
      "src":    ["tag:pol-employees", "tag:pol-ops", "tag:pol-admin",
                 "tag:pol-customer-acme", "tag:pol-customer-brightside",
                 "tag:pol-partners-northwind"],
      "dst":    ["tag:pol-minio:9000"] },

    // admin tiene el puerto de MinIO console (UI de admin) para crear buckets
    { "action": "accept",
      "src":    ["tag:pol-admin"],
      "dst":    ["tag:pol-minio:9001"] }
  ]
}
```

**Por qué este split**:
- La capa Tailscale ACL decide si el request llega a MinIO (network layer).
- MinIO IAM decide qué bucket/key puede ver el usuario (object layer).
- Dos capas independientes: si una falla, la otra todavía contiene.

### 3.7 Bucket layout

| Bucket | Owner | Tenant policy | Notas |
| --- | --- | --- | --- |
| `pol-data-employees` | `pol-admin` | readwrite por `pol-employees` | wiki/uploads, anuncios, attachments |
| `pol-data-ops` | `pol-admin` | admin por `pol-ops` | logs dumps, k8s manifests, runbooks |
| `pol-data-acme` | `pol-admin` | readwrite por `pol-customer-tenant-acme` | data del tenant Acme |
| `pol-data-brightside` | `pol-admin` | readwrite por `pol-customer-tenant-brightside` | data del tenant Brightside |
| `pol-data-partners` | `pol-admin` | readonly por `pol-partners-northwind` | data compartida con partners |

Cada bucket tiene un objeto seed (`README.md` con info del tenant) para
verificar en el walkthrough que el aislamiento funciona.

## 4. Onboarding flows

### 4.1 Empleado interno (e.g., `ada@polaris.example`)

```
1. admin entra a Keycloak admin console
2. crea usuario ada@polaris.example, password temporal
3. asigna grupos: pol-employees + pol-eng
4. ada corre:
   $ tailscale login --login-server=http://headscale:8080
   → SSO via Keycloak (client pol-tailscale)
   → claims `groups` llegan al control plane
   → headscale nodes tag --identifier=ada@... --tags='tag:pol-employees,tag:pol-eng'
5. ada entra a intranet.polaris.ts.net → OIDC contra pol-intranet
   → ya está autenticada, ve el directorio
```

### 4.2 Cliente B2B (`ext.alice@acme.example`)

```
1. admin crea el usuario en Keycloak con grupo pol-customer-tenant-acme
2. admin crea un preauth key tagged tag:pol-customer-acme
   $ headscale preauthkeys create --user acme-admin \
       --reusable --expiration 168h \
       --tags tag:pol-customer-acme
3. alice descarga tailscale + registra con la key
4. magicDNS resuelve customer.polaris.ts.net → 100.64.0.50 (sidecar)
5. alice entra a customer.polaris.ts.net → OIDC contra pol-customer
   → claim groups=pol-customer-tenant-acme
   → portal filtra y muestra solo los datos de acme
```

### 4.3 Partner (`ext.bob@northwind.example`)

Igual al B2B pero con `tag:pol-partners-northwind`. Acceso más limitado:
solo lectura sobre un set de endpoints pre-aprobados.

## 5. Tag matrix (resumen)

| Tag | Quién | Qué puede |
| --- | --- | --- |
| `tag:pol-employees` | empleados en `pol-employees` | intranet:80, grafana:80, minio:9000 (sts), bucket pol-data-employees (rw) |
| `tag:pol-eng` | ingenieros | intranet:80, k3d bastion:22 |
| `tag:pol-ops` | SRE / ops | grafana:80 (write), intranet:80, k3d bastion:22, minio:9000, bucket pol-data-ops (admin) |
| `tag:pol-admin` | admins (1-2 personas) | todo (incluye minio:9001 console) |
| `tag:pol-customer-acme` | usuarios de Acme | customer portal Acme, DB Acme, minio:9000, bucket pol-data-acme (rw) |
| `tag:pol-customer-brightside` | usuarios de Brightside | customer portal Brightside, DB Brightside, minio:9000, bucket pol-data-brightside (rw) |
| `tag:pol-partners-northwind` | usuarios de Northwind | customer portal Partners, minio:9000, bucket pol-data-partners (ro) |
| `tag:pol-minio` | solo el MinIO proxy (k8s service) | exporta la API S3 al tailnet |

Default-deny: cualquier tag no listado denegado a todo.

## 6. File map (target — fase 3, después de aprobación)

```
polaris/
├── README.md                        quickstart
├── docker-compose.yml               keycloak + postgres + headscale + minio
├── docker-compose.k3d.yml           (alternativa) k3d + portales
├── k3d/
│   ├── cluster.yaml                 1 server + 1 agent
│   └── apply.sh                     crea cluster + namespaces + ingress
├── charts/
│   ├── intranet/                    Helm chart (Deployment + Service + tailscale sidecar)
│   ├── grafana/                     Helm chart
│   ├── customer/                    Helm chart (multi-tenant aware)
│   └── minio/                       Helm chart (StatefulSet + 1 PVC + Service + sidecar)
├── acl/
│   └── policy.hujson                tagOwners + acls + ssh
├── apps/
│   ├── intranet/                    Flask app
│   ├── grafana-provisioning/        datasources, dashboards, oidc config
│   └── customer/                    Flask app (multi-tenant)
├── keycloak/
│   ├── realm-export.json            realm preconfig (groups, clients, users)
│   └── seed-users.sh                bootstrap admin + 3-4 demo users
├── headscale/
│   └── config.yaml                  control plane config
├── minio/
│   ├── oidc-config.env              OIDC settings para MinIO
│   ├── policies.json                5 IAM policies (employees/ops/admin/acme/brightside/partners)
│   ├── bucket-policies.json         5 bucket policies
│   └── seed-buckets.sh              crea los 5 buckets + sube README.md a cada uno
├── postgres/
│   └── init.sql                     schemas + seed data
├── notes/
│   └── walkthrough.md               paso a paso end-to-end
└── docs/
    ├── design.md                    ← este archivo
    └── diagrams/
        ├── architecture.mmd
        ├── idp-flow.mmd
        ├── tag-matrix.mmd
        └── iam-flow.mmd             secuencia MinIO STS + Keycloak OIDC
```

## 7. Riesgos y open questions

### 7.1 Riesgos

| # | Riesgo | Mitigación |
| --- | --- | --- |
| R1 | k3d + tailscale sidecar: cada pod necesita `/dev/net/tun` y permisos NET_ADMIN | usar `tailscale/k8s-operator` o chart oficial; para POC, init container con hostPath |
| R2 | Keycloak `groups` claim llega al control plane pero el mapping a tags es manual | empezar con sync manual (`headscale nodes tag`); tsidp como follow-up |
| R3 | Customer data isolation: SQL injection o bug en portal filtra data cross-tenant | NetworkPolicy + ACL + `SET LOCAL app.tenant` en cada query |
| R4 | MagicDNS suffix collision con otros POCs corriendo en paralelo | usar `polaris.ts.net` y stop los POCs anteriores antes |
| R5 | k3d consume mucha RAM (~4 GB) junto con keycloak + postgres + headscale + minio | usar `--memory=2G` por container, total ~10 GB; documentar mínimo 16 GB host |
| R6 | Tailscale sidecars dentro de k3d pods no pueden ver el `/dev/net/tun` del host por defecto | instalar k3d con `--k3s-arg="--kubelet-arg=feature-gates=KernelTun=true"` o usar `tailscale/k8s-operator` |
| R7 | MinIO OIDC + Keycloak: el claim `groups` puede llegar con prefijo del realm path (`/pol-employees`) y romper el policy mapping | configurar `claim_name = groups` Y `claim_prefix = ""` en MinIO; verificar con un user de prueba antes de asumir que funciona |
| R8 | STS credentials tienen TTL (default 1h); reauth SSO debería ser transparente pero a veces no | usar session cookies + OIDC refresh token en el portal; minimo impacto en POC con TTL 12h |
| R9 | Bucket policy + IAM policy = dos lugares donde definir permisos; pueden divergir | las IAM policies son el "source of truth" y los bucket policies solo restringen paths específicos (deny por defecto si IAM no permite) |

### 7.2 Resolved decisions (round 1)

| # | Pregunta | Decisión | Por qué |
| --- | --- | --- | --- |
| # | Pregunta | Decisión | Por qué |
| --- | --- | --- | --- |
| 1 | OIDC vs sync manual para Keycloak→Tailscale tags | **sync manual** vía `headscale nodes tag` | ~30 líneas de Python; tsidp queda como follow-up posterior |
| 2 | Schema-per-tenant vs RLS en Postgres | **RLS con `SET LOCAL app.tenant`** | SQL estándar, tests de aislamiento más fáciles; schema-per-tenant agrega complejidad de migraciones |
| 3 | Customer portal REST vs HTML | **solo HTML** para el POC | REST + API key per tenant se agrega más adelante cuando se valide el flujo de UI |
| 4 | Monitoring skeleton | **skip Prometheus**, Grafana con Postgres datasource | no necesitamos alerting para el POC; Grafana ya tiene metadata DB propia |
| 5 (nueva) | IAM + S3 sim | **MinIO con OIDC contra Keycloak** | lightweight, OIDC first-class, STS-style creds; LocalStack es overkill para POC |

## 8. Plan de replicación (cuando aprobemos)

1. **Etapa 1 — Stack base**: docker-compose con Keycloak + Postgres +
   Headscale + MinIO. Levantar, verificar healthchecks, configurar
   MinIO OIDC contra Keycloak, crear 5 buckets con sus políticas IAM.
2. **Etapa 2 — Cluster kind (simulando EKS)**: levantar el cluster,
   crear namespaces, deployar Traefik. Verificar que un pod puede
   resolver `headscale` y registrar un sidecar. Verificar que el pod
   puede hablarle a MinIO via tailnet (STS test).
3. **Etapa 3 — Portal intranet**: Flask + OIDC + Keycloak + endpoint
   `/files` con proxy STS a MinIO. Walkthrough end-to-end con login SSO.
4. **Etapa 4 — Portal Grafana**: OIDC + role mapping + datasources.
   Walkthrough con `pol-admin` y `pol-employees`.
5. **Etapa 5 — Portal customer multi-tenant**: aislamiento SQL RLS +
   ACL + MinIO IAM por tenant. Walkthrough con Acme + Brightside +
   Northwind.
6. **Etapa 6 — Documentación**: RUN_REPORT.md + RUN_REPORT.docx con
   las 6 capturas de cada portal + walkthrough final.

Cada día termina con un walkthrough reproducible desde cero
(`docker compose down -v && ... up -d --build && ...`) y screenshots.

## 9. Cómo aprobar / cambiar

Si algo no cierra, lo más rápido es:

- **Editar este archivo** (las secciones 2, 3, 5 son las que más
  probablemente cambien).
- Re-empezar el ciclo de revisión.

Cuando esté firmado, mover a `docs/design-final.md` y arrancar fase 3.