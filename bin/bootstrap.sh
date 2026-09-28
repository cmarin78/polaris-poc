#!/usr/bin/env bash
# polaris POC — single-command bootstrap.
#
# From a fresh checkout on a clean Linux host (with docker, kind,
# kubectl, and the cached container images available), `bin/bootstrap.sh`
# brings up:
#   - Headscale, Keycloak, Postgres, MinIO (docker-compose)
#   - MinIO bootstrap container (5 buckets + 6 IAM policies)
#   - kind cluster (1 control-plane + 1 worker) connected to the
#     docker-compose network so pods can resolve service hostnames
#   - Traefik ingress controller
#   - intranet / grafana / customer portals
#   - Headscale tagOwners + ACL policy
#
# Idempotent: re-running restarts only what changed. Safe to interrupt
# with Ctrl-C and resume.
set -euo pipefail
cd "$(dirname "$0")/.."
REPO_ROOT="$(pwd)"

log() { printf "\033[1;34m[bootstrap]\033[0m %s\n" "$*"; }
warn() { printf "\033[1;33m[bootstrap]\033[0m %s\n" "$*"; }
die() { printf "\033[1;31m[bootstrap]\033[0m %s\n" "$*" >&2; exit 1; }

# ----- preflight ---------------------------------------------------------
command -v docker        >/dev/null || die "docker not installed"
command -v kind          >/dev/null || die "kind not installed"
command -v kubectl       >/dev/null || die "kubectl not installed"
command -v docker-compose >/dev/null || command -v "docker compose" >/dev/null || die "docker compose not installed"
command -v headscale     >/dev/null || warn "headscale CLI not on PATH — Step 8 will be skipped"
[ -f .env ] || [ -f .env.example ] || die "neither .env nor .env.example present"

# ----- 1. start base stack -----------------------------------------------
log "1/8 docker compose up -d"
docker compose -f docker-compose.yml up -d

# ----- 2. wait for services healthy -------------------------------------
log "2/8 wait for services healthy"
deadline=$((SECONDS + 120))
while [ $SECONDS -lt $deadline ]; do
  if docker compose -f docker-compose.yml ps --format json 2>/dev/null | \
       grep -q '"State":"unhealthy"'; then
    sleep 3; continue
  fi
  if docker compose -f docker-compose.yml ps 2>/dev/null | \
       grep -E "(headscale|keycloak|minio|postgres).*healthy" >/dev/null; then
    break
  fi
  sleep 3
done
docker compose -f docker-compose.yml ps | grep -E "(headscale|keycloak|minio|postgres)" | grep healthy >/dev/null \
  || die "services did not become healthy in time"

# ----- 3. minio bootstrap (buckets + IAM) -------------------------------
log "3/8 minio-bootstrap (5 buckets + 6 IAM policies)"
docker compose -f docker-compose.yml run --rm minio-bootstrap | tail -5

# ----- 4. kind cluster + docker network bridge --------------------------
log "4/8 kind cluster polaris-eks-sim"
if ! kind get clusters 2>/dev/null | grep -q polaris-eks-sim; then
  kind create cluster --name polaris-eks-sim --config k3d/cluster.yaml
else
  log "   kind cluster already exists, skipping create"
fi
NETWORK="${REPO_ROOT##*/}_polaris_default"
NETWORK="polaris_polaris_default"  # docker-compose v2 namespacing
for node in polaris-eks-sim-control-plane polaris-eks-sim-worker; do
  if ! docker network inspect "$NETWORK" >/dev/null 2>&1 | grep -q "$node"; then
    log "   connect $node -> $NETWORK"
    docker network connect "$NETWORK" "$node" 2>/dev/null || \
      warn "   $node already connected or network not found"
  fi
done

# ----- 5. build + load images --------------------------------------------
log "5/8 build + load images into kind"
for app in intranet grafana customer; do
  if [ ! -f "apps/$app/Dockerfile" ]; then
    warn "   apps/$app/Dockerfile missing, skipping"; continue
  fi
  img="polaris-$app:latest"
  if ! docker images --format '{{.Repository}}:{{.Tag}}' | grep -q "^$img$"; then
    log "   build $img"
    docker build -t "$img" "apps/$app/" | tail -2
  else
    log "   $img already built"
  fi
  log "   load $img into kind"
  kind load docker-image "$img" --name polaris-eks-sim | tail -1
done

# ----- 6. apply k8s manifests --------------------------------------------
log "6/8 apply traefik + 3 portals"
kubectl --context=kind-polaris-eks-sim apply -f k3d/traefik.yaml
for app in intranet grafana customer; do
  kubectl --context=kind-polaris-eks-sim apply -f "charts/$app/deployment.yaml"
done
kubectl --context=kind-polaris-eks-sim rollout status deploy/customer -n pol-customer --timeout=60s 2>&1 | tail -1 || true
kubectl --context=kind-polaris-eks-sim rollout status deploy/intranet -n pol-intranet --timeout=60s 2>&1 | tail -1 || true
kubectl --context=kind-polaris-eks-sim rollout status deploy/grafana  -n pol-grafana   --timeout=60s 2>&1 | tail -1 || true

# ----- 7. Headscale policy -----------------------------------------------
log "7/8 headscale policy set"
if command -v headscale >/dev/null; then
  headscale --config headscale/config.yaml policy set --file acl/policy.hujson 2>&1 | tail -3 \
    || warn "   policy set failed (headscale may not be reachable from this host)"
else
  warn "   headscale CLI missing; apply the policy manually from the headscale container:"
  warn "   docker exec polaris-poc-headscale-1 headscale policy set --file /etc/headscale/acl/policy.hujson"
fi

# ----- 8. final summary --------------------------------------------------
log "8/8 done"
cat <<EOF

  ┌───────────────────────────────────────────────────────────────┐
  │  Polaris POC is up.                                           │
  │                                                               │
  │  Services:                                                    │
  │    headscale    http://localhost:28080                        │
  │    keycloak     http://localhost:8081  (admin: admin/polaris) │
  │    postgres     localhost:5433           (postgres: polaris)  │
  │    minio API    http://localhost:9000   (S3)                  │
  │    minio UI     http://localhost:9001   (polaris_admin/polaris123) │
  │                                                               │
  │  Portals (port-forward from host):                            │
  │    intranet     kubectl port-forward -n pol-intranet svc/intranet 13000:80 │
  │    grafana      kubectl port-forward -n pol-grafana  svc/grafana  13001:80 │
  │    customer     kubectl port-forward -n pol-customer svc/customer 13002:80 │
  │                                                               │
  │  Test users (all password = polaris):                          │
  │    alice / bob / carol                  (employees)            │
  │    alice-acme / bob-brightside          (customer tenants)     │
  │    carol-northwind                      (partner)              │
  │                                                               │
  │  Verify:                                                      │
  │    python3 tests/test_e2e.py                              │
  │                                                               │
  │  See RUN_REPORT.md for the full day-by-day build log.         │
  └───────────────────────────────────────────────────────────────┘
EOF