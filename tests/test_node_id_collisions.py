"""Regression tests for #952/#438/#1033 — node-ID collisions across same-basename
files, and the byte-identical contract between ``canonical_file_id`` (the code
path that the AST extractor uses) and the node-ID formula documented in the skill
markdown for semantic subagents.

The bug: symbol IDs were prefixed with only the immediate parent directory
(``{parent}.{stem}``) and file IDs kept the extension (``…_py``). Same-basename
files in different directories (``a/util/x.py`` vs ``b/util/x.py``, multiple
Next.js ``*/index.tsx``, several ``Program.cs``) collapsed onto one node when
extracted across separate/incremental runs, where the in-run disambiguator could
not see the collision.

The fix: every file's node ID and the prefix of every symbol it emits is
``canonical_file_id`` — the full repo-relative path with the extension removed,
normalized by ``_make_id``. AST nodes and skill-emitted semantic nodes for the
same file therefore land on the same ID and merge instead of splitting.
"""

from __future__ import annotations

import re
import unicodedata
from pathlib import Path

from graphify.build import build_from_json
from graphify.extract import canonical_file_id, extract, _make_id


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _ids_by_source(result: dict) -> dict[str, set[str]]:
    """Map node id -> set of distinct source_file paths that emitted it."""
    out: dict[str, set[str]] = {}
    for node in result.get("nodes", []):
        out.setdefault(node["id"], set()).add(node.get("source_file", ""))
    return out


# ── (a) duplicate-basename monorepo yields DISTINCT nodes ────────────────────


def _make_duplicate_basename_repo(root: Path) -> tuple[Path, Path]:
    a = _write(
        root / "a" / "util" / "x.py",
        "class X:\n    def run(self):\n        return 1\n\ndef helper():\n    return X()\n",
    )
    b = _write(
        root / "b" / "util" / "x.py",
        "class X:\n    def run(self):\n        return 2\n\ndef helper():\n    return X()\n",
    )
    return a, b


def test_same_basename_files_do_not_collide_single_run(tmp_path: Path):
    a, b = _make_duplicate_basename_repo(tmp_path)

    result = extract([a, b], cache_root=tmp_path)
    by_id = _ids_by_source(result)

    # No node id is shared across two different source files.
    collisions = {nid: files for nid, files in by_id.items() if len(files) > 1}
    assert not collisions, f"node ids collided across files: {collisions}"

    # Both files produce distinct file nodes and distinct class/function symbols.
    a_file = canonical_file_id(a, tmp_path)
    b_file = canonical_file_id(b, tmp_path)
    ids = set(by_id)
    assert {a_file, b_file} <= ids
    assert {f"{a_file}_x", f"{a_file}_helper"} <= ids
    assert {f"{b_file}_x", f"{b_file}_helper"} <= ids


def test_same_basename_files_do_not_collide_across_incremental_runs(tmp_path: Path):
    """The original bug only surfaced when files were extracted in SEPARATE runs
    (incremental / watch), where the in-run disambiguator cannot see the other
    file. The canonical scheme makes the IDs distinct without any disambiguation.
    """
    a, b = _make_duplicate_basename_repo(tmp_path)

    r1 = extract([a], cache_root=tmp_path)
    r2 = extract([b], cache_root=tmp_path)

    merged: dict[str, set[str]] = {}
    for node in r1["nodes"] + r2["nodes"]:
        merged.setdefault(node["id"], set()).add(node.get("source_file", ""))

    collisions = {nid: files for nid, files in merged.items() if len(files) > 1}
    assert not collisions, f"cross-run node ids collided: {collisions}"


def test_duplicate_index_files_do_not_collide(tmp_path: Path):
    """Next.js / TS monorepos have many ``*/index.tsx`` and ``*/index.ts``."""
    p1 = _write(tmp_path / "pkg1" / "index.ts", "export const value = 1\n")
    p2 = _write(tmp_path / "pkg2" / "index.ts", "export const value = 2\n")

    result = extract([p1, p2], cache_root=tmp_path)
    by_id = _ids_by_source(result)
    collisions = {nid: files for nid, files in by_id.items() if len(files) > 1}
    assert not collisions, f"index file node ids collided: {collisions}"
    assert canonical_file_id(p1, tmp_path) == "pkg1_index"
    assert canonical_file_id(p2, tmp_path) == "pkg2_index"


# ── (b) CONTRACT TEST: code path == skill-documented formula ─────────────────


