#!/usr/bin/env python3
"""
Node Label Operator Controller

A stateless Kubernetes controller that preserves and restores node labels
across node deletion/recreation events.
"""

import json
import logging
import os
import sys
import time
from datetime import datetime
from typing import Dict, Optional

from kubernetes import client, config
from kubernetes.client.rest import ApiException
from prometheus_client import Counter, Histogram, Gauge, start_http_server

# Configuration from environment
PERSIST_LABEL_PREFIX = os.getenv("PERSIST_LABEL_PREFIX", "persist.demo/")
OPERATOR_NAMESPACE = os.getenv("OPERATOR_NAMESPACE", "node-label-operator")
RECONCILE_INTERVAL_SECONDS = int(os.getenv("RECONCILE_INTERVAL_SECONDS", "5"))
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")

# Configure logging
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL.upper()),
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger(__name__)

# Global API clients
core_v1 = None

# Prometheus metrics
reconciliation_errors = Counter(
    'node_label_reconciliation_errors_total',
    'Total number of reconciliation errors'
)
reconciliation_duration = Histogram(
    'node_label_reconciliation_duration_seconds',
    'Time spent in reconciliation loop'
)
reconciliation_success = Gauge(
    'node_label_reconciliation_success_timestamp',
    'Timestamp of last successful reconciliation'
)
labels_restored = Counter(
    'node_label_labels_restored_total',
    'Total number of labels restored to nodes',
    ['node', 'label_key', 'label_value']
)
nodes_monitored = Gauge(
    'node_label_nodes_monitored',
    'Number of nodes currently being monitored'
)

def configmap_name(node_name: str) -> str:
    """Generate ConfigMap name for a given node."""
    return f"node-labels-{node_name}"


def load_configmap_state(node_name: str) -> Optional[Dict[str, str]]:
    """
    Load persisted label state from ConfigMap.
    
    Returns:
        dict: Persisted labels, or None if ConfigMap doesn't exist
    """
    try:
        cm = core_v1.read_namespaced_config_map(
            name=configmap_name(node_name),
            namespace=OPERATOR_NAMESPACE
        )
        state_json = cm.data.get("state.json", "{}")
        state = json.loads(state_json)
        return state.get("labels", {})
    except ApiException as e:
        if e.status == 404:
            return None
        logger.error(f"Error reading ConfigMap for {node_name}: {e}")
        raise


def save_configmap_state(node_name: str, labels: Dict[str, str]):
    """
    Save label state to ConfigMap.
    
    Creates ConfigMap if it doesn't exist, updates if it does.
    """
    state = {
        "nodeName": node_name,
        "labels": labels,
        "capturedAt": datetime.utcnow().isoformat() + "Z"
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
            core_v1.replace_namespaced_config_map(
                name=configmap_name(node_name),
                namespace=OPERATOR_NAMESPACE,
                body=cm
            )
            logger.debug(f"Updated ConfigMap for {node_name}")
        else:
            logger.error(f"Error saving ConfigMap for {node_name}: {e}")
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


def reconcile_node(node: client.V1Node):
    """
    Reconcile a single node's labels.
    
    Algorithm:
    1. Extract owned labels from node (matching prefix)
    2. Load persisted state from ConfigMap
    3. If node has owned labels and they differ from ConfigMap → update ConfigMap
    4. If node missing owned labels but ConfigMap has them → restore to node
    """
    node_name = node.metadata.name
    node_labels = node.metadata.labels or {}
    
    # Extract labels we own (matching our prefix)
    owned_labels = {
        k: v for k, v in node_labels.items()
        if k.startswith(PERSIST_LABEL_PREFIX)
    }
    
    # Load persisted state
    persisted_labels = load_configmap_state(node_name)
    
    # Decide action
    if owned_labels:
        # Node has owned labels → persist them (keep ConfigMap fresh)
        if persisted_labels != owned_labels:
            save_configmap_state(node_name, owned_labels)
            logger.info(f"Captured labels for {node_name}: {owned_labels}")
    
    elif persisted_labels:
        # Node missing labels but ConfigMap has them → restore
        patch_node_labels(node_name, persisted_labels)
        # Track each label individually
        for label_key, label_value in persisted_labels.items():
            labels_restored.labels(node=node_name, label_key=label_key, label_value=label_value).inc()
        logger.info(f"Restored labels for {node_name}: {persisted_labels}")


def reconcile_all_nodes():
    """
    Reconcile all nodes in the cluster.
    """
    start_time = time.time()
    try:
        nodes = core_v1.list_node()
        logger.debug(f"Reconciling {len(nodes.items)} nodes")
        
        # Update gauge for nodes monitored
        nodes_monitored.set(len(nodes.items))
        
        for node in nodes.items:
            try:
                reconcile_node(node)
            except Exception as e:
                logger.error(f"Error reconciling node {node.metadata.name}: {e}")
                reconciliation_errors.inc()
                # Continue with other nodes
        
        # Record successful reconciliation
        duration = time.time() - start_time
        reconciliation_duration.observe(duration)
        reconciliation_success.set(time.time())

    except ApiException as e:
        logger.error(f"Error listing nodes: {e}")
        reconciliation_errors.inc()
        raise


def run():
    """
    Main controller loop.
    """
    logger.info("Starting node-label-operator")
    logger.info(f"  Label prefix: {PERSIST_LABEL_PREFIX}")
    logger.info(f"  Namespace: {OPERATOR_NAMESPACE}")
    logger.info(f"  Reconcile interval: {RECONCILE_INTERVAL_SECONDS}s")
    
    while True:
        try:
            reconcile_all_nodes()
        except Exception as e:
            logger.error(f"Reconcile failed: {e}")
        
        time.sleep(RECONCILE_INTERVAL_SECONDS)


def main():
    """
    Initialize and run the controller.
    """
    global core_v1
    
    # Load Kubernetes config (in-cluster or kubeconfig)
    try:
        config.load_incluster_config()
        logger.info("Loaded in-cluster config")
    except config.ConfigException:
        try:
            config.load_kube_config()
            logger.info("Loaded kubeconfig")
        except config.ConfigException:
            logger.error("Could not load Kubernetes config")
            sys.exit(1)
    
    # Initialize API client
    core_v1 = client.CoreV1Api()
    
    logger.info(f"Using namespace: {OPERATOR_NAMESPACE}")
    
    # Start Prometheus metrics server
    metrics_port = int(os.getenv("METRICS_PORT", "8080"))
    start_http_server(metrics_port)
    logger.info(f"Metrics server started on port {metrics_port}")
    
    # Start controller loop
    run()


if __name__ == "__main__":
    main()
