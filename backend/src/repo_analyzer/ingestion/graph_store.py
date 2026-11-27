"""Store the code graph in Neo4j and read request traces back out of it.

The graph is derived data: the JSON indexes stay the source of truth, and a repository's
subgraph is rebuilt from its checkout on every indexing run, so it can be dropped at any
time. Every node carries ``ws`` (which workspace wrote it, see ``graph_workspace_key``)
and ``repo``, so the main app, Compare targets and benchmark runs can share one database.

Model::

    (Repo)-[:CONTAINS]->(File)-[:DECLARES]->(Function|Class)
    (File)-[:IMPORTS]->(Module)
    (Endpoint)-[:HANDLED_BY]->(Function)-[:CALLS {via, line}]->(Function)
    (Function)-[:READS|WRITES {line}]->(Table)

All Cypher is fixed text with ``$parameters``; reads carry a ``LIMIT`` and a timeout.
The ``neo4j`` driver is an optional extra (``pip install -e ".[graph]"``) imported on
first use, so nothing here costs anything while the graph is disabled.

``match_endpoint`` and ``assemble_trace`` are pure so the in-memory test store and the
Neo4j store build traces the same way.
"""

from __future__ import annotations

import hashlib
import logging
import re
import threading
import time
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from repo_analyzer.errors import GraphUnavailableError
from repo_analyzer.ingestion.graph_extract import READS, WRITES, GraphRecords

if TYPE_CHECKING:
    from repo_analyzer.config import Settings

logger = logging.getLogger(__name__)

MAX_TRACE_DEPTH = 6  # also fixed in _TRACE_EDGES; Cypher cannot parameterise it
MAX_TRACE_EDGES = 1000
MAX_TRACE_STEPS = 60
MAX_ENDPOINTS = 500
MAX_NEIGHBOURS = 50
_BATCH = 1000

Row = dict[str, Any]


def graph_workspace_key(data_dir: Path) -> str:
    """Stable short key for a data directory, used as ``ws`` on every node."""
    return hashlib.sha256(str(Path(data_dir).resolve()).encode("utf-8")).hexdigest()[:12]


# --- traces ---------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TraceStep:
    depth: int  # 0 is the endpoint's handler
    function: str  # qualname
    function_id: str
    path: str
    line: int
    end_line: int | None
    via: str | None  # how the parent reached it: "call", "reference"; None for the handler
    call_line: int | None  # line of that call in the parent
    tables: tuple[tuple[str, str, int], ...]  # (table, READS|WRITES, line)
    repeated: bool = False  # already listed above; its callees are not repeated


@dataclass(frozen=True, slots=True)
class Trace:
    method: str
    path: str
    file: str
    line: int
    steps: tuple[TraceStep, ...]
    gaps: tuple[str, ...]  # "caller: name" for calls that matched several functions
    truncated: bool


def _normalise_route(path: str) -> str:
    path = path.split("?", 1)[0].strip() or "/"
    path = "/" + path.lstrip("/")
    return path.rstrip("/") or "/"


def match_endpoint(endpoints: Sequence[Row], method: str, path: str) -> Row | None:
    """The endpoint for ``method path``: exact route first, then ``{param}`` templates."""
    method = method.strip().upper()
    wanted = _normalise_route(path)
    same_method = [e for e in endpoints if e["method"] == method]
    for endpoint in same_method:
        if _normalise_route(endpoint["path"]) == wanted:
            return endpoint
    for endpoint in same_method:
        pattern = re.sub(r"\\\{[^/]*?\\\}", "[^/]+", re.escape(_normalise_route(endpoint["path"])))
        if re.fullmatch(pattern, wanted):
            return endpoint
    return None


