#!/usr/bin/env python3
"""
Node Label Operator Controller

A Kubernetes controller that preserves and restores node labels
across node deletion/recreation events using kopf framework.

Authority Model:
- New nodes (on_create): ConfigMap is authoritative - apply stored labels
- Existing nodes (on_update): Node is authoritative - sync changes to ConfigMap
"""

import json
import logging
import os
import time
from datetime import datetime, timezone
from typing import Dict, Optional

import kopf
from kubernetes import client, config
from kubernetes.client.rest import ApiException
from prometheus_client import Counter, Histogram, Gauge, start_http_server

# Configuration from environment
PERSIST_LABEL_PREFIX = os.getenv("PERSIST_LABEL_PREFIX", "persist.demo/")
OPERATOR_NAMESPACE = os.getenv("OPERATOR_NAMESPACE", "node-label-operator")
RESYNC_INTERVAL_SECONDS = int(os.getenv("RESYNC_INTERVAL_SECONDS", "300"))
METRICS_PORT = int(os.getenv("METRICS_PORT", "9090"))
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")

# Configure logging
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL.upper()),
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger(__name__)

# Global API client (initialized on startup)
core_v1: Optional[client.CoreV1Api] = None

# Prometheus metrics
labels_applied = Counter(
    'node_label_labels_applied_total',
    'Total number of labels applied to nodes from ConfigMap',
    ['node']
)
labels_synced = Counter(
    'node_label_labels_synced_total',
    'Total number of label changes synced to ConfigMap',
    ['node', 'action']  # action: added, removed, changed
)
handler_errors = Counter(
    'node_label_handler_errors_total',
    'Total number of handler errors',
    ['handler']
)
handler_duration = Histogram(
    'node_label_handler_duration_seconds',
    'Time spent in handlers',
    ['handler']
)
nodes_tracked = Gauge(
    'node_label_nodes_tracked',
    'Number of nodes with stored labels'
)


def configmap_name(node_name: str) -> str:
    """Generate ConfigMap name for a given node."""
    return f"node-labels-{node_name}"


def get_owned_labels(labels: Optional[Dict[str, str]]) -> Dict[str, str]:
    """Extract labels matching our prefix."""
    if not labels:
        return {}
    return {k: v for k, v in labels.items() if k.startswith(PERSIST_LABEL_PREFIX)}


def load_configmap_state(node_name: str) -> Optional[Dict[str, str]]:
    """
    Load persisted label state from ConfigMap.
    
    Returns:
        dict: Persisted labels, or None if ConfigMap doesn't exist or contains invalid data
    """
    try:
        cm = core_v1.read_namespaced_config_map(
            name=configmap_name(node_name),
            namespace=OPERATOR_NAMESPACE
        )
        state_json = cm.data.get("state.json", "{}")
        try:
            state = json.loads(state_json)
            return state.get("labels", {})
        except json.JSONDecodeError as json_err:
            logger.error(f"Invalid JSON in ConfigMap for {node_name}: {json_err}. Treating as empty state.")
            return None
    except ApiException as e:
        if e.status == 404:
            return None
        logger.error(f"Error reading ConfigMap for {node_name}: {e}")
        raise


