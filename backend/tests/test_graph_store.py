from __future__ import annotations

import os
from pathlib import Path

import pytest

from repo_analyzer.ingestion.graph_extract import READS, WRITES, extract_graph
from repo_analyzer.ingestion.graph_store import (
    Neo4jGraphStore,
    assemble_trace,
    graph_workspace_key,
    match_endpoint,
)
from repo_analyzer.ingestion.scanner import scan_repository
from support import FakeGraphStore

SHOP_API = Path(__file__).resolve().parents[2] / "benchmarks" / "fixtures" / "shop-api"
ENDPOINTS = [
    {"method": "GET", "path": "/customers/{customer_id}", "handler": "a", "file": "f", "line": 1},
    {"method": "GET", "path": "/customers/{customer_id}/summary", "handler": "b",
     "file": "f", "line": 2},
    {"method": "POST", "path": "/orders", "handler": "c", "file": "f", "line": 3},
]  # fmt: skip


def shop_store() -> FakeGraphStore:
    store = FakeGraphStore()
    records = extract_graph(scan_repository("shop-api", SHOP_API, max_file_bytes=200_000))
    store.sync_repo("ws", "shop-api", None, records)
    return store


def test_workspace_key_is_stable_and_distinguishes_data_dirs(tmp_path: Path) -> None:
    key = graph_workspace_key(tmp_path / "data")

    assert key == graph_workspace_key(tmp_path / "data")
    assert key != graph_workspace_key(tmp_path / "other")
    assert len(key) == 12


def test_match_endpoint_exact_template_and_normalised_paths() -> None:
    assert match_endpoint(ENDPOINTS, "post", "/orders/")["handler"] == "c"  # type: ignore[index]
    assert match_endpoint(ENDPOINTS, "GET", "/customers/{customer_id}")["handler"] == "a"  # type: ignore[index]
    assert match_endpoint(ENDPOINTS, "GET", "customers/42/summary?x=1")["handler"] == "b"  # type: ignore[index]
    assert match_endpoint(ENDPOINTS, "DELETE", "/orders") is None
    assert match_endpoint(ENDPOINTS, "GET", "/customers/42/orders") is None


def test_assemble_trace_orders_by_call_line_and_marks_repeats() -> None:
    def node(fid: str, unresolved: tuple[str, ...] = ()) -> dict[str, object]:
        return {"id": fid, "name": fid, "path": "x.py", "line": 1, "end_line": 9,
                "unresolved": list(unresolved)}  # fmt: skip

    handler, late, early, shared = node("h", ("maybe",)), node("late"), node("early"), node("s")
    paths = [
        ([handler], []),
        ([handler, late], [{"via": "call", "line": 20}]),
        ([handler, late, shared], [{"via": "call", "line": 20}, {"via": "call", "line": 3}]),
        ([handler, early], [{"via": "reference", "line": 10}]),
        ([handler, early, shared], [{"via": "reference", "line": 10}, {"via": "call", "line": 4}]),
    ]
    accesses = [{"function": "s", "table": "orders", "kind": READS, "line": 5}]

    trace = assemble_trace(ENDPOINTS[2], paths, accesses)

    assert [(s.depth, s.function, s.via, s.repeated) for s in trace.steps] == [
        (0, "h", None, False),
        (1, "early", "reference", False),
        (2, "s", "call", False),
        (1, "late", "call", False),
        (2, "s", "call", True),
    ]
    assert trace.steps[2].tables == (("orders", READS, 5),)
    assert trace.steps[4].tables == ()
    assert trace.gaps == ("h: maybe",)
    assert (trace.method, trace.path, trace.truncated) == ("POST", "/orders", False)


def test_trace_from_the_handler_down_to_the_tables() -> None:
    trace = shop_store().trace_endpoint("ws", "shop-api", "POST", "/orders")

    assert trace is not None
    assert [(s.depth, s.function) for s in trace.steps] == [
        (0, "create_order"),
        (1, "connection"),
        (1, "price_order"),
        (1, "invalidate"),
        (1, "to_order"),
        (2, "cents_to_dollars"),
    ]
    handler = trace.steps[0]
    assert (handler.path, handler.line) == ("app/routers/orders.py", 33)  # the def line
    assert {(t, k) for t, k, _ in handler.tables} == {
        ("customers", READS), ("products", READS), ("orders", WRITES),
        ("order_items", WRITES), ("products", WRITES),
    }  # fmt: skip


def test_trace_follows_functions_passed_by_reference() -> None:
    trace = shop_store().trace_endpoint("ws", "shop-api", "GET", "/products")

    assert trace is not None
    loader = next(s for s in trace.steps if s.function == "_load_active_products")
    assert (loader.depth, loader.via) == (1, "reference")
    assert [t for t, _, _ in loader.tables] == ["products"]


def test_unknown_endpoint_has_no_trace() -> None:
    assert shop_store().trace_endpoint("ws", "shop-api", "DELETE", "/orders") is None
    assert shop_store().trace_endpoint("other-ws", "shop-api", "POST", "/orders") is None


def test_neo4j_store_reports_unreachable_without_raising(
    caplog: pytest.LogCaptureFixture,
) -> None:
    store = Neo4jGraphStore("bolt://127.0.0.1:1", "neo4j", "x", "neo4j")

    assert store.ping() is False
    assert store.ping() is False
    store.close()

    reasons = [r for r in caplog.records if "Neo4j is not available" in r.getMessage()]
    assert len(reasons) == 1  # the reason is logged once per outage, not on every health poll
    assert "bolt://127.0.0.1:1" in reasons[0].getMessage()


@pytest.mark.skipif(
    not os.environ.get("RA_TEST_NEO4J_URI"), reason="set RA_TEST_NEO4J_URI to run against Neo4j"
)
def test_neo4j_round_trip_matches_the_in_memory_store() -> None:
    store = Neo4jGraphStore(
        os.environ["RA_TEST_NEO4J_URI"],
        os.environ.get("RA_TEST_NEO4J_USER", "neo4j"),
        os.environ.get("RA_TEST_NEO4J_PASSWORD", "test"),
        os.environ.get("RA_TEST_NEO4J_DATABASE", "neo4j"),
    )
    records = extract_graph(scan_repository("shop-api", SHOP_API, max_file_bytes=200_000))
    ws = "pytest-graph"
    try:
        store.sync_repo(ws, "shop-api", "abc", records)
        store.sync_repo(ws, "shop-api", "abc", records)  # replacing is idempotent
        assert store.has_repo(ws, "shop-api") and not store.has_repo(ws, "shop-web")
        expected = shop_store().trace_endpoint("ws", "shop-api", "POST", "/orders")
        actual = store.trace_endpoint(ws, "shop-api", "POST", "/orders")
        assert actual is not None and expected is not None
        assert [(s.depth, s.function, s.tables) for s in actual.steps] == [
            (s.depth, s.function, s.tables) for s in expected.steps
        ]
        callers = {row["name"] for row in store.neighbours(ws, "shop-api", "to_order", "in")}
        assert callers == {"create_order", "list_orders", "get_order", "mark_order_paid"}
        callees = store.neighbours(ws, "shop-api", "list_products", "out")
        assert {(row["name"], row["via"]) for row in callees} == {
            ("cached", "call"),
            ("_load_active_products", "reference"),
        }
    finally:
        store.delete_repo(ws, "shop-api")
        assert store.endpoints(ws, "shop-api") == []
        assert not store.has_repo(ws, "shop-api")
        store.close()
