"""FalkorDB connection: a server (Docker / FalkorDB Cloud) or embedded FalkorDBLite."""

import os
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

GRAPH_NAME = os.getenv("FALKORDB_GRAPH", "supplychain")
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
    )


def graph(name: str = GRAPH_NAME):
    return _db().select_graph(name)


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