def assemble_trace(
    endpoint: Row,
    paths: Sequence[tuple[Sequence[Row], Sequence[Row]]],
    accesses: Sequence[Row],
    *,
    max_steps: int = MAX_TRACE_STEPS,
) -> Trace:
    """Turn call paths from the handler into ordered, depth-first steps.

    ``paths`` are ``(nodes, rels)`` pairs starting with ``([handler], [])``; single call
    edges ``([caller, callee], [rel])`` are enough. ``accesses`` are
    ``{function, table, kind, line}``.
    Callees are visited in call-line order; a function reached twice is listed again
    as ``repeated`` without its callees.
    """
    nodes: dict[str, Row] = {}
    children: dict[str, dict[str, Row]] = defaultdict(dict)
    root: str | None = None
    for path_nodes, rels in paths:
        if not path_nodes:
            continue
        root = root or path_nodes[0]["id"]
        for node in path_nodes:
            nodes.setdefault(node["id"], node)
        for parent, child, rel in zip(path_nodes, path_nodes[1:], rels, strict=False):
            current = children[parent["id"]].get(child["id"])
            if current is None or (rel.get("line") or 0) < (current.get("line") or 0):
                children[parent["id"]][child["id"]] = rel
    tables: dict[str, list[tuple[str, str, int]]] = defaultdict(list)
    for access in accesses:
        tables[access["function"]].append((access["table"], access["kind"], access["line"]))

    steps: list[TraceStep] = []
    seen: set[str] = set()

    def visit(fid: str, depth: int, via: str | None, call_line: int | None) -> None:
        node = nodes[fid]
        repeated = fid in seen
        steps.append(
            TraceStep(
                depth=depth,
                function=node["name"],
                function_id=fid,
                path=node["path"],
                line=node["line"],
                end_line=node.get("end_line"),
                via=via,
                call_line=call_line,
                tables=()
                if repeated
                else tuple(sorted(tables.get(fid, []), key=lambda t: (t[2], t[0], t[1]))),
                repeated=repeated,
            )
        )
        if repeated:
            return
        seen.add(fid)
        ordered = sorted(children[fid].items(), key=lambda kv: (kv[1].get("line") or 0, kv[0]))
        for child_id, rel in ordered:
            visit(child_id, depth + 1, rel.get("via"), rel.get("line"))

    if root is not None:
        visit(root, 0, None, None)
    gaps = tuple(
        f"{nodes[fid]['name']}: {name}"
        for fid in sorted(seen)
        for name in nodes[fid].get("unresolved") or ()
    )
    return Trace(
        method=endpoint["method"],
        path=endpoint["path"],
        file=endpoint["file"],
        line=endpoint["line"],
        steps=tuple(steps[:max_steps]),
        gaps=gaps,
        truncated=len(steps) > max_steps,
    )


# --- the store --------------------------------------------------------------------


class GraphStore(Protocol):
    """What indexing and the agent need from a graph database."""

    def ensure_schema(self) -> None: ...

    def ping(self) -> bool: ...

    def sync_repo(self, ws: str, repo: str, commit: str | None, records: GraphRecords) -> None:
        """Replace ``repo``'s subgraph in ``ws`` with ``records``."""
        ...

    def delete_repo(self, ws: str, repo: str) -> None: ...

    def has_repo(self, ws: str, repo: str) -> bool:
        """Whether ``repo`` has been synced into ``ws``."""
        ...

    def endpoints(self, ws: str, repo: str) -> list[Row]:
        """``{method, path, handler, file, line}`` for every endpoint of ``repo``."""
        ...

    def trace_endpoint(self, ws: str, repo: str, method: str, path: str) -> Trace | None: ...

    def neighbours(self, ws: str, repo: str, function: str, direction: str) -> list[Row]:
        """Callers (``direction="in"``) or callees (``"out"``) of a function name or id."""
        ...

    def close(self) -> None: ...


_LABELS = ("Repo", "File", "Function", "Class", "Module", "Endpoint", "Table")

_SCHEMA = (
    *(
        f"CREATE INDEX ra_{label.lower()}_scope IF NOT EXISTS FOR (n:{label}) ON (n.ws, n.repo)"
        for label in _LABELS
    ),
    "CREATE INDEX ra_file_path IF NOT EXISTS FOR (n:File) ON (n.ws, n.repo, n.path)",
    "CREATE INDEX ra_function_id IF NOT EXISTS FOR (n:Function) ON (n.ws, n.repo, n.id)",
    "CREATE INDEX ra_function_name IF NOT EXISTS FOR (n:Function) ON (n.ws, n.repo, n.name)",
    "CREATE INDEX ra_class_id IF NOT EXISTS FOR (n:Class) ON (n.ws, n.repo, n.id)",
    "CREATE INDEX ra_module_name IF NOT EXISTS FOR (n:Module) ON (n.ws, n.repo, n.name)",
    "CREATE INDEX ra_table_name IF NOT EXISTS FOR (n:Table) ON (n.ws, n.repo, n.name)",
    "CREATE INDEX ra_endpoint_route IF NOT EXISTS "
    "FOR (n:Endpoint) ON (n.ws, n.repo, n.method, n.path)",
)

