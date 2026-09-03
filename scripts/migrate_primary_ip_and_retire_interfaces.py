#!/usr/bin/env python3
"""
One-time cleanup: retire a parent/sub interface pair from a set of devices and move each
device's primary IP onto the equal-valued address in another VRF.

Every value that identifies a particular fleet -- the prefix, both VRF names and both
interface names -- is a REQUIRED argument. The script carries no site-specific defaults.

What it does, per device:

  1. Collects every IP inside --prefix in --source-vrf, and the devices those IPs are
     assigned to.
  2. Keeps only devices whose manufacturer and role match --manufacturer / --role.
  3. Finds the "equal IP" -- the same host address inside the same prefix but in
     --target-vrf -- and points the device's primary_ip4 at it.
  4. Deletes the source-VRF IP sitting on the retiring interfaces, then deletes
     --sub-iface followed by its parent --parent-iface.

Order matters: the primary IP is repointed BEFORE the old address is deleted, so the
device is never left without a primary and NetBox's "primary IP must be assigned to an
interface of this device" validation is always satisfied.

A device is SKIPPED (never partially processed) when any precondition fails:
  * neither retiring interface is present, or the source IP is not on one of them
  * more than one distinct source address on the retiring interfaces (ambiguous)
  * no equal IP in the target VRF, or more than one candidate (ambiguous)
  * the equal IP is unassigned, assigned to a different device, or assigned to one of
    the interfaces this script is about to delete
  * the retiring interfaces carry extra IPs outside the migration, or are cabled
    (both would be destroyed as collateral) -- override with --force
  * either retiring interface has a child beyond the pair itself: NetBox RESTRICTs
    Interface.parent, so the delete would fail with 409 once the addresses are already
    gone, stranding the device half-migrated
  * primary_ip4 already points at an address this script is NOT deleting -- moving it
    would discard a deliberate choice
  * primary_ip6 or oob_ip is one of the addresses that would be deleted, which would
    silently null the field (not overridable by --force)

SAFETY: this deletes objects. It previews by default; you must pass --apply to write.
Always run the preview first and read the per-device plan.

Usage (the five scope arguments are required; values below are placeholders):

    SCOPE="--prefix 192.0.2.0/24 --source-vrf SRC_VRF --target-vrf DST_VRF            --parent-iface PARENT --sub-iface PARENT.SUB"

    # preview the whole fleet (no writes)
    python scripts/migrate_primary_ip_and_retire_interfaces.py $SCOPE

    # preview one device, then apply
    python scripts/migrate_primary_ip_and_retire_interfaces.py $SCOPE --device DEVICE-NAME
    python scripts/migrate_primary_ip_and_retire_interfaces.py $SCOPE --apply

URL and token come from --url/--token or the NETBOX_URL / NETBOX_TOKEN env vars.
"""

import argparse
import importlib.util
import os
import sys
from typing import Any

import httpx

# Load netbox_client directly to avoid pulling in the full package (__init__ -> config -> pydantic)
_client_path = os.path.join(
    os.path.dirname(__file__), "..", "src", "netbox_mcp_server", "netbox_client.py"
)
_spec = importlib.util.spec_from_file_location("netbox_client", _client_path)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
NetBoxRestClient = _mod.NetBoxRestClient

# Vendor and role are generic NetBox vocabulary, so they keep defaults. Everything
# that would identify a particular network is a required argument instead.
DEFAULT_MANUFACTURER = "juniper"
DEFAULT_ROLE = "switch"

INTERFACE_OBJECT_TYPE = "dcim.interface"
ID_CHUNK = 100


def fetch_all(
    client: NetBoxRestClient, endpoint: str, params: dict | None = None, limit: int = 1000
) -> list[dict]:
    """Page through a list endpoint and return every result.

    The offset advances by the page NetBox actually returned, not by the requested limit:
    an instance with MAX_PAGE_SIZE below `limit` serves a shorter page, and stepping by
    the request size would skip every row in between and exit as if the data had ended.
    """
    results: list[dict] = []
    offset = 0
    base_params = params or {}
    while True:
        resp = client.get(endpoint, params={"limit": limit, "offset": offset, **base_params})
        page = resp.get("results", [])
        results.extend(page)
        if len(results) >= resp.get("count", 0) or not page:
            break
        offset += len(page)
    return results


