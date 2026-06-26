#!/usr/bin/env python3
"""
Clear a single custom field on every interface in a NetBox instance.

Fetches all interfaces, selects the ones where the named custom field currently
holds a value, and clears it (sets it to null) via the bulk-PATCH endpoint.
NetBox merges custom_fields on PATCH, so only the named field is touched — every
other custom field on the interface is left intact.

SAFETY: this mutates data. The script previews by default (dry run). You must
pass --apply to actually write. Always run a dry run first and eyeball the count.

Usage:
    # preview (no writes)
    python scripts/clear_interface_cf.py --field junos_inet_mtu

    # actually clear
    python scripts/clear_interface_cf.py --field junos_inet_mtu --apply

URL and token come from --url/--token or the NETBOX_URL / NETBOX_TOKEN env vars.
Use --endpoint virtualization/interfaces to target VM interfaces instead.
"""

import argparse
import importlib.util
import os
import time
from typing import Any

import httpx

# Load netbox_client directly to avoid pulling in the full package (__init__ → config → pydantic)
_client_path = os.path.join(os.path.dirname(__file__), "..", "src", "netbox_mcp_server", "netbox_client.py")
_spec = importlib.util.spec_from_file_location("netbox_client", _client_path)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
NetBoxRestClient = _mod.NetBoxRestClient


def fetch_all(client: NetBoxRestClient, endpoint: str, params: dict | None = None, limit: int = 1000) -> list[dict]:
    """Page through a list endpoint and return every result."""
    results: list[dict] = []
    offset = 0
    base_params = params or {}
    while True:
        resp = client.get(endpoint, params={"limit": limit, "offset": offset, **base_params})
        page = resp.get("results", [])
        results.extend(page)
        if len(results) >= resp.get("count", 0) or not page:
            break
        offset += limit
    return results


def _is_empty(value: Any) -> bool:
    """True when the custom field already holds no value (nothing to clear)."""
    return value is None or value == "" or value == [] or value == {}


def _bulk_patch(client: NetBoxRestClient, endpoint: str, payload: list[dict]) -> None:
    """Bulk-update a batch the way NetBox actually expects it: PATCH the list endpoint
    with a JSON array (each item carrying its `id`). Note: the shared client's
    `bulk_update` targets a non-existent `…/bulk/` sub-path, so we PATCH directly here.
    """
    url = f"{client.api_url}/{endpoint.strip('/')}/"
    response = client.session.patch(url, json=payload)
    if not response.is_success:
        raise ValueError(f"bulk PATCH {url} failed {response.status_code}: {response.text}")


def _bulk_update_with_retry(
    client: NetBoxRestClient,
    endpoint: str,
    payload: list[dict],
    max_retries: int = 3,
    base_delay: float = 2.0,
) -> None:
    """Bulk-PATCH a batch, retrying on transient PostgreSQL deadlock errors."""
    for attempt in range(max_retries):
        try:
            _bulk_patch(client, endpoint, payload)
            return
        except ValueError as exc:
            if "deadlock" not in str(exc).lower() or attempt == max_retries - 1:
                raise
            delay = base_delay * (2**attempt)
            print(f"    deadlock detected — retrying in {delay:.0f}s (attempt {attempt + 1}/{max_retries})")
            time.sleep(delay)


def clear_cf(
    client: NetBoxRestClient,
    endpoint: str,
    field: str,
    apply: bool,
    batch_size: int,
) -> None:
    # Confirm the field exists up front (cheap, definitive) so a typo fails loudly
    # instead of looking like "0 interfaces had a value".
    cf_defs = client.get("extras/custom-fields", params={"name": field})
    if cf_defs.get("count", 0) == 0:
        raise SystemExit(
            f"ERROR: custom field '{field}' does not exist. "
            f"Check the field name (it is the CF 'name', not its label)."
        )

    # Prefer a server-side filter so only interfaces that actually hold a value are fetched
    # (huge win on large fleets). Fall back to a full scan if this field type doesn't support
    # the __empty lookup. The client-side _is_empty re-check below covers the fallback path.
    print(f"Fetching {endpoint} where '{field}' is set ...")
    try:
        candidates = fetch_all(client, endpoint, params={f"cf_{field}__empty": "false"})
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code != 400:
            raise
        print(f"  server-side cf filter unsupported for this field — scanning all {endpoint} (slower)")
        candidates = fetch_all(client, endpoint)
    print(f"  {len(candidates)} fetched")

    to_clear = [
        iface["id"]
        for iface in candidates
        if not _is_empty((iface.get("custom_fields") or {}).get(field))
    ]
    print(f"  {len(to_clear)} have a value for '{field}' and will be cleared")

    if not to_clear:
        print("Nothing to do.")
        return

    if not apply:
        sample = ", ".join(str(i) for i in to_clear[:10])
        print(f"\nDRY RUN — pass --apply to clear. First ids: {sample}{' ...' if len(to_clear) > 10 else ''}")
        return

    cleared = 0
    for start in range(0, len(to_clear), batch_size):
        batch = to_clear[start : start + batch_size]
        payload = [{"id": i, "custom_fields": {field: None}} for i in batch]
        _bulk_update_with_retry(client, endpoint, payload)
        cleared += len(batch)
        print(f"  cleared {cleared}/{len(to_clear)}")

    print(f"\nDone. Cleared '{field}' on {cleared} {endpoint} object(s).")


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--url", default=os.getenv("NETBOX_URL"), required=not os.getenv("NETBOX_URL"))
    parser.add_argument("--token", default=os.getenv("NETBOX_TOKEN"), required=not os.getenv("NETBOX_TOKEN"))
    parser.add_argument("--field", required=True, help="Custom field 'name' to clear (not its label)")
    parser.add_argument(
        "--endpoint",
        default="dcim/interfaces",
        help="List endpoint to target (default dcim/interfaces; use virtualization/interfaces for VM interfaces)",
    )
    parser.add_argument("--batch-size", type=int, default=100, help="Interfaces per bulk-PATCH request (default 100)")
    parser.add_argument("--apply", action="store_true", help="Actually write changes (default is a dry-run preview)")
    parser.add_argument("--no-verify-ssl", action="store_true", help="Disable SSL certificate verification")
    args = parser.parse_args()

    client = NetBoxRestClient(url=args.url, token=args.token, verify_ssl=not args.no_verify_ssl)

    if not args.apply:
        print("DRY RUN — no changes will be made (pass --apply to write)\n")

    clear_cf(client, args.endpoint, args.field, apply=args.apply, batch_size=args.batch_size)


if __name__ == "__main__":
    main()
