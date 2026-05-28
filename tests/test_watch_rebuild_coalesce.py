"""Regression tests for #1059: back-to-back rebuild triggers must not drop the
second trigger's changed files when the per-repo rebuild lock is contended."""
import json
import sys
import textwrap
import threading
from pathlib import Path

import pytest

from graphify.watch import (
    _append_pending,
    _drain_pending,
    _rebuild_code,
    _rebuild_lock,
    _PENDING_FILE,
)


def _labels(graph_path: Path) -> set[str]:
    data = json.loads(graph_path.read_text(encoding="utf-8"))
    return {n["label"] for n in data.get("nodes", [])}


def _seed_corpus(tmp_path: Path) -> Path:
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    (corpus / "auth.py").write_text("def login(): pass\n", encoding="utf-8")
    assert _rebuild_code(corpus, acquire_lock=False) is True
    return corpus


def test_append_then_drain_roundtrip(tmp_path):
    out = tmp_path / "graphify-out"
    _append_pending(out, [Path("a.py"), Path("b.py")])
    _append_pending(out, [Path("b.py"), Path("c.py")])  # dup b.py coalesces
    full, paths = _drain_pending(out)
    assert full is False
    assert [str(p) for p in paths] == ["a.py", "b.py", "c.py"]
    # Drain consumes the queue.
    assert not (out / _PENDING_FILE).exists()
    assert _drain_pending(out) == (False, [])


def test_drain_recognizes_full_sentinel(tmp_path):
    out = tmp_path / "graphify-out"
    _append_pending(out, None)  # full-rebuild request
    full, paths = _drain_pending(out)
    assert full is True
    assert paths == []


def test_holder_drains_queued_changes(tmp_path):
    """The in-flight rebuild must fold a contender's queued changes into the
    final graph before releasing the lock."""
    corpus = _seed_corpus(tmp_path)
    out = corpus / "graphify-out"
    graph = out / "graph.json"

    (corpus / "alpha.py").write_text("def alpha(): pass\n", encoding="utf-8")
    (corpus / "beta.py").write_text("def beta(): pass\n", encoding="utf-8")

    # Simulate a contender that lost the lock and queued beta.py while the
    # holder was busy. The holder then rebuilds for alpha and must drain beta.
    _append_pending(out, [Path("beta.py")])
    assert _rebuild_code(corpus, changed_paths=[Path("alpha.py")], force=True) is True

    labels = _labels(graph)
    assert "alpha()" in labels, "holder's own change must land"
    assert "beta()" in labels, "queued contender change must be coalesced (#1059)"
    assert not (out / _PENDING_FILE).exists(), "pending queue must be drained"


@pytest.mark.skipif(sys.platform == "win32", reason="fcntl-only (POSIX)")
def test_contended_trigger_queues_instead_of_dropping(tmp_path):
    """When the lock is genuinely held by another process, a trigger must
    record its changes in the pending queue rather than discarding them."""
    corpus = _seed_corpus(tmp_path)
    out = corpus / "graphify-out"
    (corpus / "gamma.py").write_text("def gamma(): pass\n", encoding="utf-8")

    holder_has_lock = threading.Event()
    release_holder = threading.Event()

    def holder():
        with _rebuild_lock(out, blocking=False) as got:
            assert got is True
            holder_has_lock.set()
            release_holder.wait(timeout=5)

    th = threading.Thread(target=holder)
    th.start()
    assert holder_has_lock.wait(timeout=5)

    # Contender cannot get the lock; it must queue, not drop. Run on a thread
    # because the contender blocks waiting for the holder to release.
    contender_result = {}

    def contender():
        contender_result["r"] = _rebuild_code(
            corpus, changed_paths=[Path("gamma.py")], force=True
        )

    ct = threading.Thread(target=contender)
    ct.start()

    # While the holder still holds the lock, gamma must already be queued
    # (the contender appends to the pending file before blocking on the lock).
    import time as _t

    pending = out / _PENDING_FILE
    queued = False
    for _ in range(200):
        if pending.exists() and "gamma.py" in pending.read_text(encoding="utf-8"):
            queued = True
            break
        _t.sleep(0.01)
    assert queued, "contended trigger must queue its changed paths, not drop them"

    release_holder.set()
    th.join(timeout=5)
    ct.join(timeout=10)

    labels = _labels(out / "graph.json")
    assert "gamma()" in labels, "queued change must land after lock releases (#1059)"
    assert not (out / _PENDING_FILE).exists()


@pytest.mark.skipif(sys.platform == "win32", reason="fcntl-only (POSIX)")
def test_back_to_back_subprocess_triggers_both_land(tmp_path):
    """End-to-end: two real processes fire _rebuild_code back-to-back against
    the same repo; both triggers' files must be reflected in the final graph."""
    corpus = _seed_corpus(tmp_path)
    out = corpus / "graphify-out"
    (corpus / "alpha.py").write_text("def alpha(): pass\n", encoding="utf-8")
    (corpus / "beta.py").write_text("def beta(): pass\n", encoding="utf-8")

    runner = textwrap.dedent(
        """
        import sys
        from pathlib import Path
        from graphify.watch import _rebuild_code
        _rebuild_code(Path(sys.argv[1]), changed_paths=[Path(sys.argv[2])], force=True)
        """
    )
    script = tmp_path / "_runner.py"
    script.write_text(runner, encoding="utf-8")

    import subprocess

    p1 = subprocess.Popen([sys.executable, str(script), str(corpus), "alpha.py"])
    p2 = subprocess.Popen([sys.executable, str(script), str(corpus), "beta.py"])
    assert p1.wait(timeout=120) == 0
    assert p2.wait(timeout=120) == 0

    labels = _labels(out / "graph.json")
    assert "alpha()" in labels
    assert "beta()" in labels, "second back-to-back trigger must not be dropped (#1059)"
    assert not (out / _PENDING_FILE).exists()
