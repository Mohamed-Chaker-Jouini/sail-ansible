"""
filter_plugins/topology_filters.py
===================================
Custom Ansible filters for the SAIL playbook.

Filters
-------
build_topology(args)
    Build the Grafana node-graph topology dict from a dynamic zone map and deltas.

parse_srx_address_book(xml_text, set_name)
    Extract IP addresses from a Junos address-book XML stanza (single set lookup).

parse_srx_zone_map(xml_text)
    Parse the full MORPHEUS_MANAGED address-book into a zone → [ips] dict.

compute_deltas(desired, current)
    Compare desired zone_map from Morpheus against current srx_zone_map and
    return per-zone add/remove lists.

Usage in playbook
-----------------
  topology:     "{{ (zone_map, delta_map, vsrx_ip) | build_topology }}"
  srx_zone_map: "{{ srx_xml_raw.output | parse_srx_zone_map }}"
  delta_map:    "{{ zone_map | compute_deltas(srx_zone_map) }}"
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from typing import Dict, List


# ── Helpers ───────────────────────────────────────────────────────────────────

# Cycle of zone-tier node colours (srx orange is reserved).
_ZONE_COLORS = ["blue", "purple", "green", "yellow", "semi-dark-red"]


def _zone_color(index: int) -> str:
    return _ZONE_COLORS[index % len(_ZONE_COLORS)]


# ── parse_srx_address_book ────────────────────────────────────────────────────

def parse_srx_address_book(xml_text: str, set_name: str) -> List[str]:
    """
    Parse a Junos address-book XML stanza and return the list of IPs
    that belong to the requested address-set.

    The XML from the NETCONF <get-config> RPC looks like:

        <rpc-reply>
          <data>
            <configuration>
              <security>
                <address-book>
                  <name>MORPHEUS_MANAGED</name>
                  <address>
                    <name>10.0.0.1</name>
                    <ip-prefix>10.0.0.1/32</ip-prefix>
                  </address>
                  <address-set>
                    <name>SET_WEB</name>
                    <address>
                      <name>10.0.0.1</name>
                    </address>
                  </address-set>
                </address-book>
              </security>
            </configuration>
          </data>
        </rpc-reply>

    Falls back gracefully to an empty list if the XML is missing, malformed,
    or the address-set does not exist yet (first run against a clean SRX).
    """
    if not xml_text or not xml_text.strip():
        return []

    try:
        clean = re.sub(r"<\?xml[^>]+\?>", "", xml_text).strip()
        root  = ET.fromstring(clean)

        for addr_set in root.iter("address-set"):
            name_el = addr_set.find("name")
            if name_el is None or name_el.text != set_name:
                continue
            return [
                addr.findtext("name", default="")
                for addr in addr_set.findall("address")
                if addr.findtext("name", default="")
            ]
    except ET.ParseError:
        pass

    return []


# ── parse_srx_zone_map ────────────────────────────────────────────────────────

def parse_srx_zone_map(xml_text: str) -> Dict[str, List[str]]:
    """
    Parse the full MORPHEUS_MANAGED address-book XML and return a dict of:
        { "WEB": ["ip1", "ip2"], "DB": ["ip3"], ... }

    The SET_ prefix is stripped from each address-set name so zone names
    match the AppTier tag values coming from Morpheus (uppercased).

    Falls back gracefully to an empty dict on missing/malformed XML so that
    a brand-new SRX simply results in all desired IPs landing in to_add.
    """
    if not xml_text or not xml_text.strip():
        return {}

    try:
        clean = re.sub(r"<\?xml[^>]+\?>", "", xml_text).strip()
        root  = ET.fromstring(clean)
        result: Dict[str, List[str]] = {}

        for addr_set in root.iter("address-set"):
            name_el = addr_set.find("name")
            if name_el is None:
                continue
            raw_name = name_el.text or ""
            # Normalise: strip the SET_ prefix Ansible added, uppercase the rest.
            zone = raw_name.replace("SET_", "", 1).upper()
            ips  = [
                addr.findtext("name", default="")
                for addr in addr_set.findall("address")
                if addr.findtext("name", default="")
            ]
            result[zone] = ips

        return result

    except ET.ParseError:
        return {}


# ── compute_deltas ────────────────────────────────────────────────────────────

def compute_deltas(desired: Dict[str, List[str]],
                   current: Dict[str, List[str]]) -> Dict[str, Dict[str, List[str]]]:
    """
    Compare the desired zone_map (from Morpheus) against the current
    srx_zone_map (parsed from NETCONF) and return per-zone delta dicts.

    Called via Jinja2 pipe:
        zone_map | compute_deltas(srx_zone_map)

    Returns:
        {
          "WEB": { "to_add": [...], "to_remove": [...] },
          "DB":  { "to_add": [...], "to_remove": [...] },
          "APP": { "to_add": [...], "to_remove": [] },   # brand-new zone
        }

    Zones present in current but absent from desired will have an empty
    to_add and a full to_remove, allowing clean-up of orphaned sets.
    """
    all_zones = set(desired.keys()) | set(current.keys())
    deltas: Dict[str, Dict[str, List[str]]] = {}

    for zone in sorted(all_zones):
        want = set(desired.get(zone, []))
        have = set(current.get(zone, []))
        deltas[zone] = {
            "to_add":    sorted(want - have),
            "to_remove": sorted(have - want),
        }

    return deltas


# ── build_topology ────────────────────────────────────────────────────────────

def build_topology(args: tuple, _unused: str = "") -> dict:
    """
    Build the Grafana node-graph topology dict from a dynamic zone map.

    Called via the Jinja2 pipe syntax:
        (zone_map, delta_map, vsrx_ip) | build_topology

    Parameters inside args
    ----------------------
    args[0]  dict[str, list[str]]  zone_map   — { ZONE: [ips] } from Morpheus
    args[1]  dict[str, dict]       delta_map  — { ZONE: {to_add, to_remove} }
    args[2]  str                   vsrx_ip    — management IP of the vSRX

    Node IDs
    --------
    - SRX firewall  : "srx"
    - Zone tier     : "zone_{zone_lower}"          e.g. "zone_web"
    - Active VM     : "{ip}"                       e.g. "10.0.0.1"
    - Removed VM    : "removed-{zone_lower}-{ip}"  e.g. "removed-web-10.0.0.1"

    Edge IDs follow the same conventions so source→target pairs are always
    unambiguous even when the same IP appears in multiple zones.
    """
    zone_map, delta_map, vsrx_ip = args

    nodes: list = []
    edges: list = []

    # ── vSRX anchor node ──────────────────────────────────────────────────────
    nodes.append({
        "id":       "srx",
        "title":    "vSRX",
        "subTitle": vsrx_ip,
        "mainStat": "Firewall",
        "color":    "orange",
    })

    # ── Per-zone nodes and edges ───────────────────────────────────────────────
    for idx, (zone, ips) in enumerate(sorted(zone_map.items())):
        zone_lower = zone.lower()
        zone_id    = f"zone_{zone_lower}"
        color      = _zone_color(idx)

        to_add    = set(delta_map.get(zone, {}).get("to_add",    []))
        to_remove = set(delta_map.get(zone, {}).get("to_remove", []))

        # Zone tier node
        nodes.append({
            "id":       zone_id,
            "title":    f"SET_{zone}",
            "subTitle": f"{zone} Tier",
            "mainStat": f"{len(ips)} VMs",
            "color":    color,
        })

        # SRX → zone edge
        edges.append({
            "id":     f"srx-{zone_lower}",
            "source": "srx",
            "target": zone_id,
        })

        # Active VM nodes and edges
        for ip in ips:
            drift = ip in to_add
            nodes.append({
                "id":       ip,
                "title":    ip,
                "subTitle": f"{zone} VM",
                "mainStat": "DRIFT FIXED" if drift else "IN SYNC",
                "color":    "red"          if drift else "green",
            })
            edges.append({
                "id":     f"{zone_lower}-{ip}",
                "source": zone_id,
                "target": ip,
            })

        # Removed VM ghost nodes and edges
        # Node ID is prefixed with "removed-{zone_lower}-" to guarantee
        # uniqueness even when the same IP was in multiple zones.
        for ip in to_remove:
            removed_id = f"removed-{zone_lower}-{ip}"
            nodes.append({
                "id":       removed_id,
                "title":    ip,
                "subTitle": f"{zone} VM",
                "mainStat": "REMOVED",
                "color":    "red",
            })
            edges.append({
                "id":     f"{zone_lower}-removed-{ip}",
                "source": zone_id,
                "target": removed_id,
            })

    return {"nodes": nodes, "edges": edges}


# ── FilterModule ──────────────────────────────────────────────────────────────

class FilterModule:
    """Ansible filter plugin entry point."""

    def filters(self):
        return {
            "build_topology":         build_topology,
            "parse_srx_address_book": parse_srx_address_book,  # kept for single-set lookups
            "parse_srx_zone_map":     parse_srx_zone_map,
            "compute_deltas":         compute_deltas,
        }