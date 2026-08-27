#!/usr/bin/env python3
"""
Sync custom object *instances* (the content) between two NetBox instances.

Where sync_custom_object_types.py copies the schema (types and their fields),
this script copies the rows. Reads from SOURCE and upserts into TARGET, matching
instances on a natural key derived from the type's own field definitions.

Usage:
    python scripts/sync_custom_objects.py \
        --source-url https://netbox.example.com \
        --source-token <token> \
        --target-url https://netbox.test.example.com \
        --target-token <token> \
        --type per-device-name-server \
        [--type per-device-syslog-host ...] \
        [--match-field device --match-field address] \
        [--filter device_id=42] \
        [--prune] [--dry-run]

    # Every type that exists in both instances, dependency-ordered:
    python scripts/sync_custom_objects.py ... --all-types

All connection arguments can also be supplied via environment variables:
    SOURCE_URL, SOURCE_TOKEN, TARGET_URL, TARGET_TOKEN

Matching
    Instances have no slug, so the match key is derived per type, in this order:
      1. --match-field (repeatable, forms a composite key; needs a single --type)
      2. all fields flagged unique=true on the type
      3. the field flagged primary=true, plus every object/multiobject field
         (the FKs are what scope an otherwise-repeated primary value, e.g. the
         same NTP address on two devices)
    The chosen key is printed per type - check it before a non-dry run.

Foreign keys
    An object/multiobject value cannot be copied verbatim: the id belongs to the
    source instance. Each reference is re-resolved against the target by natural
    key (slug, address, prefix, vid, name, ...), scoped by its own parent
    reference (device, site, vrf, ...) where the source payload exposes one.
    A reference that resolves to zero or several target objects is reported and
    the whole instance is SKIPPED - never written with a guessed id.

    References to other custom object types are resolved through that type's own
    match key, so those types must be synced too; --all-types orders them
    automatically, and an explicit --type list is reordered the same way.

Pruning
    --prune deletes target instances whose match key is absent from the source.
    It is skipped for any type where a source instance failed to resolve: that
    instance contributed no key, so its target counterpart would look orphaned.

Not translated
    - Polymorphic object fields (related_object_types): reported and skipped.
    - FK-valued custom fields on the instance: reported and skipped (scalar
      custom field values are copied).
    Tags are copied by slug and must already exist in the target - see
    scripts/sync_tags.py.
"""

import argparse
import importlib.util
import os
import time

# Load the client and type map directly to avoid pulling in the full package
# (__init__ -> config -> pydantic), matching the other scripts in this directory.
_SRC_DIR = os.path.join(os.path.dirname(__file__), "..", "src", "netbox_mcp_server")


