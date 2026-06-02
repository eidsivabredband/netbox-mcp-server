"""Tests for core object write tools: schema discovery, create, update, delete."""

from unittest.mock import MagicMock, patch

import pytest

from netbox_mcp_server.server import (
    netbox_create_object,
    netbox_delete_object,
    netbox_get_object_schema,
    netbox_update_object,
)


# ---------------------------------------------------------------------------
# netbox_get_object_schema
# ---------------------------------------------------------------------------


def test_get_object_schema_invalid_type():
    with pytest.raises(ValueError, match="Invalid object_type"):
        netbox_get_object_schema("not.a.type")


def test_get_object_schema_returns_actions():
    mock_client = MagicMock()
    mock_client.options.return_value = {
        "POST": {
            "id": {"type": "integer", "required": False, "read_only": True, "label": "ID"},
            "name": {"type": "string", "required": True, "read_only": False, "label": "Name"},
            "slug": {"type": "string", "required": True, "read_only": False, "label": "Slug"},
        }
    }
    with patch("netbox_mcp_server.server.netbox", mock_client):
        result = netbox_get_object_schema("dcim.site")

    mock_client.options.assert_called_once_with("dcim/sites")
    assert "POST" in result
    assert result["POST"]["name"]["required"] is True
    assert result["POST"]["id"]["read_only"] is True


def test_get_object_schema_uses_correct_endpoint_for_device():
    mock_client = MagicMock()
    mock_client.options.return_value = {"POST": {}}
    with patch("netbox_mcp_server.server.netbox", mock_client):
        netbox_get_object_schema("dcim.device")
    mock_client.options.assert_called_once_with("dcim/devices")


def test_get_object_schema_uses_correct_endpoint_for_prefix():
    mock_client = MagicMock()
    mock_client.options.return_value = {"POST": {}}
    with patch("netbox_mcp_server.server.netbox", mock_client):
        netbox_get_object_schema("ipam.prefix")
    mock_client.options.assert_called_once_with("ipam/prefixes")


# ---------------------------------------------------------------------------
# netbox_create_object
# ---------------------------------------------------------------------------


def test_create_object_invalid_type():
    with pytest.raises(ValueError, match="Invalid object_type"):
        netbox_create_object("not.real", {})


def test_create_object_no_write_token():
    with patch("netbox_mcp_server.server.netbox_write", None), pytest.raises(
        ValueError, match="NETBOX_WRITE_TOKEN"
    ):
        netbox_create_object("dcim.site", {"name": "Test", "slug": "test"})


def test_create_object_calls_client():
    mock_write = MagicMock()
    mock_write.create.return_value = {"id": 42, "name": "Oslo DC", "slug": "oslo-dc"}
    with patch("netbox_mcp_server.server.netbox_write", mock_write):
        result = netbox_create_object("dcim.site", {"name": "Oslo DC", "slug": "oslo-dc"})

    mock_write.create.assert_called_once_with("dcim/sites", {"name": "Oslo DC", "slug": "oslo-dc"})
    assert result["id"] == 42


def test_create_object_vlan():
    mock_write = MagicMock()
    mock_write.create.return_value = {"id": 7, "name": "mgmt", "vid": 100}
    with patch("netbox_mcp_server.server.netbox_write", mock_write):
        netbox_create_object("ipam.vlan", {"name": "mgmt", "vid": 100, "status": "active"})

    mock_write.create.assert_called_once_with(
        "ipam/vlans", {"name": "mgmt", "vid": 100, "status": "active"}
    )


# ---------------------------------------------------------------------------
# netbox_update_object
# ---------------------------------------------------------------------------


def test_update_object_invalid_type():
    with pytest.raises(ValueError, match="Invalid object_type"):
        netbox_update_object("not.real", 1, {})


def test_update_object_no_write_token():
    with patch("netbox_mcp_server.server.netbox_write", None), pytest.raises(
        ValueError, match="NETBOX_WRITE_TOKEN"
    ):
        netbox_update_object("dcim.device", 5, {"status": "planned"})


def test_update_object_calls_client():
    mock_write = MagicMock()
    mock_write.update.return_value = {"id": 5, "status": "planned"}
    with patch("netbox_mcp_server.server.netbox_write", mock_write):
        result = netbox_update_object("dcim.device", 5, {"status": "planned"})

    mock_write.update.assert_called_once_with("dcim/devices", 5, {"status": "planned"})
    assert result["status"] == "planned"


def test_update_object_passes_id_correctly():
    mock_write = MagicMock()
    mock_write.update.return_value = {"id": 99}
    with patch("netbox_mcp_server.server.netbox_write", mock_write):
        netbox_update_object("ipam.ipaddress", 99, {"description": "updated"})

    mock_write.update.assert_called_once_with("ipam/ip-addresses", 99, {"description": "updated"})


# ---------------------------------------------------------------------------
# netbox_delete_object
# ---------------------------------------------------------------------------


def test_delete_object_invalid_type():
    with pytest.raises(ValueError, match="Invalid object_type"):
        netbox_delete_object("not.real", 1)


def test_delete_object_no_write_token():
    with patch("netbox_mcp_server.server.netbox_write", None), pytest.raises(
        ValueError, match="NETBOX_WRITE_TOKEN"
    ):
        netbox_delete_object("dcim.device", 5)


def test_delete_object_returns_true_on_success():
    mock_write = MagicMock()
    mock_write.delete.return_value = True
    with patch("netbox_mcp_server.server.netbox_write", mock_write):
        result = netbox_delete_object("dcim.device", 5)

    mock_write.delete.assert_called_once_with("dcim/devices", 5)
    assert result is True


def test_delete_object_uses_correct_endpoint():
    mock_write = MagicMock()
    mock_write.delete.return_value = True
    with patch("netbox_mcp_server.server.netbox_write", mock_write):
        netbox_delete_object("ipam.prefix", 12)

    mock_write.delete.assert_called_once_with("ipam/prefixes", 12)
