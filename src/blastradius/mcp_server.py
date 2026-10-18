"""MCP server (stdio) exposing the fixed tool catalogue. The model picks tools; it never writes Cypher."""

from mcp.server import MCPServer
from mcp.types import ToolAnnotations

from . import tools

INSTRUCTIONS = """You are a supply-chain incident agent. Your context is a FalkorDB graph of real npm
lockfiles: repos, the exact package versions they install, OSV advisories, npm maintainers,
owning teams, and past triage decisions.

- Resolve every package, repo, maintainer or advisory with a tool before reasoning about it.
  If a tool returns unknown_*, offer its suggestions; never guess a name.
- Show the dependency path next to every claim (repo -> ... -> package@version).
- not_in_graph means no modelled path. Say exactly that; never call it safe.
- Contrast direct_dependents (depth 1) with blast_radius (any depth) when it matters.
- Check memory (recall) before recommending a fix, and call record_decision when the user decides.
- For a hijacked release that no lockfile pins yet, use fresh_install_exposure.
"""

server = MCPServer(name="hijack-radar", instructions=INSTRUCTIONS)
for fn in tools.READ_TOOLS:
    server.add_tool(fn, annotations=ToolAnnotations(readOnlyHint=True))
for fn in tools.WRITE_TOOLS:
    server.add_tool(fn, annotations=ToolAnnotations(readOnlyHint=False))


def main() -> None:
    server.run()


if __name__ == "__main__":
    main()