def fetch_by_filter_ids(
    client: NetBoxRestClient, endpoint: str, param: str, ids: list[int]
) -> list[dict]:
    """Fetch through a relation filter (`interface_id`, `parent_id`) in chunks.

    Those are ModelMultipleChoiceFilters, so NetBox answers 400 -- not an empty list -- if
    any id in the chunk no longer exists. An interface deleted between two fetches would
    otherwise abort the whole run, so a rejected chunk is retried one id at a time and the
    stale ids are reported and dropped. (`id`, used by fetch_by_ids, is not validated this
    way and needs none of this.)
    """
    results: list[dict] = []
    unique = sorted(set(ids))
    for start in range(0, len(unique), ID_CHUNK):
        chunk = unique[start : start + ID_CHUNK]
        try:
            results.extend(fetch_all(client, endpoint, {param: chunk}))
            continue
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code != 400:
                raise
        for one in chunk:
            try:
                results.extend(fetch_all(client, endpoint, {param: one}))
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code != 400:
                    raise
                print(f"  note: {endpoint}?{param}={one} rejected -- object deleted meanwhile")
    return results


def failure_detail(exc: Exception) -> str:
    """NetBox's rejection reason is in the response body, and the client's update/delete
    raise on status alone -- so str(exc) alone is alarmingly just a code and a URL."""
    response = getattr(exc, "response", None)
    body = (getattr(response, "text", "") or "").strip()
    if not body:
        return str(exc)
    if len(body) > 500:
        body = body[:497] + "..."
    return f"{exc}: {body}"


def fetch_by_ids(client: NetBoxRestClient, endpoint: str, ids: list[int]) -> dict[int, dict]:
    """Fetch objects by id in chunks, keyed by id. An empty id list issues no request --
    a bare `?id=` would otherwise be dropped by NetBox and return the whole table."""
    out: dict[int, dict] = {}
    unique = sorted(set(ids))
    for start in range(0, len(unique), ID_CHUNK):
        chunk = unique[start : start + ID_CHUNK]
        for obj in fetch_all(client, endpoint, {"id": chunk}):
            out[obj["id"]] = obj
    return out


def host_of(address: str) -> str:
    """Strip the mask: the two VRFs may store the same host with different prefix
    lengths, so the host part alone is what makes two addresses 'equal' here."""
    return address.split("/", 1)[0]


def label_matches(obj: dict | None, wanted: str) -> bool:
    """Match a NetBox nested reference on either its name or its slug, case-insensitively."""
    if not obj:
        return False
    wanted = wanted.strip().lower()
    return (obj.get("name") or "").lower() == wanted or (obj.get("slug") or "").lower() == wanted


def device_role(device: dict) -> dict | None:
    """NetBox 4.x calls it `role`; 3.x called it `device_role`."""
    return device.get("role") or device.get("device_role")


def device_manufacturer(device: dict) -> dict | None:
    return (device.get("device_type") or {}).get("manufacturer")


def resolve_vrf(client: NetBoxRestClient, name: str) -> dict:
    """Resolve a VRF by exact name. Ambiguity here would silently migrate the wrong
    addresses, so refuse rather than guess."""
    matches = [v for v in fetch_all(client, "ipam/vrfs", {"name": name}) if v.get("name") == name]
    if not matches:
        raise SystemExit(f"ERROR: VRF '{name}' not found.")
    if len(matches) > 1:
        ids = ", ".join(str(v["id"]) for v in matches)
        raise SystemExit(f"ERROR: VRF name '{name}' is ambiguous (ids: {ids}).")
    return matches[0]


def interface_ids_of(ips: list[dict]) -> list[int]:
    return [
        ip["assigned_object_id"]
        for ip in ips
        if ip.get("assigned_object_type") == INTERFACE_OBJECT_TYPE and ip.get("assigned_object_id")
    ]


def report_devices_without_source_address(
    args: argparse.Namespace, retiring_ifaces: list[dict], source_device_ids: set[int]
) -> set[str]:
    """Print the devices that carry retiring interfaces but hold no source-VRF address, and
    return their names so --device can explain itself when it matches one of them."""
    names = {
        iface["device"].get("name") or f"#{iface['device']['id']}"
        for iface in retiring_ifaces
        if iface.get("device") and iface["device"]["id"] not in source_device_ids
    }
    if names:
        listed = sorted(names)
        shown = ", ".join(listed[:10]) + (
            f", ... (+{len(listed) - 10})" if len(listed) > 10 else ""
        )
        print(
            f"  note: {len(listed)} device(s) carry {args.parent_iface} but no "
            f"{args.source_vrf} address in {args.prefix} -- already migrated, or left "
            f"half-migrated by a failed run: {shown}"
        )
    return names


