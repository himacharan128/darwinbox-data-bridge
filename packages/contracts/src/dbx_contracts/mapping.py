"""Mapping evidence and proposals — where the autonomy boundary actually lives.

The score is composed in deterministic code from measured evidence. The model
contributes one capped signal among several, so no model opinion can by itself push
a mapping over the auto-apply line. That cap is the mechanism behind "a model's
self-reported confidence is not a sufficient basis for the boundary".
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field


class Signal(StrEnum):
    NAME_SIM = "name_sim"  # header vs field name + declared aliases
    TYPE_FIT = "type_fit"  # share of non-null values parsing as the target type
    CONSTRAINT_FIT = "constraint_fit"  # share satisfying pattern / enum / range / length
    UNIQUE_FIT = "unique_fit"  # distinct ratio against a unique constraint
    MASK_FIT = "mask_fit"  # dominant pattern mask vs expected shape
    LLM_VOTE = "llm_vote"  # the model's semantic judgement — capped


#: Initial weights. Replaced by the Phase 2 calibration sweep.
#: LLM_VOTE is held at 0.20, inside the 0.25 cap, on purpose.
DEFAULT_WEIGHTS: dict[Signal, float] = {
    Signal.NAME_SIM: 0.30,
    Signal.TYPE_FIT: 0.20,
    Signal.LLM_VOTE: 0.20,
    Signal.CONSTRAINT_FIT: 0.15,
    Signal.MASK_FIT: 0.10,
    Signal.UNIQUE_FIT: 0.05,
}

#: Hard ceiling on the model's share of the composite score.
LLM_VOTE_CAP = 0.25


class Veto(StrEnum):
    """Conditions that bypass the weighted sum entirely."""

    TYPE_INCOMPATIBLE = "type_incompatible"  # type_fit below floor
    UNIQUENESS_IMPOSSIBLE = "uniqueness_impossible"
    UNKNOWN_COLUMN = "unknown_column"  # proposal names a column not in the manifest
    UNKNOWN_FIELD = "unknown_field"  # proposal names a field not in the schema


class Evidence(BaseModel):
    """The signal breakdown behind one candidate. This is what a review card renders.

    Escalation cards show this, never the bare number: '42% parse as email, 51% as
    phone' is a reason a consultant can act on; '0.78' is not.
    """

    signals: dict[Signal, float] = Field(default_factory=dict)
    weights: dict[Signal, float] = Field(default_factory=lambda: dict(DEFAULT_WEIGHTS))
    vetoes: list[Veto] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)

    @property
    def applicable_weight(self) -> float:
        """Total weight of the signals that could actually be measured."""
        return sum(self.weights.get(s, 0.0) for s in self.signals)

    @property
    def score(self) -> float:
        """Weighted mean over APPLICABLE signals only.

        A signal that cannot discriminate is excluded, not scored as neutral.
        Scoring `type_fit = 1.0` for a plain-string target against every column
        would give irrelevant fields a free baseline and drown the real evidence.
        Renormalising also keeps a purely deterministic decision able to reach 1.0,
        so the auto-apply threshold does not silently require a model vote.
        """
        if self.vetoes:
            return 0.0
        total = self.applicable_weight
        if total <= 0:
            return 0.0
        raw = sum(self.weights.get(s, 0.0) * v for s, v in self.signals.items())
        return round(raw / total, 4)

    @property
    def llm_share(self) -> float:
        """How much of the score the model contributed. Must never exceed the cap."""
        total = self.score
        if total <= 0:
            return 0.0
        weight = self.applicable_weight
        if weight <= 0:
            return 0.0
        contribution = (
            self.weights.get(Signal.LLM_VOTE, 0.0) * self.signals.get(Signal.LLM_VOTE, 0.0)
        ) / weight
        return round(contribution / total, 4)

    def explain(self) -> list[str]:
        """Plain-language lines, strongest signal first."""
        ordered = sorted(
            self.signals.items(), key=lambda kv: self.weights.get(kv[0], 0.0) * kv[1], reverse=True
        )
        out = [f"{s.value}: {v:.2f}" for s, v in ordered if v > 0]
        out.extend(f"VETO: {v.value}" for v in self.vetoes)
        out.extend(self.notes)
        return out


class Candidate(BaseModel):
    target_field: str
    evidence: Evidence

    @property
    def score(self) -> float:
        return self.evidence.score


class Decision(StrEnum):
    AUTO_APPLY = "auto_apply"
    REVIEW = "review"
    UNMAPPED = "unmapped"


class Thresholds(BaseModel):
    """Calibrated in Phase 2 against the labelled corpus, not asserted here."""

    auto_apply: float = 0.90
    gap: float = 0.15
    review_floor: float = 0.70
    calibrated: bool = False
    calibration_ref: str | None = None


class ColumnMapping(BaseModel):
    """The decision for one source column, with every candidate kept for audit."""

    source_file: str
    source_sheet: str | None = None
    source_column: str
    candidates: list[Candidate] = Field(default_factory=list)
    decision: Decision = Decision.UNMAPPED
    chosen_field: str | None = None
    thresholds: Thresholds = Field(default_factory=Thresholds)

    @property
    def ranked(self) -> list[Candidate]:
        return sorted(self.candidates, key=lambda c: c.score, reverse=True)

    @property
    def best(self) -> Candidate | None:
        r = self.ranked
        return r[0] if r else None

    @property
    def runner_up(self) -> Candidate | None:
        r = self.ranked
        return r[1] if len(r) > 1 else None

    @property
    def gap(self) -> float:
        best, second = self.best, self.runner_up
        if best is None:
            return 0.0
        return round(best.score - (second.score if second else 0.0), 4)
