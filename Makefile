CLUSTER_NAME ?= nlp-demo

help:
	@echo "Node Label Preserver - Makefile targets:"
	@echo "  make up             - Create kind cluster and deploy controller"
	@echo "  make demo           - Run the demo script"
	@echo "  make logs           - Tail controller logs"
	@echo "  make dashboard      - Open Kubernetes Dashboard (web UI)"
	@echo "  make restart-worker - Restart a worker node after deleting from UI"
	@echo "  make down           - Delete the kind cluster"
	@echo "  make clean          - Alias for 'make down'"

up:
	@echo "Creating kind cluster..."
	kind create cluster --name $(CLUSTER_NAME) --config kind/kind-config.yaml
	@echo ""
	@echo "Building controller image..."
	docker build -t node-label-operator:dev controller/
	@echo ""
	@echo "Loading image into kind..."
	kind load docker-image node-label-operator:dev --name $(CLUSTER_NAME)
	@echo ""
	@echo "Deploying controller..."
	kubectl apply -f deploy/namespace.yaml
	kubectl wait --for=jsonpath='{.status.phase}'=Active namespace/node-label-operator --timeout=30s
	kubectl apply -f deploy/
	@echo ""
	@echo "Waiting for controller to be ready..."
	kubectl -n node-label-operator rollout status deploy/node-label-operator --timeout=60s
	@echo ""
	@echo "✓ Cluster is ready!"
	@echo ""
	@echo "Next steps:"
	@echo "  make demo  - Run the demo"
	@echo "  make logs  - View controller logs"

down:
	kind delete cluster --name $(CLUSTER_NAME)

logs:
	kubectl -n node-label-operator logs -f deploy/node-label-operator

dashboard:
	@echo "Starting Kubernetes Dashboard..."
	@echo ""
	@echo "Dashboard URL:"
	@echo "  http://localhost:8001/api/v1/namespaces/kubernetes-dashboard/services/https:kubernetes-dashboard:/proxy/"
	@echo ""
	@if [ ! -f dashboard-token.txt ]; then \
		kubectl -n kubernetes-dashboard create token admin-user --duration=24h > dashboard-token.txt; \
	fi
	@echo "Access Token (copy this):"
	@cat dashboard-token.txt
	@echo ""
	@echo "Opening browser and starting proxy..."
	@open "http://localhost:8001/api/v1/namespaces/kubernetes-dashboard/services/https:kubernetes-dashboard:/proxy/" 2>/dev/null || true
	kubectl proxy

restart-worker:
	@echo "After deleting a node from the Dashboard, run this to bring it back:"
	@echo ""
	@echo "Nodes in Kubernetes:"
	@kubectl get nodes -L persist.demo/type | grep -E "(NAME|worker)" || echo "  (no workers registered)"
	@echo ""
	@echo "All worker containers in Docker (can be restarted):"
	@docker ps --filter "name=nlp-demo-worker" --format "  {{.Names}}"
	@echo ""
	@read -p "Enter node name to restart: " NODE; \
	echo "" && \
	echo "Restarting $$NODE..."; \
	docker exec $$NODE systemctl restart kubelet && \
	echo "" && echo "✓ Kubelet restarted on $$NODE" && \
	echo "" && echo "Waiting for node to register..." && \
	sleep 10 && \
	echo "" && echo "=== Labels on $$NODE (should be empty initially) ===" && \
	kubectl get node $$NODE --show-labels | grep -o "persist.demo/[^,]*" || echo "No persist.demo labels found (expected after fresh registration)" && \
	echo "" && echo "Waiting for controller to restore labels (5 more seconds)..." && \
	sleep 5 && \
	echo "" && echo "=== Labels on $$NODE (after controller restoration) ===" && \
	kubectl get node $$NODE --show-labels | grep -o "persist.demo/[^,]*" && \
	echo "" && echo "✓ Label restored!" && \
	echo "" && kubectl get nodes -L persist.demo/type

clean: down
