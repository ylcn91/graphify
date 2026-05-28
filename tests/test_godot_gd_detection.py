"""Regression tests for Godot GDScript (.gd) detection and extraction (#535).

`.gd` files were not wired into CODE_EXTENSIONS or the extract dispatcher, so
they were silently classified as None (not code) and never extracted. These
tests pin both the detection and the regex-based extractor.
"""
from __future__ import annotations
from pathlib import Path

FIXTURES = Path(__file__).parent / "fixtures"


def _labels(r):
    return [n["label"] for n in r["nodes"]]


def _relations(r):
    return {e["relation"] for e in r["edges"]}


def test_classify_gd_is_code():
    from graphify.detect import classify_file, FileType
    assert classify_file(Path("player.gd")) == FileType.CODE


def test_gd_has_dispatch_extractor():
    from graphify.extract import _get_extractor, extract_gdscript
    assert _get_extractor(Path("player.gd")) is extract_gdscript


def test_gd_extraction_no_error():
    from graphify.extract import extract_gdscript
    r = extract_gdscript(FIXTURES / "sample.gd")
    assert "error" not in r


def test_gd_extraction_yields_nodes():
    from graphify.extract import extract_gdscript
    r = extract_gdscript(FIXTURES / "sample.gd")
    assert len(r["nodes"]) > 1


def test_gd_finds_class_name():
    from graphify.extract import extract_gdscript
    r = extract_gdscript(FIXTURES / "sample.gd")
    assert any("Player" in l for l in _labels(r))


def test_gd_finds_funcs():
    from graphify.extract import extract_gdscript
    r = extract_gdscript(FIXTURES / "sample.gd")
    labels = _labels(r)
    for fn in ("_ready()", "reset()", "take_damage()", "die()"):
        assert fn in labels


def test_gd_finds_inherits():
    from graphify.extract import extract_gdscript
    r = extract_gdscript(FIXTURES / "sample.gd")
    assert "inherits" in _relations(r)
    assert any(l == "CharacterBody2D" for l in _labels(r))


def test_gd_finds_members():
    from graphify.extract import extract_gdscript
    r = extract_gdscript(FIXTURES / "sample.gd")
    labels = _labels(r)
    for member in ("health", "speed", "jump_force", "died"):
        assert member in labels


def test_gd_finds_inner_class():
    from graphify.extract import extract_gdscript
    r = extract_gdscript(FIXTURES / "sample.gd")
    assert "Inventory" in _labels(r)


def test_gd_intra_file_calls():
    from graphify.extract import extract_gdscript
    r = extract_gdscript(FIXTURES / "sample.gd")
    calls = {(e["source"], e["target"]) for e in r["edges"] if e["relation"] == "calls"}
    nid = {n["label"]: n["id"] for n in r["nodes"]}
    assert (nid["take_damage()"], nid["die()"]) in calls


def test_detect_classifies_gd_fixture():
    from graphify.detect import detect
    result = detect(FIXTURES)
    assert any(f.endswith("sample.gd") for f in result["files"]["code"])
