"""Tests for custom object tools."""

from unittest.mock import MagicMock, patch

import pytest

from netbox_mcp_server.server import (
    netbox_custom_object_create,
    netbox_custom_object_delete,
    netbox_custom_object_get_by_id,
    netbox_custom_object_list,
    netbox_custom_object_type_create,
    netbox_custom_object_type_field_create,
    netbox_custom_object_update,
)

# ============================================================================
# netbox_custom_object_list
# ============================================================================


def test_custom_object_list_calls_correct_endpoint():
    mock_client = MagicMock()
    mock_client.get.return_value = {"count": 0, "results": []}
    with patch("netbox_mcp_server.server.netbox", mock_client):
        netbox_custom_object_list("fiber-splice", {})
    mock_client.get.assert_called_once_with(
        "plugins/custom-objects/fiber-splice",
        params={"limit": 5, "offset": 0},
    )


def test_custom_object_list_passes_filters():
    mock_client = MagicMock()
    mock_client.get.return_value = {"count": 1, "results": [{"id": 1}]}
    with patch("netbox_mcp_server.server.netbox", mock_client):
        netbox_custom_object_list("fiber-splice", {"site_id": 42}, limit=10)
    params = mock_client.get.call_args.kwargs["params"]
    assert params["site_id"] == 42
    assert params["limit"] == 10


def test_custom_object_list_fields():
    mock_client = MagicMock()
    mock_client.get.return_value = {"count": 0, "results": []}
    with patch("netbox_mcp_server.server.netbox", mock_client):
        netbox_custom_object_list("fiber-splice", {}, fields=["id", "name"])
    params = mock_client.get.call_args.kwargs["params"]
    assert params["fields"] == "id,name"


def test_custom_object_list_brief():
    mock_client = MagicMock()
    mock_client.get.return_value = {"count": 0, "results": []}
    with patch("netbox_mcp_server.server.netbox", mock_client):
        netbox_custom_object_list("fiber-splice", {}, brief=True)
    params = mock_client.get.call_args.kwargs["params"]
    assert params["brief"] == "1"


def test_custom_object_list_pagination():
    mock_client = MagicMock()
    mock_client.get.return_value = {"count": 20, "results": []}
    with patch("netbox_mcp_server.server.netbox", mock_client):
        netbox_custom_object_list("fiber-splice", {}, limit=10, offset=10)
    params = mock_client.get.call_args.kwargs["params"]
    assert params["limit"] == 10
    assert params["offset"] == 10


# ============================================================================
# netbox_custom_object_get_by_id
# ============================================================================


def test_custom_object_get_by_id_calls_correct_endpoint():
    mock_client = MagicMock()
    mock_client.get.return_value = {"id": 1, "name": "Splice-01"}
    with patch("netbox_mcp_server.server.netbox", mock_client):
        result = netbox_custom_object_get_by_id("fiber-splice", 1)
    mock_client.get.assert_called_once_with(
        "plugins/custom-objects/fiber-splice/1",
        params={},
    )
    assert result == {"id": 1, "name": "Splice-01"}


def test_custom_object_get_by_id_fields():
    mock_client = MagicMock()
    mock_client.get.return_value = {"id": 1}
    with patch("netbox_mcp_server.server.netbox", mock_client):
        netbox_custom_object_get_by_id("fiber-splice", 1, fields=["id", "name"])
    params = mock_client.get.call_args.kwargs["params"]
    assert params["fields"] == "id,name"


def test_custom_object_get_by_id_brief():
    mock_client = MagicMock()
    mock_client.get.return_value = {"id": 1}
    with patch("netbox_mcp_server.server.netbox", mock_client):
        netbox_custom_object_get_by_id("fiber-splice", 1, brief=True)
    params = mock_client.get.call_args.kwargs["params"]
    assert params["brief"] == "1"


def test_custom_object_get_by_id_embeds_id_in_endpoint():
    """ID must be embedded in the URL path, not passed as a query parameter."""
    mock_client = MagicMock()
    mock_client.get.return_value = {"id": 99}
    with patch("netbox_mcp_server.server.netbox", mock_client):
        netbox_custom_object_get_by_id("dhcp-scope", 99)
    called_endpoint = mock_client.get.call_args.args[0]
    assert called_endpoint == "plugins/custom-objects/dhcp-scope/99"


# ============================================================================
# netbox_custom_object_create
# ============================================================================


def test_custom_object_create_raises_without_write_token():
    with patch("netbox_mcp_server.server.netbox_write", None), pytest.raises(
        ValueError, match="NETBOX_WRITE_TOKEN"
    ):
        netbox_custom_object_create("fiber-splice", {"fiber_count": 48})


def test_custom_object_create_calls_correct_endpoint():
    mock_write = MagicMock()
    mock_write.create.return_value = {"id": 1, "fiber_count": 48}
    with patch("netbox_mcp_server.server.netbox_write", mock_write):
        result = netbox_custom_object_create("fiber-splice", {"fiber_count": 48})
    mock_write.create.assert_called_once_with(
        "plugins/custom-objects/fiber-splice",
        {"fiber_count": 48},
    )
    assert result["id"] == 1


