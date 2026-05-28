"""Tests for the `graphify lens` one-shot build+ask subcommand."""
from __future__ import annotations

import json

import graphify.__main__ as mainmod


def _write_directed_graph(tmp_path):
    data = {
        "directed": True,
        "nodes": [
            {"id": "caller", "label": "alphafunc", "source_file": "a.py", "community": 0},
            {"id": "callee", "label": "betafunc", "source_file": "b.py", "community": 0},
        ],
        "links": [
            {"source": "caller", "target": "callee", "relation": "calls",
             "confidence": "EXTRACTED", "context": "call"},
        ],
    }
    graph_path = tmp_path / "graph.json"
    graph_path.write_text(json.dumps(data))
    return graph_path


def test_lens_direct_json_renders_true_direction(monkeypatch, tmp_path, capsys):
    """lens given a graph.json directly skips the build and answers, with the
    arrow pointing caller→callee even though traversal starts from the callee."""
    graph_path = _write_directed_graph(tmp_path)
    monkeypatch.setattr(mainmod, "_check_skill_version", lambda _: None)
    monkeypatch.setattr(
        mainmod.sys, "argv",
        ["graphify", "lens", str(graph_path), "betafunc", "--budget", "300"],
    )
    mainmod.main()
    out = capsys.readouterr().out
    assert "alphafunc --calls" in out
    assert "--> betafunc" in out
    assert "betafunc --calls" not in out


def test_lens_builds_code_only_when_graph_missing(monkeypatch, tmp_path, capsys):
    """lens builds an AST-only graph (no LLM) for a repo path that has none yet,
    then answers — and the cross-file call edge keeps its real direction."""
    (tmp_path / "a.py").write_text("from b import helper\n\n\ndef main():\n    return helper()\n")
    (tmp_path / "b.py").write_text("def helper():\n    return 42\n")
    monkeypatch.setattr(mainmod, "_check_skill_version", lambda _: None)
    monkeypatch.setattr(
        mainmod.sys, "argv",
        ["graphify", "lens", str(tmp_path), "helper", "--budget", "300"],
    )
    mainmod.main()
    out = capsys.readouterr().out
    assert (tmp_path / "graphify-out" / "graph.json").exists()
    assert "helper" in out
    assert "main() --calls" in out
    assert "--> helper()" in out
