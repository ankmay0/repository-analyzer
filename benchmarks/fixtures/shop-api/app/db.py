"""PostgreSQL connection pool shared by every request."""

from collections.abc import Iterator
from contextlib import contextmanager

from psycopg import Connection
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from app.config import DATABASE_URL

pool = ConnectionPool(DATABASE_URL, min_size=1, max_size=10, open=False)


@contextmanager
def connection() -> Iterator[Connection]:
    """Borrow a connection; the block runs in one transaction (commit on success)."""
    with pool.connection() as conn:
        conn.row_factory = dict_row
        yield conn
