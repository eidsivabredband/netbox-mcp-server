#!/usr/bin/env python3
"""
Sync custom field definitions between two NetBox instances.

Reads from SOURCE and upserts into TARGET. Fields that already exist in the
target are skipped unless their choice_set or related_object_type has drifted.
Run with --dry-run to preview changes without writing anything.

Usage:
    python scripts/sync_custom_fields.py \
        --source-url https://netbox.example.com \
        --source-token <token> \
        --target-url https://netbox.test.example.com \
        --target-token <token> \
        [--object-type dcim.interface] \
        [--dry-run]

All arguments can also be supplied via environment variables:
    SOURCE_URL, SOURCE_TOKEN, TARGET_URL, TARGET_TOKEN

Notes:
  - Fields are matched by name (unique within a NetBox instance).
  - object_types and related_object_type are dotted content-type strings
    (e.g. "dcim.interface") and are stable across instances — no ID translation.
  - choice_set is resolved by name; if the named choice set does not exist in
    the target the field is skipped with FAIL. Create the choice set first.
"""

import argparse
import importlib.util
import os

# Load netbox_client directly to avoid pulling in the full package (__init__ → config → pydantic)
_client_path = os.path.join(os.path.dirname(__file__), "..", "src", "netbox_mcp_server", "netbox_client.py")
_spec = importlib.util.spec_from_file_location("netbox_client", _client_path)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
NetBoxRestClient = _mod.NetBoxRestClient


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


def _choice_value(val: object) -> object:
    """Unwrap a NetBox choice dict {"value": ..., "label": ...} to its plain value."""
    if isinstance(val, dict) and "value" in val:
        return val["value"]
    return val


def sync_choice_sets(
    source: NetBoxRestClient,
    target: NetBoxRestClient,
    dry_run: bool,
    referenced_names: set[str],
) -> dict[str, int]:
    """Sync choice sets referenced by the fields being processed.

    Returns a dict of choice set name → target ID for all sets that exist (or
    were just created) on the target, so the field sync can use it directly
    without re-querying.
    """
    if not referenced_names:
        return {}

    print("\n--- Choice Sets ---")

    src_sets = fetch_all(source, "extras/custom-field-choice-sets")
    src_by_name = {cs["name"]: cs for cs in src_sets}

    tgt_sets = fetch_all(target, "extras/custom-field-choice-sets")
    tgt_by_name = {cs["name"]: cs for cs in tgt_sets}

    result: dict[str, int] = {cs["name"]: cs["id"] for cs in tgt_sets}

    for name in sorted(referenced_names):
        src_cs = src_by_name.get(name)
        if src_cs is None:
            print(f"  FAIL  '{name}' — not found in source")
            continue

        if name in tgt_by_name:
            print(f"  SKIP  '{name}' — already exists (id={tgt_by_name[name]['id']})")
            continue

        payload: dict = {"name": name}
        for key in ("description", "base_choices", "order_alphabetically"):
            val = src_cs.get(key)
            if val is not None and val != "":
                payload[key] = val
        extra = src_cs.get("extra_choices")
        if extra:
            payload["extra_choices"] = extra

        if dry_run:
            print(f"  DRY   '{name}' — would create")
        else:
            created = target.create("extras/custom-field-choice-sets", payload)
            result[name] = created["id"]
            print(f"  CREATE '{name}' → id={created['id']}")

    return result


