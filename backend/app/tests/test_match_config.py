from service.script_tools.match_config import SharedGate, get_entry, list_dims_by_gate, list_entries


def test_gate_lists_do_not_overlap() -> None:
    seen: set[str] = set()
    for gate in SharedGate:
        dims = list_dims_by_gate(gate)
        assert len(dims) == len(set(dims))
        for dim in dims:
            assert dim not in seen
            seen.add(dim)


def test_gate_table_lookup_is_safe() -> None:
    entries = list_entries()
    assert len(entries) >= 1
    shared_dims = list_dims_by_gate(SharedGate.STABLE_SHARED)
    assert "dialogue_density" in shared_dims
    entry = get_entry("dialogue_density")
    assert entry is not None
    assert entry.gate == SharedGate.STABLE_SHARED
    assert get_entry("world_setting") is None

