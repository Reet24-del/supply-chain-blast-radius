"""Every Cypher query the product runs, in one place.

The agent never writes Cypher. It picks a tool from tools.py, and the tool fills one of
these parameterised queries after resolving every name against the graph.
"""

# ------------------------------------------------------------------ ingest (writes)

UPSERT_TEAM = """
MERGE (t:Team {name: $name})
SET t.oncall = $oncall, t.channel = $channel
"""

UPSERT_REPO = """
MERGE (r:Repo {name: $name})
SET r.url = $url
WITH r
MATCH (t:Team {name: $team})
MERGE (t)-[:OWNS]->(r)
"""

UPSERT_VERSIONS = """
UNWIND $rows AS row
MERGE (p:Package {name: row.name})
MERGE (v:Version {id: row.id})
ON CREATE SET v.name = row.name, v.version = row.version
MERGE (p)-[:HAS_VERSION]->(v)
"""

# weight = 1 on every DEPENDS_ON edge so algo.SPpaths counts hops
UPSERT_DEPENDENCIES = """
UNWIND $rows AS row
MATCH (a:Version {id: row.src}), (b:Version {id: row.dst})
MERGE (a)-[d:DEPENDS_ON]->(b)
ON CREATE SET d.range = row.range, d.weight = 1
"""

UPSERT_DIRECT = """
MATCH (r:Repo {name: $repo})
UNWIND $rows AS row
MATCH (v:Version {id: row.to})
MERGE (r)-[d:DEPENDS_ON]->(v)
SET d.range = row.range, d.dev = row.dev, d.weight = 1
"""

ALL_VERSIONS = "MATCH (v:Version) RETURN v.id AS id, v.name AS name, v.version AS version"
ALL_PACKAGES = "MATCH (p:Package) RETURN p.name AS name"

UPSERT_ADVISORIES = """
UNWIND $rows AS row
MERGE (a:Advisory {id: row.id})
SET a.summary = row.summary, a.severity = row.severity, a.published = row.published,
    a.aliases = row.aliases, a.fixed = row.fixed, a.url = row.url
"""

UPSERT_AFFECTS = """
UNWIND $rows AS row
MATCH (a:Advisory {id: row.advisory}), (v:Version {id: row.version})
MERGE (a)-[:AFFECTS]->(v)
"""

# Watched advisories can name releases no lockfile pins (e.g. a hijacked version).
UPSERT_AFFECTED_VERSIONS = """
UNWIND $rows AS row
MATCH (p:Package {name: row.name}), (a:Advisory {id: row.advisory})
MERGE (v:Version {id: row.name + '@' + row.version})
ON CREATE SET v.name = row.name, v.version = row.version
MERGE (p)-[:HAS_VERSION]->(v)
MERGE (a)-[:AFFECTS]->(v)
"""

UPSERT_MAINTAINERS = """
UNWIND $rows AS row
MATCH (p:Package {name: row.package})
UNWIND row.maintainers AS maintainer
MERGE (m:Maintainer {name: maintainer})
MERGE (m)-[:MAINTAINS]->(p)
"""

COUNT_NODES = "MATCH (n) RETURN labels(n)[0] AS label, count(n) AS n ORDER BY label"
COUNT_EDGES = "MATCH ()-[e]->() RETURN type(e) AS type, count(e) AS n ORDER BY type"

# ------------------------------------------------------------------ name resolution

PACKAGE_EXACT = "MATCH (p:Package {name: $q}) RETURN p.name AS name"
PACKAGE_FUZZY = """
MATCH (p:Package) WHERE toLower(p.name) CONTAINS toLower($q)
RETURN p.name AS name ORDER BY size(p.name), name LIMIT 5
"""

REPO_EXACT = "MATCH (r:Repo) WHERE toLower(r.name) = toLower($q) RETURN r.name AS name"
REPO_FUZZY = """
MATCH (r:Repo) WHERE toLower(r.name) CONTAINS toLower($q) OR toLower(r.url) CONTAINS toLower($q)
RETURN r.name AS name ORDER BY name LIMIT 5
"""

MAINTAINER_EXACT = "MATCH (m:Maintainer {name: $q}) RETURN m.name AS name"
MAINTAINER_FUZZY = """
MATCH (m:Maintainer) WHERE toLower(m.name) CONTAINS toLower($q)
RETURN m.name AS name ORDER BY size(m.name), name LIMIT 5
"""

