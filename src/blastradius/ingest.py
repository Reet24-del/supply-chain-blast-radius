"""Load real npm lockfiles into FalkorDB, then add OSV advisories and npm maintainers.

    uv run blast-ingest                      # repos listed in data/repos.json
    uv run blast-ingest --reset              # drop and rebuild the graph
    uv run blast-ingest --skip-maintainers   # skip one npm registry request per package

Downloads are cached in data/cache/, so re-runs are fast and work offline.
"""

import argparse
import asyncio
import json
import re
import time
from pathlib import Path

import httpx

from . import queries as Q
from .db import GRAPH_NAME, ensure_indexes, graph, rows
from .lockfile import ParsedLock, parse_lockfile

ROOT = Path(__file__).resolve().parents[2]
CACHE = ROOT / "data" / "cache"
BATCH = 1000
CONCURRENCY = 16
HEADERS = {"User-Agent": "supply-chain-blast-radius (hackathon project)"}


def chunks(items: list, n: int = BATCH):
    for i in range(0, len(items), n):
        yield items[i : i + n]


def _safe(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", name)


def fetch_lockfile(client: httpx.Client, repo: dict) -> dict:
    path = CACHE / "lockfiles" / f"{_safe(repo['name'])}.json"
    if not path.exists():
        ref = repo.get("ref", "HEAD")
        lockfile = repo.get("lockfile", "package-lock.json")
        resp = client.get(f"https://raw.githubusercontent.com/{repo['github']}/{ref}/{lockfile}")
        resp.raise_for_status()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(resp.content)
    return json.loads(path.read_text())


def load_repo(g, repo: dict, parsed: ParsedLock) -> None:
    url = f"https://github.com/{repo['github']}"
    g.query(Q.UPSERT_REPO, {"name": repo["name"], "url": url, "team": repo["team"]})
    for batch in chunks(list(parsed.versions.values())):
        g.query(Q.UPSERT_VERSIONS, {"rows": batch})
    edges = [{"src": s, "dst": d, "range": r} for (s, d), r in parsed.edges.items()]
    for batch in chunks(edges):
        g.query(Q.UPSERT_DEPENDENCIES, {"rows": batch})
    for batch in chunks(list(parsed.direct.values())):
        g.query(Q.UPSERT_DIRECT, {"repo": repo["name"], "rows": batch})


async def get_json(client, sem, url: str, cache: Path, keep=None) -> dict:
    """GET JSON with an on-disk cache and retries. `keep` trims what gets cached."""
    if cache.exists():
        return json.loads(cache.read_text())
    for attempt in range(4):
        try:
            async with sem:
                resp = await client.get(url)
            if resp.status_code == 404:
                data = {}
                break
            resp.raise_for_status()
            data = resp.json()
            break
        except (httpx.TransportError, httpx.HTTPStatusError):
            if attempt == 3:
                raise
            await asyncio.sleep(2**attempt)
    if keep:
        data = keep(data)
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(data))
    return data


async def gather_json(client, requests: list[tuple[str, Path]], keep=None) -> list[dict]:
    sem = asyncio.Semaphore(CONCURRENCY)
    results = await asyncio.gather(
        *(get_json(client, sem, url, cache, keep) for url, cache in requests), return_exceptions=True
    )
    failed = sum(isinstance(r, Exception) for r in results)
    if failed:
        print(f"  warning: {failed} requests failed; re-run to retry them")
    return [{} if isinstance(r, Exception) else r for r in results]


def advisory_row(v: dict) -> dict:
    npm = [a for a in v.get("affected", []) if a.get("package", {}).get("ecosystem") == "npm"]
    fixed = sorted(
        {
            f"{a['package']['name']}@{e['fixed']}"
            for a in npm
            for r in a.get("ranges", [])
            for e in r.get("events", [])
            if "fixed" in e
        }
    )
    severity = (v.get("database_specific") or {}).get("severity")
    if not severity:
        severity = "MALICIOUS" if v["id"].startswith("MAL-") else "UNKNOWN"
    return {
        "id": v["id"],
        "summary": (v.get("summary") or v.get("details") or "")[:300],
        "severity": severity.upper(),
        "published": v.get("published", ""),
        "aliases": v.get("aliases", []),
        "fixed": fixed,
        "url": f"https://osv.dev/vulnerability/{v['id']}",
    }


