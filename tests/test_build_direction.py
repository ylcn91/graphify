"""Regression tests for #1061: code graphs must preserve caller→callee direction.

Building undirected collapses every directed edge onto an unordered node pair,
so directional edges that share endpoints (mutual calls, or the same pair seen
twice) get dropped or reversed by last-write-wins. The build path now defaults to
a DiGraph; these tests pin that behaviour end-to-end through to_json.
"""
import json

import networkx as nx

from graphify.build import build_from_json
from graphify.export import to_json


def _caller_callee_extraction():
    """Two-file caller→callee graph with a mutual edge that collides undirected.

    a_main → b_helper and b_helper → a_main share the unordered pair
    {a_main, b_helper}; an undirected build keeps only one of them.
    """
    nodes = [
        {"id": "a_main", "label": "main", "source_file": "a/app.ts", "file_type": "code"},
        {"id": "b_helper", "label": "helper", "source_file": "b/util.ts", "file_type": "code"},
        {"id": "a_cb", "label": "cb", "source_file": "a/app.ts", "file_type": "code"},
    ]
    edges = [
        {"source": "a_main", "target": "b_helper", "relation": "calls",
         "confidence": "EXTRACTED", "source_file": "a/app.ts"},
        {"source": "b_helper", "target": "a_cb", "relation": "calls",
         "confidence": "EXTRACTED", "source_file": "b/util.ts"},
        {"source": "b_helper", "target": "a_main", "relation": "calls",
         "confidence": "EXTRACTED", "source_file": "b/util.ts"},
    ]
    return {"nodes": nodes, "edges": edges}


def test_default_build_is_directed():
    G = build_from_json(_caller_callee_extraction())
    assert isinstance(G, nx.DiGraph)


def test_directed_build_preserves_all_directional_edges():
    G = build_from_json(_caller_callee_extraction())
    assert G.number_of_edges() == 3
    assert G.has_edge("a_main", "b_helper")
    assert G.has_edge("b_helper", "a_main")
    assert G.has_edge("b_helper", "a_cb")


def test_undirected_opt_in_collapses_shared_pair():
    G = build_from_json(_caller_callee_extraction(), directed=False)
    assert isinstance(G, nx.Graph)
    assert not isinstance(G, nx.DiGraph)
    # The mutual pair collapses onto one undirected slot — the lost edge is the
    # bug #1061 documents; undirected remains available only as an explicit opt-in.
    assert G.number_of_edges() == 2


def test_graph_json_orientation_matches_caller_callee(tmp_path):
    G = build_from_json(_caller_callee_extraction())
    out = tmp_path / "graph.json"
    to_json(G, {}, str(out), force=True)
    data = json.loads(out.read_text())

    assert data.get("directed") is True
    links = {(l["source"], l["target"]) for l in data["links"]}
    assert ("a_main", "b_helper") in links
    assert ("b_helper", "a_main") in links
    assert ("b_helper", "a_cb") in links
