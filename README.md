# Node Label Operator

A stateless Kubernetes controller that preserves and restores node labels across node deletion/recreation events.

## How It Works

1. **Continuous Capture**: Controller polls all nodes every 5 seconds. When a node has labels matching the configured prefix (e.g., `persist.demo/*`), it saves them to a ConfigMap.

2. **Automatic Restore**: When a node is missing expected labels (because it was recreated), the controller patches the node with labels from the ConfigMap.

3. **Stateless Design**: All state is stored in Kubernetes ConfigMaps. The controller can restart without losing track of persisted labels.

## Architecture

```
┌─────────────┐
│   Node 1    │  persist.demo/type=expensive
│  (worker)   │  ─────┐
└─────────────┘       │
                      ↓
              ┌───────────────┐
              │  Controller   │ ←── Polls every 5s
              │   (Deployment)│
              └───────────────┘
                      ↓
              ┌───────────────┐
              │  ConfigMaps   │  State storage
              │ (per node)    │
              └───────────────┘
```

**State Storage Example**:
```yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: node-labels-kind-worker
  namespace: node-label-operator
data:
  state.json: |
    {
      "nodeName": "kind-worker",
      "labels": {
        "persist.demo/type": "expensive"
      },
      "capturedAt": "2026-01-12T17:00:00Z"
    }
```

## Project Structure

```
.
├── controller/
│   ├── main.py           # Controller implementation
│   ├── requirements.txt  # Python dependencies
│   └── Dockerfile
├── deploy/
│   ├── namespace.yaml    # node-label-operator namespace
│   ├── rbac.yaml         # ServiceAccount, ClusterRole, Bindings
│   └── deployment.yaml   # Controller deployment
├── kind/
│   └── kind-config.yaml  # Local cluster config (1 control + 2 workers)
├── demo.py               # Automated demo script
├── Makefile              # Easy commands
└── README.md
```

## Prerequisites

- **Docker** (for building images and running kind)
- **kubectl** (Kubernetes CLI)
- **kind** (Kubernetes in Docker)
  ```bash
  # Install kind
  brew install kind
  # or
  go install sigs.k8s.io/kind@latest
  ```
- **Python 3.10+** with `kubernetes` package
  ```bash
  pip install kubernetes
  ```

## Quick Start

Run the full demo with three commands:

```bash
# 1. Create cluster and deploy controller
make up

# 2. (Optional) Run the kubernetes dashboard
make dashboard

# 3. Delete a node from the dashboard or terminal

# 4. Restart the kubelet to simulate a new node registration (mimics cloud provider replacing a node)
make restart-worker

# 5. Bring down the cluster
make down
```

## Testing

Run the unit test suite:

```bash
make test
```

The test suite covers critical edge cases including:
- Label value changes detection
- Invalid JSON in ConfigMap handling
- Race conditions in ConfigMap operations

See [`controller/TESTING.md`](controller/TESTING.md) for detailed test documentation.

## Configuration

Controller behavior is configured via environment variables in `deploy/deployment.yaml`:

| Variable | Default | Description |
|----------|---------|-------------|
| `PERSIST_LABEL_PREFIX` | `persist.demo/` | Only labels with this prefix are preserved |
| `OPERATOR_NAMESPACE` | `node-label-operator` | Namespace for state ConfigMaps |
| `RECONCILE_INTERVAL_SECONDS` | `5` | How often to check all nodes |
| `LOG_LEVEL` | `INFO` | Logging level (DEBUG, INFO, WARNING, ERROR) |


### Why Polling Instead of Watches?

- **Simpler implementation**: No complex watch bookmark/resume logic
- **Naturally handles restarts**: No need to rebuild watch state
- **Fast enough for demo**: 5-second interval detects changes quickly
- **Fewer edge cases**: No need to handle watch timeouts, reconnections, etc.

## Metrics

**View metrics:**
```bash
make grafana
# Opens http://localhost:3000 with pre-built dashboard
```

## Production Considerations

1. **Stable Node IDs**: Key ConfigMaps by cloud provider instance ID instead of node name (handles node renames)
2. **Leader Election**: Run multiple replicas with leader election for HA (automatic failover)
3. **Watches**: Use watch API for lower latency in large clusters
4. **Alerting**: Alert if labels fail to restore after N attempts (use Prometheus AlertManager)
5. **Prefix Configuration**: Make prefix configurable per-node or use CRDs for more control