async def enrich_osv(g, watch: list[str]) -> None:
    versions = rows(Q.ALL_VERSIONS)
    hits: dict[str, set[str]] = {}
    async with httpx.AsyncClient(timeout=60, headers=HEADERS) as client:
        for batch in chunks(versions):
            queries = [{"package": {"ecosystem": "npm", "name": v["name"]}, "version": v["version"]} for v in batch]
            resp = await client.post("https://api.osv.dev/v1/querybatch", json={"queries": queries})
            resp.raise_for_status()
            for v, result in zip(batch, resp.json()["results"]):
                for vuln in result.get("vulns", []):
                    hits.setdefault(vuln["id"], set()).add(v["id"])
        ids = sorted(set(hits) | set(watch))
        docs = await gather_json(
            client, [(f"https://api.osv.dev/v1/vulns/{i}", CACHE / "osv" / f"{_safe(i)}.json") for i in ids]
        )
    advisories = [advisory_row(d) for d in docs if d]
    for batch in chunks(advisories):
        g.query(Q.UPSERT_ADVISORIES, {"rows": batch})
    affects = [{"advisory": a, "version": v} for a, vs in hits.items() for v in vs]
    for batch in chunks(affects):
        g.query(Q.UPSERT_AFFECTS, {"rows": batch})
    watched = [
        {"advisory": d["id"], "name": a["package"]["name"], "version": ver}
        for d in docs
        if d and d["id"] in watch
        for a in d.get("affected", [])
        if a.get("package", {}).get("ecosystem") == "npm"
        for ver in a.get("versions", [])
    ]
    for batch in chunks(watched):
        g.query(Q.UPSERT_AFFECTED_VERSIONS, {"rows": batch})
    print(f"  osv: {len(advisories)} advisories on {len(affects)} installed versions, {len(watched)} watched versions")


async def enrich_maintainers(g) -> None:
    names = [r["name"] for r in rows(Q.ALL_PACKAGES)]

    def keep(doc: dict) -> dict:  # cache npm usernames only, never emails
        return {"maintainers": sorted({m["name"] for m in doc.get("maintainers") or [] if m.get("name")})}

    async with httpx.AsyncClient(timeout=30, headers=HEADERS) as client:
        docs = await gather_json(
            client,
            [(f"https://registry.npmjs.org/{n}/latest", CACHE / "npm" / f"{_safe(n)}.json") for n in names],
            keep,
        )
    batch_rows = [{"package": n, "maintainers": d.get("maintainers", [])} for n, d in zip(names, docs)]
    for batch in chunks(batch_rows, 500):
        g.query(Q.UPSERT_MAINTAINERS, {"rows": batch})
    print(f"  npm: maintainers for {sum(bool(r['maintainers']) for r in batch_rows)}/{len(names)} packages")


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="Build the supply-chain graph in FalkorDB.")
    ap.add_argument("--repos", type=Path, default=ROOT / "data" / "repos.json")
    ap.add_argument("--reset", action="store_true", help="drop the graph before loading")
    ap.add_argument("--skip-osv", action="store_true", help="don't fetch advisories from OSV.dev")
    ap.add_argument("--skip-maintainers", action="store_true", help="don't fetch maintainers from npm")
    args = ap.parse_args(argv)

    cfg = json.loads(args.repos.read_text())
    g = graph()
    if args.reset:
        try:
            g.delete()
        except Exception:
            pass  # nothing to drop yet
        g = graph()
    ensure_indexes(g)

    started = time.perf_counter()
    for team in cfg["teams"]:
        g.query(Q.UPSERT_TEAM, team)
    print("lockfiles:")
    with httpx.Client(timeout=120, headers=HEADERS, follow_redirects=True) as client:
        for repo in cfg["repos"]:
            t = time.perf_counter()
            try:
                parsed = parse_lockfile(fetch_lockfile(client, repo))
            except (httpx.HTTPError, ValueError) as e:
                print(f"  {repo['name']:<24} skipped: {e}")
                continue
            load_repo(g, repo, parsed)
            print(
                f"  {repo['name']:<24} {len(parsed.direct):>4} direct {len(parsed.versions):>5} versions"
                f" {len(parsed.edges):>6} edges {time.perf_counter() - t:5.1f}s"
            )
    if not args.skip_osv:
        asyncio.run(enrich_osv(g, cfg.get("watch_advisories", [])))
    if not args.skip_maintainers:
        asyncio.run(enrich_maintainers(g))

    nodes = ", ".join(f"{r['n']} {r['label']}" for r in rows(Q.COUNT_NODES))
    edges = ", ".join(f"{r['n']} {r['type']}" for r in rows(Q.COUNT_EDGES))
    print(f"graph '{GRAPH_NAME}': {nodes}\n  {edges}\ndone in {time.perf_counter() - started:.0f}s")


if __name__ == "__main__":
    main()