def _skill_documented_id(rel_path: str, entity: str | None = None) -> str:
    """Independent re-implementation of the node-ID formula documented in
    ``skill.md`` — kept deliberately separate from ``extract`` so the assertion
    pins the *contract*, not a shared helper.

    Skill formula (skill.md, "Node ID format"): the stem is the full repo-relative
    path with its file extension removed, lowercased with every separator and
    non-alphanumeric char replaced by ``_`` (runs of ``_`` collapsed,
    leading/trailing ``_`` stripped); the entity is the symbol name similarly
    normalized; the id is ``{stem}_{entity}`` (or just the stem for a file node).
    """

    def normalize(text: str) -> str:
        text = unicodedata.normalize("NFKC", text)
        text = re.sub(r"[^0-9A-Za-z]+", "_", text, flags=re.UNICODE)
        text = re.sub(r"_+", "_", text)
        return text.strip("_").casefold()

    # Strip the single trailing file extension from the final path component.
    p = Path(rel_path)
    no_ext = p.with_suffix("") if p.suffix else p
    stem = normalize(str(no_ext))
    if entity is None:
        return stem
    return f"{stem}_{normalize(entity)}"


def test_canonical_file_id_matches_skill_formula_for_files():
    cases = [
        "src/auth/session.py",
        "session.py",                       # top-level
        "app/components/Button/index.tsx",  # nested, mixed case
        "a/util/x.py",
        "b/util/x.py",
        "lib/utils/helpers.py",
        r"src\auth\session.py",             # Windows-style separators
        "src/auth/session",                 # no extension
        "deep/nested/path/to/file.go",
    ]
    for rel in cases:
        # Backslash-only inputs are normalised by the skill formula too; for the
        # code path, feed a posix-style equivalent so we compare the same logical
        # path the AST extractor would see after path normalisation.
        code = canonical_file_id(rel.replace("\\", "/"))
        skill = _skill_documented_id(rel.replace("\\", "/"))
        assert code == skill, f"{rel!r}: code={code!r} != skill={skill!r}"


def test_symbol_id_matches_skill_formula():
    cases = [
        ("src/auth/session.py", "ValidateToken", "src_auth_session_validatetoken"),
        ("lib/utils/helpers.py", "parse_url", "lib_utils_helpers_parse_url"),
        ("app/components/Button/index.tsx", "render", "app_components_button_index_render"),
        ("tests/test_foo.py", "_helper", "tests_test_foo_helper"),
        ("setup.py", "my_func", "setup_my_func"),
    ]
    for rel, entity, expected in cases:
        file_id = canonical_file_id(rel)
        code_symbol = _make_id(file_id, entity)
        skill_symbol = _skill_documented_id(rel, entity)
        assert code_symbol == skill_symbol == expected, (
            f"{rel} + {entity}: code={code_symbol!r} skill={skill_symbol!r} "
            f"expected={expected!r}"
        )


# ── (c) semantic node under the NEW formula MERGES with the AST node ─────────


def test_semantic_node_merges_with_ast_node(tmp_path: Path):
    """A semantic-style node emitted under the documented formula for a file must
    land on the SAME id as the AST symbol node for that file, so build_from_json
    keeps them as one node (not two ghost duplicates)."""
    src = _write(
        tmp_path / "src" / "auth" / "session.py",
        "class Session:\n    def validate_token(self):\n        return True\n",
    )

    ast_result = extract([src], cache_root=tmp_path)

    # The AST symbol for the top-level ``Session`` class — a file-id + entity id
    # exactly matching the documented formula.
    rel = "src/auth/session.py"
    file_id = _skill_documented_id(rel)        # src_auth_session
    class_id = _skill_documented_id(rel, "Session")  # src_auth_session_session

    ast_ids = {n["id"] for n in ast_result["nodes"]}
    assert file_id in ast_ids
    assert class_id in ast_ids, f"{class_id} not in {sorted(ast_ids)}"
    ast_node_count = len(ast_result["nodes"])

    # A subagent following the skill emits a node for the same class using the
    # documented formula. Its id must equal the AST id, so the two merge.
    semantic = {
        "nodes": [
            {
                "id": class_id,
                "label": "Session (holds an authenticated user session)",
                "file_type": "code",
                "source_file": rel,
            }
        ],
        "edges": [],
    }

    combined = {
        "nodes": ast_result["nodes"] + semantic["nodes"],
        "edges": ast_result["edges"] + semantic["edges"],
    }
    G = build_from_json(combined, root=tmp_path)

    # The semantic node merged onto the AST node: no extra node was created.
    assert G.has_node(class_id)
    assert G.number_of_nodes() == ast_node_count
    # Semantic label wins (added last), proving the merge happened.
    assert "authenticated user session" in G.nodes[class_id]["label"]
