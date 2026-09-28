# Polaris POC — Reproducibility Guide

A flat, ordered list of every task needed to bring the POC up from a
clean Linux host. Each task lists the command(s), the expected output,
and a short explanation of what the command does and what the output
means. The order matters; later tasks depend on the earlier ones.

If you want one-shot bring-up, run `make bootstrap` instead — it does
tasks 1 through 8 in a single shell. This document exists so each
step can be inspected and verified independently.

## Prerequisites

Required on the host:

- Linux with kernel >= 5.x (for the WireGuard module used by tailscale)
- `docker` >= 24 with `docker compose` v2
- `kind` >= 0.22 (kubernetes-in-docker)
- `kubectl` >= 1.30
- `python3` >= 3.11 with `boto3`, `psycopg2-binary`, `playwright`
  installed in a venv
- 8 GB of free RAM and ~10 GB of free disk (kind cluster + image
  cache + Postgres + MinIO data volume)
- The following images pre-pulled (this host cannot reach `ghcr.io`
  or `dl.min.io`):
  - `quay.io/keycloak/keycloak:24.0`
  - `headscale/headscale:stable` (v0.29.4)
  - `kindest/node:v1.30.0`
  - `quay.io/minio/minio:RELEASE.2024-01-18T22-51-28Z`

## Task 1 — Start the base docker-compose stack

Brings up Headscale, Keycloak, Postgres, and MinIO on the host. Each
service has a healthcheck; the next task waits for them.

```bash
cd polaris
docker compose -f docker-compose.yml up -d
```

```text
[+] Running 5/5
 ✔ Network polaris_default         Created                                  0.1s
 ✔ Container polaris-poc-headscale-1  Started                              1.2s
 ✔ Container polaris-poc-keycloak-1   Started                              2.4s
 ✔ Container polaris-poc-postgres-1   Started                              0.9s
 ✔ Container polaris-poc-minio-1      Started                              1.5s
 ✔ Container polaris-poc-bootstrap-1  Started                              0.8s
```

Each line is a container. The bootstrap container finishes quickly
(it loads the realm JSON into Keycloak and creates the MinIO
buckets/IAM); the other four run as long-lived services.

## Task 2 — Wait for the services to become healthy

Keycloak takes 30–60 seconds to start the first time (it imports the
realm and runs its startup probe). Postgres, Headscale, and MinIO are
faster.

```bash
for svc in headscale keycloak postgres minio; do
  printf "waiting for %s ... " "$svc"
  until docker compose -f docker-compose.yml ps "$svc" 2>/dev/null | grep -q healthy; do
    sleep 3
  done
  echo "healthy"
done
```

```text
waiting for headscale ... healthy
waiting for keycloak ... healthy
waiting for postgres ... healthy
waiting for minio ... healthy
```

Each line confirms that the healthcheck for the corresponding service
is passing. `headscale` is healthy when the API listens on
`http://localhost:28080`. `keycloak` is healthy when
`/realms/polaris/.well-known/openid-configuration` returns 200.
`postgres` is healthy when `pg_isready` succeeds.
`minio` is healthy when its `/minio/health/ready` returns 200.

## Task 3 — Run the MinIO bootstrap (buckets + IAM policies)

Creates five buckets and six IAM policies that map each Keycloak
group to its bucket. The script is idempotent — safe to re-run.

```bash
docker compose -f docker-compose.yml run --rm minio-bootstrap
```

```text
[buckets] creating pol-data-employees
[buckets] creating pol-data-ops
[buckets] creating pol-data-acme
[buckets] creating pol-data-brightside
[buckets] creating pol-data-partners
[iam]     creating policy pol-employees-rw
[iam]     creating policy pol-ops-rw
[iam]     creating policy pol-admin-rw
[iam]     creating policy pol-customer-acme-rw
[iam]     creating policy pol-customer-brightside-rw
[iam]     creating policy pol-partners-northwind-rw
[oidc]    configuring MinIO OIDC against http://keycloak:8080/realms/polaris
[oidc]    claim-name=groups, role-claim-name=groups, audience=pol-intranet
```

The `[buckets]` lines show one bucket created per user role or tenant.
The `[iam]` lines show one IAM policy per group; each policy allows
`ListBucket`/`GetObject`/`PutObject` against the corresponding bucket
only. The `[oidc]` lines confirm that MinIO was told to validate
incoming JWTs against Keycloak's polaris realm and to map the
`groups` claim to a policy.

