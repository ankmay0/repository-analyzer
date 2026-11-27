"""Extract a code graph from a repository's Python sources and SQL migrations.

The result is plain records (files, functions, classes, imports, calls, HTTP endpoints,
tables and which functions read or write which tables) that ``graph_store`` loads into
Neo4j. This module is pure: it only looks at the ``SourceFile``s it is given, which come
from ``scan_repository``, so the scanner's allow-list (no secrets, lockfiles, symlinks
or oversized files) applies here too. No source text or string contents are kept: only
names, paths, line numbers, HTTP paths and table names.

What is recognised:

- Functions, classes and methods (``Class.method``) from Python's ``ast``. A function
  nested in another function, and any lambda, is folded into the enclosing function, as
  in the Ctags JSON index.
- Calls, resolved by name. A call to a name imported from a module inside the repository
  links to that module's function; a call through a module imported from outside the
  repository (``httpx.post``) is skipped. Any other name links when exactly one function
  in the repository has it, preferring one in the same file; otherwise it is recorded as
  ``unresolved`` on the caller so a trace can report the gap. ``obj.method()`` only
  considers methods. A repository function passed as an argument
  (``cached(key, ttl, load_rows)``) is recorded as a call ``via="reference"``.
- FastAPI endpoints: ``@router.get(...)`` style decorators on routers created with
  ``APIRouter(prefix=...)`` or ``FastAPI()`` in the same module.
- Tables declared by ``CREATE TABLE`` in ``.sql`` files, SQLAlchemy models
  (``__tablename__``, ``Table("name", ...)``) and Django models (``Meta.db_table`` or
  ``<app>_<model>``); table access from SQL string literals (``FROM``/``JOIN`` read,
  ``INSERT INTO``/``UPDATE ... SET``/``DELETE FROM`` write), SQLAlchemy
  (``select``/``session.query``/``session.get`` read; ``insert``/``update``/``delete``/
  ``session.add`` write) and Django managers (``Model.objects.filter`` read,
  ``Model.objects.create`` write, ...).
"""

from __future__ import annotations

import ast
import logging
import re
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import PurePosixPath

from repo_analyzer.domain import SourceFile

logger = logging.getLogger(__name__)

READS = "READS"
WRITES = "WRITES"

_HTTP_METHODS = frozenset({"get", "post", "put", "patch", "delete", "head", "options"})
_ROUTER_FACTORIES = frozenset({"APIRouter", "FastAPI"})

# SQL in string literals. ``DELETE FROM x`` is rewritten first so ``FROM x`` is not also
# counted as a read; ``SELECT ... FOR UPDATE`` stays a read because UPDATE needs ``SET``.
_SQL_START = re.compile(r"^\s*(?:SELECT|INSERT|UPDATE|DELETE|WITH)\b", re.IGNORECASE)
_IDENT = r"[\"`]?([A-Za-z_][\w.]*)"
_SQL_DELETE = re.compile(rf"\bDELETE\s+FROM\s+{_IDENT}", re.IGNORECASE)
_SQL_READ = re.compile(rf"\b(?:FROM|JOIN)\s+{_IDENT}", re.IGNORECASE)
_SQL_INSERT = re.compile(rf"\bINSERT\s+INTO\s+{_IDENT}", re.IGNORECASE)
_SQL_UPDATE = re.compile(rf"\bUPDATE\s+{_IDENT}[\"`]?\s+SET\b", re.IGNORECASE)
_CREATE_TABLE = re.compile(rf"\bCREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?{_IDENT}", re.IGNORECASE)
# Words a naive FROM/JOIN match can catch when no table list is declared to filter on.
_NOT_TABLES = frozenset({"select", "lateral", "unnest", "only", "values", "dual", "where"})

_SA_READ_FUNCTIONS = frozenset({"select"})
_SA_WRITE_FUNCTIONS = frozenset({"insert", "update", "delete"})
_SA_READ_METHODS = frozenset({"query", "get"})
_SA_ADD_METHODS = frozenset({"add", "add_all", "merge"})
_DJANGO_READS = frozenset(
    {"filter", "get", "all", "exclude", "values", "values_list", "count", "exists",
     "first", "last", "aggregate", "annotate", "order_by", "select_related"}
)  # fmt: skip
_DJANGO_WRITES = frozenset(
    {"create", "update", "delete", "bulk_create", "bulk_update", "get_or_create",
     "update_or_create"}
)  # fmt: skip