def save_configmap_state(node_name: str, labels: Dict[str, str]):
    """
    Save label state to ConfigMap.
    
    Creates ConfigMap if it doesn't exist, updates if it does.
    Handles race condition when ConfigMap is deleted between the 409 check and replace.
    """
    state = {
        "nodeName": node_name,
        "labels": labels,
        "capturedAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    }
    
    cm = client.V1ConfigMap(
        metadata=client.V1ObjectMeta(name=configmap_name(node_name)),
        data={"state.json": json.dumps(state, indent=2)}
    )
    
    try:
        core_v1.create_namespaced_config_map(
            namespace=OPERATOR_NAMESPACE,
            body=cm
        )
        logger.info(f"Created ConfigMap for {node_name}")
    except ApiException as e:
        if e.status == 409:  # Already exists
            try:
                core_v1.replace_namespaced_config_map(
                    name=configmap_name(node_name),
                    namespace=OPERATOR_NAMESPACE,
                    body=cm
                )
                logger.debug(f"Updated ConfigMap for {node_name}")
            except ApiException as replace_err:
                if replace_err.status == 404:
                    # ConfigMap was deleted between 409 and replace - retry create
                    logger.warning(f"ConfigMap deleted during update, recreating for {node_name}")
                    core_v1.create_namespaced_config_map(
                        namespace=OPERATOR_NAMESPACE,
                        body=cm
                    )
                    logger.info(f"Created ConfigMap for {node_name} (retry)")
                else:
                    logger.error(f"Error replacing ConfigMap for {node_name}: {replace_err}")
                    raise
        else:
            logger.error(f"Error saving ConfigMap for {node_name}: {e}")
            raise


def delete_configmap_state(node_name: str):
    """Delete ConfigMap for a node (optional cleanup)."""
    try:
        core_v1.delete_namespaced_config_map(
            name=configmap_name(node_name),
            namespace=OPERATOR_NAMESPACE
        )
        logger.info(f"Deleted ConfigMap for {node_name}")
    except ApiException as e:
        if e.status != 404:
            logger.error(f"Error deleting ConfigMap for {node_name}: {e}")
            raise


def patch_node_labels(node_name: str, labels: Dict[str, str]):
    """
    Patch node to apply the given labels.
    """
    body = {"metadata": {"labels": labels}}
    try:
        core_v1.patch_node(name=node_name, body=body)
        logger.info(f"Patched node {node_name} with labels: {labels}")
    except ApiException as e:
        logger.error(f"Error patching node {node_name}: {e}")
        raise


# =============================================================================
# Kopf Handlers
# =============================================================================

@kopf.on.startup()
def on_startup(settings: kopf.OperatorSettings, **kwargs):
    """Initialize the controller on startup."""
    global core_v1
    
    # Load Kubernetes config
    try:
        config.load_incluster_config()
        logger.info("Loaded in-cluster config")
    except config.ConfigException:
        try:
            config.load_kube_config()
            logger.info("Loaded kubeconfig")
        except config.ConfigException:
            raise kopf.PermanentError("Could not load Kubernetes config")
    
    # Initialize API client
    core_v1 = client.CoreV1Api()
    
    # Start Prometheus metrics server (on different port than kopf's health)
    start_http_server(METRICS_PORT)
    logger.info(f"Metrics server started on port {METRICS_PORT}")
    
    # Configure kopf settings
    settings.posting.level = logging.WARNING
    settings.watching.server_timeout = 600
    settings.watching.client_timeout = 660
    
    logger.info("Node Label Operator started")
    logger.info(f"  Label prefix: {PERSIST_LABEL_PREFIX}")
    logger.info(f"  Namespace: {OPERATOR_NAMESPACE}")
    logger.info(f"  Resync interval: {RESYNC_INTERVAL_SECONDS}s")


@kopf.on.create('', 'v1', 'nodes', retries=5, backoff=10)
def on_node_create(name: str, labels: Optional[Dict[str, str]], **kwargs):
    """
    Handle new node creation.
    
    ConfigMap is authoritative: Apply stored labels to the new/recreated node.
    This handles the case where a node was deleted and recreated with the same name.
    """
    start_time = time.time()
    try:
        stored_labels = load_configmap_state(name)
        
        if not stored_labels:
            # No stored labels - this is a truly new node
            # Capture any owned labels it might have
            owned = get_owned_labels(labels)
            if owned:
                save_configmap_state(name, owned)
                logger.info(f"New node {name}: captured {len(owned)} initial labels")
            else:
                logger.debug(f"New node {name}: no owned labels to capture")
            return
        
        # Filter to owned labels
        owned_stored = get_owned_labels(stored_labels)
        
        if not owned_stored:
            logger.debug(f"New node {name}: ConfigMap exists but no owned labels")
            return
        
        # Check what labels the node currently has
        current_owned = get_owned_labels(labels)
        
        # Find labels to apply (in ConfigMap but not on node)
        labels_to_apply = {k: v for k, v in owned_stored.items() 
                          if k not in current_owned or current_owned[k] != v}
        
        if labels_to_apply:
            patch_node_labels(name, labels_to_apply)
            labels_applied.labels(node=name).inc(len(labels_to_apply))
            logger.info(f"Node {name} created: applied {len(labels_to_apply)} labels from ConfigMap")
        else:
            logger.debug(f"Node {name} created: all labels already present")
            
    except ApiException as e:
        handler_errors.labels(handler='on_create').inc()
        if e.status >= 500:
            raise kopf.TemporaryError(f"API server error: {e}", delay=30)
        raise kopf.PermanentError(f"Unrecoverable error: {e}")
    finally:
        handler_duration.labels(handler='on_create').observe(time.time() - start_time)


@kopf.on.update('', 'v1', 'nodes', retries=5, backoff=10)
def on_node_update(name: str, old: Dict, new: Dict, diff: object, **kwargs):
    """
    Handle node updates.
    
    Node is authoritative: Sync label changes to ConfigMap.
    This allows admins to modify/delete labels and have changes persist.
    """
    start_time = time.time()
    try:
        # Extract labels from old and new state
        old_labels = old.get('metadata', {}).get('labels', {}) or {}
        new_labels = new.get('metadata', {}).get('labels', {}) or {}
        
        # Filter to owned labels
        old_owned = get_owned_labels(old_labels)
        new_owned = get_owned_labels(new_labels)
        
        # Check if owned labels changed
        if old_owned == new_owned:
            # No change to owned labels
            return
        
        # Calculate what changed
        added = set(new_owned.keys()) - set(old_owned.keys())
        removed = set(old_owned.keys()) - set(new_owned.keys())
        changed = {k for k in old_owned if k in new_owned and old_owned[k] != new_owned[k]}
        
        # Node is authoritative - persist current state to ConfigMap
        # Save even if empty (preserves ConfigMap for future extensibility)
        save_configmap_state(name, new_owned)
        
        # Update metrics
        if added:
            labels_synced.labels(node=name, action='added').inc(len(added))
        if removed:
            labels_synced.labels(node=name, action='removed').inc(len(removed))
        if changed:
            labels_synced.labels(node=name, action='changed').inc(len(changed))
        
        logger.info(f"Node {name} labels synced to ConfigMap: "
                   f"+{len(added)} added, -{len(removed)} removed, ~{len(changed)} changed")
        
    except ApiException as e:
        handler_errors.labels(handler='on_update').inc()
        if e.status >= 500:
            raise kopf.TemporaryError(f"API server error: {e}", delay=30)
        raise kopf.PermanentError(f"Unrecoverable error: {e}")
    finally:
        handler_duration.labels(handler='on_update').observe(time.time() - start_time)


@kopf.on.delete('', 'v1', 'nodes', retries=3, backoff=5)
def on_node_delete(name: str, **kwargs):
    """
    Handle node deletion.
    
    Preserve ConfigMap for potential node recreation.
    This allows labels to be restored when the node comes back.
    """
    start_time = time.time()
    try:
        # Intentionally preserve ConfigMap for recreation scenario
        logger.info(f"Node {name} deleted, preserving ConfigMap for potential recreation")
        
        # Note: If you want to cleanup ConfigMaps for permanently removed nodes,
        # you could add a TTL or separate cleanup mechanism
        
    finally:
        handler_duration.labels(handler='on_delete').observe(time.time() - start_time)


@kopf.timer('', 'v1', 'nodes', interval=RESYNC_INTERVAL_SECONDS, sharp=True)
def resync_node(name: str, labels: Optional[Dict[str, str]], **kwargs):
    """
    Periodic resync as a safety net.
    
    Catches any missed events due to network issues or controller restarts.
    Uses node-authoritative model (same as on_update).
    """
    start_time = time.time()
    try:
        owned_labels = get_owned_labels(labels)
        stored_labels = load_configmap_state(name) or {}
        stored_owned = get_owned_labels(stored_labels)
        
        # If node has labels but ConfigMap doesn't match, sync to ConfigMap
        # (node is authoritative for existing nodes)
        if owned_labels != stored_owned:
            save_configmap_state(name, owned_labels)
            if owned_labels:
                logger.info(f"Resync {name}: synced {len(owned_labels)} labels to ConfigMap")
            else:
                logger.info(f"Resync {name}: cleared labels in ConfigMap (admin removed them)")
        else:
            logger.debug(f"Resync {name}: in sync")
            
    except ApiException as e:
        handler_errors.labels(handler='resync').inc()
        if e.status >= 500:
            raise kopf.TemporaryError(f"API server error during resync: {e}", delay=60)
        logger.error(f"Error during resync for {name}: {e}")
    finally:
        handler_duration.labels(handler='resync').observe(time.time() - start_time)


# Note: Health probes are handled automatically by kopf via the --liveness CLI flag.
# Kopf exposes /healthz endpoint that Kubernetes probes can check.
