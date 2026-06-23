#!/usr/bin/env python3
"""
Copy a single device — with its interfaces and the VLANs those interfaces reference —
from a SOURCE NetBox instance to a TARGET NetBox instance, so the device can be inspected
on the target (e.g. exposed via the NetBox MCP) and fed to the config-renderer for
render-vs-device fidelity checks.

Standard-library only (urllib + json) — no pip installs required.

Fidelity copied (what the renderer reads):
  device      : name, device_type, role, site, platform, status, custom_fields (scalars; e.g. is_els),
                merged config_context (snapshotted into the target's local_context_data so the full-device
                render sees the same context, without copying shared ConfigContext objects), primary_ip4/6
  interfaces  : name, type, enabled, mtu, mode, description, custom_fields,
                parent, lag, untagged_vlan, tagged_vlans, qinq_svlan (the S-VLAN of a Q-in-Q unit)
  vlans       : vid, name, description, status, group/site, custom_fields (e.g. junos_vlan_domain_type),
                qinq_role (svlan/cvlan) and the qinq_svlan parent FK (C-VLAN -> S-VLAN), so the full
                Q-in-Q hierarchy a unit's `vlan-tags outer/inner` renders from lands on the target
  ip addresses: address, status, description, custom_fields, interface assignment (remapped).
                VRF is NOT copied (irrelevant to the family-inet render; avoids target VRF required-CF issues).

Prerequisite objects (manufacturer, device_type, role, site, platform, vlan group) are
created on the target by natural key (slug) if missing. Custom-field *definitions* must
already exist on the target — values for custom fields the target does not define are
dropped (a warning is printed). Object/FK-typed custom fields (e.g. uplink_pe,
associated_virtual_circuit) cannot be remapped across instances and are dropped too;
the renderer's ELS/VLAN behaviour does not depend on them.

Interfaces are created in two passes: first the interfaces themselves (so parent/lag
references can resolve), then a PATCH that wires parent, lag, untagged_vlan, tagged_vlans.

Idempotent: objects are matched by natural key on the target and updated rather than
duplicated, so the script can be re-run safely.

NetBox 4.x assumed (device uses the `role` field, not `device_role`).

Usage:
  python copy_device_for_render_test.py \
      --device no0501-mesna-c1 \
      --src-url https://netbox.test.internal.digital.eidsiva.no --src-token "$SRC_TOKEN" \
      --dst-url http://127.0.0.1:8000 --dst-token "$DST_TOKEN"

  Add --dry-run to print what would be created/updated without writing.
"""

import argparse
import json
import sys
import urllib.error
import urllib.parse
import urllib.request


class NetBox:
    """Minimal NetBox REST client over urllib."""

    def __init__(self, base_url, token, dry_run=False, label=""):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.dry_run = dry_run
        self.label = label

    def _request(self, method, path, params=None, body=None):
        url = f"{self.base_url}/api/{path.lstrip('/')}"
        if params:
            # NetBox repeats a query key for list-valued filters; urlencode(doseq) handles that.
            url = f"{url}?{urllib.parse.urlencode(params, doseq=True)}"
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Authorization", f"Token {self.token}")
        req.add_header("Accept", "application/json")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req) as resp:
                raw = resp.read().decode("utf-8")
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise SystemExit(
                f"[{self.label}] {method} {url} failed: HTTP {exc.code}\n{detail}"
            ) from exc

    def get_all(self, path, **params):
        """GET with pagination, returning the flattened results list."""
        params = {k: v for k, v in params.items() if v is not None}
        # In --dry-run, creates return placeholder ids (e.g. "<new-dcim/devices/>"); a dependent
        # existence check would send that placeholder to the API and 400. Treat any placeholder
        # filter as "nothing matches" so dry-run keeps flowing (it will report a create).
        if any(isinstance(v, str) and v.startswith("<new-") for v in params.values()):
            return []
        params.setdefault("limit", 200)
        results = []
        page = self._request("GET", path, params=params)
        while page is not None:
            results.extend(page.get("results", []))
            nxt = page.get("next")
            if not nxt:
                break
            # Follow the absolute `next` URL directly.
            req = urllib.request.Request(nxt, method="GET")
            req.add_header("Authorization", f"Token {self.token}")
            req.add_header("Accept", "application/json")
            with urllib.request.urlopen(req) as resp:
                page = json.loads(resp.read().decode("utf-8"))
        return results

    def get_one(self, path, **params):
        rows = self.get_all(path, **params)
        return rows[0] if rows else None

    def get_detail(self, path):
        """GET a single object by its detail path (e.g. 'dcim/devices/3/') — no list wrapper."""
        return self._request("GET", path)

    def create(self, path, body):
        if self.dry_run:
            print(f"  [dry-run] POST {path}: {json.dumps(body)}")
            return {"id": f"<new-{path}>", **body}
        return self._request("POST", path, body=body)

    def update(self, path, obj_id, body):
        if self.dry_run:
            print(f"  [dry-run] PATCH {path}{obj_id}/: {json.dumps(body)}")
            return {"id": obj_id, **body}
        return self._request("PATCH", f"{path}{obj_id}/", body=body)


