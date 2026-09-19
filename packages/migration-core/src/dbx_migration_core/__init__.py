"""Deterministic migration engine: profiling, scoring, cleanup, validation, matching."""

from .cleanup import ColumnPlan, UnsafeValue, clean, plan_column
from .matching import (
    MatchDecision,
    PairEvidence,
    candidate_pairs,
    compare,
    conflicting_fields,
    match_records,
)
from .pipeline import Overrides, Pipeline, RunResult, apply_decision
from .profiling import decidable_date_formats, normalize_tokens, pattern_mask, profile_column
from .scoring import assert_llm_cannot_decide_alone, decide, rank_column, score_pair
from .validation import check_uniqueness, validate_record

__all__ = [
    "ColumnPlan",
    "MatchDecision",
    "Overrides",
    "PairEvidence",
    "Pipeline",
    "RunResult",
    "UnsafeValue",
    "apply_decision",
    "assert_llm_cannot_decide_alone",
    "candidate_pairs",
    "check_uniqueness",
    "clean",
    "compare",
    "conflicting_fields",
    "decidable_date_formats",
    "decide",
    "match_records",
    "normalize_tokens",
    "pattern_mask",
    "plan_column",
    "profile_column",
    "rank_column",
    "score_pair",
    "validate_record",
]