def test_custom_object_create_passes_data_unchanged():
    mock_write = MagicMock()
    mock_write.create.return_value = {"id": 2}
    payload = {"fiber_count": 96, "site": 12, "label": "Splice A"}
    with patch("netbox_mcp_server.server.netbox_write", mock_write):
        netbox_custom_object_create("fiber-splice", payload)
    sent_data = mock_write.create.call_args.args[1]
    assert sent_data == payload


# ============================================================================
# netbox_custom_object_update
# ============================================================================


def test_custom_object_update_raises_without_write_token():
    with patch("netbox_mcp_server.server.netbox_write", None), pytest.raises(
        ValueError, match="NETBOX_WRITE_TOKEN"
    ):
        netbox_custom_object_update("fiber-splice", 1, {"fiber_count": 96})


def test_custom_object_update_calls_correct_endpoint():
    mock_write = MagicMock()
    mock_write.update.return_value = {"id": 1, "fiber_count": 96}
    with patch("netbox_mcp_server.server.netbox_write", mock_write):
        result = netbox_custom_object_update("fiber-splice", 1, {"fiber_count": 96})
    mock_write.update.assert_called_once_with(
        "plugins/custom-objects/fiber-splice",
        1,
        {"fiber_count": 96},
    )
    assert result["fiber_count"] == 96


def test_custom_object_update_passes_partial_data():
    """Update is PATCH — only provided keys should be sent."""
    mock_write = MagicMock()
    mock_write.update.return_value = {"id": 3}
    with patch("netbox_mcp_server.server.netbox_write", mock_write):
        netbox_custom_object_update("fiber-splice", 3, {"label": "Updated"})
    sent_data = mock_write.update.call_args.args[2]
    assert sent_data == {"label": "Updated"}


# ============================================================================
# netbox_custom_object_delete
# ============================================================================


def test_custom_object_delete_raises_without_write_token():
    with patch("netbox_mcp_server.server.netbox_write", None), pytest.raises(
        ValueError, match="NETBOX_WRITE_TOKEN"
    ):
        netbox_custom_object_delete("fiber-splice", 1)


def test_custom_object_delete_calls_correct_endpoint():
    mock_write = MagicMock()
    mock_write.delete.return_value = True
    with patch("netbox_mcp_server.server.netbox_write", mock_write):
        result = netbox_custom_object_delete("fiber-splice", 1)
    mock_write.delete.assert_called_once_with(
        "plugins/custom-objects/fiber-splice",
        1,
    )
    assert result is True


# ============================================================================
# netbox_custom_object_type_create
# ============================================================================


def test_custom_object_type_create_raises_without_write_token():
    with patch("netbox_mcp_server.server.netbox_write", None), pytest.raises(
        ValueError, match="NETBOX_WRITE_TOKEN"
    ):
        netbox_custom_object_type_create("Fiber Splice")


def test_custom_object_type_create_minimal():
    mock_write = MagicMock()
    mock_write.create.return_value = {"id": 1, "name": "Fiber Splice", "slug": "fiber-splice"}
    with patch("netbox_mcp_server.server.netbox_write", mock_write):
        result = netbox_custom_object_type_create("Fiber Splice")
    mock_write.create.assert_called_once_with(
        "plugins/custom-objects/custom-object-types",
        {"name": "Fiber Splice", "slug": "fiber-splice"},
    )
    assert result["slug"] == "fiber-splice"


def test_custom_object_type_create_with_description():
    mock_write = MagicMock()
    mock_write.create.return_value = {"id": 1}
    with patch("netbox_mcp_server.server.netbox_write", mock_write):
        netbox_custom_object_type_create("Fiber Splice", description="A splice point")
    sent_data = mock_write.create.call_args.args[1]
    assert sent_data["description"] == "A splice point"


def test_custom_object_type_create_with_verbose_names():
    mock_write = MagicMock()
    mock_write.create.return_value = {"id": 1}
    with patch("netbox_mcp_server.server.netbox_write", mock_write):
        netbox_custom_object_type_create(
            "Fiber Splice",
            verbose_name="Fiber Splice",
            verbose_name_plural="Fiber Splices",
        )
    sent_data = mock_write.create.call_args.args[1]
    assert sent_data["verbose_name"] == "Fiber Splice"
    assert sent_data["verbose_name_plural"] == "Fiber Splices"


def test_custom_object_type_create_omits_empty_optional_fields():
    """Optional fields with empty string defaults should not be sent."""
    mock_write = MagicMock()
    mock_write.create.return_value = {"id": 1}
    with patch("netbox_mcp_server.server.netbox_write", mock_write):
        netbox_custom_object_type_create("Fiber Splice")
    sent_data = mock_write.create.call_args.args[1]
    assert "description" not in sent_data
    assert "verbose_name" not in sent_data
    assert "verbose_name_plural" not in sent_data


