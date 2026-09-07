"""Tests for NetBoxRestClient fallback mechanism.

The fallback mechanism allows the client to try an alternative endpoint
when the primary endpoint returns 404. This is used for NetBox version
compatibility when endpoints move between API versions.
"""

from unittest.mock import MagicMock, patch

import httpx
import pytest

from netbox_mcp_server.netbox_client import _MAX_ERROR_BODY, NetBoxRestClient


@pytest.fixture
def client():
    """Create a test client."""
    return NetBoxRestClient(
        url="https://netbox.example.com",
        token="test-token",
        verify_ssl=True,
    )


def error_response(status_code: int, detail: str) -> MagicMock:
    """A response the client must raise on, carrying a body it is expected to report.

    The error path reads is_success, status_code, reason_phrase, text and request, so a
    bare MagicMock would look successful (every attribute is truthy) and the test would
    pass vacuously.
    """
    response = MagicMock()
    response.is_success = False
    response.status_code = status_code
    response.reason_phrase = httpx.codes.get_reason_phrase(status_code)
    response.text = '{"detail": "' + detail + '"}'
    response.request = httpx.Request("GET", "https://netbox.example.com/api/core/object-types/")
    return response


def success_response(payload: dict) -> MagicMock:
    """A 200 response carrying a JSON payload."""
    response = MagicMock()
    response.is_success = True
    response.status_code = 200
    response.json.return_value = payload
    return response


# ============================================================================
# Fallback Trigger Conditions
# ============================================================================


def test_fallback_triggered_on_404_with_fallback_endpoint(client):
    """When primary returns 404 and fallback provided, should try fallback."""
    primary_response = error_response(404, "Not found")
    fallback_response = success_response({"count": 1, "results": [{"id": 1}]})

    with patch.object(client.session, "get") as mock_get:
        mock_get.side_effect = [primary_response, fallback_response]

        result = client.get(
            "core/object-types",
            params={"limit": 5},
            fallback_endpoint="extras/object-types",
        )

        assert mock_get.call_count == 2
        # Verify first call was to primary endpoint
        assert "core/object-types" in mock_get.call_args_list[0][0][0]
        # Verify second call was to fallback endpoint
        assert "extras/object-types" in mock_get.call_args_list[1][0][0]
        assert result == {"count": 1, "results": [{"id": 1}]}


def test_fallback_not_triggered_on_success(client):
    """When primary succeeds, fallback should never be called."""
    primary_response = success_response({"count": 5, "results": []})

    with patch.object(client.session, "get") as mock_get:
        mock_get.return_value = primary_response

        result = client.get(
            "core/object-types",
            params={"limit": 5},
            fallback_endpoint="extras/object-types",
        )

        # Only one call should be made
        assert mock_get.call_count == 1
        assert result == {"count": 5, "results": []}


def test_fallback_not_triggered_on_non_404_error(client):
    """When primary returns 500/403/etc, should NOT try fallback."""
    primary_response = error_response(500, "Server error")

    with patch.object(client.session, "get") as mock_get:
        mock_get.return_value = primary_response

        with pytest.raises(httpx.HTTPStatusError):
            client.get(
                "core/object-types",
                fallback_endpoint="extras/object-types",
            )

        # Only primary should be called, not fallback
        assert mock_get.call_count == 1


def test_fallback_not_triggered_on_403_forbidden(client):
    """When primary returns 403 Forbidden, should NOT try fallback."""
    primary_response = error_response(403, "Forbidden")

    with patch.object(client.session, "get") as mock_get:
        mock_get.return_value = primary_response

        with pytest.raises(httpx.HTTPStatusError):
            client.get(
                "core/object-types",
                fallback_endpoint="extras/object-types",
            )

        assert mock_get.call_count == 1


def test_fallback_not_triggered_without_fallback_endpoint(client):
    """When no fallback provided, 404 should propagate immediately."""
    primary_response = error_response(404, "Not found")

    with patch.object(client.session, "get") as mock_get:
        mock_get.return_value = primary_response

        with pytest.raises(httpx.HTTPStatusError):
            client.get("core/object-types", fallback_endpoint=None)

        assert mock_get.call_count == 1


def test_fallback_not_triggered_with_empty_fallback(client):
    """When fallback is empty string, should not trigger fallback."""
    primary_response = error_response(404, "Not found")

    with patch.object(client.session, "get") as mock_get:
        mock_get.return_value = primary_response

        with pytest.raises(httpx.HTTPStatusError):
            client.get("core/object-types", fallback_endpoint="")

        # Empty string is falsy, so no fallback attempted
        assert mock_get.call_count == 1


# ============================================================================
# Fallback Error Handling
# ============================================================================


def test_fallback_error_propagates(client):
    """When fallback also fails, its error should propagate."""
    primary_response = error_response(404, "Not found")
    fallback_response = error_response(500, "Fallback failed")

    with patch.object(client.session, "get") as mock_get:
        mock_get.side_effect = [primary_response, fallback_response]

        with pytest.raises(httpx.HTTPStatusError, match="Fallback failed"):
            client.get(
                "core/object-types",
                fallback_endpoint="extras/object-types",
            )

        assert mock_get.call_count == 2


