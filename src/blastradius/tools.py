"""The agent's fixed tool catalogue.

Each tool resolves names against the graph first, runs parameterised Cypher from
queries.py, and returns the dependency path with every answer. Every result has an
`outcome`:

  answered              found in the graph, with paths
  not_in_graph          the entity exists but nothing connects to it; not proof of safety
  unknown_<kind>        the name didn't resolve, so no traversal ran (suggestions included)
  answered_from_memory  recalled Decisions, with who decided and when
  recorded / drafted / opened   a write: a Decision node, an outbox draft, a GitHub issue
"""

import functools
import inspect
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter

import httpx
import semantic_version as sv

from . import queries as Q
from .db import rows, use_tenant

ROOT = Path(__file__).resolve().parents[2]
OUTBOX = ROOT / "outbox"

NO_PATH = "No repo has a modelled dependency path to this. That means it isn't in the graph, not that you're safe."

_RESOLVERS = {
    "package": (Q.PACKAGE_EXACT, Q.PACKAGE_FUZZY),
    "repo": (Q.REPO_EXACT, Q.REPO_FUZZY),
    "maintainer": (Q.MAINTAINER_EXACT, Q.MAINTAINER_FUZZY),
    "advisory": (Q.ADVISORY_EXACT, Q.ADVISORY_FUZZY),
}


def _resolve(kind: str, value: str) -> tuple[str | None, list[str]]:
    """Canonical name from the graph, or None plus close matches. Never trust a raw name."""
    exact, fuzzy = _RESOLVERS[kind]
    hit = rows(exact, {"q": value.strip()})
    if hit:
        return hit[0]["name"], []
    return None, [r["name"] for r in rows(fuzzy, {"q": value.strip()})]


def _unknown(kind: str, value: str, suggestions: list[str]) -> dict:
    return {
        "outcome": f"unknown_{kind}",
        "query": value,
        "suggestions": suggestions,
        "note": f"'{value}' is not a {kind} in the graph, so no traversal ran.",
    }


def _timed(query: str, params: dict) -> tuple[list[dict], float]:
    started = perf_counter()
    result = rows(query, params)
    return result, round((perf_counter() - started) * 1000, 1)


def _memory(about: str, limit: int = 5) -> list[dict]:
    return rows(Q.RECALL, {"about": about, "limit": limit})


def _override(version_id: str, fixed: list[str]) -> dict | None:
    """The smallest fixed release above the installed one, as an npm `overrides` entry."""
    name, _, current = version_id.rpartition("@")
    try:
        installed = sv.Version(current)
    except ValueError:
        return None
    later = []
    for entry in fixed:
        pkg, _, release = entry.rpartition("@")
        try:
            candidate = sv.Version(release)
        except ValueError:
            continue
        if pkg == name and candidate > installed:
            later.append(candidate)
    if not later:
        return None
    best = min(later)
    return {"overrides": {name: str(best)}, "major_bump": best.major != installed.major}


def find_package(name: str) -> dict:
    """Resolve an npm package name against the graph: versions installed, maintainers, advisories."""
    pkg, suggestions = _resolve("package", name)
    if not pkg:
        return _unknown("package", name, suggestions)
    return {"outcome": "answered", "package": pkg, **rows(Q.PACKAGE_INFO, {"name": pkg})[0]}


def direct_dependents(package: str) -> dict:
    """Depth 1 only: repos that declare the package in their own package.json (what a grep finds)."""
    pkg, suggestions = _resolve("package", package)
    if not pkg:
        return _unknown("package", package, suggestions)
    hits = rows(Q.DIRECT_DEPENDENTS, {"name": pkg})
    return {"outcome": "answered" if hits else "not_in_graph", "package": pkg, "depth": 1, "repos": hits}


def blast_radius(package: str, limit: int = 25) -> dict:
    """Every repo that reaches the package at any depth, with the shortest dependency chain for each."""
    pkg, suggestions = _resolve("package", package)
    if not pkg:
        return _unknown("package", package, suggestions)
    hits, ms = _timed(Q.BLAST_RADIUS, {"name": pkg})
    declared = {r["repo"] for r in rows(Q.DIRECT_DEPENDENTS, {"name": pkg})}
    return {
        "outcome": "answered" if hits else "not_in_graph",
        "package": pkg,
        "repos_reached": len(hits),
        "declared_directly": len(declared),
        "hidden_transitively": sum(h["repo"] not in declared for h in hits),
        "repos": hits[:limit],
        "memory": _memory(pkg),
        "query_ms": ms,
        **({} if hits else {"note": NO_PATH}),
    }


def maintainer_blast_radius(maintainer: str, limit: int = 25) -> dict:
    """If this npm account were hijacked: which repos depend, at any depth, on a package it can publish."""
    name, suggestions = _resolve("maintainer", maintainer)
    if not name:
        return _unknown("maintainer", maintainer, suggestions)
    packages = [r["package"] for r in rows(Q.MAINTAINER_PACKAGES, {"name": name})]
    hits, ms = _timed(Q.MAINTAINER_BLAST_RADIUS, {"name": name})
    return {
        "outcome": "answered" if hits else "not_in_graph",
        "maintainer": name,
        "packages_maintained": len(packages),
        "packages": packages[:50],
        "repos_reached": len(hits),
        "repos": hits[:limit],
        "query_ms": ms,
        **({} if hits else {"note": NO_PATH}),
    }


