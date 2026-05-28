"""Tests for `--no-tests` / `--exclude` node filtering on query and lens."""
from __future__ import annotations

import json

import graphify.__main__ as mainmod


def _write_graph(tmp_path):
    """A graph with one test-file node, one generated node, and two real nodes.

    real_a --calls--> test_helper (in tests/test_real.py)
    real_a --calls--> real_b
    real_b --calls--> gen_node (in build/generated.py)
    """
    data = {
        "directed": True,
        "nodes": [
            {"id": "real_a", "label": "realfunc", "source_file": "real.py", "community": 0},
            {"id": "real_b", "label": "otherfunc", "source_file": "other.py", "community": 0},
            {"id": "test_node", "label": "test_helper", "source_file": "tests/test_real.py", "community": 0},
            {"id": "gen_node", "label": "genfunc", "source_file": "build/generated.py", "community": 0},
        ],
        "links": [
            {"source": "real_a", "target": "test_node", "relation": "calls",
             "confidence": "EXTRACTED", "context": "call"},
            {"source": "real_a", "target": "real_b", "relation": "calls",
             "confidence": "EXTRACTED", "context": "call"},
            {"source": "real_b", "target": "gen_node", "relation": "calls",
             "confidence": "EXTRACTED", "context": "call"},
        ],
    }
    graph_path = tmp_path / "graph.json"
    graph_path.write_text(json.dumps(data))
    return graph_path


def _run(monkeypatch, capsys, argv):
    monkeypatch.setattr(mainmod, "_check_skill_version", lambda _: None)
    monkeypatch.setattr(mainmod.sys, "argv", argv)
    mainmod.main()
    return capsys.readouterr().out


def test_query_no_tests_drops_test_node(monkeypatch, tmp_path, capsys):
    graph_path = _write_graph(tmp_path)
    out = _run(
        monkeypatch, capsys,
        ["graphify", "query", "realfunc", "--no-tests", "--graph", str(graph_path)],
    )
    assert "realfunc" in out
    assert "otherfunc" in out
    assert "test_helper" not in out


def test_query_without_no_tests_keeps_test_node(monkeypatch, tmp_path, capsys):
    graph_path = _write_graph(tmp_path)
    out = _run(
        monkeypatch, capsys,
        ["graphify", "query", "realfunc", "--graph", str(graph_path)],
    )
    assert "test_helper" in out


def test_query_exclude_substring_drops_node(monkeypatch, tmp_path, capsys):
    graph_path = _write_graph(tmp_path)
    out = _run(
        monkeypatch, capsys,
        ["graphify", "query", "realfunc", "--exclude", "build/", "--graph", str(graph_path)],
    )
    assert "realfunc" in out
    assert "genfunc" not in out


def test_query_exclude_glob_drops_node(monkeypatch, tmp_path, capsys):
    graph_path = _write_graph(tmp_path)
    out = _run(
        monkeypatch, capsys,
        ["graphify", "query", "realfunc", "--exclude", "build/*.py", "--graph", str(graph_path)],
    )
    assert "genfunc" not in out


def test_query_exclude_repeatable(monkeypatch, tmp_path, capsys):
    graph_path = _write_graph(tmp_path)
    out = _run(
        monkeypatch, capsys,
        ["graphify", "query", "realfunc",
         "--exclude", "build/", "--exclude", "tests/", "--graph", str(graph_path)],
    )
    assert "realfunc" in out
    assert "genfunc" not in out
    assert "test_helper" not in out


def test_lens_no_tests_and_exclude(monkeypatch, tmp_path, capsys):
    graph_path = _write_graph(tmp_path)
    out = _run(
        monkeypatch, capsys,
        ["graphify", "lens", str(graph_path), "realfunc",
         "--no-tests", "--exclude", "build/", "--budget", "300"],
    )
    assert "realfunc" in out
    assert "test_helper" not in out
    assert "genfunc" not in out


def test_node_source_file_excluded_test_markers():
    """The documented marker set classifies these source_files as test code."""
    for src in (
        "test_foo.py",
        "foo_test.py",
        "src/tests/foo.py",
        "foo.test.js",
        "foo.spec.ts",
        "foo_spec.rb",
        "src/spec/foo.rb",
        "src/__tests__/foo.js",
    ):
        assert mainmod._node_source_file_excluded(src, no_tests=True, exclude_patterns=[]), src


def test_node_source_file_not_excluded_for_real_files():
    for src in ("service.py", "src/main/handler.go", "lib/contest.py"):
        assert not mainmod._node_source_file_excluded(src, no_tests=True, exclude_patterns=[]), src
