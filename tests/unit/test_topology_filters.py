# tests/unit/test_topology_filters.py
"""
Unit tests for filter_plugins/topology_filters.py

Run with:
    pytest tests/unit/test_topology_filters.py -v
"""
import sys
import os
import pytest

# Make the filter_plugins package importable without an Ansible install
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "filter_plugins"))
from topology_filters import (
    parse_srx_address_book,
    parse_srx_zone_map,
    compute_deltas,
    build_topology,
    FilterModule,
)


# ─── Fixtures / shared test data ─────────────────────────────────────────────

SAMPLE_XML = """<?xml version="1.0"?>
<rpc-reply>
  <data>
    <configuration>
      <security>
        <address-book>
          <name>MORPHEUS_MANAGED</name>
          <address><name>10.0.0.1</name><ip-prefix>10.0.0.1/32</ip-prefix></address>
          <address><name>10.0.0.2</name><ip-prefix>10.0.0.2/32</ip-prefix></address>
          <address><name>10.0.0.3</name><ip-prefix>10.0.0.3/32</ip-prefix></address>
          <address><name>10.0.1.1</name><ip-prefix>10.0.1.1/32</ip-prefix></address>
          <address-set>
            <name>SET_WEB</name>
            <address><name>10.0.0.1</name></address>
            <address><name>10.0.0.2</name></address>
          </address-set>
          <address-set>
            <name>SET_DB</name>
            <address><name>10.0.0.3</name></address>
          </address-set>
        </address-book>
      </security>
    </configuration>
  </data>
</rpc-reply>"""

MALFORMED_XML = "<rpc-reply><data><unclosed>"
EMPTY_XML     = ""
WHITESPACE_XML = "   \n   "


# ─── parse_srx_address_book ───────────────────────────────────────────────────

class TestParseSrxAddressBook:
    def test_returns_ips_for_existing_set(self):
        result = parse_srx_address_book(SAMPLE_XML, "SET_WEB")
        assert sorted(result) == ["10.0.0.1", "10.0.0.2"]

    def test_returns_single_ip_for_db_set(self):
        result = parse_srx_address_book(SAMPLE_XML, "SET_DB")
        assert result == ["10.0.0.3"]

    def test_returns_empty_list_for_nonexistent_set(self):
        assert parse_srx_address_book(SAMPLE_XML, "SET_APP") == []

    def test_returns_empty_list_for_empty_string(self):
        assert parse_srx_address_book(EMPTY_XML, "SET_WEB") == []

    def test_returns_empty_list_for_whitespace_string(self):
        assert parse_srx_address_book(WHITESPACE_XML, "SET_WEB") == []

    def test_returns_empty_list_for_malformed_xml(self):
        assert parse_srx_address_book(MALFORMED_XML, "SET_WEB") == []

    def test_returns_empty_list_for_none(self):
        assert parse_srx_address_book(None, "SET_WEB") == []

    def test_xml_without_xml_declaration(self):
        xml_no_decl = SAMPLE_XML.replace('<?xml version="1.0"?>\n', '')
        result = parse_srx_address_book(xml_no_decl, "SET_WEB")
        assert sorted(result) == ["10.0.0.1", "10.0.0.2"]


# ─── parse_srx_zone_map ───────────────────────────────────────────────────────

class TestParseSrxZoneMap:
    def test_returns_all_zones(self):
        result = parse_srx_zone_map(SAMPLE_XML)
        assert set(result.keys()) == {"WEB", "DB"}

    def test_strips_set_prefix_and_uppercases(self):
        result = parse_srx_zone_map(SAMPLE_XML)
        assert "WEB" in result
        assert "DB"  in result

    def test_correct_ips_per_zone(self):
        result = parse_srx_zone_map(SAMPLE_XML)
        assert sorted(result["WEB"]) == ["10.0.0.1", "10.0.0.2"]
        assert result["DB"] == ["10.0.0.3"]

    def test_returns_empty_dict_for_empty_string(self):
        assert parse_srx_zone_map(EMPTY_XML) == {}

    def test_returns_empty_dict_for_malformed_xml(self):
        assert parse_srx_zone_map(MALFORMED_XML) == {}

    def test_returns_empty_dict_for_none(self):
        assert parse_srx_zone_map(None) == {}

    def test_address_book_with_no_address_sets(self):
        xml = """<rpc-reply><data><configuration><security>
          <address-book><name>MORPHEUS_MANAGED</name>
            <address><name>10.0.0.1</name></address>
          </address-book></security></configuration></data></rpc-reply>"""
        assert parse_srx_zone_map(xml) == {}

    def test_zone_with_no_member_addresses(self):
        xml = """<rpc-reply><data><configuration><security>
          <address-book><name>MORPHEUS_MANAGED</name>
            <address-set><name>SET_WEB</name></address-set>
          </address-book></security></configuration></data></rpc-reply>"""
        result = parse_srx_zone_map(xml)
        assert result.get("WEB", []) == []