def build_plans(client: NetBoxRestClient, args: argparse.Namespace) -> list[dict]:
    """Resolve every device in scope into an executable plan or a skip reason."""
    source_vrf = resolve_vrf(client, args.source_vrf)
    target_vrf = resolve_vrf(client, args.target_vrf)

    print(f"Fetching {args.prefix} addresses in VRF {args.source_vrf} ...")
    source_ips = fetch_all(
        client, "ipam/ip-addresses", {"parent": args.prefix, "vrf_id": source_vrf["id"]}
    )
    print(f"  {len(source_ips)} found")

    # An `assigned_object_id` only means a dcim.interface when the type says so. VM
    # interfaces are a separate id sequence, so keeping them would let a VM address whose
    # id collides with a device interface be filed under -- and deleted from -- a switch.
    on_device_iface = [
        ip for ip in source_ips if ip.get("assigned_object_type") == INTERFACE_OBJECT_TYPE
    ]
    if len(on_device_iface) != len(source_ips):
        print(f"  {len(source_ips) - len(on_device_iface)} not on a device interface, ignored")
    source_ips = on_device_iface

    print(f"Fetching {args.prefix} addresses in VRF {args.target_vrf} ...")
    target_ips = fetch_all(
        client, "ipam/ip-addresses", {"parent": args.prefix, "vrf_id": target_vrf["id"]}
    )
    print(f"  {len(target_ips)} found")

    target_by_host: dict[str, list[dict]] = {}
    for ip in target_ips:
        target_by_host.setdefault(host_of(ip["address"]), []).append(ip)

    # Interfaces referenced by either VRF's addresses, so every device lookup below is
    # resolved from a fully hydrated interface rather than an inline brief.
    ifaces = fetch_by_ids(
        client, "dcim/interfaces", interface_ids_of(source_ips) + interface_ids_of(target_ips)
    )

    source_ips_by_device: dict[int, list[dict]] = {}
    for ip in source_ips:
        iface = ifaces.get(ip.get("assigned_object_id") or -1)
        device = (iface or {}).get("device")
        if not device:
            continue
        source_ips_by_device.setdefault(device["id"], []).append(ip)
    print(f"  {len(source_ips_by_device)} device(s) hold a {args.source_vrf} address")

    devices = fetch_by_ids(client, "dcim/devices", list(source_ips_by_device))

    # One fleet-wide fetch of the retiring interfaces, grouped per device.
    retiring_ifaces = fetch_all(
        client, "dcim/interfaces", {"name": [args.parent_iface, args.sub_iface]}
    )
    retiring_by_device: dict[int, dict[str, dict]] = {}
    for iface in retiring_ifaces:
        device = iface.get("device")
        if not device:
            continue
        retiring_by_device.setdefault(device["id"], {})[iface["name"]] = iface

    # A device carrying retiring interfaces but holding no source address is invisible to the
    # rest of this run -- it never enters `devices`. Usually that means "already migrated",
    # but it is also what a run that died between the address deletes and the interface
    # deletes leaves behind, so report it rather than letting it drop out silently.
    no_source_address = report_devices_without_source_address(
        args, retiring_ifaces, set(source_ips_by_device)
    )

    # Filter here rather than per-plan: a device that --device excludes should not appear
    # in the report at all, otherwise one named device arrives buried under a skip line
    # for every other device in scope.
    if args.device:
        devices = {i: d for i, d in devices.items() if (d.get("name") or "") == args.device}
        if not devices:
            hint = (
                f" It does carry {args.parent_iface}, so it is either already migrated or "
                "was left half-migrated by an earlier run -- check it by hand."
                if args.device in no_source_address
                else ""
            )
            raise SystemExit(
                f"ERROR: --device {args.device} matched none of the devices holding a "
                f"{args.source_vrf} address in {args.prefix}.{hint}"
            )

    # Every address sitting on a retiring interface -- including ones outside this migration,
    # which would be collateral damage when the interface is deleted.
    retiring_iface_ids = [
        i["id"] for i in retiring_ifaces if (i.get("device") or {}).get("id") in devices
    ]
    ips_on_retiring: dict[int, list[dict]] = {}
    for ip in fetch_by_filter_ids(client, "ipam/ip-addresses", "interface_id", retiring_iface_ids):
        ips_on_retiring.setdefault(ip["assigned_object_id"], []).append(ip)

    # Children of BOTH interfaces this script deletes. NetBox RESTRICTs Interface.parent --
    # deleting an interface that still has a child answers 409 -- so an unexpected child
    # has to be found now, not once the addresses are already gone. The sub-interface is
    # itself a child of the parent, hence the exclusion in _plan_device.
    children_by_parent: dict[int, list[dict]] = {}
    for iface in fetch_by_filter_ids(client, "dcim/interfaces", "parent_id", retiring_iface_ids):
        parent_id = (iface.get("parent") or {}).get("id")
        if parent_id:
            children_by_parent.setdefault(parent_id, []).append(iface)

    plans = []
    for device_id, device in sorted(devices.items(), key=lambda kv: kv[1].get("name") or ""):
        plans.append(
            _plan_device(
                args=args,
                device=device,
                source_ips=source_ips_by_device.get(device_id, []),
                retiring=retiring_by_device.get(device_id, {}),
                ips_on_retiring=ips_on_retiring,
                children_by_parent=children_by_parent,
                target_by_host=target_by_host,
                ifaces=ifaces,
            )
        )
    return plans


