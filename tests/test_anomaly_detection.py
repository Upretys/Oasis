"""Codifies the exact real bug this detector was built to catch: a cap
rate off by 100x (CoStar stores it as a plain percentage, e.g. 5.25
meaning 5.25%, not a fraction - an early version of this project divided
nowhere and printed a nonsensical 525% cap rate on a real property)."""
from anomaly_detection import check_anomaly


def test_catches_the_real_cap_rate_bug_found_tonight():
    result = check_anomaly("cap_rate", 0.525)  # the exact unconverted value that caused the real bug
    assert result.is_anomaly is True
    assert "ANOMALY" in result.message


def test_does_not_flag_a_normal_cap_rate():
    result = check_anomaly("cap_rate", 0.07)
    assert result.is_anomaly is False


def test_does_not_flag_a_normal_residential_rent():
    result = check_anomaly("residential_rent_psf_yr", 24.0)
    assert result.is_anomaly is False


def test_flags_an_implausible_residential_rent():
    # $500/sf/yr would be an extraordinary outlier against the real
    # 18,680-point population (real median ~$25/sf/yr).
    result = check_anomaly("residential_rent_psf_yr", 500.0)
    assert result.is_anomaly is True


def test_none_value_never_flagged():
    # A missing value is a different problem (handled elsewhere) - the
    # detector must never raise or false-flag on None.
    assert check_anomaly("cap_rate", None).is_anomaly is False


def test_unknown_field_never_flagged():
    assert check_anomaly("not_a_real_field", 999.0).is_anomaly is False