def slug_of(obj):
    """Return the slug of a nested brief object, or None."""
    return obj.get("slug") if isinstance(obj, dict) else None


def value_of(obj):
    """Return the .value of a NetBox {value,label} choice object, or None."""
    return obj.get("value") if isinstance(obj, dict) else obj


def is_object_reference(value):
    """
    True if `value` is a NetBox object/FK custom-field value — a brief nested object carrying an
    instance-specific `id` (and `url`). Such references cannot be remapped across instances, so they
    are dropped. Free-form JSON custom fields (e.g. junos_input/output_vlan_header_operation's
    {operation, vlan_id}, or the interface_config_context blob) are NOT object references: they carry
    no id/url and are transferable verbatim.
    """
    return isinstance(value, dict) and "id" in value and "url" in value


def filter_custom_fields(custom_fields, allowed_names):
    """
    Keep custom fields the target defines, dropping only values that cannot be transferred across
    instances: NetBox object/FK references (a brief object with an instance-specific id/url) and lists
    of such references. Scalars AND free-form JSON content (objects/lists that are not object
    references — e.g. junos_input/output_vlan_header_operation, interface_config_context) are kept,
    since their contents are instance-agnostic.
    """
    if not custom_fields:
        return {}
    kept, dropped = {}, []
    for name, value in custom_fields.items():
        if name not in allowed_names:
            dropped.append(f"{name} (not defined on target)")
            continue
        if is_object_reference(value):
            dropped.append(f"{name} (object/FK reference — not remappable)")
            continue
        if isinstance(value, list) and any(is_object_reference(v) for v in value):
            dropped.append(f"{name} (list of object/FK references — not remappable)")
            continue
        kept[name] = value
    if dropped:
        print(f"    dropped custom fields: {', '.join(dropped)}")
    return kept


def ensure(dst, path, match_params, create_body, label):
    """Get-or-create on the target by natural key; returns the object dict (with id)."""
    existing = dst.get_one(path, **match_params)
    if existing:
        print(f"  {label}: reusing existing id={existing.get('id')}")
        return existing
    print(f"  {label}: creating")
    return dst.create(path, create_body)


def copy_prerequisites(dst, src_device, allowed_cfs):
    """Ensure manufacturer, device_type, role, site, platform exist on the target."""
    dtype = src_device["device_type"]
    manufacturer = dtype["manufacturer"]
    mfr = ensure(
        dst, "dcim/manufacturers/",
        {"slug": manufacturer["slug"]},
        {"name": manufacturer["name"], "slug": manufacturer["slug"]},
        f"manufacturer '{manufacturer['name']}'",
    )
    device_type = ensure(
        dst, "dcim/device-types/",
        {"slug": dtype["slug"]},
        {"manufacturer": mfr["id"], "model": dtype["model"], "slug": dtype["slug"]},
        f"device-type '{dtype['model']}'",
    )
    role = src_device["role"]
    role_obj = ensure(
        dst, "dcim/device-roles/",
        {"slug": role["slug"]},
        {"name": role["name"], "slug": role["slug"]},
        f"role '{role['name']}'",
    )
    site = src_device["site"]
    site_obj = ensure(
        dst, "dcim/sites/",
        {"slug": site["slug"]},
        {"name": site["name"], "slug": site["slug"]},
        f"site '{site['name']}'",
    )
    platform_obj = None
    platform = src_device.get("platform")
    if platform:
        platform_obj = ensure(
            dst, "dcim/platforms/",
            {"slug": platform["slug"]},
            {"name": platform["name"], "slug": platform["slug"]},
            f"platform '{platform['name']}'",
        )
    return device_type, role_obj, site_obj, platform_obj