def fresh_install_exposure(package: str, version: str, limit: int = 25) -> dict:
    """Repos that would install package@version on their next lockfile refresh, because a semver
    range on their dependency path accepts it. Use this for a hijacked release no lockfile pins yet."""
    pkg, suggestions = _resolve("package", package)
    if not pkg:
        return _unknown("package", package, suggestions)
    try:
        bad = sv.Version(version.strip().lstrip("v"))
    except ValueError:
        return {"outcome": "invalid_version", "query": version, "note": "Expected an exact version like 4.4.2."}
    accepting = []
    for edge in rows(Q.EDGES_INTO_PACKAGE, {"name": pkg}):
        try:
            if sv.NpmSpec(edge["range"]).match(bad):
                accepting.append(edge)
        except ValueError:  # git URLs, dist-tags and npm: aliases aren't semver ranges
            continue
    target = f"{pkg}@{bad}"
    exposed = {
        e["parent"]: {"repo": e["parent"], "depth": 1, "path": [e["parent"], target]}
        for e in accepting
        if e["kind"] == "Repo"
    }
    parents = sorted({e["parent"] for e in accepting if e["kind"] == "Version"})
    hits, ms = _timed(Q.REPOS_REACHING_VERSIONS, {"ids": parents}) if parents else ([], 0.0)
    for h in hits:
        exposed.setdefault(h["repo"], {"repo": h["repo"], "depth": h["depth"] + 1, "path": h["path"] + [target]})
    repos = sorted(exposed.values(), key=lambda r: (r["depth"], r["repo"]))
    return {
        "outcome": "answered" if repos else "not_in_graph",
        "package": pkg,
        "version": str(bad),
        "ranges_accepting": len(accepting),
        "repos_exposed": len(repos),
        "repos": repos[:limit],
        "query_ms": ms,
        "note": f"A range on each path accepts {target}, so npm update, Dependabot or an install "
        "without the lockfile would pull it in.",
    }


def advisory_impact(advisory: str, limit: int = 25) -> dict:
    """Repos whose installed tree contains a version affected by an advisory (GHSA, CVE or MAL id),
    with the path, the direct dependency to upgrade, and a suggested npm `overrides` fix."""
    aid, suggestions = _resolve("advisory", advisory)
    if not aid:
        return _unknown("advisory", advisory, suggestions)
    info = rows(Q.ADVISORY_INFO, {"id": aid})[0]
    hits, ms = _timed(Q.ADVISORY_IMPACT, {"id": aid})
    for h in hits:
        h["upgrade_via"] = h["path"][1] if len(h["path"]) > 2 else None
        h["suggested_fix"] = _override(h["versions"][0], info["fixed"] or [])
    packages = sorted({v.rpartition("@")[0] for v in info["affected_versions"]})
    result = {
        "outcome": "answered" if hits else "not_in_graph",
        "advisory": info,
        "repos_affected": len(hits),
        "repos": hits[:limit],
        "memory": _memory(aid) + [m for p in packages for m in _memory(p)],
        "query_ms": ms,
    }
    if not hits:
        result["note"] = NO_PATH + " For a hijacked release, check fresh_install_exposure."
    return result


def repo_advisories(repo: str, limit: int = 30) -> dict:
    """Advisories that reach a repo through its installed dependency tree, worst first, with paths."""
    name, suggestions = _resolve("repo", repo)
    if not name:
        return _unknown("repo", repo, suggestions)
    hits, ms = _timed(Q.REPO_ADVISORIES, {"repo": name, "limit": limit})
    return {
        "outcome": "answered" if hits else "not_in_graph",
        "repo": name,
        "advisories": hits,
        "memory": _memory(name),
        "query_ms": ms,
    }


def dependency_paths(repo: str, package: str, limit: int = 3) -> dict:
    """All equally short dependency chains from a repo to a package (FalkorDB algo.SPpaths)."""
    repo_name, suggestions = _resolve("repo", repo)
    if not repo_name:
        return _unknown("repo", repo, suggestions)
    pkg, suggestions = _resolve("package", package)
    if not pkg:
        return _unknown("package", package, suggestions)
    hits, ms = _timed(Q.DEPENDENCY_PATHS, {"repo": repo_name, "name": pkg, "limit": limit})
    return {
        "outcome": "answered" if hits else "not_in_graph",
        "repo": repo_name,
        "package": pkg,
        "paths": hits,
        "query_ms": ms,
        **({} if hits else {"note": NO_PATH}),
    }