## Task 4 — Create the kind cluster and connect it to the docker network

A one-control-plane + one-worker cluster named `polaris-eks-sim`.
The two extra steps (`docker network connect …`) are what let pods
inside the cluster reach the base stack by hostname (`keycloak`,
`minio`, `postgres`, `headscale`) — kind nodes join the docker-compose
network so the docker-compose DNS resolver sees them.

```bash
kind create cluster --name polaris-eks-sim --config k3d/cluster.yaml
docker network connect polaris_polaris_default polaris-eks-sim-control-plane
docker network connect polaris_polaris_default polaris-eks-sim-worker
```

```text
Creating cluster "polaris-eks-sim" ...
 ✓ Ensuring node image (kindest/node:v1.30.0) 🖼
 ✓ Preparing nodes 📦 📦
 ✓ Writing configuration 📜
 ✓ Starting control-plane 🕹️
 ✓ Installing CNI 🔌
 ✓ Installing StorageClass 💾
 ✓ Joining worker nodes 🚀
Set kubectl context to "kind-polaris-eks-sim"
```

`kind` output ends with `Set kubectl context to …`. Subsequent
`kubectl` commands use `--context=kind-polaris-eks-sim`. The two
`docker network connect` calls have no output on success.

## Task 5 — Build the three portal images

Each portal is a single Flask (intranet, customer) or Grafana image
that the kind cluster will pull from the host's docker daemon via
`kind load docker-image` (next task).

```bash
docker build -t polaris-intranet:latest apps/intranet/
docker build -t polaris-grafana:latest  apps/grafana/
docker build -t polaris-customer:latest apps/customer/
```

```text
#1 [internal] load build definition from Dockerfile
#1 transferring dockerfile: 32B
#1 transferring context: ...
 ...
#9 naming to docker.io/library/polaris-intranet:latest
#9 DONE 0.1s
```

Each `docker build` ends with a `naming to docker.io/library/...`
line confirming the tag. The intranet and customer Dockerfiles
extend `python:3.12-slim` and add Flask + gunicorn + boto3 +
psycopg2-binary. The grafana Dockerfile extends the tailscale-grafana
base and adds provisioning for the `polaris-postgres` datasource and
the OIDC settings.

## Task 6 — Load the images into the kind cluster

`kind load docker-image` copies each image from the host's docker
daemon into the kind node's containerd. The imagePullPolicy in the
charts is `IfNotPresent`, so without this step the pods would fail
to start with `ImagePullBackOff`.

```bash
kind load docker-image polaris-intranet:latest --name polaris-eks-sim
kind load docker-image polaris-grafana:latest  --name polaris-eks-sim
kind load docker-image polaris-customer:latest --name polaris-eks-sim
```

```text
Image: "polaris-intranet:latest" with ID sha256:...
 not yet present on node "polaris-eks-sim-control-plane", loading...
Image: "polaris-intranet:latest" with ID sha256:...
 not yet present on node "polaris-eks-sim-worker", loading...
Image: "polaris-grafana:latest" with ID sha256:...
 not yet present on node "polaris-eks-sim-control-plane", loading...
Image: "polaris-grafana:latest" with ID sha256:...
 not yet present on node "polaris-eks-sim-worker", loading...
Image: "polaris-customer:latest" with ID sha256:...
 not yet present on node "polaris-eks-sim-control-plane", loading...
Image: "polaris-customer:latest" with ID sha256:...
 not yet present on node "polaris-eks-sim-worker", loading...
```

Each image gets copied into both nodes (control-plane + worker). The
final lines confirm presence in the cluster's containerd.

## Task 7 — Apply the manifests

Traefik ingress first (so the ingress controller is ready before any
pod starts exposing services), then the three portal deployments.

```bash
kubectl --context=kind-polaris-eks-sim apply -f k3d/traefik.yaml
kubectl --context=kind-polaris-eks-sim apply -f charts/intranet/deployment.yaml
kubectl --context=kind-polaris-eks-sim apply -f charts/grafana/deployment.yaml
kubectl --context=kind-polaris-eks-sim apply -f charts/customer/deployment.yaml
```

