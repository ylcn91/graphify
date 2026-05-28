"""Tests for `graphify` manual graph-hygiene ops (#595).

Covers rename-node / merge-node / drop-edge / relabel-edge, both at the CLI
layer (argv parsing, reports, exit codes) and the pure build.py helpers.
"""
from __future__ import annotations
import json
import pytest
import graphify.__main__ as mainmod
from graphify.build import (
    rename_node_in_graph,
    merge_nodes_in_graph,
    drop_edge_in_graph,
    relabel_edge_in_graph,
)


def _write_graph(tmp_path):
    """A directed graph carrying community/norm_label/directed/hyperedges so
    we can assert the schema round-trips byte-faithfully after surgery.

      a -calls-> b
      c -calls-> a
      c -imports-> b
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
        ],
        "links": [
            {"source": "a", "target": "b", "relation": "calls", "confidence": "EXTRACTED"},
            {"source": "c", "target": "a", "relation": "calls", "confidence": "EXTRACTED"},
            {"source": "c", "target": "b", "relation": "imports", "confidence": "EXTRACTED"},
        ],
        "hyperedges": [],
        "built_at_commit": "deadbeef",
    }
    p = tmp_path / "graph.json"
    p.write_text(json.dumps(graph_data))
    return p


def _run(monkeypatch, capsys, *argv):
    monkeypatch.setattr(mainmod, "_check_skill_version", lambda _: None)
    monkeypatch.setattr(mainmod.sys, "argv", ["graphify", *argv])
    mainmod.main()
    return capsys.readouterr().out


def _expect_exit(monkeypatch, *argv):
    monkeypatch.setattr(mainmod, "_check_skill_version", lambda _: None)
    monkeypatch.setattr(mainmod.sys, "argv", ["graphify", *argv])
    with pytest.raises(SystemExit) as exc:
        mainmod.main()
    return exc.value.code


# --------------------------------------------------------------------------- #
# rename-node
# --------------------------------------------------------------------------- #
def test_rename_node_rewrites_all_edge_endpoints(monkeypatch, tmp_path, capsys):
    p = _write_graph(tmp_path)
    out = _run(monkeypatch, capsys, "rename-node", "a", "alpha2", "--graph", str(p))
    data = json.loads(p.read_text())
    assert {n["id"] for n in data["nodes"]} == {"alpha2", "b", "c"}
    # a appeared as source (a->b) and target (c->a): both rewritten.
    assert {"alpha2", "b"} == {data["links"][0]["source"], data["links"][0]["target"]}
    assert data["links"][1]["target"] == "alpha2"
    assert not any(
        e["source"] == "a" or e["target"] == "a" for e in data["links"]
    )
    assert "edge endpoints rewritten: 2" in out


def test_rename_node_positional_path(monkeypatch, tmp_path, capsys):
    p = _write_graph(tmp_path)
    out = _run(monkeypatch, capsys, "rename-node", "b", "beta2", str(p))
    data = json.loads(p.read_text())
    assert {n["id"] for n in data["nodes"]} == {"a", "beta2", "c"}
    assert str(p) in out


def test_rename_node_missing_exits_nonzero(monkeypatch, tmp_path):
    p = _write_graph(tmp_path)
    assert _expect_exit(monkeypatch, "rename-node", "nope", "x", "--graph", str(p)) == 1


def test_rename_node_collision_exits_nonzero(monkeypatch, tmp_path):
    p = _write_graph(tmp_path)
    # renaming a -> b would collide with the existing node b.
    assert _expect_exit(monkeypatch, "rename-node", "a", "b", "--graph", str(p)) == 1


def test_rename_node_preserves_schema(monkeypatch, tmp_path, capsys):
    p = _write_graph(tmp_path)
    _run(monkeypatch, capsys, "rename-node", "a", "alpha2", "--graph", str(p))
    data = json.loads(p.read_text())
    assert data["directed"] is True
    assert data["multigraph"] is False
    assert data["built_at_commit"] == "deadbeef"
    assert "hyperedges" in data
    node = next(n for n in data["nodes"] if n["id"] == "alpha2")
    assert node["community"] == 0
    assert node["norm_label"] == "alpha"


# --------------------------------------------------------------------------- #
# merge-node
# --------------------------------------------------------------------------- #
def test_merge_node_rewires_and_drops(monkeypatch, tmp_path, capsys):
    p = _write_graph(tmp_path)
    # merge c into a: c->a becomes a->a (self-loop, dropped); c->b becomes a->b
    # which duplicates the existing a->b/calls? No — c->b is relation 'imports',
    # a->b is relation 'calls', so it survives as a->b/imports.
    out = _run(monkeypatch, capsys, "merge-node", "c", "a", "--graph", str(p))
    data = json.loads(p.read_text())
    assert {n["id"] for n in data["nodes"]} == {"a", "b"}  # c dropped
    assert not any(e["source"] == "c" or e["target"] == "c" for e in data["links"])
    # self-loop a->a dropped; a->b/calls and a->b/imports both remain.
    triples = {(e["source"], e["target"], e["relation"]) for e in data["links"]}
    assert triples == {("a", "b", "calls"), ("a", "b", "imports")}
    assert "edges dropped (self-loops/dupes): 1" in out


def test_merge_node_drops_duplicate_relation(monkeypatch, tmp_path, capsys):
    """A rewired edge that duplicates an existing (src,tgt,relation) is dropped."""
    graph_data = {
        "directed": True, "multigraph": False,
        "nodes": [{"id": "a"}, {"id": "b"}, {"id": "c"}],
        "links": [
            {"source": "a", "target": "b", "relation": "calls"},
            {"source": "c", "target": "b", "relation": "calls"},
        ],
    }
    p = tmp_path / "graph.json"
    p.write_text(json.dumps(graph_data))
    out = _run(monkeypatch, capsys, "merge-node", "c", "a", "--graph", str(p))
    data = json.loads(p.read_text())
    assert {n["id"] for n in data["nodes"]} == {"a", "b"}
    # c->b/calls rewired to a->b/calls duplicates the existing edge -> dropped.
    assert data["links"] == [{"source": "a", "target": "b", "relation": "calls"}]
    assert "edges dropped (self-loops/dupes): 1" in out


def test_merge_node_into_attrs_win(monkeypatch, tmp_path, capsys):
    p = _write_graph(tmp_path)
    _run(monkeypatch, capsys, "merge-node", "c", "a", "--graph", str(p))
    data = json.loads(p.read_text())
    node_a = next(n for n in data["nodes"] if n["id"] == "a")
    # 'into' (a) attrs kept verbatim, not overwritten by 'from' (c).
    assert node_a["community"] == 0
    assert node_a["norm_label"] == "alpha"
    assert node_a["source_file"] == "a.py"


def test_merge_node_missing_exits_nonzero(monkeypatch, tmp_path):
    p = _write_graph(tmp_path)
    assert _expect_exit(monkeypatch, "merge-node", "nope", "a", "--graph", str(p)) == 1
    assert _expect_exit(monkeypatch, "merge-node", "a", "nope", "--graph", str(p)) == 1


# --------------------------------------------------------------------------- #
# drop-edge
# --------------------------------------------------------------------------- #
def test_drop_edge_removes_all_between_endpoints(monkeypatch, tmp_path, capsys):
    p = _write_graph(tmp_path)
    out = _run(monkeypatch, capsys, "drop-edge", "a", "b", "--graph", str(p))
    data = json.loads(p.read_text())
    assert not any(e["source"] == "a" and e["target"] == "b" for e in data["links"])
    assert len(data["links"]) == 2
    assert "edges removed: 1" in out


def test_drop_edge_respects_relation(monkeypatch, tmp_path, capsys):
    """With two edges c->b (one would, here only one), --relation filters."""
    graph_data = {
        "directed": True, "multigraph": False,
        "nodes": [{"id": "c"}, {"id": "b"}],
        "links": [
            {"source": "c", "target": "b", "relation": "imports"},
            {"source": "c", "target": "b", "relation": "calls"},
        ],
    }
    p = tmp_path / "graph.json"
    p.write_text(json.dumps(graph_data))
    out = _run(monkeypatch, capsys, "drop-edge", "c", "b", "--relation", "imports", "--graph", str(p))
    data = json.loads(p.read_text())
    assert data["links"] == [{"source": "c", "target": "b", "relation": "calls"}]
    assert "edges removed: 1" in out


def test_drop_edge_missing_exits_nonzero(monkeypatch, tmp_path):
    p = _write_graph(tmp_path)
    assert _expect_exit(monkeypatch, "drop-edge", "b", "a", "--graph", str(p)) == 1
    # right endpoints, wrong relation -> still no match.
    assert _expect_exit(
        monkeypatch, "drop-edge", "a", "b", "--relation", "imports", "--graph", str(p)
    ) == 1


# --------------------------------------------------------------------------- #
# relabel-edge
# --------------------------------------------------------------------------- #
def test_relabel_edge_changes_relation(monkeypatch, tmp_path, capsys):
    p = _write_graph(tmp_path)
    out = _run(monkeypatch, capsys, "relabel-edge", "a", "b", "invokes", "--graph", str(p))
    data = json.loads(p.read_text())
    edge = next(e for e in data["links"] if e["source"] == "a" and e["target"] == "b")
    assert edge["relation"] == "invokes"
    assert "edges relabeled: 1" in out


def test_relabel_edge_filters_by_old_relation(monkeypatch, tmp_path, capsys):
    graph_data = {
        "directed": True, "multigraph": False,
        "nodes": [{"id": "c"}, {"id": "b"}],
        "links": [
            {"source": "c", "target": "b", "relation": "imports"},
            {"source": "c", "target": "b", "relation": "calls"},
        ],
    }
    p = tmp_path / "graph.json"
    p.write_text(json.dumps(graph_data))
    _run(monkeypatch, capsys, "relabel-edge", "c", "b", "uses",
         "--relation", "imports", "--graph", str(p))
    data = json.loads(p.read_text())
    rels = sorted(e["relation"] for e in data["links"])
    assert rels == ["calls", "uses"]  # only the 'imports' edge changed


def test_relabel_edge_missing_exits_nonzero(monkeypatch, tmp_path):
    p = _write_graph(tmp_path)
    assert _expect_exit(monkeypatch, "relabel-edge", "b", "a", "x", "--graph", str(p)) == 1


# --------------------------------------------------------------------------- #
# arg arity / missing-file CLI guards
# --------------------------------------------------------------------------- #
def test_wrong_arity_exits_usage(monkeypatch, tmp_path):
    p = _write_graph(tmp_path)
    assert _expect_exit(monkeypatch, "rename-node", "a", "--graph", str(p)) == 2
    assert _expect_exit(monkeypatch, "relabel-edge", "a", "b", "--graph", str(p)) == 2


def test_missing_graph_exits_nonzero(monkeypatch, tmp_path):
    missing = tmp_path / "nope.json"
    assert _expect_exit(monkeypatch, "rename-node", "a", "b", "--graph", str(missing)) == 1


# --------------------------------------------------------------------------- #
# pure helpers (legacy 'edges' key, direction-agnostic behavior)
# --------------------------------------------------------------------------- #
def test_rename_helper_edges_key():
    data = {
        "nodes": [{"id": "x"}, {"id": "y"}],
        "edges": [{"source": "x", "target": "y", "relation": "r"}],
    }
    data, rewired = rename_node_in_graph(data, "x", "z")
    assert {n["id"] for n in data["nodes"]} == {"z", "y"}
    assert data["edges"][0]["source"] == "z"
    assert rewired == 1


def test_rename_helper_noop_same_id():
    data = {"nodes": [{"id": "x"}], "links": []}
    _, rewired = rename_node_in_graph(data, "x", "x")
    assert rewired == 0


def test_merge_helper_drops_self_loop():
    data = {
        "nodes": [{"id": "a"}, {"id": "b"}],
        "links": [{"source": "a", "target": "b", "relation": "r"}],
    }
    data, rewired, dropped = merge_nodes_in_graph(data, "a", "b")
    assert {n["id"] for n in data["nodes"]} == {"b"}
    assert data["links"] == []  # a->b became b->b self-loop, dropped
    assert rewired == 1 and dropped == 1


def test_drop_edge_helper_raises_on_missing():
    data = {"nodes": [{"id": "a"}, {"id": "b"}], "links": []}
    with pytest.raises(KeyError):
        drop_edge_in_graph(data, "a", "b")


def test_relabel_edge_helper_raises_on_missing():
    data = {"nodes": [{"id": "a"}, {"id": "b"}], "links": []}
    with pytest.raises(KeyError):
        relabel_edge_in_graph(data, "a", "b", "new")