def _plan_device(
    args: argparse.Namespace,
    device: dict,
    source_ips: list[dict],
    retiring: dict[str, dict],
    ips_on_retiring: dict[int, list[dict]],
    children_by_parent: dict[int, list[dict]],
    target_by_host: dict[str, list[dict]],
    ifaces: dict[int, dict],
) -> dict:
    """Decide what to do with one device. Returns a plan carrying either `skip` (with a
    reason) or the concrete ids to write and delete."""
    plan: dict[str, Any] = {
        "device_id": device["id"],
        "device": device.get("name") or f"#{device['id']}",
        "skip": None,
        "warnings": [],
        "set_primary_ip4": None,
        "delete_ip_ids": [],
        "delete_iface_ids": [],
        "summary": "",
    }

    def skip(reason: str) -> dict:
        plan["skip"] = reason
        return plan

    if not label_matches(device_manufacturer(device), args.manufacturer):
        return skip(f"manufacturer is not {args.manufacturer}")
    if not label_matches(device_role(device), args.role):
        return skip(f"role is not {args.role}")
    if not retiring:
        return skip(f"no {args.parent_iface} / {args.sub_iface} interface on device")

    retiring_ids = {i["id"] for i in retiring.values()}

    # The source addresses this migration is responsible for are exactly the ones sitting
    # on the retiring interfaces. Anything else in the source VRF stays where it is.
    on_retiring = [ip for ip in source_ips if ip.get("assigned_object_id") in retiring_ids]
    elsewhere = [ip for ip in source_ips if ip.get("assigned_object_id") not in retiring_ids]
    if not on_retiring:
        return skip(f"the {args.source_vrf} address is not on a retiring interface")
    if elsewhere:
        plan["warnings"].append(
            f"{len(elsewhere)} further {args.source_vrf} address(es) elsewhere on the device "
            f"are left untouched: {', '.join(ip['address'] for ip in elsewhere)}"
        )

    hosts = {host_of(ip["address"]) for ip in on_retiring}
    if len(hosts) > 1:
        return skip(
            f"several distinct {args.source_vrf} addresses on the retiring interfaces: {sorted(hosts)}"
        )
    host = hosts.pop()

    candidates = target_by_host.get(host, [])
    if not candidates:
        return skip(f"no {host} in VRF {args.target_vrf}")
    if len(candidates) > 1:
        return skip(f"{len(candidates)} candidates for {host} in VRF {args.target_vrf}")
    target_ip = candidates[0]

    if (target_ip.get("family") or {}).get("value") != 4:
        return skip(f"{target_ip['address']} is not IPv4 and cannot be a primary_ip4")
    if target_ip.get("assigned_object_type") != INTERFACE_OBJECT_TYPE:
        return skip(f"{target_ip['address']} ({args.target_vrf}) is not assigned to an interface")

    target_iface = ifaces.get(target_ip["assigned_object_id"])
    target_device = (target_iface or {}).get("device") or {}
    if target_device.get("id") != device["id"]:
        return skip(
            f"{target_ip['address']} ({args.target_vrf}) belongs to "
            f"{target_device.get('name') or 'another device'}, not this one"
        )
    if target_ip["assigned_object_id"] in retiring_ids:
        return skip(
            f"{target_ip['address']} ({args.target_vrf}) sits on a retiring interface "
            "that would be deleted"
        )

    # Collateral checks: everything else on the interfaces we are about to delete.
    migrating_ids = {ip["id"] for ip in on_retiring}
    collateral = [
        ip
        for iface_id in retiring_ids
        for ip in ips_on_retiring.get(iface_id, [])
        if ip["id"] not in migrating_ids
    ]
    if collateral:
        detail = ", ".join(
            f"{ip['address']} (vrf {(ip.get('vrf') or {}).get('name')})" for ip in collateral
        )
        if not args.force:
            return skip(f"retiring interfaces carry unrelated address(es): {detail} -- use --force")
        plan["warnings"].append(f"--force: also deleting unrelated address(es): {detail}")

    cabled = [i["name"] for i in retiring.values() if i.get("cable")]
    if cabled:
        if not args.force:
            return skip(f"retiring interface(s) are cabled: {', '.join(cabled)} -- use --force")
        plan["warnings"].append(f"--force: deleting cabled interface(s): {', '.join(cabled)}")

    # NetBox RESTRICTs Interface.parent, so an interface delete fails with 409 while any
    # child still hangs off it. Both interfaces in the plan are checked -- a child of the
    # SUB-interface strands the device just as surely as a child of the parent, and it
    # fails earlier, with the addresses already deleted. --force does not help either way:
    # the surviving child is the blocker, and by then the rest is gone.
    planned_ids = {i["id"] for i in retiring.values()}
    extra_children = sorted(
        child["name"]
        for iface_id in planned_ids
        for child in children_by_parent.get(iface_id, [])
        if child["id"] not in planned_ids
    )
    if extra_children:
        return skip(
            f"further child interface(s) hang off the retiring interfaces: "
            f"{', '.join(extra_children)} -- NetBox refuses to delete an interface that "
            "still has children"
        )

    missing = [n for n in (args.parent_iface, args.sub_iface) if n not in retiring]
    if missing:
        plan["warnings"].append(f"interface(s) already absent: {', '.join(missing)}")

    delete_ip_ids = [ip["id"] for ip in on_retiring] + [ip["id"] for ip in collateral]

    # primary_ip6 and oob_ip are SET_NULL on the device and this script never repoints
    # them, so deleting the address they point at just clears the field. Refuse the device
    # outright -- the operator moves them first; --force is about collateral addresses on
    # the interfaces, not about a device's own pointers. oob_ip matters most: it is an
    # IPv4 field, so it can hold the very address being migrated and would be cleared on
    # the default path with no --force involved.
    for field in ("primary_ip6", "oob_ip"):
        pointer = device.get(field) or {}
        if pointer.get("id") in delete_ip_ids:
            return skip(
                f"{field} is {pointer.get('address')}, which is on a retiring interface and "
                "would be deleted -- move it off first"
            )

    # Only a primary that this script is itself deleting may be repointed. Anything else
    # (a loopback, a permanent vlan.x address) is a deliberate choice, and moving it would
    # look entirely intentional in the plan output.
    primary_ip4 = device.get("primary_ip4") or {}
    current_primary = primary_ip4.get("id")
    if current_primary not in (None, target_ip["id"]) and current_primary not in delete_ip_ids:
        return skip(
            f"primary_ip4 is {primary_ip4.get('address')}, which this script is not "
            f"deleting -- repointing it to {target_ip['address']} would discard that choice"
        )
    if current_primary != target_ip["id"]:
        plan["set_primary_ip4"] = target_ip

    plan["delete_ip_ids"] = delete_ip_ids
    # Sub-interface before parent: the same parent restriction checked above means the
    # child has to be gone before the parent can be deleted at all.
    plan["delete_iface_ids"] = [
        retiring[name]["id"] for name in (args.sub_iface, args.parent_iface) if name in retiring
    ]

    primary_note = (
        f"primary_ip4 {primary_ip4.get('address') or '(unset)'} -> "
        f"{target_ip['address']} ({args.target_vrf})"
        if plan["set_primary_ip4"]
        else f"primary_ip4 already {target_ip['address']}"
    )
    deleted_addresses = ", ".join(ip["address"] for ip in on_retiring + collateral)
    deleted_ifaces = ", ".join(n for n in (args.sub_iface, args.parent_iface) if n in retiring)
    plan["summary"] = (
        f"{primary_note}; delete {len(plan['delete_ip_ids'])} address(es) [{deleted_addresses}] "
        f"and {len(plan['delete_iface_ids'])} interface(s) [{deleted_ifaces}]"
    )
    return plan


