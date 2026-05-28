"""Regression tests for #437/#540: INFERRED cross-file `calls` edges must be
gated on real import evidence, not bare unique-label coincidence."""

from __future__ import annotations

from graphify.symbol_resolution import _bash_make_id, resolve_cross_file_raw_calls


def _raw_call_in(source_file: str = "caller.py", callee: str = "log") -> list[dict]:
    return [
        {
            "raw_calls": [
                {
                    "caller_nid": "caller_run",
                    "callee": callee,
                    "is_member_call": False,
                    "source_file": source_file,
                    "source_location": "L2",
                }
            ]
        }
    ]


def test_no_import_same_name_emits_no_edge() -> None:
    """Two unrelated files each define `log`; caller imports nothing → no edge."""
    nodes = [
        {"id": "caller_run", "label": "run()", "file_type": "code", "source_file": "caller.py"},
        {"id": "logger_log", "label": "log()", "file_type": "code", "source_file": "logger.py"},
    ]
    assert resolve_cross_file_raw_calls(_raw_call_in(), nodes, []) == []


def test_symbol_level_import_emits_edge() -> None:
    """A named-import edge to the target symbol justifies the INFERRED call."""
    nodes = [
        {"id": "caller_run", "label": "run()", "file_type": "code", "source_file": "caller.py"},
        {"id": "logger_log", "label": "log()", "file_type": "code", "source_file": "logger.py"},
    ]
    edges = [
        {
            "source": "caller_file",
            "target": "logger_log",
            "relation": "imports",
            "source_file": "caller.py",
        }
    ]
    resolved = resolve_cross_file_raw_calls(_raw_call_in(), nodes, edges)
    assert len(resolved) == 1
    edge = resolved[0]
    assert edge["source"] == "caller_run"
    assert edge["target"] == "logger_log"
    assert edge["confidence"] == "INFERRED"
    assert edge["confidence_score"] == 0.8


def test_file_level_import_from_emits_edge() -> None:
    """A whole-file `imports_from` edge to the target's module also justifies it."""
    nodes = [
        {"id": "caller_run", "label": "run()", "file_type": "code", "source_file": "caller.py"},
        {"id": "logger_log", "label": "log()", "file_type": "code", "source_file": "logger.py"},
    ]
    edges = [
        {
            "source": "caller_file",
            "target": _bash_make_id("logger"),  # absolute `from logger import log`
            "relation": "imports_from",
            "source_file": "caller.py",
        }
    ]
    assert len(resolve_cross_file_raw_calls(_raw_call_in(), nodes, edges)) == 1


def test_import_for_different_file_does_not_justify() -> None:
    """Importing some *other* module must not justify a same-name call to a
    third, unimported file."""
    nodes = [
        {"id": "caller_run", "label": "run()", "file_type": "code", "source_file": "caller.py"},
        {"id": "logger_log", "label": "log()", "file_type": "code", "source_file": "logger.py"},
    ]
    edges = [
        {
            "source": "caller_file",
            "target": _bash_make_id("unrelated"),
            "relation": "imports_from",
            "source_file": "caller.py",
        }
    ]
    assert resolve_cross_file_raw_calls(_raw_call_in(), nodes, edges) == []