def _load_module(name: str):
    """Import a single module file from the package source directory."""
    spec = importlib.util.spec_from_file_location(name, os.path.join(_SRC_DIR, f"{name}.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


NetBoxRestClient = _load_module("netbox_client").NetBoxRestClient
NETBOX_OBJECT_TYPES = _load_module("netbox_types").NETBOX_OBJECT_TYPES

TYPES_ENDPOINT = "plugins/custom-objects/custom-object-types"
FIELDS_ENDPOINT = "plugins/custom-objects/custom-object-type-fields"

# The plugin app label differs between plugin versions/instances.
CUSTOM_OBJECT_APP_LABELS = ("custom_objects", "netbox_custom_objects")

# Native model fields worth copying when the serializer exposes them.
EXTRA_COPYABLE_KEYS = ("description", "comments")

# Tried in order when re-resolving a foreign key against the target.
NATURAL_KEY_FIELDS = ("slug", "address", "prefix", "vid", "asn", "rd", "name", "label")

# Parent references that scope an otherwise ambiguous natural key (an interface
# name is unique per device, not per fleet). Resolved recursively.
SCOPE_REF_FIELDS = ("device", "virtual_machine", "site", "rack", "vrf", "tenant", "module")

OBJECT_FIELD_TYPES = ("object", "multiobject")

MAX_RESOLUTION_DEPTH = 5


class ReferenceResolutionError(Exception):
    """A source foreign key could not be mapped to exactly one target object."""


def fetch_all(client: NetBoxRestClient, endpoint: str, params: dict | None = None) -> list[dict]:
    """Fetch every page of a list endpoint."""
    results: list[dict] = []
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
                f"    deadlock detected - retrying in {delay:.0f}s (attempt {attempt + 1}/{max_retries})"
            )
            time.sleep(delay)
    raise RuntimeError("unreachable")  # pragma: no cover


def field_type(field: dict) -> str:
    """Read a field's type, which the API returns either as a string or as {'value': ...}."""
    raw = field.get("type")
    if isinstance(raw, dict):
        return raw.get("value", "")
    return raw or ""


def type_id_of(ref) -> int | None:
    """Read the custom_object_type reference off a field definition (id or brief object)."""
    if isinstance(ref, dict):
        return ref.get("id")
    if isinstance(ref, int):
        return ref
    return None


def object_type_of(ref) -> tuple[int | None, str, str]:
    """Read a related_object_type reference as (id, app_label, model)."""
    if isinstance(ref, dict):
        return ref.get("id"), ref.get("app_label", ""), ref.get("model", "")
    if isinstance(ref, int):
        return ref, "", ""
    return None, "", ""


def instance_endpoint(slug: str) -> str:
    """The REST endpoint serving instances of one custom object type."""
    return f"plugins/custom-objects/{slug}"


def custom_object_type_id_from_model(model: str) -> int | None:
    """Parse the type's primary key out of the plugin's 'tableNmodel' content-type name."""
    if not model.startswith("table") or not model.endswith("model"):
        return None
    try:
        return int(model[5:-5])
    except ValueError:
        return None


def normalize_value(value):
    """Reduce an API value to a comparable form: brief objects to their id, lists to a sorted tuple."""
    if isinstance(value, dict):
        return value.get("id")
    if isinstance(value, list):
        return tuple(sorted(str(normalize_value(v)) for v in value))
    return value


def values_equal(left, right) -> bool:
    """Compare two API values, tolerating null/empty and int-vs-string representations."""
    left = normalize_value(left)
    right = normalize_value(right)
    if left in (None, "", ()) and right in (None, "", ()):
        return True
    if isinstance(left, bool) or isinstance(right, bool):
        return bool(left) == bool(right)
    try:
        return float(left) == float(right)
    except (TypeError, ValueError):
        return str(left) == str(right)


class ReferenceResolver:
    """Maps source object references onto the equivalent target objects."""

    def __init__(self, source: NetBoxRestClient, target: NetBoxRestClient, plan: dict):
        self.source = source
        self.target = target
        # slug -> {"src_type", "tgt_type", "src_fields", "key_fields"} for every planned type
        self.plan = plan
        self.src_types_by_id: dict[int, dict] = {}
        self._object_cache: dict[tuple[str, int], int] = {}
        self._target_instances: dict[str, list[dict]] = {}
        self._target_tags: dict[str, int] | None = None

    def endpoint_for(self, app_label: str, model: str) -> str | None:
        """Map a content type onto its REST endpoint, or None when unsupported."""
        entry = NETBOX_OBJECT_TYPES.get(f"{app_label}.{model}")
        if entry:
            return entry["endpoint"]
        return None

    def target_instances(self, slug: str) -> list[dict]:
        """All target instances of one custom object type, fetched once."""
        if slug not in self._target_instances:
            self._target_instances[slug] = fetch_all(self.target, instance_endpoint(slug))
        return self._target_instances[slug]

    def invalidate(self, slug: str) -> None:
        """Drop the cached target instances of a type after writing to it."""
        self._target_instances.pop(slug, None)

    def target_tag_id(self, slug: str) -> int:
        """Resolve a tag slug to its target id."""
        if self._target_tags is None:
            self._target_tags = {t["slug"]: t["id"] for t in fetch_all(self.target, "extras/tags")}
        if slug not in self._target_tags:
            raise ReferenceResolutionError(
                f"tag '{slug}' does not exist in target (run scripts/sync_tags.py)"
            )
        return self._target_tags[slug]

    def translate(self, field: dict, value, depth: int = 0):
        """Translate one source field value into a target-ready payload value."""
        ftype = field_type(field)
        if ftype not in OBJECT_FIELD_TYPES:
            return value
        if field.get("is_polymorphic") or field.get("related_object_types"):
            raise ReferenceResolutionError(
                f"field '{field['name']}' is polymorphic - not supported"
            )
        if value in (None, [], ""):
            return value
        if ftype == "multiobject":
            refs = value if isinstance(value, list) else [value]
            return [self.resolve_reference(field, ref, depth) for ref in refs]
        return self.resolve_reference(field, value, depth)

    def resolve_reference(self, field: dict, ref, depth: int) -> int:
        """Resolve a single source reference to the id of the equivalent target object."""
        if depth > MAX_RESOLUTION_DEPTH:
            raise ReferenceResolutionError(
                f"reference nesting deeper than {MAX_RESOLUTION_DEPTH} levels"
            )

        _, app_label, model = object_type_of(field.get("related_object_type"))
        if not model:
            raise ReferenceResolutionError(f"field '{field['name']}' has no related_object_type")

        if app_label in CUSTOM_OBJECT_APP_LABELS:
            return self.resolve_custom_object_reference(field, ref, model, depth)

        endpoint = self.endpoint_for(app_label, model)
        if endpoint is None:
            raise ReferenceResolutionError(
                f"content type {app_label}.{model} has no known endpoint"
            )
        return self.resolve_core_reference(endpoint, ref, depth)

    def resolve_core_reference(self, endpoint: str, ref, depth: int) -> int:
        """Resolve a reference to a core NetBox object by natural key."""
        source_object = self.hydrate(self.source, endpoint, ref)
        source_id = source_object["id"]
        cache_key = (endpoint, source_id)
        if cache_key in self._object_cache:
            return self._object_cache[cache_key]

        filters = self.natural_key_filters(source_object, endpoint, depth)
        matches = fetch_all(self.target, endpoint, params=filters)
        described = ", ".join(f"{k}={v}" for k, v in filters.items())
        if not matches:
            raise ReferenceResolutionError(f"no target {endpoint} matching {described}")
        if len(matches) > 1:
            raise ReferenceResolutionError(
                f"{len(matches)} target {endpoint} match {described} - ambiguous"
            )

        self._object_cache[cache_key] = matches[0]["id"]
        return matches[0]["id"]

    def natural_key_filters(self, source_object: dict, endpoint: str, depth: int) -> dict:
        """Build the target filter identifying one source object without using its id."""
        filters = {}
        for candidate in NATURAL_KEY_FIELDS:
            value = source_object.get(candidate)
            if value not in (None, ""):
                filters[candidate] = value
                break
        if not filters:
            raise ReferenceResolutionError(
                f"source {endpoint} id={source_object['id']} exposes no natural key "
                f"({', '.join(NATURAL_KEY_FIELDS)})"
            )

        for scope in SCOPE_REF_FIELDS:
            scope_ref = source_object.get(scope)
            if not isinstance(scope_ref, dict) or "id" not in scope_ref:
                continue
            scope_endpoint = self.scope_endpoint(scope)
            if scope_endpoint is None:
                continue
            filters[f"{scope}_id"] = self.resolve_core_reference(
                scope_endpoint, scope_ref, depth + 1
            )
        return filters

    def scope_endpoint(self, scope: str) -> str | None:
        """Map a scoping field name (device, site, vrf, ...) onto its REST endpoint."""
        for app_label in ("dcim", "ipam", "virtualization", "tenancy"):
            endpoint = self.endpoint_for(app_label, scope.replace("_", ""))
            if endpoint:
                return endpoint
        return None

    def resolve_custom_object_reference(self, field: dict, ref, model: str, depth: int) -> int:
        """Resolve a reference to another custom object type through that type's match key."""
        src_type_id = custom_object_type_id_from_model(model)
        src_type = self.src_types_by_id.get(src_type_id) if src_type_id is not None else None
        if src_type is None:
            raise ReferenceResolutionError(
                f"cannot map custom object model '{model}' to a source type"
            )

        slug = src_type["slug"]
        entry = self.plan.get(slug)
        if entry is None:
            raise ReferenceResolutionError(
                f"field '{field['name']}' references custom object type '{slug}', "
                f"which is not being synced - add --type {slug} (or use --all-types)"
            )

        # Always fetch in full: a brief custom object reference does not carry the
        # arbitrary fields the match key is built from.
        source_instance = self.hydrate(self.source, instance_endpoint(slug), ref, force_fetch=True)
        wanted = self.instance_key(entry, source_instance, depth + 1, translate=True)
        matches = [
            candidate
            for candidate in self.target_instances(slug)
            if self.instance_key(entry, candidate, depth + 1, translate=False) == wanted
        ]
        if not matches:
            raise ReferenceResolutionError(
                f"no target '{slug}' instance with {dict(zip(entry['key_fields'], wanted, strict=False))} "
                f"- sync that type before this one"
            )
        if len(matches) > 1:
            raise ReferenceResolutionError(
                f"{len(matches)} target '{slug}' instances share {dict(zip(entry['key_fields'], wanted, strict=False))} - ambiguous"
            )
        return matches[0]["id"]

    def instance_key(self, entry: dict, instance: dict, depth: int, translate: bool) -> tuple:
        """Build a custom object instance's match key, translating FK values when reading the source."""
        key = []
        for name in entry["key_fields"]:
            value = instance.get(name)
            if translate:
                field = entry["fields_by_name"].get(name)
                if field is not None:
                    value = self.translate(field, value, depth)
            key.append(str(normalize_value(value)))
        return tuple(key)

    def hydrate(
        self, client: NetBoxRestClient, endpoint: str, ref, force_fetch: bool = False
    ) -> dict:
        """Return a reference as a full object, fetching it when the API gave only an id."""
        if isinstance(ref, dict):
            if "id" not in ref:
                raise ReferenceResolutionError(f"reference {ref!r} has no id")
            # A brief serializer already carries the natural key; only fetch when it does not.
            has_natural_key = any(ref.get(name) not in (None, "") for name in NATURAL_KEY_FIELDS)
            if has_natural_key and not force_fetch:
                return ref
            ref = ref["id"]
        if not isinstance(ref, int):
            raise ReferenceResolutionError(f"unexpected reference value {ref!r}")
        return client.get(endpoint, id=ref)


def choose_key_fields(fields: list[dict], override: list[str]) -> list[str]:
    """Pick the field names identifying an instance, per the precedence in the module docstring."""
    by_name = {f["name"]: f for f in fields}
    if override:
        missing = [name for name in override if name not in by_name]
        if missing:
            raise ValueError(f"--match-field {', '.join(missing)} not defined on this type")
        return list(override)

    unique = [f["name"] for f in fields if f.get("unique")]
    if unique:
        return sorted(unique)

    primary = [f["name"] for f in fields if f.get("primary")]
    references = [f["name"] for f in fields if field_type(f) in OBJECT_FIELD_TYPES]
    key = sorted(set(primary) | set(references))
    if key:
        return key
    return sorted(f["name"] for f in fields)


def build_payload(resolver: ReferenceResolver, entry: dict, instance: dict) -> dict:
    """Translate one source instance into a target-ready create/update payload."""
    payload: dict = {}
    for name, field in entry["fields_by_name"].items():
        if name not in instance:
            continue
        if name not in entry["target_field_names"]:
            print(f"      note: field '{name}' not defined on the target type - omitted")
            continue
        payload[name] = resolver.translate(field, instance[name])

    for key in EXTRA_COPYABLE_KEYS:
        if key in instance and key not in payload:
            payload[key] = instance[key]

    if instance.get("tags"):
        payload["tags"] = [resolver.target_tag_id(tag["slug"]) for tag in instance["tags"]]

    custom_fields = instance.get("custom_fields") or {}
    copyable = {}
    for name, value in custom_fields.items():
        if isinstance(value, (dict, list)):
            print(f"      note: custom field '{name}' holds an object reference - omitted")
            continue
        copyable[name] = value
    if copyable:
        payload["custom_fields"] = copyable

    return payload


def changed_fields(payload: dict, target_instance: dict) -> list[str]:
    """Names of the payload fields whose value differs from the target instance."""
    drifted = []
    for name, value in payload.items():
        if name == "custom_fields":
            target_custom_fields = target_instance.get("custom_fields") or {}
            drifted.extend(
                f"custom_fields.{cf_name}"
                for cf_name, cf_value in value.items()
                if not values_equal(cf_value, target_custom_fields.get(cf_name))
            )
            continue
        if not values_equal(value, target_instance.get(name)):
            drifted.append(name)
    return drifted


def build_plan(
    source: NetBoxRestClient,
    target: NetBoxRestClient,
    wanted_slugs: list[str] | None,
    match_fields: list[str],
) -> tuple[dict, dict]:
    """Resolve the requested type slugs into a dependency-ordered plan plus the source type map."""
    src_types = fetch_all(source, TYPES_ENDPOINT)
    src_fields = fetch_all(source, FIELDS_ENDPOINT)
    tgt_types = fetch_all(target, TYPES_ENDPOINT)
    tgt_fields = fetch_all(target, FIELDS_ENDPOINT)
    print(f"  Source: {len(src_types)} types, {len(src_fields)} fields")
    print(f"  Target: {len(tgt_types)} types, {len(tgt_fields)} fields")

    src_types_by_slug = {t["slug"]: t for t in src_types}
    tgt_types_by_slug = {t["slug"]: t for t in tgt_types}

    # The API ignores a custom_object_type_id filter on the fields endpoint, so group client-side.
    src_fields_by_type: dict[int, list[dict]] = {}
    for field in src_fields:
        src_fields_by_type.setdefault(type_id_of(field.get("custom_object_type")), []).append(field)
    tgt_fields_by_type: dict[int, list[dict]] = {}
    for field in tgt_fields:
        tgt_fields_by_type.setdefault(type_id_of(field.get("custom_object_type")), []).append(field)

    if wanted_slugs is None:
        wanted_slugs = sorted(set(src_types_by_slug) & set(tgt_types_by_slug))

    plan: dict[str, dict] = {}
    for slug in wanted_slugs:
        src_type = src_types_by_slug.get(slug)
        if src_type is None:
            print(f"  FAIL  '{slug}' - no such custom object type in source")
            continue
        tgt_type = tgt_types_by_slug.get(slug)
        if tgt_type is None:
            print(
                f"  FAIL  '{slug}' - not defined in target (run sync_custom_object_types.py first)"
            )
            continue

        fields = src_fields_by_type.get(src_type["id"], [])
        if not fields:
            print(f"  FAIL  '{slug}' - source type has no fields")
            continue
        try:
            key_fields = choose_key_fields(fields, match_fields)
        except ValueError as exc:
            print(f"  FAIL  '{slug}' - {exc}")
            continue

        plan[slug] = {
            "src_type": src_type,
            "tgt_type": tgt_type,
            "fields": fields,
            "fields_by_name": {f["name"]: f for f in fields},
            "target_field_names": {f["name"] for f in tgt_fields_by_type.get(tgt_type["id"], [])},
            "key_fields": key_fields,
        }

    src_types_by_id = {t["id"]: t for t in src_types}
    return order_by_dependency(plan, src_types_by_id), src_types_by_id


def order_by_dependency(plan: dict, src_types_by_id: dict) -> dict:
    """Order the plan so a type is synced after the custom object types it references."""

    def dependencies(entry: dict) -> set[str]:
        deps = set()
        for field in entry["fields"]:
            if field_type(field) not in OBJECT_FIELD_TYPES:
                continue
            _, app_label, model = object_type_of(field.get("related_object_type"))
            if app_label not in CUSTOM_OBJECT_APP_LABELS:
                continue
            referenced_id = custom_object_type_id_from_model(model)
            referenced = src_types_by_id.get(referenced_id) if referenced_id is not None else None
            if (
                referenced
                and referenced["slug"] in plan
                and referenced["slug"] != entry["src_type"]["slug"]
            ):
                deps.add(referenced["slug"])
        return deps

    pending = {slug: dependencies(entry) for slug, entry in plan.items()}
    ordered: dict[str, dict] = {}
    while pending:
        ready = sorted(slug for slug, deps in pending.items() if not deps - set(ordered))
        if not ready:
            # A reference cycle between types: fall back to the remaining order and let
            # resolution report the instances it cannot place.
            cycle = ", ".join(sorted(pending))
            print(f"  note: reference cycle between types ({cycle}) - syncing in name order")
            ready = sorted(pending)
        for slug in ready:
            ordered[slug] = plan[slug]
            pending.pop(slug)
    return ordered


def sync_type(
    source: NetBoxRestClient,
    target: NetBoxRestClient,
    resolver: ReferenceResolver,
    slug: str,
    entry: dict,
    source_filters: dict,
    prune: bool,
    dry_run: bool,
) -> None:
    """Upsert every source instance of one custom object type into the target."""
    endpoint = instance_endpoint(slug)
    print(f"\n--- {slug} (match on {', '.join(entry['key_fields'])}) ---")

    source_instances = fetch_all(source, endpoint, params=source_filters)
    target_instances = resolver.target_instances(slug)
    print(
        f"  source: {len(source_instances)} instance(s), target: {len(target_instances)} instance(s)"
    )

    target_by_key: dict[tuple, list[dict]] = {}
    for instance in target_instances:
        key = resolver.instance_key(entry, instance, depth=0, translate=False)
        target_by_key.setdefault(key, []).append(instance)

    seen_keys: set[tuple] = set()
    failures = 0
    dirty = False
    for instance in source_instances:
        label = instance.get("display") or f"id={instance['id']}"
        try:
            payload = build_payload(resolver, entry, instance)
            key = resolver.instance_key(entry, instance, depth=0, translate=True)
        except ReferenceResolutionError as exc:
            print(f"  FAIL  {label} - {exc}")
            failures += 1
            continue

        seen_keys.add(key)
        existing = target_by_key.get(key, [])
        if len(existing) > 1:
            print(
                f"  FAIL  {label} - {len(existing)} target instances share key {key} - resolve by hand"
            )
            failures += 1
            continue

        if not existing:
            if dry_run:
                print(f"  DRY   {label} - would create")
            else:
                created = _create_with_retry(target, endpoint, payload)
                print(f"  CREATE {label} -> id={created['id']}")
                dirty = True
            continue

        target_instance = existing[0]
        drifted = changed_fields(payload, target_instance)
        if not drifted:
            print(f"  SKIP  {label} - up to date (id={target_instance['id']})")
            continue

        if dry_run:
            print(f"  DRY   {label} - would update ({', '.join(drifted)})")
        else:
            updated = target.update(endpoint, target_instance["id"], payload)
            print(f"  UPDATE {label} -> id={updated['id']} ({', '.join(drifted)})")
            dirty = True

    if prune and failures:
        # A source instance that failed to resolve contributed no key, so its target
        # counterpart looks absent from the source. Pruning now would delete valid rows.
        print(f"  PRUNE SKIPPED - {failures} source instance(s) failed; fix them and re-run")
    elif prune:
        for key, instances in sorted(target_by_key.items()):
            if key in seen_keys:
                continue
            for instance in instances:
                label = instance.get("display") or f"id={instance['id']}"
                if dry_run:
                    print(f"  DRY   {label} - would delete (not in source)")
                else:
                    target.delete(endpoint, instance["id"])
                    print(f"  DELETE {label} (id={instance['id']}, not in source)")
                    dirty = True

    if dirty:
        resolver.invalidate(slug)


def sync(
    source: NetBoxRestClient,
    target: NetBoxRestClient,
    wanted_slugs: list[str] | None,
    match_fields: list[str],
    source_filters: dict,
    prune: bool,
    dry_run: bool,
) -> None:
    """Build the plan and sync every planned custom object type."""
    print("Fetching custom object type definitions...")
    plan, src_types_by_id = build_plan(source, target, wanted_slugs, match_fields)
    if not plan:
        print("\nNothing to sync.")
        return
    print(f"\nSyncing {len(plan)} type(s): {', '.join(plan)}")

    resolver = ReferenceResolver(source, target, plan)
    # The full source type map, not just the planned types, so an unplanned
    # reference can be named in the diagnostic instead of just failing.
    resolver.src_types_by_id = src_types_by_id

    for slug, entry in plan.items():
        sync_type(source, target, resolver, slug, entry, source_filters, prune, dry_run)


def parse_filters(raw: list[str]) -> dict:
    """Parse repeated --filter key=value arguments into a query dict."""
    filters = {}
    for item in raw:
        if "=" not in item:
            raise ValueError(f"--filter must be key=value, got '{item}'")
        key, value = item.split("=", 1)
        filters[key.strip()] = value.strip()
    return filters


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--source-url", default=os.getenv("SOURCE_URL"), required=not os.getenv("SOURCE_URL")
    )
    parser.add_argument(
        "--source-token", default=os.getenv("SOURCE_TOKEN"), required=not os.getenv("SOURCE_TOKEN")
    )
    parser.add_argument(
        "--target-url", default=os.getenv("TARGET_URL"), required=not os.getenv("TARGET_URL")
    )
    parser.add_argument(
        "--target-token", default=os.getenv("TARGET_TOKEN"), required=not os.getenv("TARGET_TOKEN")
    )
    parser.add_argument(
        "--type",
        action="append",
        default=[],
        metavar="SLUG",
        help="Custom object type slug to sync (repeatable)",
    )
    parser.add_argument(
        "--all-types",
        action="store_true",
        help="Sync every custom object type present in both instances",
    )
    parser.add_argument(
        "--match-field",
        action="append",
        default=[],
        metavar="NAME",
        help="Field name forming the match key (repeatable, single --type); overrides the derived key",
    )
    parser.add_argument(
        "--filter",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="Filter applied to the source instance query (repeatable)",
    )
    parser.add_argument(
        "--prune",
        action="store_true",
        help="Delete target instances whose match key is absent from the source",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Preview changes without writing anything"
    )
    parser.add_argument(
        "--no-verify-ssl", action="store_true", help="Disable SSL certificate verification"
    )
    args = parser.parse_args()

    if not args.type and not args.all_types:
        parser.error("pass at least one --type SLUG, or --all-types")
    if args.type and args.all_types:
        parser.error("--type and --all-types are mutually exclusive")
    if args.match_field and len(args.type) != 1:
        parser.error("--match-field names fields of one type; pass exactly one --type with it")

    try:
        source_filters = parse_filters(args.filter)
    except ValueError as exc:
        parser.error(str(exc))

    verify_ssl = not args.no_verify_ssl
    source = NetBoxRestClient(url=args.source_url, token=args.source_token, verify_ssl=verify_ssl)
    target = NetBoxRestClient(url=args.target_url, token=args.target_token, verify_ssl=verify_ssl)

    if args.dry_run:
        print("DRY RUN - no changes will be made\n")
    if args.prune:
        print("PRUNE enabled - target instances missing from the source will be DELETED\n")

    sync(
        source,
        target,
        wanted_slugs=None if args.all_types else args.type,
        match_fields=args.match_field,
        source_filters=source_filters,
        prune=args.prune,
        dry_run=args.dry_run,
    )
    print("\nDone.")


if __name__ == "__main__":
    main()
