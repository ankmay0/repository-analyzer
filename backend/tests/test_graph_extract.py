from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from repo_analyzer.domain import SourceFile
from repo_analyzer.ingestion.graph_extract import READS, WRITES, GraphRecords, extract_graph
from repo_analyzer.ingestion.scanner import scan_repository

SHOP_API = Path(__file__).resolve().parents[2] / "benchmarks" / "fixtures" / "shop-api"


def short(function_id: str) -> str:
    return function_id.split(":", 1)[1]


def calls(records: GraphRecords) -> set[tuple[str, str, str]]:
    return {(short(c.caller), short(c.callee), c.via) for c in records.calls}


def accesses(records: GraphRecords) -> set[tuple[str, str, str]]:
    return {(short(a.function), a.kind, a.table) for a in records.accesses}


def py(path: str, text: str) -> SourceFile:
    return SourceFile(repo="r", path=path, language="python", text=textwrap.dedent(text))


@pytest.fixture(scope="module")
def shop() -> GraphRecords:
    return extract_graph(scan_repository("shop-api", SHOP_API, max_file_bytes=200_000))


def test_fastapi_endpoints_include_router_prefixes(shop: GraphRecords) -> None:
    routes = {(e.method, e.path, short(e.handler)) for e in shop.endpoints}

    assert routes == {
        ("GET", "/health", "health"),
        ("GET", "/customers/{customer_id}", "get_customer"),
        ("GET", "/customers/{customer_id}/summary", "get_customer_summary"),
        ("POST", "/orders", "create_order"),  # @router.post("") under prefix /orders
        ("GET", "/orders", "list_orders"),
        ("GET", "/orders/{order_id}", "get_order"),
        ("POST", "/orders/{order_id}/pay", "mark_order_paid"),
        ("POST", "/orders/{order_id}/fulfill", "fulfill_order"),  # router without prefix
        ("GET", "/fulfillments/{fulfillment_id}", "get_fulfillment"),
        ("GET", "/products", "list_products"),
    }


def test_calls_through_lambdas_and_passed_functions_are_followed(shop: GraphRecords) -> None:
    found = calls(shop)

    assert ("get_customer_summary", "_load_summary", "call") in found  # inside a lambda
    assert ("list_products", "_load_active_products", "reference") in found  # passed by name
    assert ("create_order", "price_order", "call") in found  # imported from app.services
    assert ("to_order", "cents_to_dollars", "call") in found
    assert ("fulfill_order", "book_shipment", "call") in found


def test_calls_into_code_outside_the_repository_are_skipped(shop: GraphRecords) -> None:
    callees = {callee for _, callee, _ in calls(shop)}

    assert not any(c.startswith(("post", "execute", "HTTPException")) for c in callees)
    assert ("book_shipment", "post", "call") not in calls(shop)  # httpx.post
    internal = {i.module: i.internal for i in shop.imports}
    assert internal["httpx"] is False and internal["app.services.pricing"] is True


def test_tables_come_from_migrations_and_sql_strings(shop: GraphRecords) -> None:
    assert {(t.name, t.declared_in) for t in shop.tables} == {
        ("customers", "migrations/001_initial_schema.sql"),
        ("products", "migrations/001_initial_schema.sql"),
        ("orders", "migrations/001_initial_schema.sql"),
        ("order_items", "migrations/001_initial_schema.sql"),
        ("fulfillments", "migrations/002_fulfillments.sql"),
    }
    found = accesses(shop)
    assert {(k, t) for f, k, t in found if f == "create_order"} == {
        (READS, "customers"),
        (READS, "products"),  # SELECT ... FOR UPDATE stays a read
        (WRITES, "orders"),  # INSERT in an f-string
        (WRITES, "order_items"),
        (WRITES, "products"),  # UPDATE products SET stock_quantity ...
    }
    assert ("_load_summary", READS, "orders") in found
    assert ("_load_active_products", READS, "products") in found
    assert ("fulfill_order", WRITES, "fulfillments") in found


def test_no_source_text_reaches_the_records(shop: GraphRecords) -> None:
    text = repr(shop)

    assert "SELECT" not in text and "Bearer" not in text and "Order count" not in text


def test_sql_without_declared_tables_and_delete_is_only_a_write() -> None:
    records = extract_graph(
        [
            py(
                "jobs.py",
                """
                def report(db):
                    db.run("SELECT w.id FROM widgets w JOIN parts p ON p.widget = w.id")

                def purge(db):
                    db.run("DELETE FROM widgets WHERE stale")

                def lock(db):
                    db.run("SELECT 1 FROM parts WHERE id = 1 FOR UPDATE")
                """,
            )
        ]
    )

    assert accesses(records) == {
        ("report", READS, "widgets"),
        ("report", READS, "parts"),
        ("purge", WRITES, "widgets"),
        ("lock", READS, "parts"),
    }


def test_sqlalchemy_models_and_core_tables() -> None:
    records = extract_graph(
        [
            py(
                "app/models.py",
                """
                from sqlalchemy import MetaData, Table

                metadata = MetaData()
                audit = Table("audit_log", metadata)

                class User(Base):
                    __tablename__ = "users"
                """,
            ),
            py(
                "app/service.py",
                """
                from sqlalchemy import insert, select

                from app.models import User, audit

                def find(session, uid):
                    return session.execute(select(User).where(User.id == uid))

                def legacy(session):
                    return session.query(User).all()

                def register(session, name):
                    session.add(User(name=name))
                    session.execute(insert(audit))
                """,
            ),
        ]
    )

    assert {(t.name, t.declared_in) for t in records.tables} == {
        ("users", "app/models.py"),
        ("audit_log", "app/models.py"),
    }
    assert accesses(records) == {
        ("find", READS, "users"),
        ("legacy", READS, "users"),
        ("register", WRITES, "users"),
        ("register", WRITES, "audit_log"),
    }


