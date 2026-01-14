#!/usr/bin/env python3
"""
Unit tests for Node Label Operator Controller

Tests critical edge cases including:
- on_node_create: NodeLabelState authoritative for new/recreated nodes
- on_node_update: Node authoritative for existing nodes (admin changes persist)
- Label deletion detection
- Race conditions in CRD operations
- Resync behavior
"""

import json
import unittest
from unittest.mock import Mock, MagicMock, patch
from datetime import datetime, timezone


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
    """Test cases for get_owned_labels helper function"""

    def test_filters_owned_labels(self):
        """Should only return labels matching the prefix"""
        labels = {
            "persist.demo/type": "expensive",
            "persist.demo/zone": "us-west",
            "kubernetes.io/hostname": "node-1",
            "other-label": "value"
        }
        result = main.get_owned_labels(labels)
        self.assertEqual(result, {
            "persist.demo/type": "expensive",
            "persist.demo/zone": "us-west"
        })

    def test_empty_labels(self):
        """Should return empty dict for empty labels"""
        self.assertEqual(main.get_owned_labels({}), {})

    def test_none_labels(self):
        """Should return empty dict for None labels"""
        self.assertEqual(main.get_owned_labels(None), {})

    def test_no_owned_labels(self):
        """Should return empty dict when no labels match prefix"""
        labels = {"kubernetes.io/hostname": "node-1"}
        self.assertEqual(main.get_owned_labels(labels), {})


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
        """New node with no stored labels - capture any initial owned labels"""
        with patch.object(main, 'load_state', return_value=None):
            with patch.object(main, 'save_state') as mock_save:
                with patch.object(main, 'patch_node_labels') as mock_patch:
                    main.on_node_create(
                        name="new-node",
                        labels={"persist.demo/type": "expensive"},
                    )
                    mock_save.assert_called_once_with("new-node", {"persist.demo/type": "expensive"})
                    mock_patch.assert_not_called()

    def test_recreated_node_applies_stored_labels(self):
        """CRITICAL: Recreated node should get labels from NodeLabelState"""
        stored_labels = {"persist.demo/type": "expensive", "persist.demo/zone": "us-west"}
        with patch.object(main, 'load_state', return_value=stored_labels):
            with patch.object(main, 'patch_node_labels') as mock_patch:
                main.on_node_create(name="recreated-node", labels=None)
                mock_patch.assert_called_once_with("recreated-node", stored_labels)

    def test_recreated_node_partial_labels(self):
        """Recreated node already has some labels - only apply missing ones"""
        stored_labels = {"persist.demo/type": "expensive", "persist.demo/zone": "us-west"}
        current_labels = {"persist.demo/type": "expensive"}
        with patch.object(main, 'load_state', return_value=stored_labels):
            with patch.object(main, 'patch_node_labels') as mock_patch:
                main.on_node_create(name="recreated-node", labels=current_labels)
                mock_patch.assert_called_once_with("recreated-node", {"persist.demo/zone": "us-west"})

    def test_recreated_node_different_value(self):
        """Recreated node has label with different value - NodeLabelState wins"""
        stored_labels = {"persist.demo/type": "expensive"}
        current_labels = {"persist.demo/type": "cheap"}
        with patch.object(main, 'load_state', return_value=stored_labels):
            with patch.object(main, 'patch_node_labels') as mock_patch:
                main.on_node_create(name="recreated-node", labels=current_labels)
                mock_patch.assert_called_once_with("recreated-node", {"persist.demo/type": "expensive"})

    def test_new_node_no_owned_labels(self):
        """New node with no owned labels and no state - do nothing"""
        with patch.object(main, 'load_state', return_value=None):
            with patch.object(main, 'save_state') as mock_save:
                with patch.object(main, 'patch_node_labels') as mock_patch:
                    main.on_node_create(
                        name="new-node",
                        labels={"kubernetes.io/hostname": "new-node"},
                    )
                    mock_save.assert_not_called()
                    mock_patch.assert_not_called()


