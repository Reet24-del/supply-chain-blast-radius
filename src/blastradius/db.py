"""FalkorDB connection: a server (Docker / FalkorDB Cloud) or embedded FalkorDBLite.

Each tenant (a company and its repos) gets its own graph on the same instance, named
`<FALKORDB_GRAPH>:<tenant>`. Queries go to the graph selected with `use_tenant`, so one
tenant's dependencies and memory never mix with another's.
"""

import os
import re
from contextlib import contextmanager
from contextvars import ContextVar
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

GRAPH_NAME = os.getenv("FALKORDB_GRAPH", "supplychain")
TENANT_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")
_current_graph: ContextVar[str] = ContextVar("current_graph", default=GRAPH_NAME)
# Server-side limit for agent reads, so one bad traversal can't stall the agent loop.
READ_TIMEOUT_MS = int(os.getenv("FALKORDB_READ_TIMEOUT_MS", "10000"))

INDEXES = [
    ("Repo", "name"),
    ("Package", "name"),
    ("Version", "id"),
    ("Maintainer", "name"),
    ("Advisory", "id"),
    ("Team", "name"),
]


@lru_cache(maxsize=1)
def _db():
    lite_path = os.getenv("FALKORDB_LITE_PATH")
    if lite_path:
        from redislite.falkordb_client import FalkorDB as EmbeddedFalkorDB

        Path(lite_path).parent.mkdir(parents=True, exist_ok=True)
        return EmbeddedFalkorDB(lite_path)

    from falkordb import FalkorDB

    return FalkorDB(
        host=os.getenv("FALKORDB_HOST", "localhost"),
        port=int(os.getenv("FALKORDB_PORT", "6379")),
        username=os.getenv("FALKORDB_USERNAME") or None,
        password=os.getenv("FALKORDB_PASSWORD") or None,
        ssl=os.getenv("FALKORDB_TLS", "").lower() in ("1", "true", "yes"),  # paid Cloud tiers; Free has no TLS
    )


def graph(name: str | None = None):
    return _db().select_graph(name or _current_graph.get())


def tenants() -> list[str]:
    """Tenants that have a graph on this instance (the default graph is not listed)."""
    prefix = f"{GRAPH_NAME}:"
    return sorted(name.removeprefix(prefix) for name in _db().list_graphs() if name.startswith(prefix))


def graph_name(tenant: str = "", *, must_exist: bool = True) -> str:
    """The graph holding a tenant's data; "" is the default graph. Raises ValueError if invalid."""
    if not tenant:
        return GRAPH_NAME
    if not TENANT_RE.match(tenant):
        raise ValueError(f"'{tenant}' is not a valid tenant name (lowercase letters, digits and dashes)")
    if must_exist and tenant not in tenants():
        raise ValueError(f"No tenant named '{tenant}'. Known tenants: {', '.join(tenants()) or 'none'}")
    return f"{GRAPH_NAME}:{tenant}"


@contextmanager
def _use_graph(name: str):
    token = _current_graph.set(name)
    try:
        yield name
    finally:
        _current_graph.reset(token)


def use_tenant(tenant: str = "", *, must_exist: bool = True):
    """Point graph() and rows() at one tenant's graph inside a `with` block.

    Validates when called, so a bad tenant fails before the block starts.
    """
    return _use_graph(graph_name(tenant, must_exist=must_exist))


def ensure_indexes(g) -> None:
    for label, prop in INDEXES:
        try:
            g.query(f"CREATE INDEX FOR (n:{label}) ON (n.{prop})")
        except Exception as e:  # FalkorDB raises if the index already exists
            if "already indexed" not in str(e):
                raise


def rows(query: str, params: dict | None = None, *, write: bool = False) -> list[dict]:
    """Run a query and return rows as dicts keyed by column name."""
    g = graph()
    if write:
        res = g.query(query, params or {})
    else:
        res = g.ro_query(query, params or {}, timeout=READ_TIMEOUT_MS)
    cols = [h[1] for h in res.header]
    return [dict(zip(cols, row)) for row in res.result_set]
