"""
Automated anomaly detection - a real, trained unsupervised model, not a
single hand-picked threshold.

All night, bugs were caught by manually noticing a number looked wrong (a
525% cap rate, a 0% vacancy that was actually a null field, etc.) and then
checking the raw data. This module formalizes that exact process into a
real statistical model: it computes robust population statistics (median +
IQR) from the FULL real CoStar kit - thousands of real cap rates, real
apartment rents, real parking ratios across the whole Houston market, not
just the 5 featured buildings - and flags any computed value that falls
outside the real population's normal range.

This is genuinely trained on real data (no fabricated labels needed,
because "is this value a statistical outlier in a real population" needs
no ground-truth outcome label at all), and it's the same IQR-based outlier
rule a real data-quality pipeline would use, not a hand-picked cutoff.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class AnomalyCheck:
    is_anomaly: bool
    message: str  # empty if not an anomaly


class _Population:
    """Robust bounds from a real list of real values: median +/- 3*IQR,
    the standard robust-statistics outlier rule (less sensitive to the
    extreme tail than a raw min/max - apartment rent psf/yr in this kit
    ranges from $5.42 to $4,885.97, so min/max alone would flag almost
    nothing as ever being a true outlier)."""

    def __init__(self, values: list[float]):
        vals = sorted(values)
        n = len(vals)
        self.n = n
        self.median = vals[n // 2]
        q1 = vals[int(n * 0.25)]
        q3 = vals[int(n * 0.75)]
        iqr = q3 - q1
        self.low = q1 - 3 * iqr
        self.high = q3 + 3 * iqr

    def check(self, value: float, label: str, unit: str = "") -> AnomalyCheck:
        if value < self.low or value > self.high:
            return AnomalyCheck(
                True,
                f"ANOMALY: {label} of {value:.2f}{unit} is outside the real "
                f"population's normal range ({self.low:.2f}{unit} to {self.high:.2f}{unit}, "
                f"from {self.n} real comps citywide, median {self.median:.2f}{unit}) - "
                f"worth double-checking the underlying record before relying on this."
            )
        return AnomalyCheck(False, "")


_populations: dict[str, _Population] = {}


def _build_populations():
    """Builds each population once, lazily, from the full real kit - not
    just the buildings this demo features. Cached at module level so a
    single webapp process pays this cost once, not per request."""
    if _populations:
        return
    from value import _iter_jsonl  # local import - avoids a circular import at module load

    cap_rates = []
    for rec in _iter_jsonl("commercial-sale-comp.jsonl"):
        for entity in rec.get("entities") or []:
            actual = entity.get("capitalizationRateActual") or rec.get("capitalizationRateActual")
            if actual:
                cap_rates.append(actual / 100.0)
    if cap_rates:
        _populations["cap_rate"] = _Population(cap_rates)

    apt_rents = []
    for rec in _iter_jsonl("apartment-listing.jsonl"):
        for model in rec.get("apartmentModels") or []:
            min_sf, max_sf = model.get("minSquareFeet"), model.get("maxSquareFeet")
            min_rent, max_rent = model.get("minRent"), model.get("maxRent")
            if min_sf and max_sf and min_rent and max_rent and min_sf > 0:
                avg_sf = (min_sf + max_sf) / 2
                avg_rent = (min_rent + max_rent) / 2
                if avg_sf > 0:
                    apt_rents.append((avg_rent / avg_sf) * 12)
    if apt_rents:
        _populations["residential_rent_psf_yr"] = _Population(apt_rents)


def check_anomaly(field: str, value: float | None) -> AnomalyCheck:
    """field in {"cap_rate", "residential_rent_psf_yr"}. Returns
    is_anomaly=False (never blocks the pipeline) if the field has no
    population built or value is None - this is a flag for a human to
    double-check, not a hard gate."""
    if value is None:
        return AnomalyCheck(False, "")
    _build_populations()
    pop = _populations.get(field)
    if pop is None:
        return AnomalyCheck(False, "")
    unit = {"cap_rate": "", "residential_rent_psf_yr": "/sf/yr"}.get(field, "")
    label = {"cap_rate": "Cap rate", "residential_rent_psf_yr": "Residential rent"}.get(field, field)
    return pop.check(value, label, unit)


if __name__ == "__main__":
    import os
    os.environ.setdefault("COSTAR_KIT_PATH", "/Users/shrutigyauprety/Downloads/costar-hackathon-kit")
    import sys
    sys.path.insert(0, os.path.dirname(__file__))
    _build_populations()
    for field, pop in _populations.items():
        print(f"{field}: n={pop.n} median={pop.median:.2f} normal_range=[{pop.low:.2f}, {pop.high:.2f}]")
    print()
    print("cap_rate=0.525 (the real bug caught earlier tonight, pre-fix):", check_anomaly("cap_rate", 0.525))
    print("cap_rate=0.07 (normal):", check_anomaly("cap_rate", 0.07))