def test_both_endpoints_404_propagates_fallback_error(client):
    """When both primary and fallback return 404, fallback 404 propagates."""
    primary_response = error_response(404, "Not found")
    fallback_response = error_response(404, "Not found")

    with patch.object(client.session, "get") as mock_get:
        mock_get.side_effect = [primary_response, fallback_response]

        with pytest.raises(httpx.HTTPStatusError):
            client.get(
                "core/object-types",
                fallback_endpoint="extras/object-types",
            )

        assert mock_get.call_count == 2


# ============================================================================
# Error Reporting
# ============================================================================


def test_error_message_carries_the_response_body(client):
    """NetBox puts the reason in the body; a bare status line hides it."""
    primary_response = error_response(403, "You do not have permission to perform this action.")

    with patch.object(client.session, "get") as mock_get:
        mock_get.return_value = primary_response

        with pytest.raises(httpx.HTTPStatusError, match="You do not have permission"):
            client.get("core/object-types")


def test_error_message_caps_an_oversized_body(client):
    """A proxy answering with an HTML page must not flood the message."""
    primary_response = error_response(502, "x")
    primary_response.text = "x" * (_MAX_ERROR_BODY * 5)

    with patch.object(client.session, "get") as mock_get:
        mock_get.return_value = primary_response

        with pytest.raises(httpx.HTTPStatusError) as excinfo:
            client.get("core/object-types")

    detail = str(excinfo.value).split(": ", 1)[1]
    assert len(detail) == _MAX_ERROR_BODY
    assert detail.endswith("(truncated)")


def test_error_message_names_the_status(client):
    """The reason phrase is what makes a bare status code readable in a log."""
    primary_response = error_response(403, "Nope")

    with patch.object(client.session, "get") as mock_get:
        mock_get.return_value = primary_response

        with pytest.raises(httpx.HTTPStatusError, match="failed 403 Forbidden"):
            client.get("core/object-types")


def test_error_message_stays_on_one_line(client):
    """An HTML error page must not turn one failure into a multi-line log entry."""
    primary_response = error_response(502, "x")
    primary_response.text = "<html>\n  <body>\n    Bad Gateway\r\n  </body>\n</html>"

    with patch.object(client.session, "get") as mock_get:
        mock_get.return_value = primary_response

        with pytest.raises(httpx.HTTPStatusError) as excinfo:
            client.get("core/object-types")

    message = str(excinfo.value)
    assert "\n" not in message
    assert "\r" not in message
    assert "<html> <body> Bad Gateway </body> </html>" in message


def test_whitespace_heavy_body_still_fills_the_cap(client):
    """Collapsing runs on a bounded slice must still leave enough text to reach the cap."""
    primary_response = error_response(502, "x")
    primary_response.text = "x " * (_MAX_ERROR_BODY * 2)

    with patch.object(client.session, "get") as mock_get:
        mock_get.return_value = primary_response

        with pytest.raises(httpx.HTTPStatusError) as excinfo:
            client.get("core/object-types")

    detail = str(excinfo.value).split(": ", 1)[1]
    assert len(detail) == _MAX_ERROR_BODY


def test_redirect_raises_as_stock_httpx_does(client):
    """httpx.raise_for_status returns early only on is_success, so a 3xx is an error there too."""
    primary_response = error_response(302, "Found")

    with patch.object(client.session, "get") as mock_get:
        mock_get.return_value = primary_response

        with pytest.raises(httpx.HTTPStatusError):
            client.get("core/object-types")


# ============================================================================
# Parameter Passing
# ============================================================================


def test_fallback_preserves_params(client):
    """Fallback request should use same params as primary."""
    primary_response = error_response(404, "Not found")
    fallback_response = success_response({"results": []})

    with patch.object(client.session, "get") as mock_get:
        mock_get.side_effect = [primary_response, fallback_response]

        client.get(
            "core/object-types",
            params={"limit": 10, "name__ic": "test"},
            fallback_endpoint="extras/object-types",
        )

        # Both calls should have same params
        primary_params = mock_get.call_args_list[0][1]["params"]
        fallback_params = mock_get.call_args_list[1][1]["params"]
        assert primary_params == fallback_params == {"limit": 10, "name__ic": "test"}


def test_fallback_preserves_id_in_url(client):
    """When fetching by ID, fallback should include same ID."""
    primary_response = error_response(404, "Not found")
    fallback_response = success_response({"id": 123, "name": "test"})

    with patch.object(client.session, "get") as mock_get:
        mock_get.side_effect = [primary_response, fallback_response]

        client.get(
            "core/object-types",
            id=123,
            fallback_endpoint="extras/object-types",
        )

        # Both URLs should include the ID
        primary_url = mock_get.call_args_list[0][0][0]
        fallback_url = mock_get.call_args_list[1][0][0]
        assert "/123/" in primary_url
        assert "/123/" in fallback_url


# ============================================================================
# URL Construction
# ============================================================================


def test_fallback_builds_correct_url(client):
    """Fallback should build proper API URL."""
    primary_response = error_response(404, "Not found")
    fallback_response = success_response({"results": []})

    with patch.object(client.session, "get") as mock_get:
        mock_get.side_effect = [primary_response, fallback_response]

        client.get(
            "core/object-types",
            fallback_endpoint="extras/object-types",
        )

        fallback_url = mock_get.call_args_list[1][0][0]
        assert fallback_url == "https://netbox.example.com/api/extras/object-types/"
