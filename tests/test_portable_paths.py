"""Portable-path regression tests for a git-shared graphify-out/ (#777/#722/#1012/#467).

When graphify-out/ is committed and shared across machines/clones, absolute
paths baked into manifest keys, cache source_file fields, and .graphify_root
break the incremental cache and churn the tracked files on every machine. These
tests pin the portable (relative) behavior and the single-canonical-cache-dir
invariant for subpath scans.
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

from graphify.cache import cache_dir, file_hash, load_cached, save_cached
from graphify.detect import detect, detect_incremental, save_manifest


def _read_cache_entry(f: Path, root: Path, kind: str = "ast") -> dict:
    """Read the raw on-disk cache entry (relative source_file, as stored)."""
    h = file_hash(f, root)
    entry = (root / "graphify-out" / "cache" / kind / f"{h}.json")
    return json.loads(entry.read_text())


def _make_repo(base: Path) -> None:
    (base / ".git").mkdir()
    (base / "src").mkdir()
    (base / "src" / "a.py").write_text("def foo():\n    return 1\n")
    (base / "b.py").write_text("import os\n\n\ndef bar():\n    return os.getpid()\n")


# ── #777: manifest keys are relative when written under graphify-out/ ─────────

def test_manifest_keys_relative_under_out_dir(tmp_path):
    """save_manifest stores keys relative to the scan root so the manifest is
    portable; a bare manifest path keeps absolute keys (legacy behavior)."""
    _make_repo(tmp_path)
    detected = detect(tmp_path)
    manifest_path = tmp_path / "graphify-out" / "manifest.json"
    save_manifest(detected["files"], manifest_path=str(manifest_path), kind="both")

    manifest = json.loads(manifest_path.read_text())
    keys = set(manifest)
    assert keys == {"b.py", "src/a.py"}, keys
    for k in keys:
        assert not Path(k).is_absolute()


def test_manifest_bare_path_keeps_absolute_keys(tmp_path):
    """A manifest written to a bare path (no graphify-out/ parent) cannot infer
    a root, so keys stay absolute — preserves the documented legacy contract."""
    py = tmp_path / "main.py"
    py.write_text("print('hello')\n")
    manifest_path = tmp_path / "manifest.json"
    save_manifest({"code": [str(py)]}, manifest_path=str(manifest_path), kind="ast")

    manifest = json.loads(manifest_path.read_text())
    assert str(py) in manifest


def test_manifest_explicit_root_relativizes(tmp_path):
    """An explicit root relativizes keys even for a bare manifest path."""
    _make_repo(tmp_path)
    detected = detect(tmp_path)
    manifest_path = tmp_path / "manifest.json"
    save_manifest(detected["files"], manifest_path=str(manifest_path), kind="both", root=tmp_path)

    manifest = json.loads(manifest_path.read_text())
    assert set(manifest) == {"b.py", "src/a.py"}


# ── #777: incremental cache survives a directory move (different abs prefix) ───

def test_incremental_cache_hit_after_directory_move(tmp_path):
    """After moving the repo to a different absolute prefix, an incremental scan
    sees zero changes (cache hit) instead of re-extracting everything."""
    src_repo = tmp_path / "orig"
    src_repo.mkdir()
    _make_repo(src_repo)
    manifest_path = src_repo / "graphify-out" / "manifest.json"
    detected = detect(src_repo)
    save_manifest(detected["files"], manifest_path=str(manifest_path), kind="ast")

    # Move to a wholly different absolute path.
    moved = tmp_path / "elsewhere" / "moved"
    moved.parent.mkdir()
    shutil.move(str(src_repo), str(moved))

    moved_manifest = moved / "graphify-out" / "manifest.json"
    result = detect_incremental(moved, manifest_path=str(moved_manifest), kind="ast")
    new_total = sum(len(v) for v in result["new_files"].values())
    assert new_total == 0, result["new_files"]
    assert result["deleted_files"] == []


def test_legacy_absolute_manifest_still_loads(tmp_path):
    """An existing manifest with absolute keys (pre-fix) must still resolve on
    read — the loader re-anchors so old graphify-out/ dirs are not invalidated."""
    _make_repo(tmp_path)
    detected = detect(tmp_path)
    manifest_path = tmp_path / "graphify-out" / "manifest.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)

    # Hand-craft a legacy absolute-keyed manifest.
    legacy = {}
    for flist in detected["files"].values():
        for f in flist:
            legacy[f] = {"mtime": Path(f).stat().st_mtime, "ast_hash": "x", "semantic_hash": ""}
    # Use a real ast_hash so the unchanged fast-path triggers.
    from graphify.detect import _md5_file
    for f in list(legacy):
        legacy[f]["ast_hash"] = _md5_file(Path(f))
    manifest_path.write_text(json.dumps(legacy))

    result = detect_incremental(tmp_path, manifest_path=str(manifest_path), kind="ast")
    new_total = sum(len(v) for v in result["new_files"].values())
    assert new_total == 0, "legacy absolute manifest must re-anchor to a cache hit"
    assert result["deleted_files"] == []


# ── #777: cache source_file is relative ───────────────────────────────────────

def test_cache_source_file_stored_relative(tmp_path):
    """save_cached writes relative source_file to disk (portable), and does not
    mutate the caller's dict. load_cached re-anchors to absolute on read so the
    in-process pipeline keeps its str(path) invariant."""
    _make_repo(tmp_path)
    f = tmp_path / "src" / "a.py"
    result = {
        "nodes": [{"id": "x", "source_file": str(f)}],
        "edges": [{"source": "x", "target": "y", "source_file": str(f)}],
    }
    save_cached(f, result, root=tmp_path, kind="ast")

    assert result["nodes"][0]["source_file"] == str(f)  # caller untouched

    on_disk = _read_cache_entry(f, tmp_path)
    assert on_disk["nodes"][0]["source_file"] == "src/a.py"
    assert on_disk["edges"][0]["source_file"] == "src/a.py"

    # Read re-anchors back to absolute for the extraction pipeline, matching the
    # str(path) form fresh extraction produces.
    loaded = load_cached(f, root=tmp_path, kind="ast")
    assert loaded["nodes"][0]["source_file"] == str(tmp_path / "src" / "a.py")


def test_legacy_absolute_cache_entry_still_loads(tmp_path):
    """A pre-fix cache entry with an absolute source_file loads unchanged — the
    read-side re-anchoring leaves already-absolute paths alone."""
    _make_repo(tmp_path)
    f = tmp_path / "src" / "a.py"
    # Write a raw absolute-source_file entry directly, bypassing relativization.
    h = file_hash(f, tmp_path)
    entry = cache_dir(tmp_path, "ast") / f"{h}.json"
    entry.write_text(json.dumps({"nodes": [{"id": "x", "source_file": str(f)}], "edges": []}))
    loaded = load_cached(f, root=tmp_path, kind="ast")
    assert loaded["nodes"][0]["source_file"] == str(f)


# ── #1012/#467: a subpath scan does not spawn a second cache dir ──────────────

def test_subpath_scan_uses_single_canonical_cache_dir(tmp_path):
    """When graphify-out/ already exists at the repo root, a deeper inferred
    root (e.g. src/) must reuse the root cache, not create src/graphify-out/."""
    _make_repo(tmp_path)
    (tmp_path / "graphify-out").mkdir(exist_ok=True)

    d = cache_dir(tmp_path / "src", kind="ast")
    assert d.resolve() == (tmp_path / "graphify-out" / "cache" / "ast").resolve()

    assert not (tmp_path / "src" / "graphify-out").exists()
    # Exactly one graphify-out/cache tree across the whole repo.
    assert len(list(tmp_path.rglob("graphify-out/cache"))) == 1


def test_standalone_subdir_scan_keeps_local_cache(tmp_path):
    """With no canonical graphify-out/ anywhere, a subdir scan keeps its cache
    local (does not escape past the VCS root to adopt an unrelated parent)."""
    _make_repo(tmp_path)  # .git at tmp_path, no graphify-out yet
    d = cache_dir(tmp_path / "src", kind="ast")
    assert d.resolve() == (tmp_path / "src" / "graphify-out" / "cache" / "ast").resolve()


# ── #777: graphify-out/.gitignore is written and excludes transient artifacts ─

def test_out_gitignore_written_and_excludes_transient(tmp_path):
    """cache_dir drops a .gitignore that ignores machine-local/transient files
    but keeps the portable graph + cache entries tracked."""
    _make_repo(tmp_path)
    cache_dir(tmp_path, kind="ast")
    gi = tmp_path / "graphify-out" / ".gitignore"
    assert gi.exists()
    text = gi.read_text()
    for transient in ("cache/stat-index.json", ".rebuild.lock", "needs_update", "*.tmp"):
        assert transient in text
    # Portable artifacts must NOT be ignored.
    for portable in ("graph.json", "manifest.json"):
        assert f"\n{portable}\n" not in f"\n{text}\n"