# ─── compute_deltas ───────────────────────────────────────────────────────────

class TestComputeDeltas:
    def test_no_changes_when_maps_identical(self):
        desired = {"WEB": ["10.0.0.1", "10.0.0.2"]}
        current = {"WEB": ["10.0.0.1", "10.0.0.2"]}
        deltas  = compute_deltas(desired, current)
        assert deltas["WEB"]["to_add"]    == []
        assert deltas["WEB"]["to_remove"] == []

    def test_adds_new_ip(self):
        desired = {"WEB": ["10.0.0.1", "10.0.0.3"]}
        current = {"WEB": ["10.0.0.1"]}
        deltas  = compute_deltas(desired, current)
        assert deltas["WEB"]["to_add"]    == ["10.0.0.3"]
        assert deltas["WEB"]["to_remove"] == []

    def test_removes_stale_ip(self):
        desired = {"WEB": ["10.0.0.1"]}
        current = {"WEB": ["10.0.0.1", "10.0.0.2"]}
        deltas  = compute_deltas(desired, current)
        assert deltas["WEB"]["to_add"]    == []
        assert deltas["WEB"]["to_remove"] == ["10.0.0.2"]

    def test_brand_new_zone(self):
        desired = {"APP": ["10.1.0.1"]}
        current = {}
        deltas  = compute_deltas(desired, current)
        assert deltas["APP"]["to_add"]    == ["10.1.0.1"]
        assert deltas["APP"]["to_remove"] == []

    def test_orphaned_zone_fully_removed(self):
        desired = {}
        current = {"WEB": ["10.0.0.1"]}
        deltas  = compute_deltas(desired, current)
        assert deltas["WEB"]["to_add"]    == []
        assert deltas["WEB"]["to_remove"] == ["10.0.0.1"]

    def test_both_maps_empty(self):
        assert compute_deltas({}, {}) == {}

    def test_multiple_zones_with_mixed_changes(self):
        desired = {
            "WEB": ["10.0.0.1", "10.0.0.3"],
            "DB":  ["10.0.1.1"],
            "APP": ["10.0.2.1"],
        }
        current = {
            "WEB": ["10.0.0.1", "10.0.0.2"],
            "DB":  ["10.0.1.1"],
        }
        deltas = compute_deltas(desired, current)
        assert deltas["WEB"]["to_add"]    == ["10.0.0.3"]
        assert deltas["WEB"]["to_remove"] == ["10.0.0.2"]
        assert deltas["DB"]["to_add"]     == []
        assert deltas["DB"]["to_remove"]  == []
        assert deltas["APP"]["to_add"]    == ["10.0.2.1"]
        assert deltas["APP"]["to_remove"] == []

    def test_output_lists_are_sorted(self):
        desired = {"WEB": ["10.0.0.3", "10.0.0.1", "10.0.0.2"]}
        current = {"WEB": []}
        deltas  = compute_deltas(desired, current)
        assert deltas["WEB"]["to_add"] == sorted(deltas["WEB"]["to_add"])

    def test_duplicate_ips_in_desired_treated_as_set(self):
        desired = {"WEB": ["10.0.0.1", "10.0.0.1"]}
        current = {"WEB": []}
        deltas  = compute_deltas(desired, current)
        assert deltas["WEB"]["to_add"] == ["10.0.0.1"]


# ─── build_topology ───────────────────────────────────────────────────────────

