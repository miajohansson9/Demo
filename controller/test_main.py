#!/usr/bin/env python3
"""
Unit tests for Node Label Operator Controller

Tests critical edge cases including:
- Label value changes detection
- Invalid JSON in ConfigMap
- Race conditions in ConfigMap operations
"""

import json
import unittest
from unittest.mock import Mock, MagicMock, patch, call
from datetime import datetime, timezone

# Create custom ApiException class for testing
class MockApiException(Exception):
    def __init__(self, status):
        self.status = status
        super().__init__(f"API Exception: {status}")

# Mock Kubernetes before importing main
import sys
mock_kube = MagicMock()
mock_kube_client = MagicMock()
mock_kube_rest = MagicMock()
mock_kube_rest.ApiException = MockApiException
sys.modules['kubernetes'] = mock_kube
sys.modules['kubernetes.client'] = mock_kube_client
sys.modules['kubernetes.client.rest'] = mock_kube_rest
sys.modules['prometheus_client'] = MagicMock()

import main
# Make ApiException available to main module
main.ApiException = MockApiException


class TestReconcileNode(unittest.TestCase):
    """Test cases for reconcile_node function"""
    
    def setUp(self):
        """Set up test fixtures"""
        # Mock the global core_v1 client
        main.core_v1 = Mock()
        
        # Reset metrics mocks
        main.labels_restored = Mock()
        main.labels_restored.labels = Mock(return_value=Mock())
        
        # Mock logger
        main.logger = Mock()
        
    def _create_mock_node(self, node_name: str, labels: dict):
        """Helper to create a mock V1Node"""
        node = Mock()
        node.metadata.name = node_name
        node.metadata.labels = labels
        return node
    
    def test_label_value_changed_detected_and_restored(self):
        """
        CRITICAL EDGE CASE #1: Label value changes should be detected and restored
        
        Scenario:
        - ConfigMap has persist.demo/type=expensive
        - Node has persist.demo/type=cheap (value changed)
        - Controller should restore to expensive
        """
        node_name = "test-node"
        node = self._create_mock_node(node_name, {
            "persist.demo/type": "cheap"  # Wrong value
        })
        
        # Mock ConfigMap returns correct value
        with patch.object(main, 'load_configmap_state', return_value={"persist.demo/type": "expensive"}):
            with patch.object(main, 'patch_node_labels') as mock_patch:
                with patch.object(main, 'save_configmap_state') as mock_save:
                    main.reconcile_node(node)
                    
                    # Should restore the correct value
                    mock_patch.assert_called_once_with(node_name, {"persist.demo/type": "expensive"})
                    # Should NOT save (no new labels)
                    mock_save.assert_not_called()
    
    def test_label_value_and_new_label_simultaneously(self):
        """
        CRITICAL EDGE CASE #1 (extended): Value change + new label simultaneously
        
        Scenario:
        - ConfigMap has persist.demo/type=expensive
        - Node has persist.demo/type=cheap + persist.demo/zone=us-west (new)
        - Controller should restore type AND persist zone
        
        Note: In current implementation, persisted_labels takes precedence when
        merging, so the restored value will be in the ConfigMap after the merge.
        """
        node_name = "test-node"
        node = self._create_mock_node(node_name, {
            "persist.demo/type": "cheap",      # Wrong value
            "persist.demo/zone": "us-west"     # New label
        })
        
        with patch.object(main, 'load_configmap_state', return_value={"persist.demo/type": "expensive"}):
            with patch.object(main, 'patch_node_labels') as mock_patch:
                with patch.object(main, 'save_configmap_state') as mock_save:
                    main.reconcile_node(node)
                    
                    # Should restore type
                    mock_patch.assert_called_once_with(node_name, {"persist.demo/type": "expensive"})
                    # Should persist zone, note: owned_labels still has "cheap" so merge keeps it
                    # This is expected - next reconciliation will see it's correct
                    mock_save.assert_called_once()
                    # Verify zone was added
                    call_args = mock_save.call_args[0]
                    self.assertEqual(call_args[0], node_name)
                    self.assertIn("persist.demo/zone", call_args[1])
    
    def test_multiple_value_changes(self):
        """
        CRITICAL EDGE CASE #1 (extended): Multiple label values changed
        """
        node_name = "test-node"
        node = self._create_mock_node(node_name, {
            "persist.demo/type": "wrong1",
            "persist.demo/zone": "wrong2",
            "persist.demo/env": "correct"
        })
        
        with patch.object(main, 'load_configmap_state', return_value={
            "persist.demo/type": "expensive",
            "persist.demo/zone": "us-west",
            "persist.demo/env": "correct"
        }):
            with patch.object(main, 'patch_node_labels') as mock_patch:
                with patch.object(main, 'save_configmap_state') as mock_save:
                    main.reconcile_node(node)
                    
                    # Should restore both wrong values
                    mock_patch.assert_called_once_with(node_name, {
                        "persist.demo/type": "expensive",
                        "persist.demo/zone": "us-west"
                    })
                    # Should NOT save (no new labels)
                    mock_save.assert_not_called()


