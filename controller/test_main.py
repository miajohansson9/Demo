#!/usr/bin/env python3
"""
Unit tests for Node Label Operator Controller

Tests critical edge cases including:
- on_node_create: NodeLabelState authoritative for new/recreated nodes
- on_node_labels_changed: Node authoritative for existing nodes (admin changes persist)
- Label deletion detection
- Race conditions in CRD operations
- CRD creation vs updates
"""

import unittest
from unittest.mock import Mock, MagicMock, patch


# Create custom ApiException class for testing
class MockApiException(Exception):
    def __init__(self, status):
        self.status = status
        super().__init__(f"API Exception: {status}")


# Passthrough decorator - returns function unchanged so we can test handler logic
def passthrough_decorator(*args, **kwargs):
    def decorator(fn):
        return fn
    if len(args) == 1 and callable(args[0]):
        return args[0]
    return decorator


# Mock external dependencies before importing main
import sys

# Create kopf mock with passthrough decorators
mock_kopf = MagicMock()
mock_kopf.on.create = passthrough_decorator
mock_kopf.on.update = passthrough_decorator
mock_kopf.on.delete = passthrough_decorator
mock_kopf.on.field = passthrough_decorator
mock_kopf.timer = passthrough_decorator
mock_kopf.on.startup = passthrough_decorator
mock_kopf.on.probe.liveness = passthrough_decorator
mock_kopf.on.probe.readiness = passthrough_decorator
mock_kopf.TemporaryError = Exception
mock_kopf.PermanentError = Exception
mock_kopf.OperatorSettings = MagicMock

mock_kube = MagicMock()
mock_kube_client = MagicMock()
mock_kube_config = MagicMock()
mock_kube_rest = MagicMock()
mock_kube_rest.ApiException = MockApiException
mock_prometheus = MagicMock()

sys.modules['kopf'] = mock_kopf
sys.modules['kubernetes'] = mock_kube
sys.modules['kubernetes.client'] = mock_kube_client
sys.modules['kubernetes.config'] = mock_kube_config
sys.modules['kubernetes.client.rest'] = mock_kube_rest
sys.modules['prometheus_client'] = mock_prometheus

import main

# Make ApiException available to main module
main.ApiException = MockApiException


class TestGetOwnedLabels(unittest.TestCase):
    """Test cases for get_owned_labels function (loads from CRD)"""

    def setUp(self):
        """Set up test fixtures"""
        main.custom_api = Mock()
        main.logger = Mock()

    def test_crd_exists_with_labels(self):
        """Should return owned labels when CRD exists"""
        main.custom_api.get_cluster_custom_object.return_value = {
            "spec": {
                "labels": {
                    "persist.demo/type": "expensive",
                    "persist.demo/zone": "us-west",
                    "kubernetes.io/hostname": "node-1"  # Should be filtered out
                }
            }
        }
        result = main.get_owned_labels("test-node")
        expected = {
            "persist.demo/type": "expensive",
            "persist.demo/zone": "us-west"
        }
        self.assertEqual(result, expected)

    def test_crd_not_found(self):
        """Should return None when CRD doesn't exist (404)"""
        main.custom_api.get_cluster_custom_object.side_effect = MockApiException(404)
        result = main.get_owned_labels("test-node")
        self.assertIsNone(result)

    def test_crd_exists_empty_labels(self):
        """Should return empty dict when CRD exists but has no labels"""
        main.custom_api.get_cluster_custom_object.return_value = {
            "spec": {"labels": {}}
        }
        result = main.get_owned_labels("test-node")
        self.assertEqual(result, {})

    def test_crd_exists_no_owned_labels(self):
        """Should return empty dict when CRD has labels but none match prefix"""
        main.custom_api.get_cluster_custom_object.return_value = {
            "spec": {"labels": {"kubernetes.io/hostname": "node-1"}}
        }
        result = main.get_owned_labels("test-node")
        self.assertEqual(result, {})


