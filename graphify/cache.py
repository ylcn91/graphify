# per-file extraction cache - skip unchanged files on re-run
from __future__ import annotations

import atexit
import hashlib
import json
import os
import tempfile
from pathlib import Path

# Output directory name — override with GRAPHIFY_OUT env var for worktrees or
# shared-output setups. Accepts a relative name ("graphify-out-feature") or an
# absolute path ("/shared/graphify-out").
_GRAPHIFY_OUT = os.environ.get("GRAPHIFY_OUT", "graphify-out")


def _body_content(content: bytes) -> bytes:
    """Strip YAML frontmatter from Markdown content, returning only the body."""
    text = content.decode(errors="replace")
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            return text[end + 4:].encode()
    return content


# Stat-based index: maps absolute path → {size, mtime_ns, hash}.
# Loaded once per process, flushed via atexit. Skips full file reads when
# size+mtime_ns are unchanged — same trade-off as make(1).
# Correctness risks: `touch` causes a harmless extra re-hash; same-size edits
# within NFS second-resolution mtime have a 1-second window (same as make).
# Use `graphify extract --force` to bypass when needed.
_stat_index: dict[str, dict] = {}
_stat_index_root: Path | None = None
_stat_index_dirty: bool = False


# VCS markers used to bound the upward search for a canonical output dir.
_VCS_MARKERS = (".git", ".hg", ".svn", "_darcs", ".fossil")


def _out_base(root: Path) -> Path:
    """Resolve the single canonical graphify-out/ directory for ``root``.

    A subpath scan infers a deeper ``root`` (e.g. the common prefix ``src/`` of
    the scanned files) than the project root that already owns ``graphify-out/``.
    Anchoring the cache blindly at that inferred root spawns a SECOND cache dir
    under the subdirectory while graph.json/manifest live at the project root
    (#1012/#467). To keep one canonical location, walk upward from ``root`` and
    reuse the nearest existing ``<ancestor>/graphify-out`` if one is found;
    otherwise fall back to ``root/graphify-out``.

    An absolute GRAPHIFY_OUT is already a single shared location — return it
    unchanged. The walk stops at the VCS root / home / filesystem root so an
    unrelated parent project's output dir is never adopted.
    """
    _out = Path(_GRAPHIFY_OUT)
    if _out.is_absolute():
        return _out
    start = Path(root).resolve()
    home = Path.home()
    current = start
    while True:
        candidate = current / _out
        if current is not start and candidate.is_dir():
            return candidate
        if any((current / m).exists() for m in _VCS_MARKERS):
            break
        parent = current.parent
        if parent == current or current == home:
            break
        current = parent
    return start / _out


# Written into a freshly created graphify-out/ so a committed/shared graph
# excludes transient + machine-local artifacts while keeping the portable graph,
# report, manifest, and (now relative) cache entries tracked (#777).
_OUT_GITIGNORE = """\
# Transient and machine-local graphify artifacts.
# The portable graph (graph.json, GRAPH_REPORT.md, manifest.json, labels,
# and the now-relative cache/ast + cache/semantic entries) stays tracked so a
# committed graphify-out/ is shareable across machines and clones.

# Watch/rebuild coordination — PIDs and queues are local to one machine.
.rebuild.lock
.rebuild.pending
.rebuild.pending.draining

# Transient flags and atomic-write temp files.
needs_update
.graph.tmp.json
*.tmp

# Stat fastpath index — keyed by absolute paths + mtime_ns, never portable.
cache/stat-index.json
cache/**/*.tmp

# Dated pre-overwrite snapshots — local backup history, not shared state.
20[0-9][0-9]-[0-9][0-9]-[0-9][0-9]/
"""


def ensure_out_gitignore(root: Path) -> None:
    """Drop a .gitignore into the canonical graphify-out/ if absent.

    Idempotent and best-effort: never raises so a read-only or already-tracked
    output dir can't break extraction. An absolute GRAPHIFY_OUT (shared, often
    out-of-tree) is left untouched — the user manages its VCS state.
    """
    if os.path.isabs(_GRAPHIFY_OUT):
        return
    base = _out_base(root)
    gi = base / ".gitignore"
    if gi.exists():
        return
    try:
        base.mkdir(parents=True, exist_ok=True)
        gi.write_text(_OUT_GITIGNORE, encoding="utf-8")
    except OSError:
        pass


