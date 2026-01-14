# Node Label Operator

A Kubernetes controller that preserves and restores node labels across node deletion/recreation events, built with [kopf](https://kopf.readthedocs.io/).

## How It Works

The controller uses Kubernetes watch events to respond to node changes in real-time:

1. **Node Created** (`@kopf.on.create`): When a new node appears, the controller checks for stored labels in ConfigMap and applies them. **ConfigMap is authoritative for new/recreated nodes**.

2. **Node Updated** (`@kopf.on.update`): When labels change on an existing node, the controller syncs those changes to ConfigMap. **Node is authoritative for existing nodes** - this allows admins to modify or delete labels.

3. **Node Deleted** (`@kopf.on.delete`): ConfigMap is preserved so labels can be restored when the node is recreated.

4. **Periodic Resync** (`@kopf.timer`): Every 5 minutes, a safety-net resync catches any missed events.

### Authority Model

| Scenario | Authority | Behavior |
|----------|-----------|----------|
| New/recreated node | ConfigMap | Apply stored labels to node |
| Existing node label changed | Node | Sync change to ConfigMap |
| Existing node label deleted | Node | Remove from ConfigMap |
| Node deleted | - | Preserve ConfigMap for recreation |

### Edge Case Handling

- **Label Value Changes**: Admin changes a label value → new value persists to ConfigMap
- **Label Deletion**: Admin removes a label → label removed from ConfigMap  
- **Invalid ConfigMap Data**: Corrupted JSON is logged and treated as empty state
- **Race Conditions**: ConfigMap create/replace retries handle concurrent modifications
- **Missed Events**: Periodic resync timer catches any events missed due to network issues

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
  from ConfigMap               │  ConfigMaps   │
                               │  (per node)   │
                               └───────────────┘
```

**Watch Events**:
- `ADDED` → Apply labels from ConfigMap (ConfigMap authoritative)
- `MODIFIED` → Sync labels to ConfigMap (Node authoritative)
- `DELETED` → Preserve ConfigMap

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
      "capturedAt": "2026-01-14T17:00:00Z"
    }
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

# 2. (Optional) Run the kubernetes dashboard
make dashboard

# 3. Delete a node from the dashboard or terminal

# 4. Restart the kubelet to simulate a new node registration
make restart-worker

# 5. Bring down the cluster
make down
```

## Testing

Run the unit test suite:

```bash
make test
```

The test suite covers:
- Handler logic for create/update/delete events
- Authority model (ConfigMap vs Node authoritative)
- Label deletion detection
- Invalid JSON handling
- Race conditions in ConfigMap operations

## Configuration

Controller behavior is configured via environment variables in `deploy/deployment.yaml`:

| Variable | Default | Description |
|----------|---------|-------------|
| `PERSIST_LABEL_PREFIX` | `persist.demo/` | Only labels with this prefix are preserved |
| `OPERATOR_NAMESPACE` | `node-label-operator` | Namespace for state ConfigMaps |
| `RESYNC_INTERVAL_SECONDS` | `300` | Periodic resync interval (safety net) |
| `LOG_LEVEL` | `INFO` | Logging level (DEBUG, INFO, WARNING, ERROR) |

## Why Kopf?

Kopf handles the complexity of Kubernetes watchers:

| Challenge | Kopf Solution |
|-----------|---------------|
| Watch disconnections | Automatic reconnection |
| Expired resourceVersion | Auto re-list and restart watch |
| Missed events | `@kopf.timer` for periodic resync |
| Event backpressure | Built-in workqueue with rate limiting |
| Multiple replicas | Leader election via `--peering` |
| Error handling | Configurable retries with exponential backoff |
| Health checks | Built-in `/healthz` endpoint |

## Metrics

**View metrics:**
```bash
make grafana
# Opens http://localhost:3000 with pre-built dashboard
```

## Production Considerations

1. **Stable Node IDs**: Key ConfigMaps by cloud provider instance ID instead of node name
2. **Alerting**: Alert if labels fail to restore after N attempts (Prometheus AlertManager)
3. **Prefix Configuration**: Make prefix configurable per-node or use CRDs for more control