class TestLoadConfigMapState(unittest.TestCase):
    """Test cases for load_configmap_state function"""
    
    def setUp(self):
        """Set up test fixtures"""
        main.core_v1 = Mock()
        main.logger = Mock()
        main.OPERATOR_NAMESPACE = "test-namespace"
    
    def test_invalid_json_returns_none(self):
        """
        CRITICAL EDGE CASE #2: Invalid JSON in ConfigMap should be handled gracefully
        
        Scenario:
        - ConfigMap exists but contains invalid JSON
        - Should return None and log error (not crash)
        """
        # Mock ConfigMap with invalid JSON
        mock_cm = Mock()
        mock_cm.data = {"state.json": "{ invalid json }"}
        main.core_v1.read_namespaced_config_map.return_value = mock_cm
        
        result = main.load_configmap_state("test-node")
        
        self.assertIsNone(result)
        main.logger.error.assert_called_once()
        self.assertIn("Invalid JSON", str(main.logger.error.call_args))
    
    def test_empty_json_returns_empty_dict(self):
        """
        Edge case: ConfigMap with empty JSON object
        """
        mock_cm = Mock()
        mock_cm.data = {"state.json": "{}"}
        main.core_v1.read_namespaced_config_map.return_value = mock_cm
        
        result = main.load_configmap_state("test-node")
        
        # Should return empty dict (no 'labels' key in empty JSON)
        self.assertEqual(result, {})
    
    def test_missing_state_json_key(self):
        """
        Edge case: ConfigMap exists but missing 'state.json' key
        """
        mock_cm = Mock()
        mock_cm.data = {}  # No state.json key
        main.core_v1.read_namespaced_config_map.return_value = mock_cm
        
        result = main.load_configmap_state("test-node")
        
        # Should use default "{}" and return empty dict
        self.assertEqual(result, {})
    
    def test_valid_json_returns_labels(self):
        """
        Happy path: Valid ConfigMap returns labels
        """
        mock_cm = Mock()
        mock_cm.data = {
            "state.json": json.dumps({
                "nodeName": "test-node",
                "labels": {"persist.demo/type": "expensive"},
                "capturedAt": "2026-01-13T00:00:00Z"
            })
        }
        main.core_v1.read_namespaced_config_map.return_value = mock_cm
        
        result = main.load_configmap_state("test-node")
        
        self.assertEqual(result, {"persist.demo/type": "expensive"})


