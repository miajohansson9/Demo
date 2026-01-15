# Node Label Operator

A Kubernetes controller that preserves and restores node labels across node deletion/recreation events, built with [kopf](https://kopf.readthedocs.io/).

## How It Works

The controller uses Kubernetes watch events to respond to node changes in real-time:

1. **Node Created** (`@kopf.on.create`): When a new node appears, the controller checks for stored labels in the NodeLabelState CRD and applies them. **NodeLabelState is authoritative for new/recreated nodes**.

2. **Labels Changed** (`@kopf.on.field`): When labels change on an existing node, the controller syncs those changes to NodeLabelState. **Node is authoritative for existing nodes** - this allows admins to modify or delete labels.

3. **Node Deleted**: No action. NodeLabelState CRD persists so labels can be restored when a node with the same name is recreated.

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