ADVISORY_EXACT = """
MATCH (a:Advisory)
WHERE toUpper(a.id) = toUpper($q) OR any(x IN a.aliases WHERE toUpper(x) = toUpper($q))
RETURN a.id AS name LIMIT 1
"""
ADVISORY_FUZZY = """
MATCH (a:Advisory) WHERE toLower(a.summary) CONTAINS toLower($q)
RETURN a.id AS name ORDER BY name LIMIT 5
"""

# ------------------------------------------------------------------ traversals

PACKAGE_INFO = """
MATCH (p:Package {name: $name})
OPTIONAL MATCH (p)-[:HAS_VERSION]->(v:Version)
WITH p, collect(DISTINCT v.version) AS versions
OPTIONAL MATCH (m:Maintainer)-[:MAINTAINS]->(p)
WITH p, versions, collect(DISTINCT m.name) AS maintainers
OPTIONAL MATCH (a:Advisory)-[:AFFECTS]->(:Version)<-[:HAS_VERSION]-(p)
RETURN versions, maintainers, collect(DISTINCT a.id) AS advisories
"""

# Depth 1 only: what grepping every package.json would tell you.
DIRECT_DEPENDENTS = """
MATCH (r:Repo)-[d:DEPENDS_ON]->(v:Version)<-[:HAS_VERSION]-(:Package {name: $name})
RETURN r.name AS repo, v.version AS version, d.range AS range, d.dev AS dev
ORDER BY repo
"""

# Any depth: the shortest dependency chain from each repo to any version of the package.
BLAST_RADIUS = """
MATCH (:Package {name: $name})-[:HAS_VERSION]->(v:Version)
MATCH (r:Repo)
WITH r, v, shortestPath((r)-[:DEPENDS_ON*]->(v)) AS p
WHERE p IS NOT NULL
WITH r, v, p ORDER BY length(p)
WITH r, collect(v.version) AS versions, collect(p)[0] AS p
RETURN r.name AS repo, length(p) AS depth, relationships(p)[0].dev AS via_dev,
       [n IN nodes(p) | coalesce(n.id, n.name)] AS path, versions
ORDER BY depth, repo
"""

MAINTAINER_PACKAGES = """
MATCH (:Maintainer {name: $name})-[:MAINTAINS]->(p:Package)
RETURN p.name AS package ORDER BY package
"""

# Everything a hijacked npm account could publish into, at any depth.
MAINTAINER_BLAST_RADIUS = """
MATCH (:Maintainer {name: $name})-[:MAINTAINS]->(pkg:Package)-[:HAS_VERSION]->(v:Version)
MATCH (r:Repo)
WITH r, pkg, v, shortestPath((r)-[:DEPENDS_ON*]->(v)) AS p
WHERE p IS NOT NULL
WITH r, pkg, p ORDER BY length(p)
WITH r, collect(DISTINCT pkg.name) AS packages, collect(p)[0] AS p
RETURN r.name AS repo, packages, length(p) AS depth,
       [n IN nodes(p) | coalesce(n.id, n.name)] AS path
ORDER BY size(packages) DESC, repo
"""

# Every declared range that points at the package (checked against a version in Python).
EDGES_INTO_PACKAGE = """
MATCH (parent)-[d:DEPENDS_ON]->(v:Version)<-[:HAS_VERSION]-(:Package {name: $name})
RETURN coalesce(parent.id, parent.name) AS parent, labels(parent)[0] AS kind,
       d.range AS range, v.version AS resolved
"""

REPOS_REACHING_VERSIONS = """
UNWIND $ids AS vid
MATCH (v:Version {id: vid})
MATCH (r:Repo)
WITH r, v, shortestPath((r)-[:DEPENDS_ON*]->(v)) AS p
WHERE p IS NOT NULL
WITH r, p ORDER BY length(p)
WITH r, collect(p)[0] AS p
RETURN r.name AS repo, length(p) AS depth, [n IN nodes(p) | coalesce(n.id, n.name)] AS path
ORDER BY depth, repo
"""

ADVISORY_INFO = """
MATCH (a:Advisory {id: $id})
OPTIONAL MATCH (a)-[:AFFECTS]->(v:Version)
RETURN a.id AS id, a.summary AS summary, a.severity AS severity, a.published AS published,
       a.aliases AS aliases, a.fixed AS fixed, a.url AS url, collect(v.id) AS affected_versions
"""

