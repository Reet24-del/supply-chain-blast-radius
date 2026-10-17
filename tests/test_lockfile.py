from blastradius.lockfile import parse_lockfile

LOCK = {
    "lockfileVersion": 3,
    "packages": {
        "": {
            "name": "app",
            "workspaces": ["packages/ui"],
            "dependencies": {"express": "^4.18.0", "ui": "*"},
            "devDependencies": {"debug": "^4.3.0"},
        },
        "packages/ui": {"name": "ui", "version": "1.0.0", "dependencies": {"chalk": "^5.0.0"}},
        "node_modules/ui": {"resolved": "packages/ui", "link": True},
        "node_modules/express": {"version": "4.18.2", "dependencies": {"debug": "2.6.9", "fsevents": "^2.0.0"}},
        "node_modules/express/node_modules/debug": {"version": "2.6.9", "dependencies": {"ms": "2.0.0"}},
        "node_modules/express/node_modules/ms": {"version": "2.0.0"},
        "node_modules/debug": {"version": "4.3.4", "dev": True, "dependencies": {"ms": "2.1.2"}},
        "node_modules/ms": {"version": "2.1.2", "dev": True},
        "node_modules/chalk": {"version": "5.3.0"},
        "node_modules/strip-ansi-cjs": {"name": "strip-ansi", "version": "6.0.1"},
    },
}


def test_direct_dependencies_fold_in_workspaces_and_skip_links():
    parsed = parse_lockfile(LOCK)
    assert set(parsed.direct) == {"express@4.18.2", "debug@4.3.4", "chalk@5.3.0"}
    assert parsed.direct["debug@4.3.4"]["dev"] is True
    assert parsed.direct["chalk@5.3.0"]["dev"] is False


def test_nested_node_modules_resolve_before_the_root():
    edges = parse_lockfile(LOCK).edges
    assert edges[("express@4.18.2", "debug@2.6.9")] == "2.6.9"
    assert ("debug@2.6.9", "ms@2.0.0") in edges  # walks up to express/node_modules/ms
    assert ("debug@4.3.4", "ms@2.1.2") in edges  # root copy for the root debug


def test_uninstalled_optional_deps_are_skipped_and_aliases_use_the_real_name():
    parsed = parse_lockfile(LOCK)
    assert not any(dst.startswith("fsevents@") for _, dst in parsed.edges)
    assert "strip-ansi@6.0.1" in parsed.versions
