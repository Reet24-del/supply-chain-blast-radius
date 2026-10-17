# Demo video: 2-minute shot list

Record the chat UI at 1920×1080, browser zoom 110%, bookmarks bar hidden. Each question takes the model a while to answer: cut the waiting in editing, but keep the tool cards appearing, since that's the agent at work.

## Before recording

- [ ] Graph loaded: the header shows 17 repos, 11,179 versions, 33,233 dependency edges.
- [ ] Clear rehearsal decisions so the memory scene starts clean:
  `uv run python -c "from blastradius.db import rows; rows('MATCH (d:Decision) DETACH DELETE d', write=True)"`
- [ ] Do one full rehearsal run. If an answer is long or rambling, tell me and I'll tune the prompt.
- [ ] If you record the live site, open it a minute early: the free host takes about a minute to wake up.

## Shots

| Time | Type or show | Say | Point at |
|---|---|---|---|
| 0:00–0:15 | The empty chat page | "September 8, 2025: one phished npm maintainer, 18 hijacked packages including chalk and debug, 2.6 billion downloads a week. Every team asked: were we hit?" | The headline |
| 0:15–0:40 | *Which of our repos install debug, and how many declare it directly?* | "Searching package.json finds one repo. The graph finds all 17, through chains like vscode, eslint, debug, in about 30 milliseconds." | The 17 blue repos fanning into red `debug`; the "1 declared directly · 16 hidden transitively" facts |
| 0:40–1:05 | *MAL-2025-46974 just dropped. Are we affected?* | "No lockfile pins the bad version, and the agent says exactly that, not 'you're safe'. Then it checks the version ranges: 217 of them accept the hijacked release, so the next npm update would have pulled it into every repo." | The amber "not in graph" badge, then the 217 / 17 facts |
| 1:05–1:20 | *If the npm account qix were phished, what would it reach?* | "Maintainers are nodes too. One account publishes 7 packages that reach all 17 repos." | The packages list and the graph |
| 1:20–1:35 | *Where is our next xz?* | "PageRank over the dependency graph, filtered to packages with a single maintainer: the load-bearing ones nobody watches." | The table rows |
| 1:35–1:50 | *We will pin debug with overrides everywhere. Remember that, and draft an issue for jitsi-meet.* | "Decisions become dated nodes in the graph, and the agent drafts the fix for the owning team." | The "recorded" and "drafted" badges |
| 1:50–2:00 | FalkorDB Browser running the `blast_radius` query, or the README architecture diagram | "Thirteen MCP tools, every query written by us, never by the model, and every answer carries its path. Built on FalkorDB." | End card: repo URL |

## Optional cut-ins (if time allows)

- **Track 02 isolation (15 s):** switch the Company picker to `orbit` and ask the `debug` question again: "Orbit gets its own graph. 5 repos, none declare debug, all 5 install it, and Orbit's decisions never show up for anyone else."
- The "memory" table on a follow-up `debug` question, showing the decision recalled with who and when.
- The same MCP tools inside Claude Code (`claude mcp add blast-radius -- uv run --directory "$PWD" blast-mcp`), to show the agent isn't tied to our UI.