class TestOnNodeUpdate(unittest.TestCase):
    """Test cases for on_node_update handler - Node authoritative"""

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

    def test_admin_adds_label(self):
        """CRITICAL: Admin adds a label - should persist to NodeLabelState"""
        old = {"metadata": {"labels": {}}}
        new = {"metadata": {"labels": {"persist.demo/type": "expensive"}}}
        with patch.object(main, 'save_state') as mock_save:
            with patch.object(main, 'delete_state') as mock_delete:
                main.on_node_update(name="test-node", old=old, new=new, diff=None)
                mock_save.assert_called_once_with("test-node", {"persist.demo/type": "expensive"})
                mock_delete.assert_not_called()

    def test_admin_changes_label_value(self):
        """CRITICAL: Admin changes label value - should persist new value"""
        old = {"metadata": {"labels": {"persist.demo/type": "expensive"}}}
        new = {"metadata": {"labels": {"persist.demo/type": "cheap"}}}
        with patch.object(main, 'save_state') as mock_save:
            main.on_node_update(name="test-node", old=old, new=new, diff=None)
            mock_save.assert_called_once_with("test-node", {"persist.demo/type": "cheap"})

    def test_admin_deletes_label(self):
        """CRITICAL: Admin deletes a label - should remove from NodeLabelState"""
        old = {"metadata": {"labels": {"persist.demo/type": "expensive", "persist.demo/zone": "us-west"}}}
        new = {"metadata": {"labels": {"persist.demo/type": "expensive"}}}
        with patch.object(main, 'save_state') as mock_save:
            with patch.object(main, 'delete_state') as mock_delete:
                main.on_node_update(name="test-node", old=old, new=new, diff=None)
                mock_save.assert_called_once_with("test-node", {"persist.demo/type": "expensive"})
                mock_delete.assert_not_called()

    def test_admin_deletes_all_labels(self):
        """CRITICAL: Admin deletes ALL owned labels - should save empty state"""
        old = {"metadata": {"labels": {"persist.demo/type": "expensive"}}}
        new = {"metadata": {"labels": {}}}
        with patch.object(main, 'save_state') as mock_save:
            main.on_node_update(name="test-node", old=old, new=new, diff=None)
            # Save empty dict instead of deleting (preserves CRD)
            mock_save.assert_called_once_with("test-node", {})

    def test_non_owned_label_change_ignored(self):
        """Changes to non-owned labels should be ignored"""
        old = {"metadata": {"labels": {"kubernetes.io/hostname": "old-name", "persist.demo/type": "expensive"}}}
        new = {"metadata": {"labels": {"kubernetes.io/hostname": "new-name", "persist.demo/type": "expensive"}}}
        with patch.object(main, 'save_state') as mock_save:
            with patch.object(main, 'delete_state') as mock_delete:
                main.on_node_update(name="test-node", old=old, new=new, diff=None)
                mock_save.assert_not_called()
                mock_delete.assert_not_called()

    def test_multiple_label_changes(self):
        """Multiple label changes at once - add, remove, change"""
        old = {"metadata": {"labels": {
            "persist.demo/type": "expensive",
            "persist.demo/zone": "us-west",
            "persist.demo/env": "prod"
        }}}
        new = {"metadata": {"labels": {
            "persist.demo/type": "cheap",
            "persist.demo/region": "east",
            "persist.demo/env": "prod"
        }}}
        with patch.object(main, 'save_state') as mock_save:
            main.on_node_update(name="test-node", old=old, new=new, diff=None)
            expected = {
                "persist.demo/type": "cheap",
                "persist.demo/region": "east",
                "persist.demo/env": "prod"
            }
            mock_save.assert_called_once_with("test-node", expected)