# ============================================================================
# netbox_custom_object_type_field_create
# ============================================================================


def test_field_create_raises_without_write_token():
    with patch("netbox_mcp_server.server.netbox_write", None), pytest.raises(
        ValueError, match="NETBOX_WRITE_TOKEN"
    ):
        netbox_custom_object_type_field_create(1, "fiber_count", "Fiber Count", "integer")


def test_field_create_invalid_type():
    mock_write = MagicMock()
    with patch("netbox_mcp_server.server.netbox_write", mock_write), pytest.raises(
        ValueError, match="Invalid field type"
    ):
        netbox_custom_object_type_field_create(1, "x", "X", "invalid_type")


def test_field_create_select_requires_choice_set():
    mock_write = MagicMock()
    with patch("netbox_mcp_server.server.netbox_write", mock_write), pytest.raises(
        ValueError, match="requires choice_set_id"
    ):
        netbox_custom_object_type_field_create(1, "status", "Status", "select")


def test_field_create_multiselect_requires_choice_set():
    mock_write = MagicMock()
    with patch("netbox_mcp_server.server.netbox_write", mock_write), pytest.raises(
        ValueError, match="requires choice_set_id"
    ):
        netbox_custom_object_type_field_create(1, "tags", "Tags", "multiselect")


def test_field_create_object_requires_related_type():
    mock_write = MagicMock()
    with patch("netbox_mcp_server.server.netbox_write", mock_write), pytest.raises(
        ValueError, match="requires related_object_type"
    ):
        netbox_custom_object_type_field_create(1, "device", "Device", "object")


def test_field_create_multiobject_requires_related_type():
    mock_write = MagicMock()
    with patch("netbox_mcp_server.server.netbox_write", mock_write), pytest.raises(
        ValueError, match="requires related_object_type"
    ):
        netbox_custom_object_type_field_create(1, "devices", "Devices", "multiobject")


def test_field_create_integer():
    mock_write = MagicMock()
    mock_write.create.return_value = {"id": 1}
    with patch("netbox_mcp_server.server.netbox_write", mock_write):
        netbox_custom_object_type_field_create(1, "fiber_count", "Fiber Count", "integer")
    sent_data = mock_write.create.call_args.args[1]
    assert sent_data["name"] == "fiber_count"
    assert sent_data["label"] == "Fiber Count"
    assert sent_data["type"] == "integer"
    assert sent_data["custom_object_type"] == 1


def test_field_create_select_with_choice_set():
    mock_write = MagicMock()
    mock_write.create.return_value = {"id": 1}
    with patch("netbox_mcp_server.server.netbox_write", mock_write):
        netbox_custom_object_type_field_create(
            1, "status", "Status", "select", choice_set_id=5
        )
    sent_data = mock_write.create.call_args.args[1]
    assert sent_data["choice_set"] == 5


def test_field_create_object_with_related_type():
    mock_write = MagicMock()
    mock_write.create.return_value = {"id": 1}
    mock_read = MagicMock()
    mock_read.get.return_value = {"results": [{"id": 7}]}
    with patch("netbox_mcp_server.server.netbox_write", mock_write), \
         patch("netbox_mcp_server.server.netbox", mock_read):
        netbox_custom_object_type_field_create(
            1, "site", "Site", "object", related_object_type="dcim.site"
        )
    mock_read.get.assert_called_once_with(
        "core/object-types", params={"app_label": "dcim", "model": "site"}
    )
    sent_data = mock_write.create.call_args.args[1]
    assert sent_data["app_label"] == "dcim"
    assert sent_data["model"] == "site"
    assert "related_object_type" not in sent_data


def test_field_create_all_valid_types():
    """Every supported type should pass validation (no ValueError for type itself)."""
    valid_types = [
        "text",
        "longtext",
        "integer",
        "decimal",
        "boolean",
        "date",
        "datetime",
        "url",
        "json",
    ]
    mock_write = MagicMock()
    mock_write.create.return_value = {"id": 1}
    for field_type in valid_types:
        with patch("netbox_mcp_server.server.netbox_write", mock_write):
            netbox_custom_object_type_field_create(1, "f", "F", field_type)


def test_field_create_required_flag():
    mock_write = MagicMock()
    mock_write.create.return_value = {"id": 1}
    with patch("netbox_mcp_server.server.netbox_write", mock_write):
        netbox_custom_object_type_field_create(
            1, "fiber_count", "Fiber Count", "integer", required=True
        )
    sent_data = mock_write.create.call_args.args[1]
    assert sent_data["required"] is True


def test_field_create_calls_correct_endpoint():
    mock_write = MagicMock()
    mock_write.create.return_value = {"id": 1}
    with patch("netbox_mcp_server.server.netbox_write", mock_write):
        netbox_custom_object_type_field_create(1, "f", "F", "text")
    called_endpoint = mock_write.create.call_args.args[0]
    assert called_endpoint == "plugins/custom-objects/custom-object-type-fields"