def copy_device(dst, src_device, device_type, role_obj, site_obj, platform_obj, allowed_cfs):
    body = {
        "name": src_device["name"],
        "device_type": device_type["id"],
        "role": role_obj["id"],
        "site": site_obj["id"],
        "status": value_of(src_device.get("status")) or "active",
        "custom_fields": filter_custom_fields(src_device.get("custom_fields"), allowed_cfs),
        # Config context drives the full-device render (system / protocols / firewall / routing-options).
        # Snapshot the source's *merged* config_context (shared ConfigContext objects + the device's own
        # local_context_data) into the target's local_context_data, so the target's rendered config_context
        # reproduces what the source device actually sees — without copying the shared ConfigContext objects.
        # Falls back to local_context_data if config_context wasn't returned. NetBox validates this against the
        # config-context-profile schema on save (see scripts/local_context_data.schema.json in net-oss).
        "local_context_data": src_device.get("config_context") or src_device.get("local_context_data"),
    }
    if platform_obj:
        body["platform"] = platform_obj["id"]

    existing = dst.get_one("dcim/devices/", name=src_device["name"])
    if existing:
        print(f"  device '{src_device['name']}': updating id={existing['id']}")
        return dst.update("dcim/devices/", existing["id"], body)
    print(f"  device '{src_device['name']}': creating")
    return dst.create("dcim/devices/", body)


def copy_vlans(src, dst, interfaces, allowed_cfs):
    """
    Copy every VLAN referenced by the interfaces (untagged + tagged + each Q-in-Q unit's
    qinq_svlan service VLAN), plus — transitively — the S-VLAN that any C-VLAN points at via
    its own qinq_svlan parent FK, so the full Q-in-Q hierarchy (S-VLAN <- C-VLAN) lands on the
    target even when no interface references the S-VLAN directly. Returns a
    source-vlan-id -> target-vlan-id map for remapping interface assignments.

    S-VLANs (and plain VLANs) are created before C-VLANs, with each C-VLAN's qinq_svlan parent FK
    set inline in its create — NetBox rejects a Q-in-Q customer VLAN that has no service VLAN, so the
    link cannot be deferred to a second pass.
    """
    # Seed from interface references: untagged, tagged, and the interface's own S-VLAN.
    src_vlan_ids = set()
    for iface in interfaces:
        if iface.get("untagged_vlan"):
            src_vlan_ids.add(iface["untagged_vlan"]["id"])
        for tv in iface.get("tagged_vlans") or []:
            src_vlan_ids.add(tv["id"])
        if iface.get("qinq_svlan"):
            src_vlan_ids.add(iface["qinq_svlan"]["id"])

    # Fetch each VLAN, following qinq_svlan parent links transitively so a C-VLAN's S-VLAN is
    # copied even when only the C-VLAN is referenced by an interface (tagged-vlan inner tag).
    vlans_by_src_id = {}
    pending = list(src_vlan_ids)
    while pending:
        src_vlan_id = pending.pop()
        if src_vlan_id in vlans_by_src_id:
            continue
        vlan = src.get_one("ipam/vlans/", id=src_vlan_id)
        if not vlan:
            print(f"  vlan id={src_vlan_id}: not found on source, skipping")
            continue
        vlans_by_src_id[src_vlan_id] = vlan
        parent = vlan.get("qinq_svlan")
        if parent and parent["id"] not in vlans_by_src_id:
            pending.append(parent["id"])

    id_map = {}
    group_cache = {}
    # Create S-VLANs (and plain VLANs, qinq_svlan == None) BEFORE C-VLANs, and set the qinq_svlan parent FK
    # inline in the C-VLAN's create: NetBox rejects a Q-in-Q customer VLAN that has no service VLAN ("A
    # Q-in-Q customer VLAN must be assigned to a service VLAN"), so the link cannot be deferred to a second
    # pass. Ordering by (has-parent, src-id) puts every parent ahead of its children (the hierarchy is one
    # level: an S-VLAN's own qinq_svlan is null), so id_map already holds the parent when the child is built.
    ordered_ids = sorted(
        vlans_by_src_id,
        key=lambda sid: (vlans_by_src_id[sid].get("qinq_svlan") is not None, sid),
    )
    for src_vlan_id in ordered_ids:
        vlan = vlans_by_src_id[src_vlan_id]
        # Match on vid AND name: a device can carry several distinct VLANs sharing a vid (e.g. a Q-in-Q
        # inner `…-ae2-cvlan-712-29` and an unrelated `…-et-0/0/17-vlan-29`, both vid 29). Matching by vid
        # alone collapses them into one target object, so an interface's tagged_vlans/qinq_svlan then
        # resolves to the wrong VLAN (or none). The name disambiguates same-vid duplicates.
        match = {"vid": vlan["vid"], "name": vlan["name"]}
        body = {
            "vid": vlan["vid"],
            "name": vlan["name"],
            "description": vlan.get("description", ""),
            "status": value_of(vlan.get("status")) or "active",
            "qinq_role": value_of(vlan.get("qinq_role")),
            "custom_fields": filter_custom_fields(vlan.get("custom_fields"), allowed_cfs),
        }

        parent = vlan.get("qinq_svlan")
        if parent:
            mapped_parent = id_map.get(parent["id"])
            if mapped_parent:
                body["qinq_svlan"] = mapped_parent
            else:
                print(f"  vlan {vlan['vid']} '{vlan['name']}': qinq_svlan parent {parent['id']} not copied — "
                      f"creating without the link (NetBox will reject a cvlan with no service VLAN)")

        group = vlan.get("group")
        if group:
            if group["slug"] not in group_cache:
                group_cache[group["slug"]] = ensure(
                    dst, "ipam/vlan-groups/",
                    {"slug": group["slug"]},
                    {"name": group["name"], "slug": group["slug"]},
                    f"vlan-group '{group['name']}'",
                )
            body["group"] = group_cache[group["slug"]]["id"]
            match["group_id"] = group_cache[group["slug"]]["id"]

        existing = dst.get_one("ipam/vlans/", **match)
        if existing:
            print(f"  vlan {vlan['vid']} '{vlan['name']}': updating id={existing['id']}")
            dst_vlan = dst.update("ipam/vlans/", existing["id"], body)
        else:
            print(f"  vlan {vlan['vid']} '{vlan['name']}': creating")
            dst_vlan = dst.create("ipam/vlans/", body)
        id_map[src_vlan_id] = dst_vlan["id"]

    return id_map