# --- records ------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FileRecord:
    path: str
    language: str


@dataclass(frozen=True, slots=True)
class FunctionRecord:
    id: str  # "path:qualname"
    name: str  # qualname: "func" or "Class.method"
    path: str
    line: int
    end_line: int | None
    unresolved: tuple[str, ...] = ()  # call names that matched several functions


@dataclass(frozen=True, slots=True)
class ClassRecord:
    id: str
    name: str
    path: str
    line: int
    end_line: int | None


@dataclass(frozen=True, slots=True)
class ImportRecord:
    path: str  # importing file
    module: str
    line: int
    internal: bool  # the module is a file in this repository


@dataclass(frozen=True, slots=True)
class CallRecord:
    caller: str  # function id
    callee: str  # function id
    line: int
    via: str  # "call" or "reference"


@dataclass(frozen=True, slots=True)
class EndpointRecord:
    method: str  # upper case
    path: str  # full route path, router prefix included
    handler: str  # function id
    file: str
    line: int


@dataclass(frozen=True, slots=True)
class TableRecord:
    name: str  # lower case, schema prefix dropped
    declared_in: str | None  # file that declares it, if any


@dataclass(frozen=True, slots=True)
class AccessRecord:
    function: str  # function id
    table: str
    kind: str  # READS or WRITES
    line: int


@dataclass(frozen=True, slots=True)
class GraphRecords:
    files: tuple[FileRecord, ...] = ()
    functions: tuple[FunctionRecord, ...] = ()
    classes: tuple[ClassRecord, ...] = ()
    imports: tuple[ImportRecord, ...] = ()
    calls: tuple[CallRecord, ...] = ()
    endpoints: tuple[EndpointRecord, ...] = ()
    tables: tuple[TableRecord, ...] = ()
    accesses: tuple[AccessRecord, ...] = ()


# --- per-file facts gathered in the first pass ------------------------------------

FunctionNode = ast.FunctionDef | ast.AsyncFunctionDef


@dataclass(slots=True)
class _Module:
    path: str
    tree: ast.Module
    package: str  # dotted package used for relative imports
    # local name -> (module, symbol); symbol None means the name is the module itself
    names: dict[str, tuple[str, str | None]] = field(default_factory=dict)
    functions: dict[str, str] = field(default_factory=dict)  # top-level name -> id
    methods: dict[str, dict[str, str]] = field(default_factory=dict)  # class -> name -> id
    routers: dict[str, str] = field(default_factory=dict)  # variable -> prefix


def _module_names(path: str) -> list[str]:
    """Dotted names a file can be imported as (``src/`` layouts get both)."""
    parts = list(PurePosixPath(path).with_suffix("").parts)
    if parts and parts[-1] == "__init__":
        parts.pop()
    if not parts:
        return []
    names = [".".join(parts)]
    if parts[0] == "src" and len(parts) > 1:
        names.append(".".join(parts[1:]))
    return names


def _package_of(path: str) -> str:
    names = _module_names(path)
    module = names[-1] if names else ""
    if PurePosixPath(path).name == "__init__.py":
        return module
    return module.rpartition(".")[0]


def _end(node: ast.AST) -> int | None:
    return getattr(node, "end_lineno", None)


def _const_str(node: ast.AST | None) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _call_name(node: ast.expr) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _is_test(path: str) -> bool:
    p = PurePosixPath(path)
    return (
        bool({"test", "tests"} & set(p.parts[:-1]))
        or p.name.startswith("test_")
        or p.name.endswith("_test.py")
        or p.name == "conftest.py"
    )


