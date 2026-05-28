"""Regression tests for #949: same-basename modules must resolve by package prefix.

`from pkg_a.settings import Settings` must resolve to ``pkg_a``'s settings module,
never ``pkg_b``'s — and the choice must be deterministic regardless of the order in
which colliding files are processed (the bug was OS-nondeterministic because
``os.walk`` yields entries in arbitrary filesystem order and a first-writer-wins
bare-name map then picked whichever same-basename file came first).
"""

from __future__ import annotations

import ast
from pathlib import Path

from graphify.extract import _file_stem, _resolve_cross_file_imports, extract
from graphify.symbol_resolution import (
    build_python_symbol_index,
    find_unique_python_symbol,
    parse_python_import_aliases,
)


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _node_id(result: dict, label: str, source_file: str) -> str:
    matches = [
        node["id"]
        for node in result["nodes"]
        if node.get("label") == label and node.get("source_file") == source_file
    ]
    assert len(matches) == 1, f"expected one {label!r} in {source_file}, got {matches}"
    return matches[0]


def _edge_targets(result: dict, source: str, relation: str) -> set[str]:
    return {
        edge["target"]
        for edge in result["edges"]
        if edge["source"] == source and edge["relation"] == relation
    }


def _make_collision_fixture(tmp_path: Path) -> tuple[Path, Path, Path]:
    a = _write(tmp_path / "pkg_a" / "settings.py", "class Settings:\n    pass\n")
    b = _write(tmp_path / "pkg_b" / "settings.py", "class Settings:\n    pass\n")
    app = _write(
        tmp_path / "app.py",
        "from pkg_a.settings import Settings\n\n"
        "class Consumer:\n"
        "    def get(self):\n"
        "        return Settings()\n",
    )
    return a, b, app


def test_package_prefix_resolves_to_named_module_not_same_basename(tmp_path: Path):
    a, b, app = _make_collision_fixture(tmp_path)

    result = extract([a, b, app], cache_root=tmp_path)

    consumer = _node_id(result, "Consumer", "app.py")
    pkg_a_settings = _node_id(result, "Settings", "pkg_a/settings.py")
    pkg_b_settings = _node_id(result, "Settings", "pkg_b/settings.py")
    app_file = _node_id(result, "app.py", "app.py")

    # The import names pkg_a.settings, so every resolved edge must land on
    # pkg_a's Settings node and never on pkg_b's same-basename node.
    assert pkg_a_settings in _edge_targets(result, app_file, "imports")
    assert pkg_b_settings not in _edge_targets(result, app_file, "imports")

    assert pkg_a_settings in _edge_targets(result, consumer, "uses")
    assert pkg_b_settings not in _edge_targets(result, consumer, "uses")


def test_resolution_is_order_independent(tmp_path: Path):
    a, b, app = _make_collision_fixture(tmp_path)

    def consumer_use_target(paths: list[Path]) -> str:
        result = extract(paths, cache_root=tmp_path)
        consumer = _node_id(result, "Consumer", "app.py")
        targets = _edge_targets(result, consumer, "uses")
        assert len(targets) == 1, targets
        return next(iter(targets))

    pkg_a_settings = _node_id(
        extract([a, b, app], cache_root=tmp_path), "Settings", "pkg_a/settings.py"
    )

    # Feeding the colliding files in both orders must yield the identical target.
    assert consumer_use_target([a, b, app]) == pkg_a_settings
    assert consumer_use_target([b, a, app]) == pkg_a_settings


def _fake_per_file(path: Path) -> dict:
    """Minimal extraction fragment: file node + each top-level class node."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    stem = _file_stem(path)
    nodes = [
        {"id": f"{stem}::file", "label": path.name, "source_file": str(path), "file_type": "code"}
    ]
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            nodes.append(
                {
                    "id": f"{stem}::{node.name}",
                    "label": node.name,
                    "source_file": str(path),
                    "file_type": "code",
                }
            )
    return {"nodes": nodes, "edges": []}


def test_cross_file_imports_disambiguate_by_prefix_both_orders(tmp_path: Path):
    a, b, app = _make_collision_fixture(tmp_path)

    def use_targets(paths: list[Path]) -> set[str]:
        per_file = [_fake_per_file(p) for p in paths]
        edges = _resolve_cross_file_imports(per_file, paths)
        return {e["target"] for e in edges if e["relation"] == "uses"}

    expected = {f"{_file_stem(a)}::Settings"}
    assert use_targets([a, b, app]) == expected
    assert use_targets([b, a, app]) == expected


def test_import_guided_resolver_picks_prefixed_module(tmp_path: Path):
    a, b, app = _make_collision_fixture(tmp_path)

    nodes = [
        {"id": "a_settings", "label": "Settings", "source_file": str(a), "file_type": "code"},
        {"id": "b_settings", "label": "Settings", "source_file": str(b), "file_type": "code"},
    ]
    index = build_python_symbol_index(nodes)
    imported = parse_python_import_aliases(app)["Settings"]

    assert imported.module_qualified == "pkg_a.settings"
    assert find_unique_python_symbol(index, imported) == "a_settings"


def test_single_component_import_still_resolves(tmp_path: Path):
    """A non-colliding bare `from settings import X` must keep resolving."""
    settings = _write(tmp_path / "settings.py", "class Settings:\n    pass\n")
    consumer = _write(tmp_path / "consumer.py", "from settings import Settings\n")

    nodes = [
        {"id": "settings", "label": "Settings", "source_file": str(settings), "file_type": "code"},
    ]
    index = build_python_symbol_index(nodes)
    imported = parse_python_import_aliases(consumer)["Settings"]

    assert imported.module_qualified == ""
    assert find_unique_python_symbol(index, imported) == "settings"
