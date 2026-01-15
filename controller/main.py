#!/usr/bin/env python3
"""
Node Label Operator Controller

A Kubernetes controller that preserves and restores node labels
across node deletion/recreation events using kopf framework.

Authority Model:
- New nodes (on_create): NodeLabelState CRD is authoritative - apply stored labels
- Existing nodes (on_update): Node is authoritative - sync changes to NodeLabelState
"""

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
METRICS_PORT = int(os.getenv("METRICS_PORT", "9090"))
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")

# CRD constants
CRD_GROUP = "persist.demo"
CRD_VERSION = "v1"
CRD_PLURAL = "nodelabelstates"

# Configure logging
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL.upper()),
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger(__name__)

# Global API clients (initialized on startup)
core_v1: Optional[client.CoreV1Api] = None
custom_api: Optional[client.CustomObjectsApi] = None

# Prometheus metrics
labels_applied = Counter(
    'node_labels_applied_total',
    'Total number of labels applied to nodes from storage',
    ['node']
)
labels_synced = Counter(
    'node_labels_synced_total',
    'Total number of label changes synced to storage',
    ['node', 'action']  # action: added, removed, changed
)
handler_errors = Counter(
    'node_handler_errors_total',
    'Total number of handler errors',
    ['handler']
)
handler_duration = Histogram(
    'node_handler_duration_seconds',
    'Time spent in handlers',
    ['handler']
)


def get_owned_labels(node_name: str) -> Optional[Dict[str, str]]:
    """
    Get owned labels for a node from NodeLabelState CRD.
    
    Returns:
        dict: Owned labels (matching our prefix) from storage
        None: If CRD doesn't exist
    """
    try:
        obj = custom_api.get_cluster_custom_object(
            group=CRD_GROUP,
            version=CRD_VERSION,
            plural=CRD_PLURAL,
            name=node_name
        )
        stored_labels = obj.get("spec", {}).get("labels", {})
        return {k: v for k, v in stored_labels.items() if k.startswith(PERSIST_LABEL_PREFIX)}
    except ApiException as e:
        if e.status == 404:
            return None
        logger.error(f"Error reading NodeLabelState for {node_name}: {e}")
        raise


def create_state(node_name: str, labels: Dict[str, str]):
    """Create a new NodeLabelState CRD."""
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    
    body = {
        "apiVersion": f"{CRD_GROUP}/{CRD_VERSION}",
        "kind": "NodeLabelState",
        "metadata": {
            "name": node_name
        },
        "spec": {
            "nodeName": node_name,
            "labels": labels
        }
    }
    
    custom_api.create_cluster_custom_object(
        group=CRD_GROUP,
        version=CRD_VERSION,
        plural=CRD_PLURAL,
        body=body
    )
    logger.info(f"Created NodeLabelState for {node_name}")
    _update_status(node_name, labels, now)


def save_state(node_name: str, labels: Dict[str, str]):
    """Update existing NodeLabelState CRD. Assumes CRD already exists."""
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    
    # Get current to preserve resourceVersion
    current = custom_api.get_cluster_custom_object(
        group=CRD_GROUP,
        version=CRD_VERSION,
        plural=CRD_PLURAL,
        name=node_name
    )
    
    body = {
        "apiVersion": f"{CRD_GROUP}/{CRD_VERSION}",
        "kind": "NodeLabelState",
        "metadata": {
            "name": node_name,
            "resourceVersion": current["metadata"]["resourceVersion"]
        },
        "spec": {
            "nodeName": node_name,
            "labels": labels
        }
    }
    
    custom_api.replace_cluster_custom_object(
        group=CRD_GROUP,
        version=CRD_VERSION,
        plural=CRD_PLURAL,
        name=node_name,
        body=body
    )
    logger.debug(f"Updated NodeLabelState for {node_name}")
    _update_status(node_name, labels, now)


def _update_status(node_name: str, labels: Dict[str, str], timestamp: str):
    """Update the status subresource of a NodeLabelState."""
    try:
        status_body = {
            "status": {
                "lastUpdated": timestamp,
                "labelCount": len(labels)
            }
        }
        custom_api.patch_cluster_custom_object_status(
            group=CRD_GROUP,
            version=CRD_VERSION,
            plural=CRD_PLURAL,
            name=node_name,
            body=status_body
        )
    except ApiException as e:
        # Status update failure is not critical
        logger.warning(f"Failed to update status for {node_name}: {e}")


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

