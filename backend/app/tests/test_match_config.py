from service.script_tools.match_config import SharedGate, get_entry, list_dims_by_gate, list_entries


def test_gate_lists_do_not_overlap() -> None:
    seen: set[str] = set()
    for gate in SharedGate:
        dims = list_dims_by_gate(gate)
        assert len(dims) == len(set(dims))
        for dim in dims:
            assert dim not in seen
            seen.add(dim)


def test_empty_gate_table_is_safe() -> None:
    assert list_entries() == ()
    for gate in SharedGate:
        assert list_dims_by_gate(gate) == ()
    assert get_entry("world_setting") is None