def _stat_index_file(root: Path) -> Path:
    return _out_base(root) / "cache" / "stat-index.json"


def _ensure_stat_index(root: Path) -> None:
    global _stat_index, _stat_index_root, _stat_index_dirty
    if _stat_index_root is not None:
        return
    _stat_index_root = Path(root).resolve()
    p = _stat_index_file(_stat_index_root)
    if p.exists():
        try:
            _stat_index = json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            _stat_index = {}
    else:
        _stat_index = {}
    atexit.register(_flush_stat_index)


def _flush_stat_index() -> None:
    global _stat_index_dirty, _stat_index_root
    if not _stat_index_dirty or _stat_index_root is None:
        return
    p = _stat_index_file(_stat_index_root)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=p.parent, prefix="stat-index.", suffix=".tmp")
        try:
            os.write(fd, json.dumps(_stat_index, separators=(",", ":")).encode())
            os.close(fd)
            os.replace(tmp, p)
        except Exception:
            try:
                os.close(fd)
            except OSError:
                pass
            try:
                os.unlink(tmp)
            except OSError:
                pass
    except OSError:
        pass
    _stat_index_dirty = False


def _normalize_path(path: Path) -> Path:
    """Normalize path for consistent cache keys across Windows path spellings."""
    import sys
    if sys.platform != "win32":
        return path
    s = str(path)
    if s.startswith("\\\\?\\"):
        s = s[4:]  # strip extended-length prefix \\?\
    return Path(os.path.normcase(s))


def file_hash(path: Path, root: Path = Path(".")) -> str:
    """SHA256 of file contents + path relative to root.

    Uses a stat-based fastpath (size + mtime_ns) to skip full reads when the
    file hasn't changed. Falls through to full SHA256 on first encounter or
    when stat changes. Index is flushed atomically at process exit.

    Using a relative path (not absolute) makes cache entries portable across
    machines and checkout directories, so shared caches and CI work correctly.
    Falls back to the resolved absolute path if the file is outside root.

    For Markdown files (.md), only the body below the YAML frontmatter is hashed,
    so metadata-only changes (e.g. reviewed, status, tags) do not invalidate the cache.
    """
    global _stat_index_dirty
    p = _normalize_path(Path(path))
    root = _normalize_path(Path(root))
    if not p.is_file():
        raise IsADirectoryError(f"file_hash requires a file, got: {p}")

    _ensure_stat_index(root)
    abs_key = str(p.resolve())
    st: "os.stat_result | None" = None
    try:
        st = p.stat()
        entry = _stat_index.get(abs_key)
        if (entry
                and entry.get("size") == st.st_size
                and entry.get("mtime_ns") == st.st_mtime_ns):
            return entry["hash"]
    except OSError:
        pass

    raw = p.read_bytes()
    content = _body_content(raw) if p.suffix.lower() == ".md" else raw
    h = hashlib.sha256()
    h.update(content)
    h.update(b"\x00")
    try:
        rel = p.resolve().relative_to(Path(root).resolve())
        h.update(rel.as_posix().lower().encode())
    except ValueError:
        h.update(p.resolve().as_posix().lower().encode())
    digest = h.hexdigest()

    if st is not None:
        _stat_index[abs_key] = {"size": st.st_size, "mtime_ns": st.st_mtime_ns, "hash": digest}
        _stat_index_dirty = True

    return digest


def cache_dir(root: Path = Path("."), kind: str = "ast") -> Path:
    """Returns graphify-out/cache/{kind}/ - creates it if needed.

    kind is "ast" or "semantic". Separate subdirectories prevent semantic cache
    entries from overwriting AST cache entries for the same source_file (#582).
    """
    d = _out_base(root) / "cache" / kind
    d.mkdir(parents=True, exist_ok=True)
    ensure_out_gitignore(root)
    return d


