from summarizer import Summarizer


def _make_messages(positions: list[int]) -> list[dict]:
    return [{"message_id": pos} for pos in positions]


def test_chaining_does_not_merge_all():
    """Refs evenly spaced at 50 apart with radius=30 (threshold=60) should NOT all merge."""
    messages = _make_messages(list(range(0, 250)))
    ref_ids = [20, 70, 120, 170, 220]
    groups = Summarizer._group_nearby_refs(ref_ids, messages, radius=30)
    assert len(groups) > 1, f"Expected multiple groups, got {groups}"


def test_nearby_refs_merge():
    """Two refs within 2*radius should merge into one group."""
    messages = _make_messages(list(range(0, 100)))
    ref_ids = [10, 30]
    groups = Summarizer._group_nearby_refs(ref_ids, messages, radius=30)
    assert groups == [[10, 30]]


def test_distant_refs_separate():
    """Two refs far apart should be in separate groups."""
    messages = _make_messages(list(range(0, 200)))
    ref_ids = [10, 150]
    groups = Summarizer._group_nearby_refs(ref_ids, messages, radius=30)
    assert groups == [[10], [150]]


def test_empty_refs():
    messages = _make_messages(list(range(0, 50)))
    groups = Summarizer._group_nearby_refs([], messages, radius=30)
    assert groups == []


def test_single_ref():
    messages = _make_messages(list(range(0, 50)))
    groups = Summarizer._group_nearby_refs([25], messages, radius=30)
    assert groups == [[25]]


def test_duplicate_refs():
    """Duplicate ref_ids should be deduplicated."""
    messages = _make_messages(list(range(0, 100)))
    ref_ids = [10, 10, 30, 30]
    groups = Summarizer._group_nearby_refs(ref_ids, messages, radius=30)
    assert groups == [[10, 30]]


def test_group_span_capped_at_2_radius():
    """No group should span more than 2*radius positions."""
    messages = _make_messages(list(range(0, 300)))
    ref_ids = [10, 40, 70, 100, 130, 160, 190, 220, 250]
    radius = 30
    groups = Summarizer._group_nearby_refs(ref_ids, messages, radius=radius)
    for group in groups:
        positions = [ref for ref in group]
        span = max(positions) - min(positions)
        assert span <= 2 * radius, f"Group {group} has span {span} > {2 * radius}"