def _type_name(annotation: ast.expr | None) -> str | None:
    """The class an annotation names: ``X``, ``mod.X``, ``"X"``, ``X | None``, ``Optional[X]``."""
    if isinstance(annotation, ast.Name):
        return annotation.id
    if isinstance(annotation, ast.Attribute):
        return annotation.attr
    if isinstance(annotation, ast.Constant) and isinstance(annotation.value, str):
        text = annotation.value.split("[")[0].split("|")[0].strip()
        return text.rsplit(".", 1)[-1] or None
    if isinstance(annotation, ast.BinOp) and isinstance(annotation.op, ast.BitOr):
        left = annotation.left
        is_none = isinstance(left, ast.Constant) and left.value is None
        return _type_name(annotation.right if is_none else left)
    if isinstance(annotation, ast.Subscript) and _type_name(annotation.value) in (
        "Optional",
        "Annotated",
    ):
        inner = annotation.slice
        return _type_name(inner.elts[0] if isinstance(inner, ast.Tuple) else inner)
    return None


def _parameters(node: FunctionNode) -> list[ast.arg]:
    a = node.args
    return [*a.posonlyargs, *a.args, *a.kwonlyargs, *(x for x in (a.vararg, a.kwarg) if x)]


def _local_names(node: FunctionNode) -> set[str]:
    """Parameters and names bound inside the function (they shadow repo functions)."""
    names = {p.arg for p in _parameters(node)}
    for child in ast.walk(node):
        if isinstance(child, ast.Name) and isinstance(child.ctx, ast.Store):
            names.add(child.id)
        elif isinstance(child, ast.arg):
            names.add(child.arg)
        elif isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef) and child is not node:
            names.add(child.name)
    return names


def _local_types(node: FunctionNode) -> dict[str, str]:
    """``name -> class name`` from parameter annotations and ``x = Cls(...)``."""
    types = {p.arg: t for p in _parameters(node) if (t := _type_name(p.annotation))}
    for child in ast.walk(node):
        if (
            isinstance(child, ast.Assign)
            and len(child.targets) == 1
            and isinstance(child.targets[0], ast.Name)
            and isinstance(child.value, ast.Call)
            and isinstance(child.value.func, ast.Name)
        ):
            types[child.targets[0].id] = child.value.func.id
        elif (
            isinstance(child, ast.AnnAssign)
            and isinstance(child.target, ast.Name)
            and (t := _type_name(child.annotation))
        ):
            types[child.target.id] = t
    return types


def _join_route(prefix: str, path: str) -> str:
    joined = prefix.rstrip("/") + ("/" + path.lstrip("/") if path.strip("/") else "")
    return joined or "/"


def _table_name(raw: str) -> str:
    return raw.split(".")[-1].strip('"`').lower()


# --- the extractor ---------------------------------------------------------------