_DELETE = tuple(f"MATCH (n:{label} {{ws: $ws, repo: $repo}}) DETACH DELETE n" for label in _LABELS)

_CREATE_REPO = "CREATE (:Repo {ws: $ws, repo: $repo, name: $repo, commit: $commit})"
_CREATE_FILES = """
UNWIND $rows AS r
CREATE (f:File {ws: $ws, repo: $repo, path: r.path, language: r.language})
WITH f MATCH (rp:Repo {ws: $ws, repo: $repo}) CREATE (rp)-[:CONTAINS]->(f)
"""
_CREATE_FUNCTIONS = """
UNWIND $rows AS r
MATCH (f:File {ws: $ws, repo: $repo, path: r.path})
CREATE (f)-[:DECLARES]->(:Function {ws: $ws, repo: $repo, id: r.id, name: r.name,
    path: r.path, line: r.line, end_line: r.end_line, unresolved: r.unresolved})
"""
_CREATE_CLASSES = """
UNWIND $rows AS r
MATCH (f:File {ws: $ws, repo: $repo, path: r.path})
CREATE (f)-[:DECLARES]->(:Class {ws: $ws, repo: $repo, id: r.id, name: r.name,
    path: r.path, line: r.line, end_line: r.end_line})
"""
_CREATE_MODULES = """
UNWIND $rows AS r
CREATE (:Module {ws: $ws, repo: $repo, name: r.name, internal: r.internal})
"""
_CREATE_IMPORTS = """
UNWIND $rows AS r
MATCH (f:File {ws: $ws, repo: $repo, path: r.path})
MATCH (m:Module {ws: $ws, repo: $repo, name: r.module})
CREATE (f)-[:IMPORTS {line: r.line}]->(m)
"""
_CREATE_TABLES = """
UNWIND $rows AS r
CREATE (:Table {ws: $ws, repo: $repo, name: r.name, declared_in: r.declared_in})
"""
_CREATE_ENDPOINTS = """
UNWIND $rows AS r
MATCH (h:Function {ws: $ws, repo: $repo, id: r.handler})
CREATE (:Endpoint {ws: $ws, repo: $repo, method: r.method, path: r.path, handler: r.handler,
    file: r.file, line: r.line})-[:HANDLED_BY]->(h)
"""
_CREATE_CALLS = """
UNWIND $rows AS r
MATCH (a:Function {ws: $ws, repo: $repo, id: r.caller})
MATCH (b:Function {ws: $ws, repo: $repo, id: r.callee})
CREATE (a)-[:CALLS {line: r.line, via: r.via}]->(b)
"""
_CREATE_ACCESS = {
    kind: f"""
UNWIND $rows AS r
MATCH (f:Function {{ws: $ws, repo: $repo, id: r.function}})
MATCH (t:Table {{ws: $ws, repo: $repo, name: r.table}})
CREATE (f)-[:{kind} {{line: r.line}}]->(t)
"""
    for kind in (READS, WRITES)
}

