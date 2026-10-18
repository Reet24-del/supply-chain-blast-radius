---
title: "Hijack Radar: your lockfile said you were safe. Your dependency graph knew better."
published: false
description: "How we built an AI agent on FalkorDB that traces phished npm maintainers and hijacked releases through 33,000 real dependency edges, for Graph Hacks 2026."
tags: ai, security, graphdatabase, hackathon
---

## Monday, September 8, 2025

A maintainer of some of the most-used packages on npm gets an email. It says his two-factor settings need updating before the 10th. The link looks like npm. It isn't.

Minutes later, new versions of 18 packages appear on the registry, `chalk` and `debug` among them. Together they're downloaded about **2.6 billion times a week**. The new code quietly rewrites crypto wallet transactions in the browser. The bad versions are pulled within hours, but every engineering team spends that afternoon asking the same question:

**Were we hit?**

The obvious way to answer is to search your repos for `debug` in `package.json`. We did exactly that across 17 real open-source codebases: VS Code, Playwright, Bitwarden, Jitsi Meet, Expensify and more.

**One** repo declares `debug`. **All seventeen** install it.

The other sixteen pull it in through chains like `vscode → eslint@10.8.0 → debug@4.4.3`. A text search can't see those chains. A graph can. That gap is the whole project.

{% youtube DForouXJVOE %}

## What Hijack Radar does

Hijack Radar is an AI agent for the attacks CVE scanners weren't built for: **account takeovers and hijacked releases**. You ask in plain English, and it answers from a FalkorDB graph of what your repos *really* install, with the exact path behind every answer.

- **"If the npm account `qix` were phished, what would it reach?"** That account publishes 7 packages, and they reach all 17 repos. The answer comes back in milliseconds, with a path for each repo.
- **"MAL-2025-46974 just dropped. Are we affected?"** This is the real advisory from that day. A lockfile scanner says *safe*, because no lockfile pins `debug@4.4.2`. The agent says that honestly, then checks the version ranges: **217 of them accept the hijacked release**, so your next `npm update` would have pulled it into every repo.
- **"Where is our next xz?"** PageRank over the dependency graph, filtered to packages that a single npm account controls. These are the load-bearing packages nobody is watching.
- **"We'll pin debug with overrides everywhere. Remember that, and draft an issue for jitsi-meet."** The decision is stored as a dated node in the graph, and the agent drafts the fix for the owning team.

Every answer sits next to an **evidence panel**: the tool calls it made, how long FalkorDB took, and a graph of every dependency path it found, with repos in blue and the compromised package in red. There's also a **radar view**: the 17 repos as dots, one ring per dependency hop. Replay an attack and the sweep lights up every exposed repo on the ring where the graph reaches it.

## Why this has to be a graph

Supply-chain questions are questions about **connections**, and they're several hops deep:

```
(:Maintainer)-[:MAINTAINS]->(:Package)-[:HAS_VERSION]->(:Version)
(:Repo)-[:DEPENDS_ON {range}]->(:Version)-[:DEPENDS_ON {range}]->(:Version)
(:Advisory)-[:AFFECTS]->(:Version)
(:Team)-[:OWNS]->(:Repo)
(:Decision)-[:ABOUT]->(:Repo | :Package | :Advisory)
```

We parse real `package-lock.json` files using Node's own module-resolution rules, so the graph holds what is *installed*, not just what is declared. Then we add advisories from OSV.dev and maintainer usernames from the npm registry. The result is 17 repos, 11,179 package versions, **33,233 dependency edges**, 357 advisories and 4,099 maintainers. It fits in about 19 MB.

Two modelling choices did most of the work:

1. **Every dependency edge stores its semver range.** That's how we answer "would a lockfile refresh pull the hijacked version?", the question that mattered on September 8 and that lockfile scanners can't answer.
2. **Maintainers are nodes.** "If this account is phished" becomes a traversal: person → packages → every version → every repo that reaches one.

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