ADVISORY_IMPACT = """
MATCH (:Advisory {id: $id})-[:AFFECTS]->(v:Version)
MATCH (r:Repo)
WITH r, v, shortestPath((r)-[:DEPENDS_ON*]->(v)) AS p
WHERE p IS NOT NULL
WITH r, v, p ORDER BY length(p)
WITH r, collect(v.id) AS versions, collect(p)[0] AS p
RETURN r.name AS repo, versions, length(p) AS depth, relationships(p)[0].dev AS via_dev,
       [n IN nodes(p) | coalesce(n.id, n.name)] AS path
ORDER BY depth, repo
"""

REPO_ADVISORIES = """
MATCH (r:Repo {name: $repo})
MATCH (a:Advisory)-[:AFFECTS]->(v:Version)
WITH r, a, v, shortestPath((r)-[:DEPENDS_ON*]->(v)) AS p
WHERE p IS NOT NULL
RETURN a.id AS advisory, a.severity AS severity, a.summary AS summary, v.id AS version,
       length(p) AS depth, relationships(p)[0].dev AS via_dev,
       [n IN nodes(p) | coalesce(n.id, n.name)] AS path
ORDER BY CASE a.severity WHEN 'MALICIOUS' THEN 0 WHEN 'CRITICAL' THEN 1 WHEN 'HIGH' THEN 2
                         WHEN 'MODERATE' THEN 3 ELSE 4 END, depth, advisory
LIMIT $limit
"""

# All equally short chains between a repo and a package (FalkorDB algo.SPpaths).
# shortestPath first: it skips unreachable versions and caps maxLen, which keeps
# SPpaths from enumerating every path in the tree.
DEPENDENCY_PATHS = """
MATCH (r:Repo {name: $repo}), (:Package {name: $name})-[:HAS_VERSION]->(v:Version)
WITH r, v, shortestPath((r)-[:DEPENDS_ON*]->(v)) AS sp
WHERE sp IS NOT NULL
CALL algo.SPpaths({sourceNode: r, targetNode: v, relTypes: ['DEPENDS_ON'],
                   weightProp: 'weight', maxLen: length(sp), pathCount: $limit})
YIELD path, pathWeight
RETURN v.version AS version, toInteger(pathWeight) AS depth,
       [n IN nodes(path) | coalesce(n.id, n.name)] AS path
ORDER BY depth LIMIT $limit
"""

# "Where is our next xz?": PageRank over the dependency graph, filtered to packages
# that only a few npm accounts can publish.
LOAD_BEARING = """
CALL algo.pageRank('Version', 'DEPENDS_ON') YIELD node, score
MATCH (pkg:Package)-[:HAS_VERSION]->(node)
WITH pkg, sum(score) AS rank
MATCH (m:Maintainer)-[:MAINTAINS]->(pkg)
WITH pkg, rank, collect(m.name) AS maintainers
WHERE size(maintainers) <= $max_maintainers
RETURN pkg.name AS package, rank, maintainers
ORDER BY rank DESC LIMIT $limit
"""

WHO_OWNS = """
MATCH (r:Repo {name: $repo})
OPTIONAL MATCH (t:Team)-[:OWNS]->(r)
RETURN r.name AS repo, r.url AS url, t.name AS team, t.oncall AS oncall, t.channel AS channel
"""

# ------------------------------------------------------------------ memory

_TARGETS = {
    "repo": "(t:Repo {name: $about})",
    "package": "(t:Package {name: $about})",
    "advisory": "(t:Advisory {id: $about})",
}

RECORD_DECISION = {
    kind: f"""
MATCH {pattern}
CREATE (d:Decision {{id: $id, text: $text, by: $by, status: $status, at: $at}})-[:ABOUT]->(t)
RETURN d.id AS id, d.at AS at
"""
    for kind, pattern in _TARGETS.items()
}

RECALL = """
MATCH (d:Decision)-[:ABOUT]->(t)
WHERE (t:Repo AND t.name = $about) OR (t:Package AND t.name = $about)
   OR (t:Advisory AND t.id = $about)
RETURN d.text AS decision, d.by AS by, d.status AS status, d.at AS at,
       labels(t)[0] AS kind, coalesce(t.name, t.id) AS about
ORDER BY at DESC LIMIT $limit
"""
