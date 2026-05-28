"""Ranking regression tests for query term-matching and seed selection.

Covers #449 (verbose free-form labels must not out-compete concise identifier
matches) and #445 (image/document captions must not dominate seed selection for
code-oriented queries). All deterministic, no LLM involved.
"""
import networkx as nx

from graphify.serve import _score_nodes, _pick_seeds, _query_graph_text


# --- #449: verbose labels must not poison term matching ---

def test_verbose_substring_does_not_outrank_concise_exact():
    """A code node whose label IS the query term must rank at/above a verbose
    document node that merely contains the term as a substring."""
    G = nx.Graph()
    G.add_node(
        "code",
        label="parse_config",
        file_type="code",
        source_file="config.py",
        community=0,
    )
    G.add_node(
        "doc",
        label="This module is responsible for reading and validating the "
        "application config before any service starts up at runtime",
        file_type="document",
        source_file="notes.md",
        community=0,
    )
    scored = _score_nodes(G, ["parse_config"])
    assert scored[0][1] == "code"


def test_verbose_substring_does_not_outrank_concise_substring():
    """For a bare term ('config'), a concise code node that contains it must
    still rank above a long prose caption that also contains it (#449)."""
    G = nx.Graph()
    G.add_node(
        "code",
        label="load_config",
        file_type="code",
        source_file="loader.py",
        community=0,
    )
    G.add_node(
        "doc",
        label="The configuration is read from disk and merged with defaults "
        "so that downstream consumers always see a fully populated config",
        file_type="document",
        source_file="notes.md",
        community=0,
    )
    scored = _score_nodes(G, ["config"])
    order = [nid for _, nid in scored]
    assert order.index("code") < order.index("doc")


def test_verbose_doc_node_still_matchable():
    """Damping must not exclude doc nodes — a term only a verbose doc carries
    still produces a result (#449: weaken, don't filter)."""
    G = nx.Graph()
    G.add_node(
        "doc",
        label="A long architectural rationale describing how the scheduler "
        "coordinates retries across the distributed worker pool over time",
        file_type="document",
        source_file="rationale.md",
        community=0,
    )
    scored = _score_nodes(G, ["scheduler"])
    assert [nid for _, nid in scored] == ["doc"]


# --- #445: seeds prefer code/concept over image/document captions ---

def test_pick_seeds_prefers_code_over_image_caption():
    """When an image caption and a code node both match a term comparably, the
    code node must be seeded (and seeded first) for a code-oriented query."""
    G = nx.Graph()
    G.add_node(
        "img",
        label="diagram showing the auth flow",
        file_type="image",
        source_file="auth.png",
        community=0,
    )
    G.add_node(
        "code",
        label="auth_handler",
        file_type="code",
        source_file="auth.py",
        community=0,
    )
    scored = _score_nodes(G, ["auth"])
    seeds = _pick_seeds(scored)
    assert seeds[0] == "code"
    assert "code" in seeds


def test_image_node_still_seedable_when_dominant():
    """An image node that is the only / clearly best match stays seedable —
    the file_type weight is a tie-break, not a hard exclusion (#445)."""
    G = nx.Graph()
    G.add_node(
        "img",
        label="histogram",
        file_type="image",
        source_file="hist.png",
        community=0,
    )
    G.add_node(
        "code",
        label="render_table",
        file_type="code",
        source_file="render.py",
        community=0,
    )
    scored = _score_nodes(G, ["histogram"])
    seeds = _pick_seeds(scored)
    assert seeds == ["img"]


def test_query_text_seeds_code_not_caption():
    """End-to-end: a code-oriented query whose term appears in both an image
    caption and a code symbol must start traversal from the code node."""
    G = nx.Graph()
    G.add_node(
        "img",
        label="screenshot of the parser output",
        file_type="image",
        source_file="parser.png",
        source_location="L1",
        community=0,
    )
    G.add_node(
        "code",
        label="parser",
        file_type="code",
        source_file="parser.py",
        source_location="L10",
        community=0,
    )
    G.add_node(
        "dep",
        label="Tokenizer",
        file_type="code",
        source_file="lexer.py",
        source_location="L1",
        community=0,
    )
    G.add_edge("code", "dep", relation="uses", confidence="EXTRACTED")
    text = _query_graph_text(G, "parser", mode="bfs", depth=2)
    assert "Start: ['parser']" in text
    assert "Tokenizer" in text


# --- sanity: ordinary code-symbol queries unchanged ---

def test_plain_code_symbol_query_unchanged():
    """An ordinary code graph (no doc/image nodes, concise labels) ranks the
    exact match first exactly as before the ranking change."""
    G = nx.Graph()
    G.add_node("a", label="extract", file_type="code", source_file="extract.py", community=0)
    G.add_node("b", label="cluster", file_type="code", source_file="cluster.py", community=0)
    G.add_node("c", label="build", file_type="code", source_file="build.py", community=1)
    scored = _score_nodes(G, ["extract"])
    assert scored[0][1] == "a"
    assert _pick_seeds(scored)[0] == "a"