def copy_interfaces(dst, dst_device, interfaces, vlan_id_map, allowed_cfs):
    """
    Pass 1: create/update each interface with its scalar attributes.
    Pass 2: PATCH parent, lag, untagged_vlan, tagged_vlans (remapped to target ids).
    """
    iface_id_map = {}

    # Pass 1 — base attributes (no relationships yet).
    for iface in interfaces:
        body = {
            "device": dst_device["id"],
            "name": iface["name"],
            "type": value_of(iface.get("type")) or "other",
            "enabled": iface.get("enabled", True),
            "mtu": iface.get("mtu"),
            "mode": value_of(iface.get("mode")),
            "description": iface.get("description", ""),
            "custom_fields": filter_custom_fields(iface.get("custom_fields"), allowed_cfs),
        }
        existing = dst.get_one("dcim/interfaces/", device_id=dst_device["id"], name=iface["name"])
        if existing:
            print(f"  interface '{iface['name']}': updating id={existing['id']}")
            dst_iface = dst.update("dcim/interfaces/", existing["id"], body)
        else:
            print(f"  interface '{iface['name']}': creating")
            dst_iface = dst.create("dcim/interfaces/", body)
        iface_id_map[iface["id"]] = dst_iface["id"]

    # Pass 2 — relationships, now that all interface ids exist on the target.
    for iface in interfaces:
        rel = {}
        if iface.get("parent"):
            mapped = iface_id_map.get(iface["parent"]["id"])
            if mapped:
                rel["parent"] = mapped
        if iface.get("lag"):
            mapped = iface_id_map.get(iface["lag"]["id"])
            if mapped:
                rel["lag"] = mapped
        if iface.get("untagged_vlan"):
            mapped = vlan_id_map.get(iface["untagged_vlan"]["id"])
            if mapped:
                rel["untagged_vlan"] = mapped
        tagged = [vlan_id_map[tv["id"]] for tv in (iface.get("tagged_vlans") or []) if tv["id"] in vlan_id_map]
        if tagged:
            rel["tagged_vlans"] = tagged
        if iface.get("qinq_svlan"):
            mapped = vlan_id_map.get(iface["qinq_svlan"]["id"])
            if mapped:
                rel["qinq_svlan"] = mapped

        if rel:
            print(f"  interface '{iface['name']}': wiring {', '.join(rel)}")
            dst.update("dcim/interfaces/", iface_id_map[iface["id"]], rel)
    return iface_id_map