def load_bearing_packages(max_maintainers: int = 1, limit: int = 10) -> dict:
    """'Where is our next xz?': the most depended-on packages (PageRank over the dependency graph)
    that no more than `max_maintainers` npm accounts can publish."""
    hits, ms = _timed(Q.LOAD_BEARING, {"max_maintainers": max_maintainers, "limit": limit})
    for h in hits:
        h["rank"] = round(h["rank"], 4)
        h["repos_reached"] = len(rows(Q.BLAST_RADIUS, {"name": h["package"]}))
    result = {"outcome": "answered" if hits else "not_in_graph", "packages": hits, "query_ms": ms}
    if not hits:
        result["note"] = "No maintainer data in the graph. Run blast-ingest without --skip-maintainers."
    return result


def who_owns(repo: str) -> dict:
    """Owning team, on-call handle and incident channel for a repo."""
    name, suggestions = _resolve("repo", repo)
    if not name:
        return _unknown("repo", repo, suggestions)
    return {"outcome": "answered", **rows(Q.WHO_OWNS, {"repo": name})[0]}


def recall(about: str, limit: int = 10) -> dict:
    """Past decisions about a repo, package or advisory, newest first, with who decided and when."""
    for kind in ("advisory", "repo", "package"):
        name, _ = _resolve(kind, about)
        if name:
            decisions = _memory(name, limit)
            return {
                "outcome": "answered_from_memory" if decisions else "not_in_graph",
                "about": name,
                "decisions": decisions,
                **({} if decisions else {"note": "Nothing has been decided about this yet."}),
            }
    return _unknown("entity", about, [])


def record_decision(about: str, kind: str, decision: str, by: str, status: str = "note") -> dict:
    """Remember a triage decision as a dated node linked to a repo, package or advisory.
    kind: repo | package | advisory. status: accepted_risk | fixing | fixed | note."""
    if kind not in Q.RECORD_DECISION:
        return {"outcome": "invalid_kind", "note": "kind must be repo, package or advisory"}
    name, suggestions = _resolve(kind, about)
    if not name:
        return _unknown(kind, about, suggestions)
    params = {
        "about": name,
        "id": uuid.uuid4().hex[:12],
        "text": decision,
        "by": by,
        "status": status,
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    saved = rows(Q.RECORD_DECISION[kind], params, write=True)[0]
    return {"outcome": "recorded", "about": name, "kind": kind, **saved}


def open_issue(repo: str, title: str, body: str) -> dict:
    """Open a remediation issue addressed to the repo's owning team. Posts to the sandbox repo in
    DEMO_ISSUES_REPO when GITHUB_TOKEN is set; otherwise writes a draft to outbox/."""
    name, suggestions = _resolve("repo", repo)
    if not name:
        return _unknown("repo", repo, suggestions)
    owner = rows(Q.WHO_OWNS, {"repo": name})[0]
    full_title = f"[{name}] {title}"
    full_body = (
        f"{body}\n\n---\nOwner: {owner['team']} ({owner['oncall']}, {owner['channel']})\n"
        "Opened by the supply-chain blast radius agent."
    )
    token, target = os.getenv("GITHUB_TOKEN"), os.getenv("DEMO_ISSUES_REPO")
    if token and target:
        resp = httpx.post(
            f"https://api.github.com/repos/{target}/issues",
            headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
            json={"title": full_title, "body": full_body},
            timeout=30,
        )
        resp.raise_for_status()
        outcome, url = "opened", resp.json()["html_url"]
    else:
        OUTBOX.mkdir(exist_ok=True)
        path = OUTBOX / f"{datetime.now():%Y%m%d-%H%M%S}-{name}.md"
        path.write_text(f"# {full_title}\n\n{full_body}\n")
        outcome, url = "drafted", str(path.relative_to(ROOT))
    record_decision(name, "repo", f"Opened issue: {title} ({url})", by="agent", status="fixing")
    return {"outcome": outcome, "repo": name, "url": url, "owner": owner}


def tenant_scoped(fn):
    """Expose a tool with an extra `tenant` argument that runs it against that tenant's graph.

    The web agent fills `tenant` from the chat session and hides it from the model, so the
    model can't reach another tenant's data. Empty means the default graph.
    """

    @functools.wraps(fn)
    def tool(*args, tenant: str = "", **kwargs):
        try:
            scope = use_tenant(tenant)
        except ValueError as e:
            return {"outcome": "unknown_tenant", "query": tenant, "note": str(e)}
        with scope:
            return fn(*args, **kwargs)

    sig = inspect.signature(fn)
    tenant_param = inspect.Parameter("tenant", inspect.Parameter.KEYWORD_ONLY, default="", annotation=str)
    tool.__signature__ = sig.replace(parameters=[*sig.parameters.values(), tenant_param])
    return tool


READ_TOOLS = [
    tenant_scoped(fn)
    for fn in (
        find_package,
        direct_dependents,
        blast_radius,
        maintainer_blast_radius,
        fresh_install_exposure,
        advisory_impact,
        repo_advisories,
        dependency_paths,
        load_bearing_packages,
        who_owns,
        recall,
    )
]
WRITE_TOOLS = [tenant_scoped(fn) for fn in (record_decision, open_issue)]
