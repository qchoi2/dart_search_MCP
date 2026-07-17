"""Evidence gate for the optional Stage 8 permanent index."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class IndexNeedEvidence:
    repeated_period_searches: bool = False
    repeated_market_wide_searches: bool = False
    deep_search_cost_is_material: bool = False
    measured_recall_or_cost_improvement: bool = False


@dataclass(frozen=True)
class IndexRecommendation:
    recommended: bool
    status: str
    reasons: tuple[str, ...]


def evaluate_index_need(evidence: IndexNeedEvidence) -> IndexRecommendation:
    """Recommend activation only after measured demand and benefit coexist.

    This function is intentionally side-effect free: it cannot create an index,
    download filings, or toggle the runtime feature flag.
    """

    demand = any(
        (
            evidence.repeated_period_searches,
            evidence.repeated_market_wide_searches,
            evidence.deep_search_cost_is_material,
        )
    )
    reasons: list[str] = []
    if not demand:
        reasons.append("recurring_usage_pressure_not_demonstrated")
    if not evidence.measured_recall_or_cost_improvement:
        reasons.append("measured_index_benefit_not_demonstrated")
    if reasons:
        return IndexRecommendation(False, "not_activated", tuple(reasons))
    return IndexRecommendation(True, "eligible_for_separate_approval", ("demand_and_measured_benefit_confirmed",))
