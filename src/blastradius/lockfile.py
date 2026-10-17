"""Parse an npm package-lock.json (lockfileVersion 2 or 3) into a resolved dependency graph.

Workspace packages are folded into the repo: their declared dependencies become the
repo's direct dependencies, and links to them are not edges.
"""

from dataclasses import dataclass, field

OWN_DEP_FIELDS = ("dependencies", "devDependencies", "optionalDependencies", "peerDependencies")
DEP_FIELDS = ("dependencies", "optionalDependencies", "peerDependencies")


@dataclass
class ParsedLock:
    versions: dict[str, dict] = field(default_factory=dict)  # id -> {id, name, version}
    direct: dict[str, dict] = field(default_factory=dict)  # id -> {to, range, dev}
    edges: dict[tuple[str, str], str] = field(default_factory=dict)  # (src, dst) -> range


def _is_own_code(path: str) -> bool:
    """The root ("") and workspace folders, as opposed to installed packages."""
    return not path.startswith("node_modules/") and "/node_modules/" not in path


def _resolve(packages: dict, from_path: str, dep: str) -> str | None:
    """Node's lookup: <dir>/node_modules/<dep>, then each parent package dir, then the root."""
    base = from_path
    while True:
        candidate = f"{base}/node_modules/{dep}" if base else f"node_modules/{dep}"
        if candidate in packages:
            return candidate
        if not base:
            return None
        cut = base.rfind("/node_modules/")
        base = base[:cut] if cut != -1 else ""


def parse_lockfile(lock: dict) -> ParsedLock:
    if lock.get("lockfileVersion", 1) < 2 or "packages" not in lock:
        raise ValueError("only lockfileVersion 2/3 is supported; regenerate it with npm >= 7")
    packages = lock["packages"]
    out = ParsedLock()

    def version_id(path: str) -> str | None:
        """The Version installed at `path`, or None for links, own code and entries without a version."""
        entry = packages.get(path)
        if entry is None or entry.get("link") or _is_own_code(path) or "version" not in entry:
            return None
        # `name` is only present for npm aliases (e.g. string-width-cjs -> string-width)
        name = entry.get("name") or path.rsplit("node_modules/", 1)[-1]
        vid = f"{name}@{entry['version']}"
        out.versions.setdefault(vid, {"id": vid, "name": name, "version": entry["version"]})
        return vid

    for path, entry in packages.items():
        own = _is_own_code(path)
        src = None if own else version_id(path)
        if not own and src is None:
            continue
        for dep_field in OWN_DEP_FIELDS if own else DEP_FIELDS:
            for dep, rng in (entry.get(dep_field) or {}).items():
                target = _resolve(packages, path, dep)
                dst = version_id(target) if target else None
                if dst is None:  # not installed (optional/platform) or a workspace link
                    continue
                if own:
                    dev = dep_field == "devDependencies"
                    prev = out.direct.get(dst)
                    if prev is None or (prev["dev"] and not dev):  # prod wins over dev
                        out.direct[dst] = {"to": dst, "range": rng, "dev": dev}
                else:
                    out.edges.setdefault((src, dst), rng)
    return out