_HAS_REPO = "MATCH (r:Repo {ws: $ws, repo: $repo}) RETURN count(r) AS n"
_ENDPOINTS = """
MATCH (e:Endpoint {ws: $ws, repo: $repo})
RETURN e.method AS method, e.path AS path, e.handler AS handler, e.file AS file, e.line AS line
ORDER BY path, method LIMIT $limit
"""
# The functions reachable from the handler (distinct, so no path explosion) and the
# calls between them; assemble_trace rebuilds the ordered tree from these edges.
_TRACE_EDGES = """
MATCH (h:Function {ws: $ws, repo: $repo, id: $handler})
OPTIONAL MATCH (h)-[:CALLS*1..6]->(f:Function)
WITH h, collect(DISTINCT f) + [h] AS reach
UNWIND reach AS a
OPTIONAL MATCH (a)-[c:CALLS]->(b:Function)
WHERE b IN reach
RETURN h {.id, .name, .path, .line, .end_line, .unresolved} AS root,
       a {.id, .name, .path, .line, .end_line, .unresolved} AS caller,
       b {.id, .name, .path, .line, .end_line, .unresolved} AS callee,
       c {.via, .line} AS rel
LIMIT $limit
"""
_TRACE_ACCESS = """
MATCH (f:Function {ws: $ws, repo: $repo})-[a:READS|WRITES]->(t:Table)
WHERE f.id IN $ids
RETURN f.id AS function, t.name AS table, type(a) AS kind, a.line AS line
LIMIT $limit
"""
_NEIGHBOUR_FIELDS = (
    "n.id AS id, n.name AS name, n.path AS path, n.line AS line, n.end_line AS end_line, "
    "r.via AS via, r.line AS call_line, f.id AS target"
)
_TARGET = "f.name = $function OR f.id = $function OR f.name ENDS WITH ('.' + $function)"
_CALLERS = f"""
MATCH (n:Function {{ws: $ws, repo: $repo}})-[r:CALLS]->(f:Function {{ws: $ws, repo: $repo}})
WHERE {_TARGET}
RETURN {_NEIGHBOUR_FIELDS} ORDER BY path, call_line LIMIT $limit
"""
_CALLEES = f"""
MATCH (f:Function {{ws: $ws, repo: $repo}})-[r:CALLS]->(n:Function {{ws: $ws, repo: $repo}})
WHERE {_TARGET}
RETURN {_NEIGHBOUR_FIELDS} ORDER BY call_line, path LIMIT $limit
"""


def _batches(rows: list[Row]) -> list[list[Row]]:
    return [rows[i : i + _BATCH] for i in range(0, len(rows), _BATCH)]