def load_cached(path: Path, root: Path = Path("."), kind: str = "ast") -> dict | None:
    """Return cached extraction for this file if hash matches, else None.

    Cache key: SHA256 of file contents.
    Cache value: stored as graphify-out/cache/{kind}/{hash}.json

    For kind="ast", also checks the legacy flat cache/  directory so users
    upgrading from pre-0.5.3 don't lose their existing AST cache entries.
    Returns None if no cache entry or file has changed.
    """
    try:
        h = file_hash(path, root)
    except OSError:
        return None
    entry = cache_dir(root, kind) / f"{h}.json"
    if entry.exists():
        try:
            return _absolutize_source_files(json.loads(entry.read_text(encoding="utf-8")), root)
        except (json.JSONDecodeError, OSError):
            return None
    # Migration fallback: check legacy flat cache/ dir for AST entries
    if kind == "ast":
        legacy = _out_base(root) / "cache" / f"{h}.json"
        if legacy.exists():
            try:
                return _absolutize_source_files(json.loads(legacy.read_text(encoding="utf-8")), root)
            except (json.JSONDecodeError, OSError):
                return None
    return None


def _absolutize_source_files(result: dict, root: Path) -> dict:
    """Inverse of :func:`_relativize_source_files`: re-anchor relative
    ``source_file`` fields back to absolute against ``root`` on read.

    Cache entries are stored relative for portability (#777), but the in-process
    extraction pipeline (cross-file import/call resolution) matches per-file
    nodes against ``str(path)`` (absolute). Re-anchoring here makes a cache hit
    indistinguishable from a fresh extraction, while existing absolute caches
    (already absolute) pass through unchanged so they still load. The on-disk
    file is untouched; only the returned dict is rewritten.
    """
    if not isinstance(result, dict):
        return result
    # Re-anchor against root AS PASSED (not resolved): fresh extraction records
    # source_file as str(path) using the caller's unresolved paths, so joining
    # the relative tail onto the same unresolved root reproduces that exact
    # string. Resolving here would diverge on /var → /private/var style symlinks
    # and silently break the per-file source_file == str(path) match in
    # cross-file resolution.
    root = Path(root)

    def _abs(item: dict) -> dict:
        source = item.get("source_file")
        if not source:
            return item
        source_path = Path(source)
        if source_path.is_absolute():
            return item
        return {**item, "source_file": str(root / source_path)}

    out = dict(result)
    for bucket in ("nodes", "edges", "hyperedges"):
        items = result.get(bucket)
        if isinstance(items, list):
            out[bucket] = [_abs(it) if isinstance(it, dict) else it for it in items]
    return out


def _relativize_source_files(result: dict, root: Path) -> dict:
    """Return a copy of ``result`` with absolute ``source_file`` fields made
    relative to ``root``.

    Mirrors watch._relativize_source_files so cache entries written under a
    git-shared graphify-out/ stay portable (#777): a different absolute prefix
    on another clone no longer churns every cache/ast/*.json file. Paths already
    relative, or outside ``root``, are left untouched. The original dict is not
    mutated. Loaders (build._norm_source_file) re-anchor against the active root
    on read, so existing absolute caches still load.
    """
    root = Path(root).resolve()

    def _rel(item: dict) -> dict:
        source = item.get("source_file")
        if not source:
            return item
        source_path = Path(source)
        if not source_path.is_absolute():
            return item
        try:
            rel = source_path.resolve().relative_to(root).as_posix()
        except ValueError:
            return item
        return {**item, "source_file": rel}

    out = dict(result)
    for bucket in ("nodes", "edges", "hyperedges"):
        items = result.get(bucket)
        if isinstance(items, list):
            out[bucket] = [_rel(it) if isinstance(it, dict) else it for it in items]
    return out