class TestOnNodeCreate(unittest.TestCase):
    """Test cases for on_node_create handler - NodeLabelState authoritative"""

    def setUp(self):
        """Set up test fixtures"""
        main.core_v1 = Mock()
        main.custom_api = Mock()
        main.logger = Mock()
        main.labels_applied = Mock()
        main.labels_applied.labels = Mock(return_value=Mock())
        main.handler_errors = Mock()
        main.handler_errors.labels = Mock(return_value=Mock())
        main.handler_duration = Mock()
        main.handler_duration.labels = Mock(return_value=Mock())

    def test_new_node_no_state(self):
        """New node with no stored labels - do nothing"""
        with patch.object(main, 'get_owned_labels', return_value=None):
            with patch.object(main, 'patch_node_labels') as mock_patch:
                main.on_node_create(name="new-node", labels={"kubernetes.io/hostname": "new-node"})
                mock_patch.assert_not_called()

    def test_recreated_node_applies_stored_labels(self):
        """CRITICAL: Recreated node should get ALL labels from NodeLabelState"""
        stored_labels = {"persist.demo/type": "expensive", "persist.demo/zone": "us-west"}
        with patch.object(main, 'get_owned_labels', return_value=stored_labels):
            with patch.object(main, 'patch_node_labels') as mock_patch:
                main.on_node_create(name="recreated-node", labels=None)
                mock_patch.assert_called_once_with("recreated-node", stored_labels)

    def test_crd_exists_but_empty(self):
        """CRD exists but has no owned labels - do nothing"""
        with patch.object(main, 'get_owned_labels', return_value={}):
            with patch.object(main, 'patch_node_labels') as mock_patch:
                main.on_node_create(name="test-node", labels={})
                mock_patch.assert_not_called()

    def test_new_node_no_owned_labels(self):
        """New node with no owned labels and no state - do nothing"""
        with patch.object(main, 'get_owned_labels', return_value=None):
            with patch.object(main, 'patch_node_labels') as mock_patch:
                main.on_node_create(
                    name="new-node",
                    labels={"kubernetes.io/hostname": "new-node"},
                )
                mock_patch.assert_not_called()