def sync(
    source: NetBoxRestClient,
    target: NetBoxRestClient,
    dry_run: bool,
    object_type_filter: str | None,
) -> None:
    filter_params = {"object_types": object_type_filter} if object_type_filter else {}

    print("Fetching source custom fields...")
    src_fields = fetch_all(source, "extras/custom-fields", filter_params)
    print(f"  Source: {len(src_fields)} custom fields")

    print("Fetching target custom fields...")
    tgt_fields = fetch_all(target, "extras/custom-fields", filter_params)
    tgt_by_name = {f["name"]: f for f in tgt_fields}
    print(f"  Target: {len(tgt_fields)} custom fields")

    # Collect choice set names referenced by select/multiselect fields in the source
    referenced_choice_sets = {
        field["choice_set"]["name"]
        for field in src_fields
        if _choice_value(field.get("type")) in ("select", "multiselect")
        and isinstance(field.get("choice_set"), dict)
        and field["choice_set"].get("name")
    }
    tgt_choice_set_ids = sync_choice_sets(source, target, dry_run, referenced_choice_sets)

    print("\n--- Custom Fields ---")
    for field in src_fields:
        name = field["name"]
        field_type = _choice_value(field.get("type"))
        existing = tgt_by_name.get(name)

        # Build payload — start with all stable scalar fields
        payload: dict = {
            "object_types": field.get("object_types", []),
            "type": field_type,
            "name": name,
        }
        for key in (
            "label",
            "group_name",
            "description",
            "required",
            "unique",
            "search_weight",
            "is_cloneable",
            "default",
            "weight",
            "validation_minimum",
            "validation_maximum",
            "validation_regex",
            "comments",
        ):
            val = field.get(key)
            if val is not None and val != "":
                payload[key] = val

        # Choice fields come back as {"value": ..., "label": ...} from GET — unwrap for POST
        for key in ("filter_logic", "ui_visible", "ui_editable"):
            val = _choice_value(field.get(key))
            if val is not None and val != "":
                payload[key] = val

        # related_object_type — stable dotted string, copy directly
        resolved_rot = None
        if field_type in ("object", "multiobject"):
            rot = field.get("related_object_type")
            rot = _choice_value(rot)  # may be a dict with "value" in some versions
            if not rot:
                print(f"  FAIL  {name} — object/multiobject field has no related_object_type")
                continue
            resolved_rot = rot
            payload["related_object_type"] = rot

        # choice_set — look up from the pre-synced map
        resolved_cs_id = None
        if field_type in ("select", "multiselect"):
            cs = field.get("choice_set")
            cs_name = cs.get("name") if isinstance(cs, dict) else None
            if not cs_name:
                print(f"  FAIL  {name} — select/multiselect field has no resolvable choice_set")
                continue
            resolved_cs_id = tgt_choice_set_ids.get(cs_name)
            if resolved_cs_id is None:
                print(f"  FAIL  {name} — choice_set '{cs_name}' not available on target (check pre-pass output)")
                continue
            payload["choice_set"] = resolved_cs_id

        if existing:
            needs_update = False
            if resolved_rot is not None:
                existing_rot = _choice_value(existing.get("related_object_type"))
                if existing_rot != resolved_rot:
                    needs_update = True
            if resolved_cs_id is not None:
                existing_cs = existing.get("choice_set")
                existing_cs_id = existing_cs.get("id") if isinstance(existing_cs, dict) else existing_cs
                if existing_cs_id != resolved_cs_id:
                    needs_update = True

            if not needs_update:
                print(f"  SKIP  {name} ({field_type}) — already exists")
                continue

            if dry_run:
                print(f"  DRY   {name} ({field_type}) — would update (FK mismatch)")
            else:
                updated = target.update("extras/custom-fields", existing["id"], payload)
                print(f"  UPDATE {name} ({field_type}) → id={updated['id']}")
        else:
            if dry_run:
                print(f"  DRY   {name} ({field_type}) — would create")
            else:
                created = target.create("extras/custom-fields", payload)
                print(f"  CREATE {name} ({field_type}) → id={created['id']}")


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
        "--object-type",
        metavar="APP.MODEL",
        help="Only sync fields assigned to this object type (e.g. dcim.interface)",
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

    sync(source, target, dry_run=args.dry_run, object_type_filter=args.object_type)
    print("\nDone.")


if __name__ == "__main__":
    main()