class _Extractor:
    def __init__(self, files: list[SourceFile]) -> None:
        self.files = files
        self.modules: dict[str, _Module] = {}  # path -> module
        self.module_paths: dict[str, str] = {}  # dotted name -> path
        self.internal_prefixes: set[str] = set()
        self.functions: dict[str, FunctionRecord] = {}
        self.classes: list[ClassRecord] = []
        self.class_names: set[str] = set()
        self.top_by_name: dict[str, list[str]] = defaultdict(list)
        self.methods_by_name: dict[str, list[str]] = defaultdict(list)
        self.class_methods: dict[str, list[dict[str, str]]] = defaultdict(list)  # name -> methods
        self.interfaces: set[str] = set()  # Protocol / ABC class names
        self.attr_types: dict[tuple[str, str], dict[str, str]] = {}  # (path, class) -> attr -> type
        # (path, class or None, name) -> SQL text held in a module or class constant
        self.sql_constants: dict[tuple[str, str | None, str], str] = {}
        self.model_tables: dict[str, str] = {}  # ORM class or Table variable -> table
        self.declared: dict[str, str] = {}  # table -> declaring file

    # first pass: parse, index definitions, imports, routers and declared tables

    def index(self) -> None:
        for file in self.files:
            if file.language == "sql":
                for match in _CREATE_TABLE.finditer(file.text):
                    self.declared.setdefault(_table_name(match.group(1)), file.path)
            if file.language != "python":
                continue
            try:
                tree = ast.parse(file.text, filename=file.path)
            except (SyntaxError, ValueError) as exc:
                logger.info("Graph: skipping %s: %s", file.path, exc)
                continue
            module = _Module(file.path, tree, _package_of(file.path))
            self.modules[file.path] = module
            for name in _module_names(file.path):
                self.module_paths[name] = file.path
                parts = name.split(".")
                self.internal_prefixes.update(".".join(parts[:i]) for i in range(1, len(parts)))
        for module in self.modules.values():
            self._index_definitions(module)
        for module in self.modules.values():
            self._index_imports(module)

    def _is_internal(self, module: str) -> bool:
        return module in self.module_paths or module in self.internal_prefixes

    def _add_function(self, module: _Module, node: FunctionNode, qualname: str) -> str:
        fid = f"{module.path}:{qualname}"
        self.functions[fid] = FunctionRecord(fid, qualname, module.path, node.lineno, _end(node))
        return fid

    def _index_definitions(self, module: _Module) -> None:
        for stmt in module.tree.body:
            if isinstance(stmt, ast.FunctionDef | ast.AsyncFunctionDef):
                fid = self._add_function(module, stmt, stmt.name)
                module.functions[stmt.name] = fid
                self.top_by_name[stmt.name].append(fid)
            elif isinstance(stmt, ast.ClassDef):
                self._index_class(module, stmt)
            elif isinstance(stmt, ast.Assign) and len(stmt.targets) == 1:
                self._index_assignment(module, stmt)
            self._index_sql_constant(module.path, None, stmt)

    def _index_sql_constant(self, path: str, cls: str | None, stmt: ast.stmt) -> None:
        """``_SQL = "SELECT ..."`` at module or class level, used later as ``_SQL`` or
        ``self._SQL``."""
        if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1:
            target, value = stmt.targets[0], stmt.value
        elif isinstance(stmt, ast.AnnAssign) and stmt.value is not None:
            target, value = stmt.target, stmt.value
        else:
            return
        if isinstance(target, ast.Name) and (sql := self._sql_text(value)) is not None:
            self.sql_constants[(path, cls, target.id)] = sql

    def _index_class(self, module: _Module, node: ast.ClassDef) -> None:
        cid = f"{module.path}:{node.name}"
        self.classes.append(ClassRecord(cid, node.name, module.path, node.lineno, _end(node)))
        self.class_names.add(node.name)
        methods = module.methods.setdefault(node.name, {})
        self.class_methods[node.name].append(methods)
        if any(_type_name(base) in ("Protocol", "ABC") for base in node.bases):
            self.interfaces.add(node.name)
        attrs = self.attr_types.setdefault((module.path, node.name), {})
        for stmt in node.body:
            self._index_sql_constant(module.path, node.name, stmt)
            if (
                isinstance(stmt, ast.AnnAssign)
                and isinstance(stmt.target, ast.Name)
                and (t := _type_name(stmt.annotation))
            ):
                attrs[stmt.target.id] = t  # dataclass-style field
            if isinstance(stmt, ast.FunctionDef | ast.AsyncFunctionDef):
                fid = self._add_function(module, stmt, f"{node.name}.{stmt.name}")
                methods[stmt.name] = fid
                self.methods_by_name[stmt.name].append(fid)
                self._index_attribute_types(stmt, attrs)
        table = self._orm_table(module, node)
        if table:
            self.model_tables[node.name] = table
            self.declared.setdefault(table, module.path)

    @staticmethod
    def _index_attribute_types(method: FunctionNode, attrs: dict[str, str]) -> None:
        """``self.x = param`` (param annotated) and ``self.x: T = ...`` give ``x`` a type."""
        params = _local_types(method)
        for child in ast.walk(method):
            if isinstance(child, ast.Assign):
                targets, value = child.targets, child.value
            elif isinstance(child, ast.AnnAssign):
                targets, value = [child.target], None
            else:
                continue
            for target in targets:
                if not (
                    isinstance(target, ast.Attribute)
                    and isinstance(target.value, ast.Name)
                    and target.value.id == "self"
                ):
                    continue
                if isinstance(child, ast.AnnAssign) and (t := _type_name(child.annotation)):
                    attrs[target.attr] = t
                elif isinstance(value, ast.Name) and value.id in params:
                    attrs.setdefault(target.attr, params[value.id])
                elif isinstance(value, ast.Call) and isinstance(value.func, ast.Name):
                    attrs.setdefault(target.attr, value.func.id)

    def _orm_table(self, module: _Module, node: ast.ClassDef) -> str | None:
        for stmt in node.body:  # SQLAlchemy declarative
            if (
                isinstance(stmt, ast.Assign)
                and any(isinstance(t, ast.Name) and t.id == "__tablename__" for t in stmt.targets)
                and (value := _const_str(stmt.value))
            ):
                return value.lower()
        is_django = any(
            isinstance(base, ast.Attribute)
            and base.attr == "Model"
            and isinstance(base.value, ast.Name)
            and base.value.id == "models"
            for base in node.bases
        )
        if not is_django:
            return None
        for stmt in node.body:
            if isinstance(stmt, ast.ClassDef) and stmt.name == "Meta":
                for item in stmt.body:
                    if not isinstance(item, ast.Assign):
                        continue
                    targets = {t.id for t in item.targets if isinstance(t, ast.Name)}
                    if "abstract" in targets and getattr(item.value, "value", False) is True:
                        return None
                    if "db_table" in targets and (value := _const_str(item.value)):
                        return value.lower()
        path = PurePosixPath(module.path)
        app = path.parent.parent.name if path.parent.name == "models" else path.parent.name
        return f"{app}_{node.name.lower()}" if app else node.name.lower()

    def _index_assignment(self, module: _Module, stmt: ast.Assign) -> None:
        target = stmt.targets[0]
        if not isinstance(target, ast.Name) or not isinstance(stmt.value, ast.Call):
            return
        factory = _call_name(stmt.value.func)
        if factory in _ROUTER_FACTORIES:
            prefix = next(
                (_const_str(k.value) for k in stmt.value.keywords if k.arg == "prefix"), None
            )
            module.routers[target.id] = prefix or ""
        elif factory == "Table" and stmt.value.args:  # SQLAlchemy Core
            name = _const_str(stmt.value.args[0])
            if name:
                self.model_tables[target.id] = name.lower()
                self.declared.setdefault(name.lower(), module.path)

    def _index_imports(self, module: _Module) -> None:
        for stmt in ast.walk(module.tree):
            if isinstance(stmt, ast.Import):
                for alias in stmt.names:
                    local = alias.asname or alias.name.split(".")[0]
                    target = alias.name if alias.asname else alias.name.split(".")[0]
                    module.names[local] = (target, None)
            elif isinstance(stmt, ast.ImportFrom):
                base = self._absolute(module, stmt.module, stmt.level)
                for alias in stmt.names:
                    if alias.name == "*":
                        continue
                    local = alias.asname or alias.name
                    submodule = f"{base}.{alias.name}" if base else alias.name
                    if submodule in self.module_paths:
                        module.names[local] = (submodule, None)
                    else:
                        module.names[local] = (base, alias.name)

    @staticmethod
    def _absolute(module: _Module, name: str | None, level: int) -> str:
        if level == 0:
            return name or ""
        parts = module.package.split(".") if module.package else []
        base = parts[: len(parts) - (level - 1)] if level > 1 else parts
        return ".".join([*base, name] if name else base)

    def import_records(self) -> list[ImportRecord]:
        seen: dict[tuple[str, str], ImportRecord] = {}
        for module in self.modules.values():
            for stmt in ast.walk(module.tree):
                if isinstance(stmt, ast.Import):
                    modules = [alias.name for alias in stmt.names]
                elif isinstance(stmt, ast.ImportFrom):
                    modules = [self._absolute(module, stmt.module, stmt.level)]
                else:
                    continue
                for name in modules:
                    if name and (module.path, name) not in seen:
                        seen[(module.path, name)] = ImportRecord(
                            module.path, name, stmt.lineno, self._is_internal(name)
                        )
        return list(seen.values())

    # second pass: endpoints, calls and table access per function

    def endpoints(self) -> list[EndpointRecord]:
        found: dict[tuple[str, str], EndpointRecord] = {}
        for module in self.modules.values():
            for stmt in module.tree.body:
                if not isinstance(stmt, ast.FunctionDef | ast.AsyncFunctionDef):
                    continue
                for method, route in self._routes(module, stmt):
                    key = (method, route)
                    if key not in found:
                        found[key] = EndpointRecord(
                            method, route, module.functions[stmt.name], module.path, stmt.lineno
                        )
        return list(found.values())

    @staticmethod
    def _routes(module: _Module, node: FunctionNode) -> list[tuple[str, str]]:
        routes = []
        for decorator in node.decorator_list:
            if not (
                isinstance(decorator, ast.Call)
                and isinstance(decorator.func, ast.Attribute)
                and isinstance(decorator.func.value, ast.Name)
                and decorator.func.value.id in module.routers
            ):
                continue
            prefix = module.routers[decorator.func.value.id]
            attr = decorator.func.attr
            path = _const_str(decorator.args[0]) if decorator.args else None
            keywords = {k.arg: k.value for k in decorator.keywords if k.arg}
            if path is None:
                path = _const_str(keywords.get("path"))
            if path is None:
                continue
            if attr in _HTTP_METHODS:
                methods = [attr.upper()]
            elif attr == "api_route":
                listed = keywords.get("methods")
                methods = (
                    [m.upper() for e in listed.elts if (m := _const_str(e))]
                    if isinstance(listed, ast.List | ast.Tuple)
                    else ["GET"]
                )
            else:
                continue
            routes += [(m, _join_route(prefix, path)) for m in methods]
        return routes

    def function_nodes(self) -> Iterable[tuple[_Module, str | None, FunctionNode, str]]:
        for module in self.modules.values():
            for stmt in module.tree.body:
                if isinstance(stmt, ast.FunctionDef | ast.AsyncFunctionDef):
                    yield module, None, stmt, module.functions[stmt.name]
                elif isinstance(stmt, ast.ClassDef):
                    for item in stmt.body:
                        if isinstance(item, ast.FunctionDef | ast.AsyncFunctionDef):
                            yield module, stmt.name, item, module.methods[stmt.name][item.name]

    def _pick(self, candidates: list[str], path: str) -> tuple[str | None, bool]:
        """``(function id, ambiguous)`` under the unique-name / same-file rule."""
        if not _is_test(path):  # production code never calls into test files
            candidates = [c for c in candidates if not _is_test(self.functions[c].path)]
        if len(candidates) == 1:
            return candidates[0], False
        local = [c for c in candidates if self.functions[c].path == path]
        if len(local) == 1:
            return local[0], False
        return None, len(candidates) > 1

    def _imported_function(self, module: str, symbol: str) -> str | None:
        path = self.module_paths.get(module)
        return self.modules[path].functions.get(symbol) if path else None

    def resolve_name(self, module: _Module, name: str) -> tuple[str | None, bool]:
        if name in module.names:
            source, symbol = module.names[name]
            if symbol is None or not self._is_internal(source):
                return None, False  # a module object, or something from outside the repo
            return self._imported_function(source, symbol), False
        if name in module.functions:
            return module.functions[name], False
        if name in self.class_names:
            return None, False
        return self._pick(self.top_by_name.get(name, []), module.path)

    def _typed_method(self, type_name: str, method: str) -> str | None:
        """``method`` of the class ``type_name``; for a Protocol/ABC, its one implementation."""
        owners = [m[method] for m in self.class_methods.get(type_name, []) if method in m]
        if len(owners) != 1:
            return None  # not a repo class, no such method, or the class name is ambiguous
        if type_name not in self.interfaces:
            return owners[0]
        # Structural match: classes that define every method the interface declares.
        required = set().union(*(m.keys() for m in self.class_methods[type_name])) - {"__init__"}
        implementers = [
            methods[method]
            for name, listed in self.class_methods.items()
            if name not in self.interfaces
            for methods in listed
            if method in methods
            and required <= methods.keys()
            and not _is_test(self.functions[methods[method]].path)
        ]
        if len(implementers) == 1:
            return implementers[0]
        implementations = [
            c
            for c in self.methods_by_name.get(method, [])
            if c not in owners
            and self.functions[c].name.split(".")[0] not in self.interfaces
            and not _is_test(self.functions[c].path)
        ]
        # ponytail: one implementation is linked; with several, the interface method stands in.
        return implementations[0] if len(implementations) == 1 else owners[0]

    def resolve_call(
        self,
        module: _Module,
        cls: str | None,
        func: ast.expr,
        local_names: frozenset[str] = frozenset(),
        local_types: dict[str, str] | None = None,
    ) -> tuple[str | None, bool]:
        if isinstance(func, ast.Name):
            if func.id in local_names:
                return None, False  # a parameter or local variable, not a repo function
            return self.resolve_name(module, func.id)
        if not isinstance(func, ast.Attribute):
            return None, False
        base = func.value
        receiver: str | None = None
        if (
            isinstance(base, ast.Attribute)
            and isinstance(base.value, ast.Name)
            and base.value.id == "self"
            and cls is not None
        ):
            receiver = self.attr_types.get((module.path, cls), {}).get(base.attr)
        elif isinstance(base, ast.Name) and local_types and base.id in local_types:
            receiver = local_types[base.id]
        if receiver and (typed := self._typed_method(receiver, func.attr)):
            return typed, False
        if isinstance(base, ast.Name):
            if base.id in module.names:
                source, symbol = module.names[base.id]
                target = source if symbol is None else f"{source}.{symbol}"
                if not self._is_internal(source):
                    return None, False  # e.g. httpx.post
                if symbol is None or target in self.module_paths:
                    return self._imported_function(target, func.attr), False
            if base.id in ("self", "cls") and cls is not None:
                own = module.methods.get(cls, {}).get(func.attr)
                if own:
                    return own, False
        return self._pick(self.methods_by_name.get(func.attr, []), module.path)

    def analyse(
        self, module: _Module, cls: str | None, node: FunctionNode, fid: str
    ) -> tuple[list[CallRecord], list[AccessRecord], set[str]]:
        calls: dict[tuple[str, str], CallRecord] = {}
        accesses: list[AccessRecord] = []
        unresolved: set[str] = set()
        docstring = ast.get_docstring(node, clean=False)
        body = node.body[1:] if docstring is not None else node.body

        def add_call(callee: str, line: int, via: str) -> None:
            key = (callee, via)
            if key not in calls or calls[key].line > line:
                calls[key] = CallRecord(fid, callee, line, via)

        local_names = frozenset(_local_names(node))
        local_types = _local_types(node)
        for stmt in body:
            for child in ast.walk(stmt):
                if isinstance(child, ast.Call):
                    callee, ambiguous = self.resolve_call(
                        module, cls, child.func, local_names, local_types
                    )
                    if callee:
                        add_call(callee, child.lineno, "call")
                    elif ambiguous and (name := _call_name(child.func)):
                        unresolved.add(name)
                    for argument in [*child.args, *(k.value for k in child.keywords)]:
                        if isinstance(argument, ast.Name) and argument.id not in local_names:
                            ref, _ = self.resolve_name(module, argument.id)
                            if ref:
                                add_call(ref, child.lineno, "reference")
                    accesses += self._orm_access(fid, child)
                sql = self._sql_text(child)
                if sql is None:
                    sql = self._sql_constant(module, cls, child, local_names)
                if sql is not None and isinstance(child, ast.expr):
                    accesses += self._sql_access(fid, sql, child.lineno)
        return list(calls.values()), accesses, unresolved

    def _sql_constant(
        self, module: _Module, cls: str | None, node: ast.AST, local_names: frozenset[str]
    ) -> str | None:
        """SQL from a constant the function refers to: ``_SQL``, ``self._SQL``, ``Cls._SQL``."""
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            if node.id not in local_names:
                return self.sql_constants.get((module.path, None, node.id))
        elif isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            owner = cls if node.value.id in ("self", "cls") else node.value.id
            return self.sql_constants.get((module.path, owner, node.attr))
        return None

    @staticmethod
    def _sql_text(node: ast.AST) -> str | None:
        text: str | None
        if isinstance(node, ast.JoinedStr):
            text = "".join(
                str(part.value) if isinstance(part, ast.Constant) else " ? " for part in node.values
            )
        else:
            text = _const_str(node)
        return text if text and _SQL_START.match(text) else None

    def _sql_access(self, fid: str, text: str, line: int) -> list[AccessRecord]:
        found: list[AccessRecord] = []
        for match in _SQL_DELETE.finditer(text):
            found.append(AccessRecord(fid, _table_name(match.group(1)), WRITES, line))
        reads_text = _SQL_DELETE.sub(" ", text)
        for pattern in (_SQL_INSERT, _SQL_UPDATE):
            for match in pattern.finditer(reads_text):
                found.append(AccessRecord(fid, _table_name(match.group(1)), WRITES, line))
        for match in _SQL_READ.finditer(reads_text):
            found.append(AccessRecord(fid, _table_name(match.group(1)), READS, line))
        return found

    def _model(self, node: ast.expr | None) -> str | None:
        if isinstance(node, ast.Name):
            return self.model_tables.get(node.id)
        return None

    def _orm_access(self, fid: str, call: ast.Call) -> list[AccessRecord]:
        name = _call_name(call.func)
        first = call.args[0] if call.args else None
        line = call.lineno
        if name in _SA_READ_FUNCTIONS and (table := self._model(first)):
            return [AccessRecord(fid, table, READS, line)]
        if name in _SA_WRITE_FUNCTIONS and (table := self._model(first)):
            return [AccessRecord(fid, table, WRITES, line)]
        if isinstance(call.func, ast.Attribute):
            if name in _SA_READ_METHODS and (table := self._model(first)):
                return [AccessRecord(fid, table, READS, line)]
            if (
                name in _SA_ADD_METHODS
                and isinstance(first, ast.Call)
                and (table := self._model(first.func))
            ):
                return [AccessRecord(fid, table, WRITES, line)]
            manager = call.func.value
            if (
                isinstance(manager, ast.Attribute)
                and manager.attr == "objects"
                and (table := self._model(manager.value))
            ):
                if name in _DJANGO_WRITES:
                    return [AccessRecord(fid, table, WRITES, line)]
                if name in _DJANGO_READS:
                    return [AccessRecord(fid, table, READS, line)]
        return []