@kopf.on.create('', 'v1', 'nodes', retries=5, backoff=10)
def on_node_create(name: str, labels: Optional[Dict[str, str]], **kwargs):
    """
    Handle new node creation.
    
    NodeLabelState is authoritative: Apply stored labels to the new/recreated node.
    This handles the case where a node was deleted and recreated with the same name.
    """
    start_time = time.time()
    try:
        # Get owned labels from storage
        stored_owned = get_owned_labels(name)
        
        # Check if CRD exists and has labels to restore
        if stored_owned is None or not stored_owned:
            # No CRD or no stored labels to restore (assumption is that nodes do not have labels when created)
            return
        
        # Apply all stored labels (node shouldn't have any of our labels yet)
        patch_node_labels(name, stored_owned)
        labels_applied.labels(node=name).inc(len(stored_owned))
        logger.info(f"Node {name} created: applied {len(stored_owned)} labels from NodeLabelState")

    except ApiException as e:
        handler_errors.labels(handler='on_create').inc()
        if e.status >= 500:
            raise kopf.TemporaryError(f"API server error: {e}", delay=30)
        raise kopf.PermanentError(f"Unrecoverable error: {e}")
    finally:
        handler_duration.labels(handler='on_create').observe(time.time() - start_time)


@kopf.on.field('', 'v1', 'nodes', field='metadata.labels', retries=5, backoff=10)
def on_node_labels_changed(name: str, old: Optional[Dict[str, str]], new: Optional[Dict[str, str]], **kwargs):
    """
    Handle node label changes.
    
    Uses field handler to only trigger on metadata.labels changes,
    avoiding unnecessary invocations on status updates.
    
    Node is authoritative: Sync label changes to NodeLabelState.
    This allows admins to modify/delete labels and have changes persist.
    """
    start_time = time.time()
    try:
        # old and new are the label dicts directly (from field handler)
        old_labels = old or {}
        new_labels = new or {}
        
        # Filter to owned labels
        old_owned = {k: v for k, v in old_labels.items() if k.startswith(PERSIST_LABEL_PREFIX)}
        new_owned = {k: v for k, v in new_labels.items() if k.startswith(PERSIST_LABEL_PREFIX)}
        
        # Check if owned labels changed
        if old_owned == new_owned:
            # No change to owned labels
            return
        
        # Calculate what changed
        added = set(new_owned.keys()) - set(old_owned.keys())
        removed = set(old_owned.keys()) - set(new_owned.keys())
        changed = {k for k in old_owned if k in new_owned and old_owned[k] != new_owned[k]}
        
        # Check if CRD exists
        stored_owned = get_owned_labels(name)
        if stored_owned is None:
            # CRD doesn't exist - create it
            create_state(name, new_owned)
        else:
            # CRD exists - update it
            save_state(name, new_owned)
        
        # Update metrics
        if added:
            labels_synced.labels(node=name, action='added').inc(len(added))
        if removed:
            labels_synced.labels(node=name, action='removed').inc(len(removed))
        if changed:
            labels_synced.labels(node=name, action='changed').inc(len(changed))
        
        logger.info(f"Node {name} labels synced to NodeLabelState: "
                   f"+{len(added)} added, -{len(removed)} removed, ~{len(changed)} changed")
        
    except ApiException as e:
        handler_errors.labels(handler='on_labels_changed').inc()
        if e.status >= 500:
            raise kopf.TemporaryError(f"API server error: {e}", delay=30)
        raise kopf.PermanentError(f"Unrecoverable error: {e}")
    finally:
        handler_duration.labels(handler='on_labels_changed').observe(time.time() - start_time)


@kopf.on.startup()
def configure(settings: kopf.OperatorSettings, **_):
    """Configure operator on startup."""
    global core_v1, custom_api
    
    # Load kubeconfig
    try:
        config.load_incluster_config()
        logger.info("Loaded in-cluster configuration")
    except config.ConfigException:
        config.load_kube_config()
        logger.info("Loaded local kubeconfig")
    
    # Initialize API clients
    core_v1 = client.CoreV1Api()
    custom_api = client.CustomObjectsApi()
    
    # Start Prometheus metrics server
    start_http_server(METRICS_PORT)
    logger.info(f"Started metrics server on port {METRICS_PORT}")
    
    # Configure kopf settings
    settings.posting.level = logging.WARNING  # Reduce noise in logs
    logger.info(f"Operator starting with label prefix: {PERSIST_LABEL_PREFIX}")