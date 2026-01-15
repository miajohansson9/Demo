# Node Label Operator

A Kubernetes controller that preserves and restores node labels across node deletion/recreation events, built with [kopf](https://kopf.readthedocs.io/).

## How It Works

The controller uses Kubernetes watch events to respond to node changes in real-time:

1. **Node Created** (`@kopf.on.create`): When a new node appears, the controller checks for stored labels in the NodeLabelState CRD and applies them. **NodeLabelState is authoritative for new/recreated nodes**.

2. **Labels Changed** (`@kopf.on.field`): When labels change on an existing node, the controller syncs those changes to NodeLabelState. **Node is authoritative for existing nodes** - this allows admins to modify or delete labels.

3. **Node Deleted**: NodeLabelState CRD stays unchangedd so labels can be restored when a node with the same name is recreated.

### Authority Model

| Scenario | Authority | Behavior |
|----------|-----------|----------|
| New/recreated node | NodeLabelState | Apply stored labels to node |
| Existing node label changed | Node | Sync change to NodeLabelState |
| Existing node label deleted | Node | Remove from NodeLabelState |
| Node deleted | - | Preserve NodeLabelState for recreation |

### Edge Case Handling

- **Label Value Changes**: Admin changes a label value → new value persists to NodeLabelState
- **Label Deletion**: Admin removes a label → label removed from NodeLabelState  
- **Empty Labels**: When all labels are removed, CRD is updated with empty labels (not deleted)
- **CRD Creation**: First label added creates the CRD; subsequent changes update it

## Architecture

```
┌─────────────┐     ADDED      ┌───────────────┐
│   Node 1    │ ──────────────→│               │
│  (worker)   │                │   Controller  │
└─────────────┘     MODIFIED   │    (kopf)     │
       ↑       ←──────────────→│               │
       │                       └───────────────┘
       │                              ↓
  labels restored              ┌───────────────┐
  from CRD                     │NodeLabelState │
                               │     CRD       │
                               └───────────────┘
```

**Watch Events**:
- `ADDED` → Apply labels from NodeLabelState (CRD authoritative)
- `MODIFIED` → Sync labels to NodeLabelState (Node authoritative)
- `DELETED` → Preserve NodeLabelState

**Why CRDs over ConfigMaps?**

| Benefit | Description |
|---------|-------------|
| **Native kubectl UX** | `kubectl get nodelabelstates` works naturally |
| **Watch efficiency** | Dedicated API endpoints for better scalability |
| **Schema validation** | OpenAPI validation built-in |
| **Better kubectl output** | Custom printer columns show node, label count, last updated |

**State Storage Example**:
```yaml
apiVersion: persist.demo/v1
kind: NodeLabelState
metadata:
  name: nlo-demo-worker
spec:
  nodeName: nlo-demo-worker
  labels:
    persist.demo/type: expensive
status:
  lastUpdated: "2026-01-14T17:00:00Z"
  labelCount: 1
```

**Viewing stored state**:
```bash
# List all stored label states
kubectl get nodelabelstates
# or shorthand
kubectl get nls

# View details for a specific node
kubectl describe nodelabelstate nlo-demo-worker
```

## Project Structure

```
.
├── controller/
│   ├── main.py           # Controller implementation (kopf handlers)
│   ├── test_main.py      # Unit tests
│   ├── requirements.txt  # Python dependencies (kubernetes, kopf)
│   └── Dockerfile
├── deploy/
│   ├── namespace.yaml    # node-label-operator namespace
│   ├── crd.yaml          # NodeLabelState CRD definition
│   ├── rbac.yaml         # ServiceAccount, ClusterRole, Bindings
│   ├── deployment.yaml   # Controller deployment with kopf
├── monitoring/
│   ├── prometheus.yaml   # Prometheus deployment
│   └── grafana.yaml      # Grafana with dashboard
├── kind/
│   └── kind-config.yaml  # Local cluster config (1 control + 2 workers)
├── Makefile              # Easy commands
└── README.md
```

## Prerequisites

- **Docker** (for building images and running kind)
- **kubectl** (Kubernetes CLI)
- **kind** (Kubernetes in Docker)
  ```bash
  brew install kind
  ```
- **Python 3.10+** (for running tests locally)
  ```bash
  pip install kubernetes kopf
  ```

## Quick Start

```bash
# 1. Create cluster and deploy controller
make up

# 2. View stored label states
make states

# 3. (Optional) Open Kubernetes Dashboard
make dashboard

# 4. Delete a node from the dashboard or terminal

# 5. Restart the kubelet to simulate a new node registration
make restart-worker

# 6. Bring down the cluster
make down
```

## Testing

Run the unit test suite:

```bash
make test
```

The test suite covers:
- Handler logic for create/update/delete events
- Authority model (NodeLabelState vs Node authoritative)
- Label deletion detection
- Race conditions in CRD operations

## Configuration

Controller behavior is configured via environment variables in `deploy/deployment.yaml`:

| Variable | Default | Description |
|----------|---------|-------------|
| `PERSIST_LABEL_PREFIX` | `persist.demo/` | Only labels with this prefix are preserved |
| `METRICS_PORT` | `9090` | Port for Prometheus metrics endpoint |
| `LOG_LEVEL` | `INFO` | Logging level (DEBUG, INFO, WARNING, ERROR) |

## Why Kopf?

Kopf handles the complexity of Kubernetes watchers:

| Challenge | Kopf Solution |
|-----------|---------------|
| Watch disconnections | Automatic reconnection |
| Expired resourceVersion | Auto re-list and restart watch |
| Missed events | Startup reconciliation re-lists all resources |
| Event backpressure | Built-in workqueue with rate limiting |
| Multiple replicas | Leader election via `--peering` |
| Error handling | Configurable retries with exponential backoff |
| Health checks | Built-in `/healthz` endpoint |

## Metrics

The controller exposes Prometheus metrics on port 9090:

| Metric | Type | Description |
|--------|------|-------------|
| `node_labels_applied_total` | Counter | Labels applied from storage to nodes (by node) |
| `node_labels_synced_total` | Counter | Labels synced to storage (by node, action) |
| `node_handler_errors_total` | Counter | Handler errors (by handler) |
| `node_handler_duration_seconds` | Histogram | Handler execution duration (by handler) |

**View metrics:**
```bash
make grafana
# Opens http://localhost:3000 with pre-built dashboard
```

## Production Considerations

1. **Stable Node IDs**: Key NodeLabelStates by cloud provider instance ID instead of node name
2. **Alerting**: Alert if labels fail to restore after N attempts (Prometheus AlertManager)
3. **Prefix Configuration**: Make prefix configurable per-node via annotations
