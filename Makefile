CLUSTER_NAME ?= nlo-demo

help:
	@echo "Node Label Operator - Makefile targets:"
	@echo "  make up             - Create kind cluster and deploy controller"
	@echo "  make logs           - Tail controller logs"
	@echo "  make grafana        - Open Grafana dashboard with metrics"
	@echo "  make dashboard      - Open Kubernetes Dashboard (web UI)"
	@echo "  make restart-worker - Restart a worker node after deleting from UI"
	@echo "  make down           - Delete the kind cluster"

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
	@echo "Installing Kubernetes Dashboard..."
	kubectl apply -f https://raw.githubusercontent.com/kubernetes/dashboard/v2.7.0/aio/deploy/recommended.yaml
	kubectl create serviceaccount admin-user -n kubernetes-dashboard
	kubectl create clusterrolebinding admin-user --clusterrole=cluster-admin --serviceaccount=kubernetes-dashboard:admin-user
	kubectl wait --for=condition=available --timeout=60s deployment/kubernetes-dashboard -n kubernetes-dashboard
	@echo ""
	@echo "Applying demo labels to worker nodes..."
	kubectl label node nlo-demo-worker persist.demo/type=expensive --overwrite
	kubectl label node nlo-demo-worker2 persist.demo/type=cheap --overwrite
	@echo ""
	@echo "✓ Cluster is ready!"
	@kubectl get nodes -L persist.demo/type
	@echo ""
	@echo "Next steps:"
	@echo "  make dashboard - Open Kubernetes Dashboard (web UI)"
	@echo "  make logs      - View controller logs"
	@echo "  make grafana   - View metrics in Grafana"

down:
	@pkill -f "kubectl proxy" 2>/dev/null || true
	kind delete cluster --name $(CLUSTER_NAME)

dashboard:
	@echo "Starting Kubernetes Dashboard..."
	@echo ""
	@echo "Dashboard URL:"
	@echo "  http://localhost:8001/api/v1/namespaces/kubernetes-dashboard/services/https:kubernetes-dashboard:/proxy/"
	@echo ""
	@if [ ! -s dashboard-token.txt ]; then \
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
	@docker ps --filter "name=nlo-demo-worker" --format "  {{.Names}}"
	@echo ""
	@read -p "Enter node name to restart: " NODE; \
	echo "" && \
	echo "Restarting $$NODE..."; \
	docker exec $$NODE systemctl restart kubelet && \
	echo "" && echo "✓ Kubelet restarted on $$NODE" && \
	echo "" && echo "Waiting for node to register..." && \
	sleep 2 && \
	echo "" && echo "=== Labels on $$NODE" && \
	kubectl get node $$NODE --show-labels | grep -o "persist.demo/[^,]*" || echo "No persist.demo labels found (expected after fresh registration)" && \
	echo "" && echo "Waiting for controller to restore labels (5 more seconds)..." && \
	sleep 10 && \
	echo "" && echo "=== Labels on $$NODE (after controller restoration) ===" && \
	kubectl get node $$NODE --show-labels | grep -o "persist.demo/[^,]*" && \
	echo "" && echo "✓ Label restored!" && \
	echo "" && kubectl get nodes -L persist.demo/type

grafana:
	@echo "Setting up Grafana with Prometheus..."
	@echo ""
	@if ! kubectl get deployment prometheus -n node-label-operator >/dev/null 2>&1; then \
		echo "Deploying Prometheus and Grafana..."; \
		kubectl apply -f monitoring/; \
		echo ""; \
		echo "Waiting for Prometheus to be ready..."; \
		kubectl wait --for=condition=available --timeout=60s deployment/prometheus -n node-label-operator; \
		echo "Waiting for Grafana to be ready..."; \
		kubectl wait --for=condition=available --timeout=60s deployment/grafana -n node-label-operator; \
		echo ""; \
		echo "✓ Monitoring stack deployed!"; \
	else \
		echo "Monitoring stack already deployed"; \
	fi
	@echo ""
	@echo "Opening Grafana dashboard..."
	@echo "URL: http://localhost:3000"
	@echo ""
	@echo "Dashboard: Node Label Operator (auto-loaded)"
	@echo "No login required (anonymous admin access enabled for demo)"
	@echo ""
	@open "http://localhost:3000/d/node-label-operator/node-label-operator" 2>/dev/null || true
	@echo "Press Ctrl+C to stop port-forward"
	@echo ""
	kubectl port-forward -n node-label-operator svc/grafana 3000:3000

logs:
	kubectl -n node-label-operator logs -f deploy/node-label-operator
