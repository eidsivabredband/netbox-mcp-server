#!/usr/bin/env python3
"""
Sync custom object type definitions (types + fields) between two NetBox instances.

Reads from SOURCE and upserts into TARGET. Existing types/fields in the target
are skipped; missing ones are created. Run with --dry-run to preview changes.

Usage:
    python scripts/sync_custom_object_types.py \
        --source-url https://netbox.example.com \
        --source-token <token> \
        --target-url https://netbox.test.example.com \
        --target-token <token> \
        [--dry-run]

All arguments can also be supplied via environment variables:
    SOURCE_URL, SOURCE_TOKEN, TARGET_URL, TARGET_TOKEN
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


def fetch_all(client: NetBoxRestClient, endpoint: str) -> list[dict]:
    results = []
    offset = 0
    limit = 200
    while True:
        resp = client.get(endpoint, params={"limit": limit, "offset": offset})
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
    print("Fetching source custom object types and fields...")
    src_types = fetch_all(source, "plugins/custom-objects/custom-object-types")
    src_fields = fetch_all(source, "plugins/custom-objects/custom-object-type-fields")
    src_types_by_id = {t["id"]: t for t in src_types}
    src_types_by_slug = {t["slug"]: t for t in src_types}
    print(f"  Source: {len(src_types)} types, {len(src_fields)} fields")

    print("Fetching core object types (for FK field resolution)...")
    src_core_obj_types = fetch_all(source, "core/object-types")
    src_obj_type_by_id = {ot["id"]: ot for ot in src_core_obj_types}
    tgt_core_obj_types = fetch_all(target, "core/object-types")
    # (app_label, model) → target content-type entry
    tgt_obj_type_by_label_model = {(ot["app_label"], ot["model"]): ot for ot in tgt_core_obj_types}
    print(f"  Source: {len(src_core_obj_types)} core object types, Target: {len(tgt_core_obj_types)} core object types")

    print("Fetching source choice sets (for select field resolution)...")
    src_choice_sets = fetch_all(source, "extras/custom-field-choice-sets")
    src_choice_set_by_id = {cs["id"]: cs for cs in src_choice_sets}
    print(f"  Source: {len(src_choice_sets)} choice sets")

    print("Fetching target custom object types and fields...")
    tgt_types = fetch_all(target, "plugins/custom-objects/custom-object-types")
    tgt_fields = fetch_all(target, "plugins/custom-objects/custom-object-type-fields")
    tgt_types_by_slug = {t["slug"]: t for t in tgt_types}
    tgt_types_by_id = {t["id"]: t for t in tgt_types}
    tgt_obj_type_by_id = {ot["id"]: ot for ot in tgt_core_obj_types}

    print("Fetching target choice sets (for select field comparison)...")
    tgt_choice_sets = fetch_all(target, "extras/custom-field-choice-sets")
    tgt_choice_set_by_id = {cs["id"]: cs for cs in tgt_choice_sets}
    print(f"  Target: {len(tgt_choice_sets)} choice sets")

    def _resolve_slug(type_ref, by_id_map: dict) -> str:
        if isinstance(type_ref, dict):
            return type_ref.get("slug", "")
        if isinstance(type_ref, int):
            return by_id_map.get(type_ref, {}).get("slug", "")
        return ""

    # Dict instead of set so we can access existing field id/values for upsert comparison
    tgt_fields_dict = {}
    for f in tgt_fields:
        if f.get("custom_object_type"):
            slug = _resolve_slug(f["custom_object_type"], tgt_types_by_id)
            tgt_fields_dict[(slug, f["name"])] = f
    print(f"  Target: {len(tgt_types)} types, {len(tgt_fields)} fields")

    # slug → target ID, pre-seeded with already-existing target types.
    # In dry-run mode, would-create types are added with _DRY_ID as a placeholder so that
    # field resolution can proceed and show DRY instead of FAIL.
    _DRY_ID = 0
    slug_to_target_id = {slug: t["id"] for slug, t in tgt_types_by_slug.items()}

    print("\n--- Types ---")
    for src_type in src_types:
        slug = src_type["slug"]
        if slug in tgt_types_by_slug:
            print(f"  SKIP  {slug} (already exists, id={tgt_types_by_slug[slug]['id']})")
            continue

        payload = {"name": src_type["name"], "slug": slug}
        for opt in ("description", "verbose_name", "verbose_name_plural"):
            if src_type.get(opt):
                payload[opt] = src_type[opt]

        if dry_run:
            print(f"  DRY   {slug} — would create")
            slug_to_target_id[slug] = _DRY_ID
            tgt_types_by_slug[slug] = {"id": _DRY_ID, "slug": slug}
        else:
            created = target.create("plugins/custom-objects/custom-object-types", payload)
            slug_to_target_id[slug] = created["id"]
            print(f"  CREATE {slug} → id={created['id']}")

    # After creating all types, re-fetch target state so that field FK resolution sees the
    # newly-created types and their auto-registered content types.  Skipped in dry-run because
    # no types were actually written.
    if not dry_run:
        print("\n  (Re-fetching target state after type creation...)")
        tgt_types = fetch_all(target, "plugins/custom-objects/custom-object-types")
        tgt_types_by_slug = {t["slug"]: t for t in tgt_types}
        tgt_types_by_id = {t["id"]: t for t in tgt_types}
        tgt_core_obj_types = fetch_all(target, "core/object-types")
        tgt_obj_type_by_id = {ot["id"]: ot for ot in tgt_core_obj_types}
        tgt_obj_type_by_label_model = {(ot["app_label"], ot["model"]): ot for ot in tgt_core_obj_types}
        slug_to_target_id = {slug: t["id"] for slug, t in tgt_types_by_slug.items()}

    print("\n--- Fields ---")
    for field in src_fields:
        src_type_slug = _resolve_slug(field.get("custom_object_type"), src_types_by_id)
        field_name = field["name"]
        key = (src_type_slug, field_name)
        existing_field = tgt_fields_dict.get(key)

        target_type_id = slug_to_target_id.get(src_type_slug)
        if target_type_id is None:
            print(f"  FAIL  {src_type_slug}.{field_name} — target type not found (was source type synced?)")
            continue

        field_type = field["type"]["value"] if isinstance(field["type"], dict) else field["type"]

        payload: dict = {
            "custom_object_type": target_type_id,
            "name": field_name,
            "label": field.get("label") or field_name,
            "type": field_type,
            "required": field.get("required", False),
        }
        for opt in ("description", "primary", "unique", "default"):
            if field.get(opt):
                payload[opt] = field[opt]

        resolved_rot = None  # (app_label, model) resolved for the target
        if field_type in ("object", "multiobject"):
            rot_id = field.get("related_object_type")
            if rot_id is None:
                print(f"  FAIL  {src_type_slug}.{field_name} — no related_object_type in source field")
                continue
            src_ot = src_obj_type_by_id.get(rot_id)
            if src_ot is None:
                print(f"  FAIL  {src_type_slug}.{field_name} — related_object_type id={rot_id} not in source core/object-types")
                continue
            app_label = src_ot["app_label"]
            model = src_ot["model"]
            # For custom object types the model name may be instance-specific (e.g. a numeric
            # suffix).  Resolve by slug: find which source custom type this content-type belongs to,
            # then look up the same slug on the target's core/object-types.
            if "custom_objects" in app_label:
                # app_label may be "custom_objects" or "netbox_custom_objects" depending on the
                # plugin version/instance — treat both as the same custom-objects namespace.
                # The content-type model name is tableNmodel where N is the custom type's PK.
                # The source type API response has no "model" or "content_type_id" field,
                # so parse N out of the model name and match by id.
                src_type_id_from_model = None
                if model.startswith("table") and model.endswith("model"):
                    try:
                        src_type_id_from_model = int(model[5:-5])
                    except ValueError:
                        pass
                src_ref_slug = next(
                    (t["slug"] for t in src_types if t.get("content_type_id") == rot_id
                     or t.get("model") == model or t.get("slug") == model
                     or (src_type_id_from_model is not None and t["id"] == src_type_id_from_model)),
                    None,
                )
                if src_ref_slug is None:
                    print(f"  FAIL  {src_type_slug}.{field_name} — cannot map custom_objects model '{model}' (id={rot_id}) to a source slug")
                    continue
                tgt_ref = tgt_types_by_slug.get(src_ref_slug)
                if tgt_ref is None:
                    print(f"  FAIL  {src_type_slug}.{field_name} — referenced custom type '{src_ref_slug}' not in target (sync types first?)")
                    continue
                # In dry-run the referenced type may be a placeholder (not yet created).
                # Its content type won't exist until the real run, so skip FK resolution.
                if tgt_ref.get("id") != _DRY_ID:
                    # Derive the target model name from the target type's own ID.
                    # Custom object types use tableNmodel naming where N is the type's primary key.
                    # Do NOT fall back to the source model name — source and target IDs can differ.
                    tgt_model = f"table{tgt_ref['id']}model"
                    tgt_ot = next((ot for ot in tgt_core_obj_types if ot["model"] == tgt_model), None)
                    if tgt_ot is None:
                        print(f"  FAIL  {src_type_slug}.{field_name} — no content type for model '{tgt_model}' in target")
                        continue
                    app_label = tgt_ot["app_label"]
                    model = tgt_ot["model"]
                    resolved_rot = (app_label, model)
                    payload["app_label"] = app_label
                    payload["model"] = model
            else:
                # Standard NetBox content type — app_label+model is stable across instances
                if (app_label, model) not in tgt_obj_type_by_label_model:
                    print(f"  FAIL  {src_type_slug}.{field_name} — content type {app_label}.{model} not found in target")
                    continue
                resolved_rot = (app_label, model)
                payload["app_label"] = app_label
                payload["model"] = model

        resolved_cs_id = None
        if field_type in ("select", "multiselect"):
            cs = field.get("choice_set")
            if isinstance(cs, dict):
                cs_name = cs.get("name")
            elif isinstance(cs, int):
                cs_name = src_choice_set_by_id.get(cs, {}).get("name")
            else:
                cs_name = None
            if not cs_name:
                print(f"  FAIL  {src_type_slug}.{field_name} — select field has no resolvable choice_set (raw value: {cs!r})")
                continue
            resp = target.get("extras/custom-field-choice-sets", params={"name": cs_name, "limit": 1})
            tgt_cs = resp.get("results", [])
            if not tgt_cs:
                print(f"  FAIL  {src_type_slug}.{field_name} — choice_set '{cs_name}' not found in target")
                continue
            resolved_cs_id = tgt_cs[0]["id"]
            payload["choice_set"] = resolved_cs_id

        if existing_field:
            # Determine if FK-derived fields differ from what's stored on the target
            needs_update = False
            if resolved_rot is not None:
                existing_rot = existing_field.get("related_object_type")
                existing_rot_id = existing_rot.get("id") if isinstance(existing_rot, dict) else existing_rot
                existing_ot = tgt_obj_type_by_id.get(existing_rot_id, {})
                if (existing_ot.get("app_label"), existing_ot.get("model")) != resolved_rot:
                    needs_update = True
            if resolved_cs_id is not None:
                existing_cs = existing_field.get("choice_set")
                existing_cs_id = existing_cs.get("id") if isinstance(existing_cs, dict) else existing_cs
                if existing_cs_id != resolved_cs_id:
                    needs_update = True

            if not needs_update:
                print(f"  SKIP  {src_type_slug}.{field_name} (up to date)")
                continue

            if dry_run:
                print(f"  DRY   {src_type_slug}.{field_name} ({field_type}) — would update (FK mismatch)")
            else:
                updated = target.update(
                    "plugins/custom-objects/custom-object-type-fields",
                    existing_field["id"],
                    payload,
                )
                print(f"  UPDATE {src_type_slug}.{field_name} ({field_type}) → id={updated['id']}")
        else:
            if dry_run:
                print(f"  DRY   {src_type_slug}.{field_name} ({field_type}) — would create")
            else:
                created = target.create("plugins/custom-objects/custom-object-type-fields", payload)
                print(f"  CREATE {src_type_slug}.{field_name} ({field_type}) → id={created['id']}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--source-url", default=os.getenv("SOURCE_URL"), required=not os.getenv("SOURCE_URL"))
    parser.add_argument("--source-token", default=os.getenv("SOURCE_TOKEN"), required=not os.getenv("SOURCE_TOKEN"))
    parser.add_argument("--target-url", default=os.getenv("TARGET_URL"), required=not os.getenv("TARGET_URL"))
    parser.add_argument("--target-token", default=os.getenv("TARGET_TOKEN"), required=not os.getenv("TARGET_TOKEN"))
    parser.add_argument("--dry-run", action="store_true", help="Preview changes without writing anything")
    parser.add_argument("--no-verify-ssl", action="store_true", help="Disable SSL certificate verification")
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