class TestBuildTopology:
    @pytest.fixture
    def basic_args(self):
        zone_map  = {"WEB": ["10.0.0.1", "10.0.0.2"], "DB": ["10.0.1.1"]}
        delta_map = {
            "WEB": {"to_add": ["10.0.0.2"], "to_remove": []},
            "DB":  {"to_add": [],           "to_remove": ["10.0.1.99"]},
        }
        vsrx_ip   = "192.168.1.1"
        return (zone_map, delta_map, vsrx_ip)

    def test_returns_nodes_and_edges_keys(self, basic_args):
        result = build_topology(basic_args)
        assert "nodes" in result
        assert "edges" in result

    def test_srx_anchor_node_present(self, basic_args):
        nodes = build_topology(basic_args)["nodes"]
        srx   = next((n for n in nodes if n["id"] == "srx"), None)
        assert srx is not None
        assert srx["color"] == "orange"
        assert srx["subTitle"] == "192.168.1.1"

    def test_zone_tier_nodes_created(self, basic_args):
        nodes   = build_topology(basic_args)["nodes"]
        node_ids = {n["id"] for n in nodes}
        assert "zone_web" in node_ids
        assert "zone_db"  in node_ids

    def test_vm_nodes_created_for_active_ips(self, basic_args):
        nodes   = build_topology(basic_args)["nodes"]
        node_ids = {n["id"] for n in nodes}
        assert "10.0.0.1" in node_ids
        assert "10.0.0.2" in node_ids
        assert "10.0.1.1" in node_ids

    def test_drift_node_is_red(self, basic_args):
        nodes = build_topology(basic_args)["nodes"]
        drifted = next(n for n in nodes if n["id"] == "10.0.0.2")
        assert drifted["color"]    == "red"
        assert drifted["mainStat"] == "DRIFT FIXED"

    def test_in_sync_node_is_green(self, basic_args):
        nodes = build_topology(basic_args)["nodes"]
        synced = next(n for n in nodes if n["id"] == "10.0.0.1")
        assert synced["color"]    == "green"
        assert synced["mainStat"] == "IN SYNC"

    def test_removed_vm_ghost_node_created(self, basic_args):
        nodes   = build_topology(basic_args)["nodes"]
        node_ids = {n["id"] for n in nodes}
        assert "removed-db-10.0.1.99" in node_ids

    def test_removed_ghost_node_is_red_removed(self, basic_args):
        nodes   = build_topology(basic_args)["nodes"]
        removed = next(n for n in nodes if n["id"] == "removed-db-10.0.1.99")
        assert removed["color"]    == "red"
        assert removed["mainStat"] == "REMOVED"

    def test_srx_to_zone_edges_present(self, basic_args):
        edges   = build_topology(basic_args)["edges"]
        edge_ids = {e["id"] for e in edges}
        assert "srx-web" in edge_ids
        assert "srx-db"  in edge_ids

    def test_zone_to_vm_edges_present(self, basic_args):
        edges   = build_topology(basic_args)["edges"]
        edge_ids = {e["id"] for e in edges}
        assert "web-10.0.0.1" in edge_ids
        assert "db-10.0.1.1"  in edge_ids

    def test_empty_zone_map_yields_only_srx_node(self):
        result = build_topology(({}, {}, "1.2.3.4"))
        assert len(result["nodes"]) == 1
        assert result["nodes"][0]["id"] == "srx"
        assert result["edges"] == []

    def test_zone_count_stat_on_tier_node(self, basic_args):
        nodes   = build_topology(basic_args)["nodes"]
        web_node = next(n for n in nodes if n["id"] == "zone_web")
        assert "2 VMs" in web_node["mainStat"]

    def test_no_duplicate_node_ids(self, basic_args):
        nodes = build_topology(basic_args)["nodes"]
        ids   = [n["id"] for n in nodes]
        assert len(ids) == len(set(ids)), "Duplicate node IDs found"

    def test_no_duplicate_edge_ids(self, basic_args):
        edges = build_topology(basic_args)["edges"]
        ids   = [e["id"] for e in edges]
        assert len(ids) == len(set(ids)), "Duplicate edge IDs found"


# ─── FilterModule entry point ─────────────────────────────────────────────────

class TestFilterModule:
    def test_all_filters_registered(self):
        fm      = FilterModule()
        filters = fm.filters()
        assert "build_topology"         in filters
        assert "parse_srx_address_book" in filters
        assert "parse_srx_zone_map"     in filters
        assert "compute_deltas"         in filters

    def test_filter_callables(self):
        for name, fn in FilterModule().filters().items():
            assert callable(fn), f"{name} is not callable"