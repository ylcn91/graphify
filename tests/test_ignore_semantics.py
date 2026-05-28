"""Tests for ignore semantics: .gitignore is respected during detection
(in addition to .graphifyignore), and --skip-docs limits collection to code.

See #1043 / #189.
"""
from pathlib import Path

from graphify.detect import detect, detect_incremental


# --- #1043/#189: .gitignore respected during detection ---

def test_gitignore_glob_pattern_excluded(tmp_path):
    """A *.glob pattern in .gitignore excludes matching code files."""
    (tmp_path / ".git").mkdir()
    (tmp_path / ".gitignore").write_text("*.generated.py\n")
    (tmp_path / "main.py").write_text("x = 1")
    (tmp_path / "schema.generated.py").write_text("x = 2")

    code = detect(tmp_path)["files"]["code"]
    assert any("main.py" in f for f in code)
    assert not any("generated" in f for f in code)


def test_gitignore_directory_pattern_excluded(tmp_path):
    """A `dir/` pattern in .gitignore excludes the whole directory.

    Uses a non-noise directory name (graphify already hard-skips build/,
    dist/, node_modules/ regardless of any ignore file), so the exclusion
    is attributable to .gitignore parsing rather than _SKIP_DIRS.
    """
    (tmp_path / ".git").mkdir()
    (tmp_path / ".gitignore").write_text("codegen_out/\n")
    gen = tmp_path / "codegen_out"
    gen.mkdir()
    (gen / "stub.py").write_text("x = 1")
    (tmp_path / "main.py").write_text("x = 2")

    code = detect(tmp_path)["files"]["code"]
    assert any("main.py" in f for f in code)
    assert not any("codegen_out" in f for f in code)


def test_gitignore_negation_reincludes(tmp_path):
    """A `!` negation in .gitignore re-includes a previously ignored file."""
    (tmp_path / ".git").mkdir()
    (tmp_path / ".gitignore").write_text("*.log.py\n!keep.log.py\n")
    (tmp_path / "drop.log.py").write_text("x = 1")
    (tmp_path / "keep.log.py").write_text("x = 2")

    code = detect(tmp_path)["files"]["code"]
    assert any("keep.log.py" in f for f in code)
    assert not any("drop.log.py" in f for f in code)


def test_gitignore_leading_slash_anchored_file_excluded(tmp_path):
    """A leading-/ anchored pattern in .gitignore excludes the root-level file."""
    (tmp_path / ".git").mkdir()
    (tmp_path / ".gitignore").write_text("/config.py\n")
    (tmp_path / "config.py").write_text("x = 1")
    (tmp_path / "main.py").write_text("x = 2")

    code = detect(tmp_path)["files"]["code"]
    assert any("main.py" in f for f in code)
    assert not any(f.endswith("/config.py") for f in code)


def test_gitignore_requires_no_graphifyignore_to_be_present(tmp_path):
    """.gitignore alone (no .graphifyignore) is honored."""
    (tmp_path / ".git").mkdir()
    (tmp_path / ".gitignore").write_text("ignored.py\n")
    (tmp_path / "ignored.py").write_text("x = 1")
    (tmp_path / "kept.py").write_text("x = 2")

    code = detect(tmp_path)["files"]["code"]
    assert any("kept.py" in f for f in code)
    assert not any("ignored.py" in f for f in code)


def test_gitignore_and_graphifyignore_are_additive(tmp_path):
    """Both files apply together; neither suppresses the other (#1043/#189)."""
    (tmp_path / ".git").mkdir()
    (tmp_path / ".gitignore").write_text("from_git.py\n")
    (tmp_path / ".graphifyignore").write_text("from_graphify.py\n")
    (tmp_path / "main.py").write_text("x = 1")
    (tmp_path / "from_git.py").write_text("x = 2")
    (tmp_path / "from_graphify.py").write_text("x = 3")

    code = detect(tmp_path)["files"]["code"]
    assert any("main.py" in f for f in code)
    assert not any("from_git.py" in f for f in code)
    assert not any("from_graphify.py" in f for f in code)


def test_graphifyignore_negation_overrides_gitignore(tmp_path):
    """.graphifyignore (read last) wins on conflict via last-match-wins."""
    (tmp_path / ".git").mkdir()
    (tmp_path / ".gitignore").write_text("*.py\n")
    (tmp_path / ".graphifyignore").write_text("!keep.py\n")
    (tmp_path / "keep.py").write_text("x = 1")
    (tmp_path / "drop.py").write_text("x = 2")

    code = detect(tmp_path)["files"]["code"]
    assert any("keep.py" in f for f in code)
    assert not any("drop.py" in f for f in code)


# --- #189: --skip-docs (skip_docs=True) limits collection to code ---

def _make_mixed_corpus(root: Path) -> None:
    (root / "app.py").write_text("def f():\n    return 1\n")
    (root / "notes.md").write_text("# notes\n\nsome documentation text.\n")
    (root / "spec.txt").write_text("a spec file\n")
    (root / "diagram.png").write_bytes(b"\x89PNG\r\n\x1a\n")


def test_skip_docs_drops_doc_and_image_files(tmp_path):
    _make_mixed_corpus(tmp_path)
    result = detect(tmp_path, skip_docs=True)
    assert any("app.py" in f for f in result["files"]["code"])
    assert result["files"]["document"] == []
    assert result["files"]["paper"] == []
    assert result["files"]["image"] == []


def test_skip_docs_off_keeps_doc_and_image_files(tmp_path):
    _make_mixed_corpus(tmp_path)
    result = detect(tmp_path, skip_docs=False)
    assert any("app.py" in f for f in result["files"]["code"])
    assert any("notes.md" in f for f in result["files"]["document"])
    assert any("diagram.png" in f for f in result["files"]["image"])


def test_skip_docs_default_is_off(tmp_path):
    _make_mixed_corpus(tmp_path)
    result = detect(tmp_path)
    assert any("notes.md" in f for f in result["files"]["document"])


def test_skip_docs_threads_through_incremental(tmp_path):
    """detect_incremental forwards skip_docs to detect()."""
    _make_mixed_corpus(tmp_path)
    result = detect_incremental(tmp_path, manifest_path=str(tmp_path / "no_manifest.json"), skip_docs=True)
    assert result["files"]["document"] == []
    assert result["files"]["image"] == []
    assert any("app.py" in f for f in result["files"]["code"])


# --- interaction: skip_docs + ignore files ---

def test_skip_docs_and_gitignore_compose(tmp_path):
    """A gitignored code file stays excluded and docs are dropped under skip_docs."""
    (tmp_path / ".git").mkdir()
    (tmp_path / ".gitignore").write_text("ignored.py\n")
    (tmp_path / "kept.py").write_text("x = 1")
    (tmp_path / "ignored.py").write_text("x = 2")
    (tmp_path / "readme.md").write_text("# doc\n")

    result = detect(tmp_path, skip_docs=True)
    code = result["files"]["code"]
    assert any("kept.py" in f for f in code)
    assert not any("ignored.py" in f for f in code)
    assert result["files"]["document"] == []