def print_plans(plans: list[dict], show_skipped: bool) -> list[dict]:
    actionable = [p for p in plans if not p["skip"]]
    skipped = [p for p in plans if p["skip"]]

    print("\n--- Planned changes ---")
    if not actionable:
        print("  (none)")
    for plan in actionable:
        print(f"  {plan['device']}: {plan['summary']}")
        for warning in plan["warnings"]:
            print(f"      ! {warning}")

    if skipped and show_skipped:
        print(f"\n--- Skipped ({len(skipped)}) ---")
        for plan in skipped:
            print(f"  {plan['device']}: {plan['skip']}")
    elif skipped:
        print(f"\n{len(skipped)} device(s) skipped -- rerun with --show-skipped for the reasons.")
    return actionable


def apply_plan(client: NetBoxRestClient, plan: dict) -> None:
    """Repoint the primary IP first, then delete -- so the device always has a valid
    primary_ip4 and NetBox's device validation never sees a dangling reference."""
    if plan["set_primary_ip4"]:
        client.update(
            "dcim/devices", plan["device_id"], {"primary_ip4": plan["set_primary_ip4"]["id"]}
        )
        print(f"      primary_ip4 set to {plan['set_primary_ip4']['address']}")
    for ip_id in plan["delete_ip_ids"]:
        client.delete("ipam/ip-addresses", ip_id)
        print(f"      deleted ip-address {ip_id}")
    for iface_id in plan["delete_iface_ids"]:
        client.delete("dcim/interfaces", iface_id)
        print(f"      deleted interface {iface_id}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--url", default=os.getenv("NETBOX_URL"), required=not os.getenv("NETBOX_URL")
    )
    parser.add_argument(
        "--token", default=os.getenv("NETBOX_TOKEN"), required=not os.getenv("NETBOX_TOKEN")
    )
    parser.add_argument("--prefix", required=True, help="Prefix holding the addresses to migrate")
    parser.add_argument("--source-vrf", required=True, help="VRF of the addresses being retired")
    parser.add_argument(
        "--target-vrf", required=True, help="VRF holding the equal-valued replacements"
    )
    parser.add_argument(
        "--parent-iface", required=True, help="Name of the parent interface to delete"
    )
    parser.add_argument(
        "--sub-iface", required=True, help="Name of its sub-interface, deleted first"
    )
    parser.add_argument("--manufacturer", default=DEFAULT_MANUFACTURER, help="name or slug")
    parser.add_argument("--role", default=DEFAULT_ROLE, help="name or slug")
    parser.add_argument("--device", help="Restrict to a single device name (recommended first run)")
    parser.add_argument(
        "--force",
        action="store_true",
        help="Proceed even when the retiring interfaces are cabled or carry unrelated addresses "
        "(those addresses are then deleted too)",
    )
    parser.add_argument(
        "--show-skipped", action="store_true", help="List every skipped device and why"
    )
    parser.add_argument(
        "--apply", action="store_true", help="Actually write changes (default is a dry-run preview)"
    )
    parser.add_argument(
        "--no-verify-ssl", action="store_true", help="Disable SSL certificate verification"
    )
    args = parser.parse_args()

    client = NetBoxRestClient(url=args.url, token=args.token, verify_ssl=not args.no_verify_ssl)

    if not args.apply:
        print("DRY RUN -- no changes will be made (pass --apply to write)\n")

    plans = build_plans(client, args)
    # --device narrows the report to one device, so always give its skip reason: that
    # reason is the whole point of a single-device preview.
    actionable = print_plans(plans, show_skipped=args.show_skipped or bool(args.device))

    if not actionable:
        print("\nNothing to do.")
        return

    if not args.apply:
        # Dry-run counts are INTENDED actions, not performed ones -- label them as such.
        print(
            f"\nDRY RUN -- would update {len(actionable)} device(s), "
            f"delete {sum(len(p['delete_ip_ids']) for p in actionable)} address(es) and "
            f"{sum(len(p['delete_iface_ids']) for p in actionable)} interface(s). "
            "Pass --apply to perform."
        )
        return

    done, failed = 0, []
    for plan in actionable:
        print(f"  {plan['device']} ...")
        try:
            apply_plan(client, plan)
            done += 1
        except Exception as exc:  # keep going; one bad device must not strand the rest
            detail = failure_detail(exc)
            failed.append((plan["device"], detail))
            print(f"      FAILED: {detail}")

    print(f"\nDone. {done} device(s) migrated, {len(failed)} failed.")
    if failed:
        print("A device that failed part-way may be half-migrated -- check it by hand.")
        for name, detail in failed:
            print(f"  {name}: {detail}")
        sys.exit(1)


if __name__ == "__main__":
    main()