def copy_ip_addresses(src, dst, src_device, iface_id_map, allowed_cfs):
    """
    Copy IP addresses assigned to the device's interfaces, remapping each assignment to the
    corresponding target interface. Returns a map of address-string -> target IP id (used to set
    the device's primary IPs). Only interface-assigned IPs are copied.

    VRF is intentionally NOT copied: the renderer emits `family inet address` regardless of the IP's
    VRF (interface-VRF drives routing-instances only on routers, which this tool doesn't target), and
    creating VRFs trips target-specific required custom fields. IPs land in the global table, which
    renders identically for the family-inet line.
    """
    ips = src.get_all("ipam/ip-addresses/", device_id=src_device["id"])
    print(f"  found {len(ips)} IP addresses on the device")
    address_to_id = {}

    for ip in ips:
        # Resolve the source interface this IP is assigned to, then map it to the target interface.
        if ip.get("assigned_object_type") != "dcim.interface":
            print(f"  ip {ip['address']}: not interface-assigned ({ip.get('assigned_object_type')}), skipping")
            continue
        src_iface_id = ip.get("assigned_object_id") or (ip.get("assigned_object") or {}).get("id")
        dst_iface_id = iface_id_map.get(src_iface_id)
        if not dst_iface_id:
            print(f"  ip {ip['address']}: assigned interface {src_iface_id} not copied, skipping")
            continue

        body = {
            "address": ip["address"],
            "status": value_of(ip.get("status")) or "active",
            "description": ip.get("description", ""),
            "assigned_object_type": "dcim.interface",
            "assigned_object_id": dst_iface_id,
            "custom_fields": filter_custom_fields(ip.get("custom_fields"), allowed_cfs),
        }
        existing = dst.get_one("ipam/ip-addresses/", address=ip["address"])
        if existing:
            print(f"  ip {ip['address']}: updating id={existing['id']}")
            dst_ip = dst.update("ipam/ip-addresses/", existing["id"], body)
        else:
            print(f"  ip {ip['address']}: creating")
            dst_ip = dst.create("ipam/ip-addresses/", body)
        address_to_id[ip["address"]] = dst_ip["id"]
    return address_to_id


def set_primary_ips(dst, dst_device, src_device, address_to_id):
    """Point the target device's primary_ip4/primary_ip6 at the copied IPs (by address)."""
    body = {}
    for field in ("primary_ip4", "primary_ip6"):
        src_primary = src_device.get(field)
        if src_primary and src_primary.get("address") in address_to_id:
            body[field] = address_to_id[src_primary["address"]]
    if body:
        print(f"  device '{dst_device.get('name')}': setting {', '.join(body)}")
        dst.update("dcim/devices/", dst_device["id"], body)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--device", required=True, help="Exact device name to copy")
    parser.add_argument("--src-url", required=True)
    parser.add_argument("--src-token", required=True)
    parser.add_argument("--dst-url", required=True)
    parser.add_argument("--dst-token", required=True)
    parser.add_argument("--dry-run", action="store_true", help="Print actions without writing to the target")
    args = parser.parse_args()

    src = NetBox(args.src_url, args.src_token, label="src")
    dst = NetBox(args.dst_url, args.dst_token, dry_run=args.dry_run, label="dst")

    print(f"Fetching device '{args.device}' from source...")
    src_device = src.get_one("dcim/devices/", name=args.device)
    if not src_device:
        sys.exit(f"Device '{args.device}' not found on source {args.src_url}")
    # The list endpoint omits config_context; re-fetch the detail so we get the *merged* config_context
    # (shared ConfigContext objects + local_context_data) — that is what the renderer reads.
    src_device = src.get_detail(f"dcim/devices/{src_device['id']}/")

    interfaces = src.get_all("dcim/interfaces/", device_id=src_device["id"])
    print(f"  found {len(interfaces)} interfaces")

    # Custom fields the target defines — values for anything else are dropped.
    allowed_cfs = {cf["name"] for cf in dst.get_all("extras/custom-fields/")}
    print(f"Target defines {len(allowed_cfs)} custom fields.")

    print("Ensuring prerequisite objects on target...")
    device_type, role_obj, site_obj, platform_obj = copy_prerequisites(dst, src_device, allowed_cfs)

    print("Copying device...")
    dst_device = copy_device(dst, src_device, device_type, role_obj, site_obj, platform_obj, allowed_cfs)

    print("Copying VLANs referenced by interfaces...")
    vlan_id_map = copy_vlans(src, dst, interfaces, allowed_cfs)

    print("Copying interfaces (two passes)...")
    iface_id_map = copy_interfaces(dst, dst_device, interfaces, vlan_id_map, allowed_cfs)

    print("Copying interface IP addresses...")
    address_to_id = copy_ip_addresses(src, dst, src_device, iface_id_map, allowed_cfs)

    print("Setting device primary IPs...")
    set_primary_ips(dst, dst_device, src_device, address_to_id)

    print("\nDone." + (" (dry-run — nothing written)" if args.dry_run else f" Device '{args.device}' copied to {args.dst_url}."))


if __name__ == "__main__":
    main()