class Neo4jGraphStore:
    """``GraphStore`` backed by a Neo4j 5 database through the official driver."""

    def __init__(
        self,
        uri: str,
        user: str,
        password: str,
        database: str,
        *,
        query_timeout: float = 30.0,  # reads are LIMITed; a busy Neo4j can take >10 s
        write_timeout: float = 120.0,
    ) -> None:
        self._uri = uri
        self._auth = (user, password)
        self._database = database
        self._query_timeout = query_timeout
        self._write_timeout = write_timeout
        self._driver: Any = None
        self._schema_ready = False
        self._reachable: bool | None = None  # last ping result, to log only changes
        self._lock = threading.Lock()

    @classmethod
    def from_settings(cls, settings: Settings) -> Neo4jGraphStore:
        password = settings.neo4j_password.get_secret_value() if settings.neo4j_password else ""
        return cls(settings.neo4j_uri, settings.neo4j_user, password, settings.neo4j_database)

    def _neo4j(self) -> Any:
        try:
            import neo4j  # type: ignore[import-not-found, unused-ignore]
        except ImportError as exc:
            raise GraphUnavailableError(
                'The code graph needs the optional extra: pip install -e ".[graph]"'
            ) from exc
        return neo4j

    def _get_driver(self) -> Any:
        with self._lock:
            if self._driver is None:
                neo4j = self._neo4j()
                logger.info("Neo4j driver %s connecting to %s", neo4j.__version__, self._uri)
                self._driver = neo4j.GraphDatabase.driver(
                    self._uri,
                    auth=self._auth,
                    connection_timeout=5.0,
                    connection_acquisition_timeout=10.0,
                )
            return self._driver

    def _read(self, text: str, **params: Any) -> list[Row]:
        neo4j = self._neo4j()
        with self._get_driver().session(database=self._database) as session:
            result = session.run(neo4j.Query(text, timeout=self._query_timeout), params)
            return [dict(record) for record in result]

    def ensure_schema(self) -> None:
        with self._get_driver().session(database=self._database) as session:
            for statement in _SCHEMA:
                session.run(statement)
        self._schema_ready = True
        logger.info(
            "Neo4j indexes ready (%d, database %r at %s)", len(_SCHEMA), self._database, self._uri
        )

    def ping(self) -> bool:
        try:
            self._get_driver().verify_connectivity()
        except Exception as exc:  # missing extra, unreachable server, bad credentials
            if self._reachable is not False:  # log the reason once per outage, not every poll
                logger.warning(
                    "Neo4j is not available at %s: %s: %s", self._uri, type(exc).__name__, exc
                )
            self._reachable = False
            return False
        if self._reachable is not True:
            logger.info("Neo4j is reachable at %s", self._uri)
        self._reachable = True
        return True

    def sync_repo(self, ws: str, repo: str, commit: str | None, records: GraphRecords) -> None:
        if not self._schema_ready:
            self.ensure_schema()
        started = time.perf_counter()
        modules = {i.module: i.internal for i in records.imports}
        steps: list[tuple[str, list[Row]]] = [
            (_CREATE_FILES, [asdict(f) for f in records.files]),
            (_CREATE_FUNCTIONS, [{**asdict(f), "unresolved": list(f.unresolved)}
                                 for f in records.functions]),
            (_CREATE_CLASSES, [asdict(c) for c in records.classes]),
            (_CREATE_MODULES, [{"name": m, "internal": i} for m, i in modules.items()]),
            (_CREATE_IMPORTS, [asdict(i) for i in records.imports]),
            (_CREATE_TABLES, [asdict(t) for t in records.tables]),
            (_CREATE_ENDPOINTS, [asdict(e) for e in records.endpoints]),
            (_CREATE_CALLS, [asdict(c) for c in records.calls]),
            *(
                (_CREATE_ACCESS[kind], [asdict(a) for a in records.accesses if a.kind == kind])
                for kind in (READS, WRITES)
            ),
        ]  # fmt: skip

        def write(tx: Any) -> None:
            for statement in _DELETE:
                tx.run(statement, ws=ws, repo=repo)
            tx.run(_CREATE_REPO, ws=ws, repo=repo, commit=commit)
            for statement, rows in steps:
                for batch in _batches(rows):
                    tx.run(statement, ws=ws, repo=repo, rows=batch)

        neo4j = self._neo4j()
        with self._get_driver().session(database=self._database) as session:
            session.execute_write(neo4j.unit_of_work(timeout=self._write_timeout)(write))
        logger.info(
            "Neo4j: synced %s (ws=%s): %d files, %d functions, %d calls, %d endpoints, "
            "%d tables, %d reads/writes in %d ms",
            repo,
            ws,
            len(records.files),
            len(records.functions),
            len(records.calls),
            len(records.endpoints),
            len(records.tables),
            len(records.accesses),
            round((time.perf_counter() - started) * 1000),
        )

    def delete_repo(self, ws: str, repo: str) -> None:
        def delete(tx: Any) -> None:
            for statement in _DELETE:
                tx.run(statement, ws=ws, repo=repo)

        neo4j = self._neo4j()
        with self._get_driver().session(database=self._database) as session:
            session.execute_write(neo4j.unit_of_work(timeout=self._write_timeout)(delete))
        logger.info("Neo4j: removed %s (ws=%s)", repo, ws)

    def has_repo(self, ws: str, repo: str) -> bool:
        return bool(self._read(_HAS_REPO, ws=ws, repo=repo)[0]["n"])

    def endpoints(self, ws: str, repo: str) -> list[Row]:
        return self._read(_ENDPOINTS, ws=ws, repo=repo, limit=MAX_ENDPOINTS)

    def trace_endpoint(self, ws: str, repo: str, method: str, path: str) -> Trace | None:
        endpoint = match_endpoint(self.endpoints(ws, repo), method, path)
        if endpoint is None:
            return None
        rows = self._read(
            _TRACE_EDGES, ws=ws, repo=repo, handler=endpoint["handler"], limit=MAX_TRACE_EDGES
        )
        if not rows:
            return assemble_trace(endpoint, [], [])
        paths: list[tuple[list[Row], list[Row]]] = [([rows[0]["root"]], [])]
        paths += [([row["caller"], row["callee"]], [row["rel"]]) for row in rows if row["callee"]]
        ids = sorted({node["id"] for nodes, _ in paths for node in nodes})
        accesses = self._read(_TRACE_ACCESS, ws=ws, repo=repo, ids=ids, limit=MAX_TRACE_EDGES)
        return assemble_trace(endpoint, paths, accesses)

    def neighbours(self, ws: str, repo: str, function: str, direction: str) -> list[Row]:
        query = _CALLERS if direction == "in" else _CALLEES
        return self._read(query, ws=ws, repo=repo, function=function, limit=MAX_NEIGHBOURS)

    def close(self) -> None:
        with self._lock:
            if self._driver is not None:
                self._driver.close()
                self._driver = None
