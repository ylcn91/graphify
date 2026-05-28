"""Tests for `graphify prune --min-degree N` graph hygiene (#728)."""
from __future__ import annotations
import json
import pytest
import graphify.__main__ as mainmod
from graphify.build import prune_low_degree_nodes
from graphify.export import prune_dangling_edges


def _write_graph(tmp_path):
    """A directed graph with:
      - a, b connected by a->b (each degree 1)
      - c->a so a has degree 2 (in+out), c has degree 1
      - iso: isolated node (degree 0)
    Carries community/norm_label/directed so we can assert schema round-trips.
    """
    graph_data = {
        "directed": True, "multigraph": False, "graph": {},
        "nodes": [
            {"id": "a", "label": "Alpha", "source_file": "a.py",
             "community": 0, "norm_label": "alpha", "file_type": "code"},
            {"id": "b", "label": "Beta", "source_file": "b.py",
             "community": 0, "norm_label": "beta", "file_type": "code"},
            {"id": "c", "label": "Gamma", "source_file": "c.py",
             "community": 1, "norm_label": "gamma", "file_type": "code"},
            {"id": "iso", "label": "Lonely", "source_file": "iso.py",
             "community": 1, "norm_label": "lonely", "file_type": "code"},
        ],
        "links": [
            {"source": "a", "target": "b", "relation": "calls", "confidence": "EXTRACTED"},
            {"source": "c", "target": "a", "relation": "calls", "confidence": "EXTRACTED"},
        ],
        "hyperedges": [],
        "built_at_commit": "deadbeef",
    }
    p = tmp_path / "graph.json"
    p.write_text(json.dumps(graph_data))
    return p


def _run(monkeypatch, capsys, *argv):
    monkeypatch.setattr(mainmod, "_check_skill_version", lambda _: None)
    monkeypatch.setattr(mainmod.sys, "argv", ["graphify", "prune", *argv])
    mainmod.main()
    return capsys.readouterr().out


def test_min_degree_1_drops_isolated_only(monkeypatch, tmp_path, capsys):
    p = _write_graph(tmp_path)
    out = _run(monkeypatch, capsys, "--graph", str(p), "--min-degree", "1")
    data = json.loads(p.read_text())
    ids = {n["id"] for n in data["nodes"]}
    assert ids == {"a", "b", "c"}  # iso dropped, all connected nodes kept
    assert len(data["links"]) == 2  # no dangling edges, both survive
    assert "nodes: 4 -> 3 (1 removed)" in out


def test_min_degree_2_drops_degree_1_leaves(monkeypatch, tmp_path, capsys):
    p = _write_graph(tmp_path)
    out = _run(monkeypatch, capsys, "--graph", str(p), "--min-degree", "2")
    data = json.loads(p.read_text())
    ids = {n["id"] for n in data["nodes"]}
    # a has in+out degree 2 (c->a, a->b); b and c have degree 1; iso degree 0.
    assert ids == {"a"}
    # Both edges become dangling once b and c are removed.
    assert data["links"] == []
    assert "nodes: 4 -> 1 (3 removed)" in out
    assert "edges: 2 -> 0 (2 removed)" in out


def test_default_min_degree_is_1(monkeypatch, tmp_path, capsys):
    p = _write_graph(tmp_path)
    out = _run(monkeypatch, capsys, "--graph", str(p))
    data = json.loads(p.read_text())
    assert {n["id"] for n in data["nodes"]} == {"a", "b", "c"}
    assert "(--min-degree 1)" in out


def test_schema_fields_preserved(monkeypatch, tmp_path, capsys):
    p = _write_graph(tmp_path)
    _run(monkeypatch, capsys, "--graph", str(p), "--min-degree", "1")
    data = json.loads(p.read_text())
    assert data["directed"] is True
    assert data["multigraph"] is False
    assert data["built_at_commit"] == "deadbeef"
    assert "hyperedges" in data
    node_a = next(n for n in data["nodes"] if n["id"] == "a")
    assert node_a["community"] == 0
    assert node_a["norm_label"] == "alpha"
    assert node_a["source_file"] == "a.py"
    link = data["links"][0]
    assert link["relation"] == "calls"
    assert link["confidence"] == "EXTRACTED"


def test_positional_path_argument(monkeypatch, tmp_path, capsys):
    p = _write_graph(tmp_path)
    out = _run(monkeypatch, capsys, str(p), "--min-degree", "1")
    data = json.loads(p.read_text())
    assert {n["id"] for n in data["nodes"]} == {"a", "b", "c"}
    assert str(p) in out


def test_min_degree_equals_form(monkeypatch, tmp_path, capsys):
    p = _write_graph(tmp_path)
    _run(monkeypatch, capsys, "--graph", str(p), "--min-degree=2")
    data = json.loads(p.read_text())
    assert {n["id"] for n in data["nodes"]} == {"a"}


def test_missing_graph_exits(monkeypatch, tmp_path, capsys):
    missing = tmp_path / "nope.json"
    monkeypatch.setattr(mainmod, "_check_skill_version", lambda _: None)
    monkeypatch.setattr(mainmod.sys, "argv",
                        ["graphify", "prune", "--graph", str(missing)])
    with pytest.raises(SystemExit):
        mainmod.main()


def test_invalid_min_degree_exits(monkeypatch, tmp_path, capsys):
    p = _write_graph(tmp_path)
    monkeypatch.setattr(mainmod, "_check_skill_version", lambda _: None)
    monkeypatch.setattr(mainmod.sys, "argv",
                        ["graphify", "prune", "--graph", str(p), "--min-degree", "x"])
    with pytest.raises(SystemExit):
        mainmod.main()


def test_helper_counts_total_in_out_degree():
    """prune_low_degree_nodes counts in+out degree, not just out-degree."""
    data = {
        "nodes": [{"id": "a"}, {"id": "b"}, {"id": "sink"}],
        "links": [
            {"source": "a", "target": "sink"},
            {"source": "b", "target": "sink"},
        ],
    }
    # sink has in-degree 2; a and b each have out-degree 1.
    _, removed = prune_low_degree_nodes(data, 2)
    ids = {n["id"] for n in data["nodes"]}
    assert ids == {"sink"}  # only sink reaches total degree 2
    assert removed == 2


def test_helper_then_dangling_edges_edges_key():
    """Works against the legacy 'edges' key, not just 'links'."""
    data = {
        "nodes": [{"id": "x"}, {"id": "y"}, {"id": "iso"}],
        "edges": [{"source": "x", "target": "y"}],
    }
    data, removed_nodes = prune_low_degree_nodes(data, 1)
    data, removed_edges = prune_dangling_edges(data)
    assert {n["id"] for n in data["nodes"]} == {"x", "y"}
    assert removed_nodes == 1
    assert removed_edges == 0
