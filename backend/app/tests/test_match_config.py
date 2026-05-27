from service.script_tools.match_config import SharedGate, get_entry, list_dims_by_gate, list_entries


def test_gate_lists_do_not_overlap() -> None:
    seen: set[str] = set()
    for gate in SharedGate:
        dims = list_dims_by_gate(gate)
        assert len(dims) == len(set(dims))
        for dim in dims:
            assert dim not in seen
            seen.add(dim)


def test_get_entry_round_trip() -> None:
    for entry in list_entries():
        resolved = get_entry(entry.dim)
        assert resolved is not None
        assert resolved.dim == entry.dim
        assert resolved.gate == entry.gate