```text
daemonset.apps/traefik created
deployment.apps/intranet created
service/intranet created
ingress.networking.k8s.io/intranet created
service/intranet-nodeport created
deployment.apps/grafana created
service/grafana created
ingress.networking.k8s.io/grafana created
service/grafana-nodeport created
deployment.apps/customer created
service/customer created
ingress.networking.k8s.io/customer created
service/customer-nodeport created
```

Each manifest creates one or more resources. The portal deployments
also create a `Service` (ClusterIP, used inside the cluster) and an
`Ingress` (host-based routing through Traefik) plus a `NodePort`
Service (for host access from the test browser).

Wait for the pods to be ready:

```bash
kubectl --context=kind-polaris-eks-sim wait --for=condition=ready pod \
  -l app=intranet -n pol-intranet --timeout=90s
kubectl --context=kind-polaris-eks-sim wait --for=condition=ready pod \
  -l app=grafana  -n pol-grafana  --timeout=90s
kubectl --context=kind-polaris-eks-sim wait --for=condition=ready pod \
  -l app=customer -n pol-customer --timeout=90s
```

```text
pod/intranet-77f9fc678-sfv2b condition met
pod/grafana-dd4c84848-qrwhk condition met
pod/customer-76495b45cc-8b8lw condition met
```

`condition met` confirms the readiness probe is passing for each pod.

## Task 8 — Apply the Headscale ACL policy

The policy file declares which user can register nodes with which
tag (`tagOwners`) and which tagged nodes can talk to which other
tagged nodes on which ports (`acls` and `ssh`). Without this step
the tailscale clients would have no access rules and would default to
allow (permissive).

```bash
headscale --config headscale/config.yaml policy set --file acl/policy.hujson
```

```text
Policy updated
```

A single-line confirmation. Verify the policy is loaded:

```bash
headscale --config headscale/config.yaml policy get
```

```text
{
  "tagOwners": {
    "tag:pol-intranet": ["user:polaris@"],
    ...
  },
  "acls": [
    ...
  ]
}
```

This is the same JSON that lives in `acl/policy.hujson` — `policy get`
just echoes what the server has loaded.

## Task 9 — Validate end-to-end (per user)

Each script takes a username and password, logs in via OIDC, and
exercises the relevant endpoints. The combined output for one user
looks like this:

```bash
python3 tests/test_intranet_files.py alice polaris
```

```text
=== alice (employee, no tenant group) ===
  /files: HTTP 403 (alice has no MinIO bucket policy via groups)
  /data:  HTTP 403 (alice has no tenant group; RLS rejects)
```

```bash
python3 tests/test_customer_data.py alice-acme polaris
```

```text
=== alice-acme (tenant=acme) ===
  /data: 2 rows
    contract-id: ACME-2026-001
    monthly-revenue: $ 4.2M
  /files: STS OK (G0WGQSIFG2CCTDZGF2OK...) bucket=pol-data-acme
```

```bash
python3 tests/test_grafana_role.py carol polaris
```

```text
=== carol (pol-admin) ===
  Grafana role: Admin (JMESPath from groups claim)
  Datasource polaris-postgres reachable: yes
```

Each line is a check the script performs: HTTP status code, list of
rows returned, STS temporary access key prefix, Grafana role mapping,
etc. The cross-checks together prove that OIDC, RLS, and STS all
work end-to-end for the given user.

## Task 10 — Capture the six POC screenshots

Drives a headless Chromium through the OIDC flow for all six
portal/user combinations and saves PNGs under `docs/screenshots/`.

```bash
python3 tests/capture_screenshots.py
```

```text
[01] intranet /directory as alice (employee)
  landed: http://127.0.0.1:13000/directory
  saved 01-intranet-directory-alice.png (58,500 B)

[02] grafana home as carol (pol-admin → Admin)
  landed: http://127.0.0.1:13001/?from=now-6h&to=now&timezone=browser
  saved 02-grafana-home-carol-admin.png (14,467 B)

[03-05] customer alice-acme (home + data + files)
  [03] landed: http://127.0.0.1:13002/
  saved 03-customer-home-alice-acme.png (62,224 B)
  [04] landed: http://127.0.0.1:13002/data
  saved 04-customer-data-alice-acme-acme-only.png (49,081 B)
  [05] landed: http://127.0.0.1:13002/files
  saved 05-customer-files-alice-acme.png (32,478 B)

[06] customer /data as alice (no tenant → 403)
  landed: http://127.0.0.1:13002/data
  saved 06-customer-data-alice-403-no-tenant.png (10,289 B)
```