def save_cached(path: Path, result: dict, root: Path = Path("."), kind: str = "ast") -> None:
    """Save extraction result for this file.

    Stores as graphify-out/cache/{kind}/{hash}.json where hash = SHA256 of current file contents.
    result should be a dict with 'nodes' and 'edges' lists.

    No-ops if `path` is not a regular file. Subagent-produced semantic fragments
    occasionally carry a directory path in `source_file`; skipping them prevents
    IsADirectoryError from aborting the whole batch.

    Absolute ``source_file`` fields are relativized against ``root`` before
    writing so a committed/shared cache stays portable across machines (#777).
    """
    p = Path(path)
    if not p.is_file():
        return
    h = file_hash(p, root)
    target_dir = cache_dir(root, kind)
    entry = target_dir / f"{h}.json"
    result = _relativize_source_files(result, root)
    fd, tmp_path = tempfile.mkstemp(dir=target_dir, prefix=f"{h}.", suffix=".tmp")
    try:
        os.write(fd, json.dumps(result).encode())
        os.close(fd)
        try:
            os.replace(tmp_path, entry)
        except PermissionError:
            # Windows: os.replace can fail with WinError 5 if the target is
            # briefly locked. Fall back to copy-then-delete.
            import shutil
            shutil.copy2(tmp_path, entry)
            os.unlink(tmp_path)
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def cached_files(root: Path = Path(".")) -> set[str]:
    """Return set of file hashes that have a valid cache entry (any kind)."""
    base = _out_base(root) / "cache"
    hashes: set[str] = set()
    # Legacy flat entries
    if base.is_dir():
        hashes.update(p.stem for p in base.glob("*.json"))
    # Namespaced entries
    for kind in ("ast", "semantic"):
        d = base / kind
        if d.is_dir():
            hashes.update(p.stem for p in d.glob("*.json"))
    return hashes


def clear_cache(root: Path = Path(".")) -> None:
    """Delete all cache entries (ast/, semantic/, and legacy flat entries)."""
    base = _out_base(root) / "cache"
    # Legacy flat entries
    if base.is_dir():
        for f in base.glob("*.json"):
            f.unlink()
    # Namespaced entries
    for kind in ("ast", "semantic"):
        d = base / kind
        if d.is_dir():
            for f in d.glob("*.json"):
                f.unlink()


def check_semantic_cache(
    files: list[str],
    root: Path = Path("."),
) -> tuple[list[dict], list[dict], list[dict], list[str]]:
    """Check semantic extraction cache for a list of absolute file paths.

    Returns (cached_nodes, cached_edges, cached_hyperedges, uncached_files).
    Uncached files need Claude extraction; cached files are merged directly.
    """
    cached_nodes: list[dict] = []
    cached_edges: list[dict] = []
    cached_hyperedges: list[dict] = []
    uncached: list[str] = []

    for fpath in files:
        p = Path(fpath)
        if not p.is_absolute():
            p = Path(root) / p
        result = load_cached(p, root, kind="semantic")
        if result is not None:
            cached_nodes.extend(result.get("nodes", []))
            cached_edges.extend(result.get("edges", []))
            cached_hyperedges.extend(result.get("hyperedges", []))
        else:
            uncached.append(fpath)

    return cached_nodes, cached_edges, cached_hyperedges, uncached


def save_semantic_cache(
    nodes: list[dict],
    edges: list[dict],
    hyperedges: list[dict] | None = None,
    root: Path = Path("."),
) -> int:
    """Save semantic extraction results to cache, keyed by source_file.

    Groups nodes and edges by source_file, then saves one cache entry per file
    under cache/semantic/ (separate from AST entries in cache/ast/) to prevent
    hash-key collisions (#582).
    Returns the number of files cached.
    """
    from collections import defaultdict

    by_file: dict[str, dict] = defaultdict(lambda: {"nodes": [], "edges": [], "hyperedges": []})
    for n in nodes:
        src = n.get("source_file", "")
        if src:
            by_file[src]["nodes"].append(n)
    for e in edges:
        src = e.get("source_file", "")
        if src:
            by_file[src]["edges"].append(e)
    for h in (hyperedges or []):
        src = h.get("source_file", "")
        if src:
            by_file[src]["hyperedges"].append(h)

    saved = 0
    for fpath, result in by_file.items():
        p = Path(fpath)
        if not p.is_absolute():
            p = Path(root) / p
        if p.is_file():
            save_cached(p, result, root, kind="semantic")
            saved += 1
    return saved
