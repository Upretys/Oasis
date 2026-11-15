"""Covers the real bugs found and fixed in the value pipeline tonight:
the negative-dollar double-negative formatting bug, and the cap rate
sensitivity/breakeven math."""
from value import run_value


def test_negative_value_range_formats_with_sign_before_dollar(building):
    val = run_value(
        property_id=building["property_id"], floor_name=building["floor_name"],
        convertible_sqft=10000.0, query_lat=building["query_lat"], query_lon=building["query_lon"],
    )
    # The real bug: f"${lo}-${hi}" on a negative range produced
    # "$-1,958,403-$-1,041,725" (a double negative). Never again.
    assert "$-" not in val.reasoning_text, (
        f"reasoning_text contains a malformed double-negative dollar sign: {val.reasoning_text!r}"
    )


def test_cap_rate_sensitivity_is_monotonic_in_cap_rate(building):
    val = run_value(
        property_id=building["property_id"], floor_name=building["floor_name"],
        convertible_sqft=10000.0, query_lat=building["query_lat"], query_lon=building["query_lon"],
    )
    points = val.cap_rate_sensitivity
    if not points:
        return  # value_uplift <= 0 or similar - no sensitivity grid to check
    # implied value = uplift / cap_rate - cost: strictly decreasing as
    # cap_rate rises, for a fixed positive uplift. If this ever breaks,
    # the sensitivity chart would show a verdict getting MORE attractive
    # at a worse cap rate, which is financially nonsensical.
    mids = [(p["implied_low"] + p["implied_high"]) / 2 for p in points]
    for a, b in zip(mids, mids[1:]):
        assert a >= b, "cap rate sensitivity is not monotonically decreasing as cap rate rises"


def test_breakeven_range_is_ordered_and_positive(building):
    val = run_value(
        property_id=building["property_id"], floor_name=building["floor_name"],
        convertible_sqft=10000.0, query_lat=building["query_lat"], query_lon=building["query_lon"],
    )
    if val.breakeven_cap_rate_low is None:
        return  # no positive uplift - breakeven correctly doesn't exist
    assert val.breakeven_cap_rate_low > 0
    assert val.breakeven_cap_rate_high > 0
    assert val.breakeven_cap_rate_low <= val.breakeven_cap_rate_high, (
        "the conservative (worst-case cost) breakeven rate should never exceed "
        "the optimistic (best-case cost) one"
    )