class TestOnNodeLabelsChanged(unittest.TestCase):
    """Test cases for on_node_labels_changed field handler - Node authoritative"""

    def setUp(self):
        """Set up test fixtures"""
        main.core_v1 = Mock()
        main.custom_api = Mock()
        main.logger = Mock()
        main.labels_synced = Mock()
        main.labels_synced.labels = Mock(return_value=Mock())
        main.handler_errors = Mock()
        main.handler_errors.labels = Mock(return_value=Mock())
        main.handler_duration = Mock()
        main.handler_duration.labels = Mock(return_value=Mock())

    def test_admin_adds_first_label_creates_crd(self):
        """CRITICAL: Admin adds first label - should create CRD"""
        old = {}
        new = {"persist.demo/type": "expensive"}
        with patch.object(main, 'get_owned_labels', return_value=None):  # CRD doesn't exist
            with patch.object(main, 'create_state') as mock_create:
                with patch.object(main, 'save_state') as mock_save:
                    main.on_node_labels_changed(name="test-node", old=old, new=new)
                    mock_create.assert_called_once_with("test-node", {"persist.demo/type": "expensive"})
                    mock_save.assert_not_called()

    def test_admin_adds_label_updates_crd(self):
        """CRITICAL: Admin adds label when CRD exists - should update CRD"""
        old = {}
        new = {"persist.demo/type": "expensive"}
        with patch.object(main, 'get_owned_labels', return_value={}):  # CRD exists but empty
            with patch.object(main, 'create_state') as mock_create:
                with patch.object(main, 'save_state') as mock_save:
                    main.on_node_labels_changed(name="test-node", old=old, new=new)
                    mock_save.assert_called_once_with("test-node", {"persist.demo/type": "expensive"})
                    mock_create.assert_not_called()

    def test_admin_changes_label_value(self):
        """CRITICAL: Admin changes label value - should update CRD"""
        old = {"persist.demo/type": "expensive"}
        new = {"persist.demo/type": "cheap"}
        with patch.object(main, 'get_owned_labels', return_value={"persist.demo/type": "expensive"}):
            with patch.object(main, 'save_state') as mock_save:
                main.on_node_labels_changed(name="test-node", old=old, new=new)
                mock_save.assert_called_once_with("test-node", {"persist.demo/type": "cheap"})

    def test_admin_deletes_label(self):
        """CRITICAL: Admin deletes a label - should update CRD"""
        old = {"persist.demo/type": "expensive", "persist.demo/zone": "us-west"}
        new = {"persist.demo/type": "expensive"}
        with patch.object(main, 'get_owned_labels', return_value={"persist.demo/type": "expensive", "persist.demo/zone": "us-west"}):
            with patch.object(main, 'save_state') as mock_save:
                main.on_node_labels_changed(name="test-node", old=old, new=new)
                mock_save.assert_called_once_with("test-node", {"persist.demo/type": "expensive"})

    def test_admin_deletes_all_labels(self):
        """CRITICAL: Admin deletes ALL owned labels - should save empty state (not delete CRD)"""
        old = {"persist.demo/type": "expensive"}
        new = {}
        with patch.object(main, 'get_owned_labels', return_value={"persist.demo/type": "expensive"}):
            with patch.object(main, 'save_state') as mock_save:
                main.on_node_labels_changed(name="test-node", old=old, new=new)
                # Save empty dict instead of deleting (preserves CRD)
                mock_save.assert_called_once_with("test-node", {})

    def test_non_owned_label_change_ignored(self):
        """Changes to non-owned labels should be ignored"""
        old = {"kubernetes.io/hostname": "old-name", "persist.demo/type": "expensive"}
        new = {"kubernetes.io/hostname": "new-name", "persist.demo/type": "expensive"}
        with patch.object(main, 'get_owned_labels', return_value={"persist.demo/type": "expensive"}):
            with patch.object(main, 'save_state') as mock_save:
                main.on_node_labels_changed(name="test-node", old=old, new=new)
                mock_save.assert_not_called()

    def test_multiple_label_changes(self):
        """Multiple label changes at once - add, remove, change"""
        old = {
            "persist.demo/type": "expensive",
            "persist.demo/zone": "us-west",
            "persist.demo/env": "prod"
        }
        new = {
            "persist.demo/type": "cheap",
            "persist.demo/region": "east",
            "persist.demo/env": "prod"
        }
        with patch.object(main, 'get_owned_labels', return_value=old):
            with patch.object(main, 'save_state') as mock_save:
                main.on_node_labels_changed(name="test-node", old=old, new=new)
                expected = {
                    "persist.demo/type": "cheap",
                    "persist.demo/region": "east",
                    "persist.demo/env": "prod"
                }
                mock_save.assert_called_once_with("test-node", expected)

class TestCreateState(unittest.TestCase):
    """Test cases for create_state function (creates new CRD)"""

    def setUp(self):
        """Set up test fixtures"""
        main.custom_api = Mock()
        main.logger = Mock()

    def test_create_new_crd(self):
        """Create a new NodeLabelState CRD"""
        main.custom_api.create_cluster_custom_object.return_value = None
        main.custom_api.patch_cluster_custom_object_status.return_value = None
        
        main.create_state("test-node", {"persist.demo/type": "expensive"})
        
        main.custom_api.create_cluster_custom_object.assert_called_once()
        call_args = main.custom_api.create_cluster_custom_object.call_args
        body = call_args[1]['body']
        self.assertEqual(body["spec"]["labels"], {"persist.demo/type": "expensive"})
        self.assertEqual(body["metadata"]["name"], "test-node")

    def test_create_with_empty_labels(self):
        """Create CRD with empty labels"""
        main.custom_api.create_cluster_custom_object.return_value = None
        main.custom_api.patch_cluster_custom_object_status.return_value = None
        
        main.create_state("test-node", {})
        
        main.custom_api.create_cluster_custom_object.assert_called_once()
        call_args = main.custom_api.create_cluster_custom_object.call_args
        body = call_args[1]['body']
        self.assertEqual(body["spec"]["labels"], {})


