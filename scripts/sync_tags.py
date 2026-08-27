#!/usr/bin/env python3
"""
Sync tag definitions between two NetBox instances.

Reads from SOURCE and upserts into TARGET. Tags that already exist in the
target (matched by slug) are updated when color, description, or object_types
has drifted, and skipped otherwise. Run with --dry-run to preview changes
without writing anything.

Usage:
    python scripts/sync_tags.py \
        --source-url https://netbox.example.com \
        --source-token <token> \
        --target-url https://netbox.test.example.com \
        --target-token <token> \
        [--dry-run]

All arguments can also be supplied via environment variables:
    SOURCE_URL, SOURCE_TOKEN, TARGET_URL, TARGET_TOKEN

Notes:
  - Tags are matched by slug (unique within a NetBox instance, and the
    identifier code actually references — see NetBoxTags-style constants).
  - object_types is a list of dotted content-type strings (e.g.
    "dcim.interface") and is stable across instances — no ID translation.
  - A drifted object_types list is UPDATED, not just reported — same
    update-and-report posture sync_custom_fields.py takes on choice_set/
    related_object_type mismatches.
"""

import argparse
import importlib.util
import os
import time

# Load netbox_client directly to avoid pulling in the full package (__init__ → config → pydantic)
_client_path = os.path.join(
    os.path.dirname(__file__), "..", "src", "netbox_mcp_server", "netbox_client.py"
)
_spec = importlib.util.spec_from_file_location("netbox_client", _client_path)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
NetBoxRestClient = _mod.NetBoxRestClient


def _create_with_retry(
    client: NetBoxRestClient,
    endpoint: str,
    payload: dict,
    max_retries: int = 3,
    base_delay: float = 2.0,
) -> dict:
    """Create an object, retrying on transient PostgreSQL deadlock errors."""
    for attempt in range(max_retries):
        try:
            return client.create(endpoint, payload)
        except ValueError as exc:
            if "deadlock" not in str(exc).lower() or attempt == max_retries - 1:
                raise
            delay = base_delay * (2**attempt)
            print(
                f"    deadlock detected — retrying in {delay:.0f}s (attempt {attempt + 1}/{max_retries})"
            )
            time.sleep(delay)
    raise RuntimeError("unreachable")  # pragma: no cover


def fetch_all(client: NetBoxRestClient, endpoint: str, params: dict | None = None) -> list[dict]:
    results = []
    offset = 0
    limit = 200
    base_params = params or {}
    while True:
        resp = client.get(endpoint, params={"limit": limit, "offset": offset, **base_params})
        page = resp.get("results", [])
        results.extend(page)
        if len(results) >= resp.get("count", 0) or not page:
            break
        offset += limit
    return results


def sync(
    source: NetBoxRestClient,
    target: NetBoxRestClient,
    dry_run: bool,
) -> None:
    print("Fetching source tags...")
    src_tags = fetch_all(source, "extras/tags")
    print(f"  Source: {len(src_tags)} tags")

    print("Fetching target tags...")
    tgt_tags = fetch_all(target, "extras/tags")
    tgt_by_slug = {t["slug"]: t for t in tgt_tags}
    print(f"  Target: {len(tgt_tags)} tags")

    print("\n--- Tags ---")
    for tag in src_tags:
        slug = tag["slug"]
        name = tag["name"]
        existing = tgt_by_slug.get(slug)

        payload: dict = {
            "name": name,
            "slug": slug,
            "color": tag.get("color") or "",
            "description": tag.get("description") or "",
            "object_types": sorted(tag.get("object_types", [])),
        }

        if existing:
            existing_payload = {
                "name": existing.get("name"),
                "color": existing.get("color") or "",
                "description": existing.get("description") or "",
                "object_types": sorted(existing.get("object_types", [])),
            }
            needs_update = (
                existing_payload["name"] != payload["name"]
                or existing_payload["color"] != payload["color"]
                or existing_payload["description"] != payload["description"]
                or existing_payload["object_types"] != payload["object_types"]
            )

            if not needs_update:
                print(f"  SKIP  '{slug}' — already exists (id={existing['id']})")
                continue

            if dry_run:
                print(f"  DRY   '{slug}' — would update (drifted)")
            else:
                updated = target.update("extras/tags", existing["id"], payload)
                print(f"  UPDATE '{slug}' → id={updated['id']}")
        else:
            if dry_run:
                print(f"  DRY   '{slug}' — would create")
            else:
                created = _create_with_retry(target, "extras/tags", payload)
                print(f"  CREATE '{slug}' → id={created['id']}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--source-url",
        default=os.getenv("SOURCE_URL"),
        required=not os.getenv("SOURCE_URL"),
    )
    parser.add_argument(
        "--source-token",
        default=os.getenv("SOURCE_TOKEN"),
        required=not os.getenv("SOURCE_TOKEN"),
    )
    parser.add_argument(
        "--target-url",
        default=os.getenv("TARGET_URL"),
        required=not os.getenv("TARGET_URL"),
    )
    parser.add_argument(
        "--target-token",
        default=os.getenv("TARGET_TOKEN"),
        required=not os.getenv("TARGET_TOKEN"),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview changes without writing anything",
    )
    parser.add_argument(
        "--no-verify-ssl",
        action="store_true",
        help="Disable SSL certificate verification",
    )
    args = parser.parse_args()

    verify_ssl = not args.no_verify_ssl
    source = NetBoxRestClient(url=args.source_url, token=args.source_token, verify_ssl=verify_ssl)
    target = NetBoxRestClient(url=args.target_url, token=args.target_token, verify_ssl=verify_ssl)

    if args.dry_run:
        print("DRY RUN — no changes will be made\n")

    sync(source, target, dry_run=args.dry_run)
    print("\nDone.")


if __name__ == "__main__":
    main()