The script also requires three `kubectl port-forward`s to be running
on `127.0.0.1:13000`, `127.0.0.1:13001`, `127.0.0.1:13002` before it
runs. The simplest way to set those up is:

```bash
nohup kubectl --context=kind-polaris-eks-sim port-forward -n pol-intranet svc/intranet 13000:80 > /tmp/pf-intranet.log 2>&1 &
nohup kubectl --context=kind-polaris-eks-sim port-forward -n pol-grafana  svc/grafana  13001:80 > /tmp/pf-grafana.log  2>&1 &
nohup kubectl --context=kind-polaris-eks-sim port-forward -n pol-customer svc/customer 13002:80 > /tmp/pf-customer.log 2>&1 &
sleep 3
curl -s -o /dev/null -w "intranet: %{http_code}\n" http://127.0.0.1:13000/healthz
curl -s -o /dev/null -w "grafana:  %{http_code}\n" http://127.0.0.1:13001/api/health
curl -s -o /dev/null -w "customer: %{http_code}\n" http://127.0.0.1:13002/healthz
```

```text
intranet: 200
grafana:  200
customer: 200
```

The three `200`s confirm the port-forwards are healthy. If any of
them returns `000`, the port-forward failed — usually because the
pod is still starting up; rerun the wait command in Task 7.

## Task 11 — Tear down

Stops the portals and the kind cluster, then stops the docker-compose
services. The Postgres volume and MinIO data volume are preserved
unless explicitly removed.

```bash
kubectl --context=kind-polaris-eks-sim delete -f charts/intranet/deployment.yaml
kubectl --context=kind-polaris-eks-sim delete -f charts/grafana/deployment.yaml
kubectl --context=kind-polaris-eks-sim delete -f charts/customer/deployment.yaml
kind delete cluster --name polaris-eks-sim
docker compose -f docker-compose.yml down
```

```text
deployment.apps "intranet" deleted
service "intranet" deleted
ingress.networking.k8s.io "intranet" deleted
service "intranet-nodeport" deleted
deployment.apps "grafana" deleted
service "grafana" deleted
ingress.networking.k8s.io "grafana" deleted
service "grafana-nodeport" deleted
deployment.apps "customer" deleted
service "customer" deleted
ingress.networking.k8s.io "customer" deleted
service "customer-nodeport" deleted
Deleting cluster "polaris-eks-sim" ...
Deleted nodes: ["polaris-eks-sim-control-plane" "polaris-eks-sim-worker"]
[+] Running 5/5
 ✔ Container polaris-poc-keycloak-1   Removed                           0.2s
 ✔ Container polaris-poc-headscale-1  Removed                           0.1s
 ✔ Container polaris-poc-postgres-1   Removed                           0.3s
 ✔ Container polaris-poc-minio-1      Removed                           0.1s
 ✔ Container polaris-poc-bootstrap-1  Removed                           0.0s
 ✔ Network polaris_default           Removed                           0.1s
```

The kind cluster deletion removes both nodes. The docker-compose
teardown removes the four long-lived services plus the bootstrap
container. Volumes are kept; run `docker volume prune` to clear them
if you want a clean slate.

## Sanity checks between tasks

If something looks wrong between tasks, the following one-liners help
isolate where the breakage is.

```bash
# Are the base services healthy?
docker compose -f docker-compose.yml ps
# Expected: headscale, keycloak, postgres, minio all 'healthy'.

# Can the kind cluster reach the base stack by hostname?
kubectl --context=kind-polaris-eks-sim run -n pol-system --rm -it --restart=Never \
  --image=curlimages/curl -- curl -sf http://keycloak:8080/realms/polaris/.well-known/openid-configuration | head -c 200
# Expected: a JSON document starting with `{"issuer":"http://keycloak:8080/realms/polaris", ...`

# Are the portal pods Ready?
kubectl --context=kind-polaris-eks-sim get pods -A -l 'app in (intranet,grafana,customer)'
# Expected: three pods, all 1/1 Running.

# Does the Headscale policy match what's on disk?
headscale --config headscale/config.yaml policy get | diff -u - acl/policy.hujson
# Expected: empty diff (no differences).
```

If any of these fails, the corresponding earlier task is incomplete
and needs to be re-run.