class TestSaveState(unittest.TestCase):
    """Test cases for save_state function (assumes CRD exists)"""

    def setUp(self):
        """Set up test fixtures"""
        main.custom_api = Mock()
        main.logger = Mock()

    def test_replace_existing(self):
        """Happy path: Replace existing NodeLabelState"""
        main.custom_api.get_cluster_custom_object.return_value = {
            "metadata": {"resourceVersion": "123"}
        }
        main.custom_api.replace_cluster_custom_object.return_value = None
        main.custom_api.patch_cluster_custom_object_status.return_value = None
        
        main.save_state("test-node", {"persist.demo/type": "expensive"})
        
        main.custom_api.replace_cluster_custom_object.assert_called_once()
        # Verify resourceVersion is passed
        call_args = main.custom_api.replace_cluster_custom_object.call_args
        body = call_args[1]['body']
        self.assertEqual(body["metadata"]["resourceVersion"], "123")
        self.assertEqual(body["spec"]["labels"], {"persist.demo/type": "expensive"})

    def test_empty_labels_replaces(self):
        """Replace with empty labels properly clears all labels"""
        main.custom_api.get_cluster_custom_object.return_value = {
            "metadata": {"resourceVersion": "123"}
        }
        main.custom_api.replace_cluster_custom_object.return_value = None
        main.custom_api.patch_cluster_custom_object_status.return_value = None
        
        main.save_state("test-node", {})
        
        call_args = main.custom_api.replace_cluster_custom_object.call_args
        body = call_args[1]['body']
        # Replace with empty labels dict
        self.assertEqual(body["spec"]["labels"], {})

class TestAuthorityModelIntegration(unittest.TestCase):
    """Integration tests verifying the authority model"""

    def setUp(self):
        """Set up test fixtures"""
        main.core_v1 = Mock()
        main.custom_api = Mock()
        main.logger = Mock()
        main.labels_applied = Mock()
        main.labels_applied.labels = Mock(return_value=Mock())
        main.labels_synced = Mock()
        main.labels_synced.labels = Mock(return_value=Mock())
        main.handler_errors = Mock()
        main.handler_errors.labels = Mock(return_value=Mock())
        main.handler_duration = Mock()
        main.handler_duration.labels = Mock(return_value=Mock())

    def test_full_node_lifecycle(self):
        """Test complete node lifecycle: create, update, recreate"""
        # Step 1: New node with no CRD - nothing happens
        with patch.object(main, 'get_owned_labels', return_value=None):
            with patch.object(main, 'patch_node_labels') as mock_patch:
                main.on_node_create(
                    name="lifecycle-node",
                    labels={"persist.demo/type": "expensive"}
                )
                mock_patch.assert_not_called()  # No CRD, so nothing to restore

        # Step 2: Admin adds a label - creates CRD
        with patch.object(main, 'get_owned_labels', return_value=None):
            with patch.object(main, 'create_state') as mock_create:
                main.on_node_labels_changed(
                    name="lifecycle-node",
                    old={},
                    new={"persist.demo/type": "expensive"}
                )
                mock_create.assert_called_once_with("lifecycle-node", {"persist.demo/type": "expensive"})

        # Step 3: Admin changes the label - updates CRD
        with patch.object(main, 'get_owned_labels', return_value={"persist.demo/type": "expensive"}):
            with patch.object(main, 'save_state') as mock_save:
                main.on_node_labels_changed(
                    name="lifecycle-node",
                    old={"persist.demo/type": "expensive"},
                    new={"persist.demo/type": "cheap"}
                )
                mock_save.assert_called_once_with("lifecycle-node", {"persist.demo/type": "cheap"})

        # Step 4: Node deleted - NodeLabelState is preserved (no delete handler needed)
        # The CRD remains with the label state for when the node is recreated

        # Step 5: Node recreated - should get labels from NodeLabelState
        with patch.object(main, 'get_owned_labels', return_value={"persist.demo/type": "cheap"}):
            with patch.object(main, 'patch_node_labels') as mock_patch:
                main.on_node_create(name="lifecycle-node", labels=None)
                mock_patch.assert_called_once_with("lifecycle-node", {"persist.demo/type": "cheap"})


if __name__ == '__main__':
    unittest.main()