On a laptop, it answers for all 17 repos in about 30 ms. We also use FalkorDB's built-in algorithms as tools: `algo.SPpaths` to list every equally short chain between a repo and a package, and `algo.pageRank` to find the "next xz".

## How the agent works

We set one rule early: **the model never writes Cypher.**

The agent picks from **13 fixed tools**, served over **MCP**. Each tool runs a parameterised query we wrote and tested. Before any traversal runs, the tool checks that the package, repo, maintainer or advisory actually exists in the graph. Unknown names get suggestions back, never a guess.

Every result carries an explicit outcome: `answered`, `not_in_graph`, `unknown_package` or `answered_from_memory`. That's what keeps the agent honest. For the real Sept 2025 advisory, it says *"not in the graph, not that you're safe"* and then checks the ranges, instead of announcing an all-clear.

The model is **Groq's `gpt-oss-120b`**. The free tier is small (8,000 tokens a minute), so the model only reads the first rows of each tool result, plus a count. The UI still draws every path, because it gets the full result separately. The same 13 tools also work in Claude Code or Claude Desktop over MCP: the web agent is just one client.

## The tracks we entered, and why it fits

### Track 01: Agents That Act on Connected Data

This track asks for agents that use the graph to *decide and act*: multi-hop reasoning, graph algorithms as tools, tool selection through MCP, and explainable results. That's Hijack Radar's core:

- **Multi-hop reasoning:** every answer is a traversal across up to several hops, from maintainer to package to version to repo.
- **Graph algorithms as tools:** `shortestPath`, `algo.SPpaths` and `algo.pageRank` are tools the agent chooses.
- **MCP tool selection:** 13 fixed tools, and the model picks which ones to call.
- **Action:** it suggests an npm `overrides` fix and drafts an issue for the team that owns the repo.
- **Explainable:** every result carries its dependency path, drawn beside the answer.

### Track 02: Agent Memory and Coordination

This track asks for agents that remember across sessions and keep separate memory per user or tenant:

- **Memory as graph nodes:** a triage decision such as "pin debug with overrides" becomes a dated `Decision` node linked to the package. Later answers about `debug`, or about advisories that touch it, come back with that decision attached: who decided and when.
- **Shared across agents:** the memory lives in the graph, not in a chat window, so every MCP client (our web agent, Claude Code, Claude Desktop) sees the same decisions.
- **One graph per company:** each tenant gets its own isolated graph on the same FalkorDB instance (`supplychain` and `supplychain:orbit` in our demo). The server pins each chat to its tenant, and the model never even sees the tenant argument. Ask about `debug` as our second demo company, Orbit, and you get Orbit's 5 repos, none of which declare `debug` and all of which install it. Orbit's decisions never appear for anyone else.

## Things that bit us

- **Unbounded path enumeration.** Our first `algo.SPpaths` call hung when the target wasn't reachable, because it tried to enumerate every path in the tree. Running `shortestPath` first and passing its length as `maxLen` brought it down to a few milliseconds.
- **An empty graph is an error, not an empty result.** A fresh cloud instance made our health check fail and the host restart the app in a loop. Now health reports the problem instead of crashing.
- **Lockfiles are subtle.** Nested `node_modules`, symlinked workspaces, npm aliases (`string-width-cjs` is really `string-width`) and optional platform packages all have to be handled before the graph tells the truth.
- **Prompts need to say what the UI needs.** On the first real run, the model answered "who installs debug?" from memory instead of calling the tool, so the graph never appeared. One line in the prompt ("call tools for every question; the page draws evidence only from this turn") fixed it.

## Try it

{% github Reet24-del/supply-chain-blast-radius %}

The README has the graph model, every Cypher query the product depends on, and a local setup that runs with embedded FalkorDB, no Docker needed. You need a free Groq key for the chat.

*Built for **Graph Hacks: Context for AI Agents** (FalkorDB × WeMakeDevs), October 2026. Built with Claude Code as a coding assistant, as disclosed in the README.*
