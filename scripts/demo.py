"""Run the demo storyline against the graph and print compact answers.

    uv run python scripts/demo.py          # read-only questions
    uv run python scripts/demo.py --act    # also records a decision and drafts an issue
"""

import sys

from blastradius import tools


def show(title: str, result: dict, list_key: str | None = None, n: int = 3) -> None:
    print(f"\n=== {title}")
    summary = {k: v for k, v in result.items() if not isinstance(v, (list, dict))}
    print("   ", summary)
    for item in (result.get(list_key) or [])[:n]:
        path = item.get("path")
        line = " -> ".join(path) if path else {k: v for k, v in item.items() if k != "path"}
        print("    *", line)


show("Depth 1: who declares debug directly?", tools.direct_dependents("debug"), "repos")
show("Any depth: who actually installs debug?", tools.blast_radius("debug"), "repos")
show("Sep 8 2025 hijack: does any lockfile pin it?", tools.advisory_impact("MAL-2025-46974"), "repos")
show("Would a lockfile refresh pull debug@4.4.2?", tools.fresh_install_exposure("debug", "4.4.2"), "repos")
show("If npm account 'qix' were phished", tools.maintainer_blast_radius("qix"), "repos")
show("Worst advisories reaching expensify-app", tools.repo_advisories("expensify-app", limit=5), "advisories")
show("Every shortest chain vscode -> debug", tools.dependency_paths("vscode", "debug"), "paths")
show("Where is our next xz?", tools.load_bearing_packages(limit=5), "packages", n=5)
show("Unknown names are refused, not guessed", tools.blast_radius("left-padd"))
show("Owner of jitsi-meet", tools.who_owns("jitsi-meet"))

if "--act" in sys.argv:
    show(
        "Record a decision",
        tools.record_decision("debug", "package", "Pin debug via overrides in all repos", by="security-lead"),
    )
    show("Recall it", tools.recall("debug"), "decisions")
    show(
        "Draft an issue for the owners",
        tools.open_issue("jitsi-meet", "Pin debug against hijacked 4.4.2", "Add `overrides: {debug: 4.4.1}`."),
    )
