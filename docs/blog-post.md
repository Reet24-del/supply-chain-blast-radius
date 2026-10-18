# Hijack Radar: your lockfile says you're safe. Your dependency graph disagrees.

*How we built an agent that traces phished npm maintainers and hijacked releases through FalkorDB for Graph Hacks 2026, and why "were we hit?" is a graph traversal, not a search.*

<!-- Screenshot: the chat UI with the 17-repo fan-in graph for `debug`. -->

## The two hours that mattered

On September 8, 2025, attackers phished a single npm maintainer and published malicious versions of 18 packages, `chalk` and `debug` among them. Together those packages are downloaded about 2.6 billion times a week. The payload hijacked crypto wallet transactions in the browser. The bad versions were live for a couple of hours before they were pulled.

Every engineering team asked the same question that afternoon: **were we hit?**

The obvious way to answer is to search your repos for `debug` in `package.json`. We did exactly that across 17 real open-source codebases (VS Code, Playwright, Bitwarden, Jitsi Meet, Expensify and more). **One repo declares `debug` directly. All 17 install it.** The other 16 pull it in through chains like `vscode → eslint@10.8.0 → debug@4.4.3`. A text search can't see those chains. A graph can.

## What we built

**Hijack Radar** is an agent for the attacks CVE scanners weren't built for: account takeovers and hijacked releases. It answers from a FalkorDB graph of real, installed dependency trees:

- *If this npm account were phished, what could the attacker publish into our repos?*
- *Would our next `npm update` pull in the hijacked release, even though no lockfile pins it yet?*
- *Where is our next xz: which packages sit under everything but have one maintainer?*
- *Which repos install this package or advisory, at any depth, and through which path?*

Every answer comes with the dependency paths that produced it, drawn next to the answer.

<!-- Screenshot: evidence panel with tool calls, outcomes and timings. -->

## The graph

We parse real `package-lock.json` files with Node's own module-resolution rules, so the graph holds what is actually installed, not what is declared:

```
(:Team)-[:OWNS]->(:Repo)
(:Repo)-[:DEPENDS_ON {range, dev}]->(:Version)
(:Version)-[:DEPENDS_ON {range}]->(:Version)
(:Package)-[:HAS_VERSION]->(:Version)
(:Maintainer)-[:MAINTAINS]->(:Package)
(:Advisory)-[:AFFECTS]->(:Version)
(:Decision)-[:ABOUT]->(:Repo | :Package | :Advisory)
```

On top of the lockfiles we load advisories from OSV.dev and maintainer usernames from the npm registry. The result is 17 repos, 11,179 package versions, 33,233 dependency edges, 357 advisories and 4,099 maintainers. A full load takes under a minute from cache and fits in about 19 MB, comfortably inside FalkorDB Cloud's free tier.

Two details made a big difference:

1. **We store the semver range on every edge.** Lockfiles pin exact versions, so no lockfile in our set contained the hijacked `debug@4.4.2`. But the edge from `eslint@10.8.0` to `debug` says `^4.3.2`, and that range accepts 4.4.2. Checking ranges along the paths answers the scarier question: *would a lockfile refresh have pulled it in?* In our data, 217 ranges accept the bad release, and they expose all 17 repos.
2. **Maintainers are nodes.** "If account X is phished" becomes a traversal from a person to packages to every repo that reaches them.

## How the agent uses the graph

We followed one rule throughout: **the model never writes Cypher.** It picks from 13 fixed tools, served over MCP, and each tool runs a parameterised query we wrote and tested. Before any traversal runs, the tool checks that the package, repo, maintainer or advisory actually exists in the graph. If it doesn't, the agent gets suggestions back instead of a guess.

The core query is short:

```cypher
MATCH (:Package {name: $name})-[:HAS_VERSION]->(v:Version)
MATCH (r:Repo)
WITH r, v, shortestPath((r)-[:DEPENDS_ON*]->(v)) AS p
WHERE p IS NOT NULL
WITH r, v, p ORDER BY length(p)
WITH r, collect(v.version) AS versions, collect(p)[0] AS p
RETURN r.name, length(p), [n IN nodes(p) | coalesce(n.id, n.name)]
```

For `debug` it answers in about 30 ms. We also use FalkorDB's built-in algorithms as tools:

- **`algo.SPpaths`** returns every equally short chain between a repo and a package, so you can see all the routes a fix has to cut.
- **`algo.pageRank`** over the dependency graph, filtered to packages with a single maintainer, is our "next xz" detector. It surfaces packages like `postcss` and `js-tokens` that sit under almost everything.

Every tool result carries an explicit outcome: `answered`, `not_in_graph`, `unknown_package` or `answered_from_memory`. That keeps the agent honest. For the real advisory behind the 2025 hijack (`MAL-2025-46974`), the tool returns `not_in_graph`, because no lockfile pins that exact version. Its note says this means "not in the graph, not that you're safe" and points to the fresh-install check, which is where the real risk was.

## Memory

Incident response is a conversation that lasts days. When someone decides "we'll pin `debug` with npm overrides everywhere", the agent stores that as a dated `Decision` node linked to the package. The next question about `debug`, or any advisory that touches it, comes back with that decision attached: who made it and when. Drafting an issue for the owning team is recorded the same way.

Memory has to stay with the company it belongs to, so each company (tenant) gets its own graph on the same FalkorDB instance: `supplychain` for one, `supplychain:orbit` for another. The server pins a chat session to its tenant and fills the tools' `tenant` argument itself; the model never sees that argument. Ask about `debug` as our second demo company, Orbit, and you get Orbit's 5 repos, none of which declare `debug` and all of which install it. Orbit's decisions never appear for anyone else.

## The stack

- **FalkorDB**, in Docker, FalkorDB Cloud, or embedded via FalkorDBLite for local development
- **MCP server** (Python SDK) exposing the 13 tools to any client: Claude Code, Claude Desktop, or our own web agent
- **Claude** (`claude-opus-5-5`) running a streaming tool-use loop as an in-process MCP client
- **FastAPI and a small vanilla-JS UI** with Cytoscape.js, drawing the paths from the tool results, not from the model's text

## Things that bit us

- **Unbounded path enumeration.** Our first `algo.SPpaths` call hung when the target version wasn't reachable from the repo: it tried to enumerate every path in the tree. Running `shortestPath` first and passing its length as `maxLen` brought it down to a few milliseconds.
- **An empty graph is an error, not an empty result.** Querying a graph that doesn't exist yet fails, so a fresh cloud instance made our health check fail and the host restart the app in a loop. Health now reports the problem instead of crashing.
- **Lockfiles are subtle.** Nested `node_modules`, workspaces linked by symlink, npm aliases (`string-width-cjs` is really `string-width`) and optional platform packages all need handling before the graph tells the truth.

## Try it

- Repo: https://github.com/Reet24-del/supply-chain-blast-radius
- Live demo: TODO
- Demo video: TODO

The whole thing runs locally in a few commands, and the README explains every Cypher query the product depends on.

*Built for Graph Hacks: Context for AI Agents (FalkorDB × WeMakeDevs), October 2026.*