class TestSaveConfigMapState(unittest.TestCase):
    """Test cases for save_configmap_state function"""
    
    def setUp(self):
        """Set up test fixtures"""
        main.core_v1 = Mock()
        main.logger = Mock()
        main.OPERATOR_NAMESPACE = "test-namespace"
        main.client = MagicMock()
    
    def test_race_condition_create_409_then_404(self):
        """
        CRITICAL EDGE CASE #3: Race condition handling
        
        Scenario:
        1. Try to create ConfigMap → 409 (already exists)
        2. Try to replace ConfigMap → 404 (was deleted)
        3. Should retry create
        """
        # Create mock exceptions using MockApiException
        create_409 = MockApiException(status=409)
        replace_404 = MockApiException(status=404)
        
        # Setup mock to raise 409, then 404, then succeed
        main.core_v1.create_namespaced_config_map.side_effect = [
            create_409,  # First create attempt
            None         # Retry create succeeds
        ]
        main.core_v1.replace_namespaced_config_map.side_effect = replace_404
        
        # Should not raise exception
        main.save_configmap_state("test-node", {"persist.demo/type": "expensive"})
        
        # Should have called create twice (initial + retry)
        self.assertEqual(main.core_v1.create_namespaced_config_map.call_count, 2)
        # Should have called replace once (which failed with 404)
        main.core_v1.replace_namespaced_config_map.assert_called_once()
        # Should have logged warning about retry
        main.logger.warning.assert_called_once()
        self.assertIn("deleted during update", str(main.logger.warning.call_args))
    
    def test_normal_create_flow(self):
        """
        Happy path: Create new ConfigMap
        """
        main.core_v1.create_namespaced_config_map.return_value = None
        
        main.save_configmap_state("test-node", {"persist.demo/type": "expensive"})
        
        main.core_v1.create_namespaced_config_map.assert_called_once()
        main.logger.info.assert_called_with("Created ConfigMap for test-node")
    
    def test_normal_update_flow(self):
        """
        Happy path: Update existing ConfigMap
        """
        create_409 = MockApiException(status=409)
        
        main.core_v1.create_namespaced_config_map.side_effect = create_409
        main.core_v1.replace_namespaced_config_map.return_value = None
        
        main.save_configmap_state("test-node", {"persist.demo/type": "expensive"})
        
        main.core_v1.replace_namespaced_config_map.assert_called_once()
        main.logger.debug.assert_called_with("Updated ConfigMap for test-node")


class TestReconcileNodeEdgeCases(unittest.TestCase):
    """Additional edge case tests for reconcile_node"""
    
    def setUp(self):
        """Set up test fixtures"""
        main.core_v1 = Mock()
        main.labels_restored = Mock()
        main.labels_restored.labels = Mock(return_value=Mock())
        main.logger = Mock()
    
    def test_no_owned_labels_no_configmap(self):
        """
        Edge case: New node with no persist.demo/ labels
        Should do nothing
        """
        node = Mock()
        node.metadata.name = "test-node"
        node.metadata.labels = {"kubernetes.io/hostname": "test-node"}
        
        with patch.object(main, 'load_configmap_state', return_value=None):
            with patch.object(main, 'patch_node_labels') as mock_patch:
                with patch.object(main, 'save_configmap_state') as mock_save:
                    main.reconcile_node(node)
                    
                    mock_patch.assert_not_called()
                    mock_save.assert_not_called()
    
    def test_node_labels_none(self):
        """
        Edge case: Node with metadata.labels = None
        Should handle gracefully
        """
        node = Mock()
        node.metadata.name = "test-node"
        node.metadata.labels = None  # Can happen in some scenarios
        
        with patch.object(main, 'load_configmap_state', return_value=None):
            with patch.object(main, 'patch_node_labels') as mock_patch:
                with patch.object(main, 'save_configmap_state') as mock_save:
                    # Should not crash
                    main.reconcile_node(node)
                    
                    mock_patch.assert_not_called()
                    mock_save.assert_not_called()
    
    def test_all_labels_removed_restored(self):
        """
        Edge case: All owned labels removed from node
        Should restore all
        """
        node = Mock()
        node.metadata.name = "test-node"
        node.metadata.labels = {"kubernetes.io/hostname": "test-node"}  # No owned labels
        
        with patch.object(main, 'load_configmap_state', return_value={
            "persist.demo/type": "expensive",
            "persist.demo/zone": "us-west"
        }):
            with patch.object(main, 'patch_node_labels') as mock_patch:
                with patch.object(main, 'save_configmap_state') as mock_save:
                    main.reconcile_node(node)
                    
                    # Should restore all labels
                    mock_patch.assert_called_once_with("test-node", {
                        "persist.demo/type": "expensive",
                        "persist.demo/zone": "us-west"
                    })
                    mock_save.assert_not_called()


if __name__ == '__main__':
    unittest.main()
