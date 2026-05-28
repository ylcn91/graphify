"""Tests for Markdown link edges (#951).

An inline/reference-style Markdown link to another file in the corpus becomes a
``file --references--> <target file node id>`` edge, using the canonical node-ID
scheme so the edge survives ``build_from_json``. Links inside fenced code blocks
and external URLs (http/https/mailto) never produce file edges.
"""
from __future__ import annotations

from pathlib import Path

from graphify.build import build_from_json
from graphify.extract import canonical_file_id, extract, extract_markdown


def _make_corpus(tmp_path: Path) -> tuple[Path, Path, Path]:
    """docs/a.md links to ./b.md and ../src/x.py; b.md and src/x.py exist."""
    (tmp_path / "docs").mkdir()
    (tmp_path / "src").mkdir()
    a = tmp_path / "docs" / "a.md"
    b = tmp_path / "docs" / "b.md"
    x = tmp_path / "src" / "x.py"
    a.write_text(
        "# Doc A\n"
        "\n"
        "[see b](./b.md) and [code](../src/x.py).\n"
        "\n"
        "```md\n"
        "[ignored](./b.md)\n"
        "```\n"
        "\n"
        "[external](https://example.com) and [mail](mailto:x@y.com).\n",
        encoding="utf-8",
    )
    b.write_text("# Doc B\n", encoding="utf-8")
    x.write_text("def foo():\n    pass\n", encoding="utf-8")
    return a, b, x


def _ref_targets(result: dict) -> list[str]:
    return [e["target"] for e in result["edges"] if e["relation"] == "references"]


def test_internal_md_to_md_link_edge(tmp_path):
    """[see b](./b.md) emits a references edge to b.md's canonical node id."""
    a, b, x = _make_corpus(tmp_path)
    res = extract([a, b, x], cache_root=tmp_path, parallel=False)
    a_id = canonical_file_id(a, tmp_path.resolve())
    b_id = canonical_file_id(b, tmp_path.resolve())
    refs = [e for e in res["edges"] if e["relation"] == "references" and e["source"] == a_id]
    assert any(e["target"] == b_id for e in refs), f"{[e['target'] for e in refs]}"


def test_internal_md_to_py_link_edge(tmp_path):
    """[code](../src/x.py) emits a references edge to x.py's canonical node id."""
    a, b, x = _make_corpus(tmp_path)
    res = extract([a, b, x], cache_root=tmp_path, parallel=False)
    a_id = canonical_file_id(a, tmp_path.resolve())
    x_id = canonical_file_id(x, tmp_path.resolve())
    refs = [e for e in res["edges"] if e["relation"] == "references" and e["source"] == a_id]
    assert any(e["target"] == x_id for e in refs), f"{[e['target'] for e in refs]}"


def test_external_link_produces_no_file_edge(tmp_path):
    """https:// and mailto: links never become file edges."""
    a, _, _ = _make_corpus(tmp_path)
    r = extract_markdown(a)
    for tgt in _ref_targets(r):
        assert "example" not in tgt and "mailto" not in tgt, tgt


def test_link_inside_code_block_is_ignored(tmp_path):
    """A link inside a fenced code block emits exactly one b.md edge (line 3),
    not two — the duplicate ./b.md link on line 6 lives in a ```md block."""
    a, b, _ = _make_corpus(tmp_path)
    r = extract_markdown(a)
    b_edges = [e for e in r["edges"] if e["relation"] == "references" and e["target"].endswith("_b")]
    assert len(b_edges) == 1, f"expected 1 b.md edge, got {b_edges}"
    assert b_edges[0]["source_location"] == "L3"


def test_reference_style_link_edge(tmp_path):
    """[text][ref] + [ref]: target (definition below the use) resolves."""
    (tmp_path / "docs").mkdir()
    a = tmp_path / "docs" / "a.md"
    b = tmp_path / "docs" / "b.md"
    a.write_text(
        "# A\n\nSee [the guide][g].\n\n[g]: ./b.md\n",
        encoding="utf-8",
    )
    b.write_text("# B\n", encoding="utf-8")
    res = extract([a, b], cache_root=tmp_path, parallel=False)
    a_id = canonical_file_id(a, tmp_path.resolve())
    b_id = canonical_file_id(b, tmp_path.resolve())
    assert any(
        e["source"] == a_id and e["target"] == b_id and e["relation"] == "references"
        for e in res["edges"]
    )


def test_anchor_is_stripped(tmp_path):
    """[x](./b.md#section) resolves to b.md, not a dangling anchor target."""
    (tmp_path / "docs").mkdir()
    a = tmp_path / "docs" / "a.md"
    b = tmp_path / "docs" / "b.md"
    a.write_text("# A\n\n[x](./b.md#section)\n", encoding="utf-8")
    b.write_text("# B\n", encoding="utf-8")
    res = extract([a, b], cache_root=tmp_path, parallel=False)
    b_id = canonical_file_id(b, tmp_path.resolve())
    assert b_id in _ref_targets(res)


def test_link_to_file_outside_corpus_is_dropped_by_build(tmp_path):
    """A link to a missing file produces no resolvable edge (no file on disk)."""
    (tmp_path / "docs").mkdir()
    a = tmp_path / "docs" / "a.md"
    a.write_text("# A\n\n[gone](./missing.md)\n", encoding="utf-8")
    r = extract_markdown(a)
    assert _ref_targets(r) == []


def test_internal_link_survives_build_from_json(tmp_path):
    """After build_from_json over the corpus, the internal link edge is present."""
    a, b, x = _make_corpus(tmp_path)
    res = extract([a, b, x], cache_root=tmp_path, parallel=False)
    a_id = canonical_file_id(a, tmp_path.resolve())
    b_id = canonical_file_id(b, tmp_path.resolve())
    x_id = canonical_file_id(x, tmp_path.resolve())
    g = build_from_json(res)
    assert g.has_edge(a_id, b_id), "a.md -> b.md dropped as dangling"
    assert g.has_edge(a_id, x_id), "a.md -> x.py dropped as dangling"
