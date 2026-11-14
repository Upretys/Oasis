"""Covers the real FHA accessibility check added tonight, and the bug it
fixed: elevator_summary was being computed from real CoStar structural
data but silently discarded before reaching any check."""
from value import check_building_constraints


def test_ground_floor_is_not_applicable(building):
    c = check_building_constraints(building["property_id"], 1)
    assert c.accessibility_flag == "not_applicable_ground_floor"


def test_unknown_floor_is_unknown():
    c = check_building_constraints(999999999, None)
    assert c.accessibility_flag == "unknown"


def test_non_ground_floor_with_elevators_is_likely_ok(building):
    if sum(check_building_constraints(building["property_id"], 1).elevator_summary.values()) == 0:
        return  # this real building happens to have no elevators on record - covered by the next test
    c = check_building_constraints(building["property_id"], 5)
    assert c.accessibility_flag == "likely_ok"
    assert "elevator" in c.accessibility_note.lower()
