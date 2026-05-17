"""
filter_plugins/topology_filters.py
===================================
Custom Ansible filters for the SAIL playbook.

Filters
-------
build_topology(args, srx_ip)
    Build the Grafana node-graph topology dict from IP lists and delta sets.

parse_srx_address_book(xml_text, set_name)
    Extract IP addresses from a Junos address-book XML stanza.

Usage in playbook
-----------------
  topology: "{{ (web_ips, db_ips, web_to_add, db_to_add, vsrx_ip) | build_topology }}"
  srx_web_ips: "{{ srx_xml_raw.stdout[0] | parse_srx_address_book('SET_WEB') }}"
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from typing import List


# ── build_topology ────────────────────────────────────────────────────────────

def build_topology(args: tuple, srx_ip: str = "") -> dict:
    """
    Called via the Jinja2 pipe syntax:
        (web_ips, db_ips, web_to_add, db_to_add, vsrx_ip) | build_topology

    Ansible passes the left-hand value as the first positional argument and
    the filter name resolves to this function, so `args` is the tuple and
    `srx_ip` is unused (vsrx_ip is already inside args[4]).

    Parameters inside args
    ----------------------
    args[0]  list[str]  web_ips        — desired Web VM IPs from Morpheus
    args[1]  list[str]  db_ips         — desired DB VM IPs from Morpheus
    args[2]  list[str]  web_to_add     — IPs being added this run
    args[3]  list[str]  db_to_add      — IPs being added this run
    args[4]  list[str]  web_to_remove  — IPs being removed this run
    args[5]  list[str]  db_to_remove   — IPs being removed this run
    args[6]  str        vsrx_ip        — management IP of the vSRX
    """
    web_ips, db_ips, web_to_add, db_to_add, web_to_remove, db_to_remove, vsrx_ip = args

    web_to_add_set = set(web_to_add)
    db_to_add_set  = set(db_to_add)
    web_to_remove_set = set(web_to_remove)
    db_to_remove_set = set(web_to_remove)

    # ── Nodes ─────────────────────────────────────────────────────────────────

    web_removed_nodes = [
        {
            "id":       ip,
            "title":    ip,
            "subTitle": "Web VM",
            "mainStat": "REMOVED",
            "color":    "red",
        }
        for ip in web_to_remove_set
    ]

    db_removed_nodes = [
        {
            "id":       ip,
            "title":    ip,
            "subTitle": "DB VM",
            "mainStat": "REMOVED",
            "color":    "red",
        }
        for ip in db_to_remove_set
    ]

    srx_node = {
        "id":       "srx",
        "title":    "vSRX",
        "subTitle": vsrx_ip,
        "mainStat": "Firewall",
        "color":    "orange",
    }

    zone_web_node = {
        "id":       "zone_web",
        "title":    "SET_WEB",
        "subTitle": "Web Tier",
        "mainStat": f"{len(web_ips)} VMs",
        "color":    "blue",
    }

    zone_db_node = {
        "id":       "zone_db",
        "title":    "SET_DB",
        "subTitle": "DB Tier",
        "mainStat": f"{len(db_ips)} VMs",
        "color":    "purple",
    }

    web_vm_nodes = [
        {
            "id":       ip,
            "title":    ip,
            "subTitle": "Web VM",
            "mainStat": "DRIFT FIXED" if ip in web_to_add_set else "IN SYNC",
            "color":    "red"          if ip in web_to_add_set else "green",
        }
        for ip in web_ips
    ]

    db_vm_nodes = [
        {
            "id":       ip,
            "title":    ip,
            "subTitle": "DB VM",
            "mainStat": "DRIFT FIXED" if ip in db_to_add_set else "IN SYNC",
            "color":    "red"          if ip in db_to_add_set else "green",
        }
        for ip in db_ips
    ]

    # ── Edges ─────────────────────────────────────────────────────────────────

    web_removed_edges = [
        {"id": f"web-{ip}", "source": "zone_web", "target": ip}
        for ip in web_to_remove_set
    ]

    db_removed_edges = [
        {"id": f"db-{ip}", "source": "zone_db", "target": ip}
        for ip in db_to_remove_set
    ]
    
    srx_to_zone_edges = [
        {"id": "srx-web", "source": "srx", "target": "zone_web"},
        {"id": "srx-db",  "source": "srx", "target": "zone_db"},
    ]

    web_edges = [
        {"id": f"web-{ip}", "source": "zone_web", "target": ip}
        for ip in web_ips
    ]

    db_edges = [
        {"id": f"db-{ip}", "source": "zone_db", "target": ip}
        for ip in db_ips
    ]

    return {
        "nodes": [srx_node, zone_web_node, zone_db_node] 
        + web_vm_nodes + db_vm_nodes
        + web_removed_nodes + db_removed_nodes,
        "edges": srx_to_zone_edges + web_edges + db_edges
        + web_removed_edges + db_removed_edges,
    }


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
                  ...
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

    We use `iter("address-set")` so the search is namespace-agnostic and
    works regardless of where in the envelope the stanza lives.

    Falls back gracefully to an empty list if the XML is missing, malformed,
    or the address-set doesn't exist yet (first run against a clean SRX).
    This means a brand-new SRX will simply result in web_to_add == web_ips
    and db_to_add == db_ips, which is exactly the right behaviour.
    """
    if not xml_text or not xml_text.strip():
        return []

    try:
        # Junos may emit an XML declaration and namespace prefixes;
        # strip the declaration so ElementTree doesn't choke on encoding attrs.
        clean = re.sub(r"<\?xml[^>]+\?>", "", xml_text).strip()
        root  = ET.fromstring(clean)

        # Walk every <address-set> regardless of namespace.
        for addr_set in root.iter("address-set"):
            name_el = addr_set.find("name")
            if name_el is None or name_el.text != set_name:
                continue
            # Collect the <name> of each <address> child (the IP string).
            return [
                addr.findtext("name", default="")
                for addr in addr_set.findall("address")
                if addr.findtext("name", default="")
            ]
    except ET.ParseError:
        # Non-fatal — return empty so the delta logic adds everything fresh.
        pass

    return []


# ── FilterModule ──────────────────────────────────────────────────────────────

class FilterModule:
    """Ansible filter plugin entry point."""

    def filters(self):
        return {
            "build_topology":          build_topology,
            "parse_srx_address_book":  parse_srx_address_book,
        }