def test_django_models_and_managers() -> None:
    records = extract_graph(
        [
            py(
                "shop/models.py",
                """
                from django.db import models

                class Order(models.Model):
                    total = models.IntegerField()

                class Customer(models.Model):
                    class Meta:
                        db_table = "crm_customers"

                class Timestamped(models.Model):
                    class Meta:
                        abstract = True
                """,
            ),
            py(
                "shop/views.py",
                """
                from shop.models import Customer, Order

                def recent():
                    return list(Order.objects.filter(total__gt=0))

                def signup(name):
                    Customer.objects.create(name=name)
                """,
            ),
        ]
    )

    assert {t.name for t in records.tables} == {"shop_order", "crm_customers"}
    assert accesses(records) == {
        ("recent", READS, "shop_order"),
        ("signup", WRITES, "crm_customers"),
    }


def test_ambiguous_names_prefer_the_same_file_or_are_reported_as_gaps() -> None:
    records = extract_graph(
        [
            py("a.py", "def helper(): ...\n\ndef run():\n    helper()\n"),
            py("b.py", "def helper(): ...\n"),
            py("c.py", "def go():\n    helper()\n"),
        ]
    )

    assert calls(records) == {("run", "helper", "call")}
    assert next(c.callee for c in records.calls) == "a.py:helper"
    unresolved = {short(f.id): f.unresolved for f in records.functions}
    assert unresolved["go"] == ("helper",)


def test_methods_resolve_through_self_and_never_to_top_level_functions() -> None:
    records = extract_graph(
        [
            py(
                "svc.py",
                """
                class Repo:
                    def load(self):
                        return self.parse()

                    def parse(self): ...

                def connection(): ...

                def use(pool):
                    return pool.connection()
                """,
            )
        ]
    )

    assert calls(records) == {("Repo.load", "Repo.parse", "call")}


def test_api_route_app_routes_and_empty_paths() -> None:
    records = extract_graph(
        [
            py(
                "main.py",
                """
                from fastapi import APIRouter, FastAPI

                app = FastAPI()
                router = APIRouter(prefix="/v1/")

                @app.get("/health")
                def health(): ...

                @router.api_route("/items/", methods=["GET", "PUT"])
                def items(): ...

                @router.post("")
                def create(): ...
                """,
            )
        ]
    )

    assert {(e.method, e.path) for e in records.endpoints} == {
        ("GET", "/health"),
        ("GET", "/v1/items/"),
        ("PUT", "/v1/items/"),
        ("POST", "/v1"),
    }


def test_files_that_do_not_parse_are_skipped() -> None:
    records = extract_graph([py("bad.py", "def (:\n"), py("good.py", "def ok(): ...\n")])

    assert [f.name for f in records.functions] == ["ok"]
    assert {f.path for f in records.files} == {"bad.py", "good.py"}


def test_layered_app_resolves_through_type_hints_and_protocols() -> None:
    """Route -> service (typed parameter) -> repository (typed attribute, Protocol with one
    implementation) -> table (SQL in a class constant)."""
    records = extract_graph(
        [
            py(
                "app/repos.py",
                """
                from typing import Protocol

                class VideoRepo(Protocol):
                    def get(self, vid): ...
                    def save(self, video): ...

                class PgVideoRepo:
                    _GET_SQL = "SELECT id FROM videos WHERE id = %s"

                    def get(self, vid):
                        return self._pool.execute(self._GET_SQL, (vid,))

                    def save(self, video): ...

                class Cache:
                    def get(self, key): ...
                """,
            ),
            py(
                "app/services.py",
                """
                from app.repos import VideoRepo

                class Service:
                    def get(self, vid): ...

                class VideoService:
                    def __init__(self, *, repo: VideoRepo) -> None:
                        self._repo = repo

                    def find(self, vid):
                        return self._repo.get(vid)
                """,
            ),
            py(
                "app/routes.py",
                """
                from app.services import VideoService

                def show(video_id, service: VideoService | None = None):
                    return service.find(video_id=video_id)
                """,
            ),
            py("tests/test_routes.py", "def video_id(): ...\n"),  # a pytest fixture
        ]
    )

    assert calls(records) == {
        ("show", "VideoService.find", "call"),  # from the parameter's type hint
        ("VideoService.find", "PgVideoRepo.get", "call"),  # the class that implements VideoRepo
    }  # the video_id parameter is not linked to the test fixture of the same name
    assert accesses(records) == {("PgVideoRepo.get", READS, "videos")}


def test_module_sql_constants_and_local_names_shadowing_functions() -> None:
    records = extract_graph(
        [
            py(
                "store.py",
                """
                _DELETE_SQL = "DELETE FROM sessions WHERE id = %s"

                def handler(): ...

                def purge(conn, handler):
                    conn.execute(_DELETE_SQL)
                    handler()
                """,
            )
        ]
    )

    assert accesses(records) == {("purge", WRITES, "sessions")}
    assert calls(records) == set()  # the handler parameter is not the handler function