class TestResyncNode(unittest.TestCase):
    """Test cases for resync_node timer handler"""

    def setUp(self):
        """Set up test fixtures"""
        main.core_v1 = Mock()
        main.custom_api = Mock()
        main.logger = Mock()
        main.handler_errors = Mock()
        main.handler_errors.labels = Mock(return_value=Mock())
        main.handler_duration = Mock()
        main.handler_duration.labels = Mock(return_value=Mock())

    def test_resync_in_sync(self):
        """Node and NodeLabelState are in sync - do nothing"""
        labels = {"persist.demo/type": "expensive"}
        with patch.object(main, 'load_state', return_value=labels):
            with patch.object(main, 'save_state') as mock_save:
                main.resync_node(name="test-node", labels=labels)
                mock_save.assert_not_called()

    def test_resync_missing_state(self):
        """Node has labels but NodeLabelState doesn't exist - create it"""
        labels = {"persist.demo/type": "expensive"}
        with patch.object(main, 'load_state', return_value=None):
            with patch.object(main, 'save_state') as mock_save:
                main.resync_node(name="test-node", labels=labels)
                mock_save.assert_called_once_with("test-node", labels)

    def test_resync_labels_differ(self):
        """Node and NodeLabelState have different labels - node wins"""
        node_labels = {"persist.demo/type": "cheap", "persist.demo/zone": "east"}
        stored_labels = {"persist.demo/type": "expensive"}
        with patch.object(main, 'load_state', return_value=stored_labels):
            with patch.object(main, 'save_state') as mock_save:
                main.resync_node(name="test-node", labels=node_labels)
                mock_save.assert_called_once_with("test-node", node_labels)

    def test_resync_state_has_labels_node_empty(self):
        """NodeLabelState has labels but node doesn't - save empty state"""
        stored_labels = {"persist.demo/type": "expensive"}
        with patch.object(main, 'load_state', return_value=stored_labels):
            with patch.object(main, 'save_state') as mock_save:
                main.resync_node(name="test-node", labels={})
                # Save empty dict instead of deleting (preserves CRD)
                mock_save.assert_called_once_with("test-node", {})


class TestLoadState(unittest.TestCase):
    """Test cases for load_state function"""

    def setUp(self):
        """Set up test fixtures"""
        main.custom_api = Mock()
        main.logger = Mock()

    def test_state_not_found(self):
        """NodeLabelState doesn't exist - return None"""
        main.custom_api.get_cluster_custom_object.side_effect = MockApiException(status=404)
        result = main.load_state("test-node")
        self.assertIsNone(result)

    def test_empty_state_returns_empty_dict(self):
        """NodeLabelState with empty labels"""
        main.custom_api.get_cluster_custom_object.return_value = {
            "spec": {"nodeName": "test-node", "labels": {}}
        }
        result = main.load_state("test-node")
        self.assertEqual(result, {})

    def test_missing_spec_returns_empty(self):
        """NodeLabelState exists but missing spec"""
        main.custom_api.get_cluster_custom_object.return_value = {}
        result = main.load_state("test-node")
        self.assertEqual(result, {})

    def test_valid_state_returns_labels(self):
        """Happy path: Valid NodeLabelState returns labels"""
        main.custom_api.get_cluster_custom_object.return_value = {
            "spec": {
                "nodeName": "test-node",
                "labels": {"persist.demo/type": "expensive"}
            }
        }
        result = main.load_state("test-node")
        self.assertEqual(result, {"persist.demo/type": "expensive"})

    def test_api_error_propagates(self):
        """Non-404 API errors should propagate"""
        main.custom_api.get_cluster_custom_object.side_effect = MockApiException(status=500)
        with self.assertRaises(MockApiException):
            main.load_state("test-node")