def extract_graph(files: Iterable[SourceFile]) -> GraphRecords:
    """Build the graph records for one repository from the scanner's files."""
    file_list = list(files)
    extractor = _Extractor(file_list)
    extractor.index()

    calls: list[CallRecord] = []
    raw_accesses: list[AccessRecord] = []
    unresolved: dict[str, set[str]] = {}
    for module, cls, node, fid in extractor.function_nodes():
        found_calls, found_accesses, ambiguous = extractor.analyse(module, cls, node, fid)
        calls += found_calls
        raw_accesses += found_accesses
        unresolved[fid] = ambiguous

    declared = extractor.declared
    accesses: dict[tuple[str, str, str], AccessRecord] = {}
    for access in raw_accesses:
        keep = access.table in declared if declared else access.table not in _NOT_TABLES
        key = (access.function, access.table, access.kind)
        if keep and (key not in accesses or accesses[key].line > access.line):
            accesses[key] = access

    tables = {name: TableRecord(name, path) for name, path in declared.items()}
    for access in accesses.values():
        tables.setdefault(access.table, TableRecord(access.table, None))

    functions = tuple(
        FunctionRecord(f.id, f.name, f.path, f.line, f.end_line, tuple(sorted(unresolved[f.id])))
        if unresolved.get(f.id)
        else f
        for f in extractor.functions.values()
    )
    return GraphRecords(
        files=tuple(FileRecord(f.path, f.language) for f in file_list),
        functions=functions,
        classes=tuple(extractor.classes),
        imports=tuple(extractor.import_records()),
        calls=tuple(calls),
        endpoints=tuple(extractor.endpoints()),
        tables=tuple(sorted(tables.values(), key=lambda t: t.name)),
        accesses=tuple(sorted(accesses.values(), key=lambda a: (a.function, a.line, a.table))),
    )
