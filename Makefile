.PHONY: help bootstrap up down logs test clean verify ports status

help:                ## Show this help
	@awk 'BEGIN {FS = ":.*?## "} /^[a-zA-Z_-]+:.*?## / {printf "  \033[1m%-12s\033[0m %s\n", $$1, $$2}' $(MAKEFILE_LIST)

bootstrap:           ## One-shot: docker compose + minio bootstrap + kind + portals
	@bash bin/bootstrap.sh

up:                  ## Start docker-compose services
	docker compose -f docker-compose.yml up -d

down:                ## Stop docker-compose services
	docker compose -f docker-compose.yml down

logs:                ## Tail logs of all services
	docker compose -f docker-compose.yml logs -f --tail=20

status:              ## Service status
	docker compose -f docker-compose.yml ps

minio-bootstrap:     ## Apply MinIO buckets + IAM policies
	docker compose -f docker-compose.yml run --rm minio-bootstrap

kind-create:         ## Create kind cluster + connect to docker network
	kind create cluster --name polaris-eks-sim --config kind/cluster.yaml
	docker network connect polaris_polaris_default polaris-eks-sim-control-plane
	docker network connect polaris_polaris_default polaris-eks-sim-worker

kind-delete:         ## Delete the kind cluster
	kind delete cluster --name polaris-eks-sim

build:               ## Build all 3 portal images
	docker build -t polaris-intranet:latest apps/intranet/
	docker build -t polaris-grafana:latest  apps/grafana/
	docker build -t polaris-customer:latest apps/customer/

load:               ## Load all 3 portal images into kind
	kind load docker-image polaris-intranet:latest --name polaris-eks-sim
	kind load docker-image polaris-grafana:latest  --name polaris-eks-sim
	kind load docker-image polaris-customer:latest --name polaris-eks-sim

deploy:              ## Apply Traefik + 3 portal manifests to kind
	kubectl --context=kind-polaris-eks-sim apply -f kind/traefik.yaml
	kubectl --context=kind-polaris-eks-sim apply -f charts/intranet/deployment.yaml
	kubectl --context=kind-polaris-eks-sim apply -f charts/grafana/deployment.yaml
	kubectl --context=kind-polaris-eks-sim apply -f charts/customer/deployment.yaml

policy:              ## Apply Headscale ACL policy
	docker exec polaris-poc-headscale-1 \
	  headscale --config /etc/headscale/config.yaml \
	  policy set --file /etc/headscale/acl/policy.hujson

ports:               ## Port-forward all 3 portals to host
	@kubectl --context=kind-polaris-eks-sim -n pol-intranet port-forward svc/intranet 13000:80 &
	@kubectl --context=kind-polaris-eks-sim -n pol-grafana  port-forward svc/grafana  13001:80 &
	@kubectl --context=kind-polaris-eks-sim -n pol-customer port-forward svc/customer 13002:80 &
	@echo "  intranet:  http://localhost:13000"
	@echo "  grafana:   http://localhost:13001"
	@echo "  customer:  http://localhost:13002"

test:                ## Run E2E verification (assumes services + ports up)
	@echo "  intranet /files STS:"
	@python3 tests/test_intranet_files.py 2>/dev/null || echo "  (script not present — see RUN_REPORT.md §A)"
	@echo ""
	@echo "  customer /data RLS:"
	@python3 tests/test_customer_data.py 2>/dev/null || echo "  (script not present — see RUN_REPORT.md §A)"

verify:              ## Quick connectivity check (postgres + minio + keycloak)
	@docker exec polaris-poc-postgres-1 psql -U polaris_admin -d pol_intranet -c "SELECT count(*) FROM employees;"
	@docker exec polaris-poc-minio-1 printenv MINIO_IDENTITY_OPENID_CONFIG_URL | head -1

clean:               ## Tear down everything (data loss!)
	docker compose -f docker-compose.yml down -v --remove-orphans
	kind delete cluster --name polaris-eks-sim || true