class TestSaveState(unittest.TestCase):
    """Test cases for save_state function"""

    def setUp(self):
        """Set up test fixtures"""
        main.custom_api = Mock()
        main.logger = Mock()

    def test_race_condition_create_409_then_404(self):
        """Race condition: 409 on create, 404 on replace, retry create"""
        create_409 = MockApiException(status=409)
        replace_404 = MockApiException(status=404)
        main.custom_api.create_cluster_custom_object.side_effect = [create_409, None]
        main.custom_api.get_cluster_custom_object.return_value = {"metadata": {"resourceVersion": "123"}}
        main.custom_api.replace_cluster_custom_object.side_effect = replace_404
        main.save_state("test-node", {"persist.demo/type": "expensive"})
        self.assertEqual(main.custom_api.create_cluster_custom_object.call_count, 2)
        main.custom_api.replace_cluster_custom_object.assert_called_once()
        main.logger.warning.assert_called_once()
        self.assertIn("deleted during update", str(main.logger.warning.call_args))

    def test_normal_create_flow(self):
        """Happy path: Create new NodeLabelState"""
        main.custom_api.create_cluster_custom_object.return_value = None
        main.save_state("test-node", {"persist.demo/type": "expensive"})
        main.custom_api.create_cluster_custom_object.assert_called_once()
        main.logger.info.assert_called_with("Created NodeLabelState for test-node")

    def test_normal_update_flow(self):
        """Happy path: Update existing NodeLabelState"""
        create_409 = MockApiException(status=409)
        main.custom_api.create_cluster_custom_object.side_effect = create_409
        main.custom_api.get_cluster_custom_object.return_value = {"metadata": {"resourceVersion": "123"}}
        main.custom_api.replace_cluster_custom_object.return_value = None
        main.save_state("test-node", {"persist.demo/type": "expensive"})
        main.custom_api.replace_cluster_custom_object.assert_called_once()
        main.logger.debug.assert_called_with("Updated NodeLabelState for test-node")


class TestDeleteState(unittest.TestCase):
    """Test cases for delete_state function"""

    def setUp(self):
        """Set up test fixtures"""
        main.custom_api = Mock()
        main.logger = Mock()

    def test_delete_success(self):
        """Happy path: Delete NodeLabelState"""
        main.custom_api.delete_cluster_custom_object.return_value = None
        main.delete_state("test-node")
        main.custom_api.delete_cluster_custom_object.assert_called_once()
        main.logger.info.assert_called_with("Deleted NodeLabelState for test-node")

    def test_delete_not_found_ignored(self):
        """NodeLabelState already deleted - should not raise"""
        main.custom_api.delete_cluster_custom_object.side_effect = MockApiException(status=404)
        main.delete_state("test-node")

    def test_delete_error_propagates(self):
        """Non-404 errors should propagate"""
        main.custom_api.delete_cluster_custom_object.side_effect = MockApiException(status=500)
        with self.assertRaises(MockApiException):
            main.delete_state("test-node")


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
        """Test complete node lifecycle: create, update, delete, recreate"""
        # Step 1: New node with initial labels
        with patch.object(main, 'load_state', return_value=None):
            with patch.object(main, 'save_state') as mock_save:
                main.on_node_create(
                    name="lifecycle-node",
                    labels={"persist.demo/type": "expensive"}
                )
                mock_save.assert_called_once_with("lifecycle-node", {"persist.demo/type": "expensive"})

        # Step 2: Admin changes the label
        with patch.object(main, 'save_state') as mock_save:
            main.on_node_update(
                name="lifecycle-node",
                old={"metadata": {"labels": {"persist.demo/type": "expensive"}}},
                new={"metadata": {"labels": {"persist.demo/type": "cheap"}}},
                diff=None
            )
            mock_save.assert_called_once_with("lifecycle-node", {"persist.demo/type": "cheap"})

        # Step 3: Node deleted (NodeLabelState preserved)
        main.on_node_delete(name="lifecycle-node")

        # Step 4: Node recreated - should get labels from NodeLabelState
        with patch.object(main, 'load_state', return_value={"persist.demo/type": "cheap"}):
            with patch.object(main, 'patch_node_labels') as mock_patch:
                main.on_node_create(name="lifecycle-node", labels=None)
                mock_patch.assert_called_once_with("lifecycle-node", {"persist.demo/type": "cheap"})


if __name__ == '__main__':
    unittest.main()
