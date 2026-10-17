import pytest

from blastradius.db import GRAPH_NAME, graph_name


def test_default_tenant_is_the_shared_graph():
    assert graph_name("") == GRAPH_NAME


def test_each_tenant_gets_its_own_graph():
    assert graph_name("orbit", must_exist=False) == f"{GRAPH_NAME}:orbit"


@pytest.mark.parametrize("bad", ["../etc", "Orbit", "a b", "x" * 33, "-lead", "orbit:other"])
def test_invalid_tenant_names_are_rejected(bad):
    with pytest.raises(ValueError):
        graph_name(bad, must_exist=False)
