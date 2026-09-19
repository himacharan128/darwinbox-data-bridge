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


#: Calibrated against the Phase 1 corpus (scripts/eval_mapping.py).
#: Value evidence outweighs the header name on purpose: a header is a label somebody
#: typed, the values are the data. "mgr" is a poor name match for manager_employee_id
#: yet every value satisfies its pattern, and the pattern is the better witness.
DEFAULT_WEIGHTS: dict[Signal, float] = {
    Signal.TYPE_FIT: 0.25,
    Signal.NAME_SIM: 0.20,
    Signal.CONSTRAINT_FIT: 0.20,
    Signal.LLM_VOTE: 0.20,
    Signal.MASK_FIT: 0.15,
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
    def deterministic_signals(self) -> dict[Signal, float]:
        return {s: v for s, v in self.signals.items() if s is not Signal.LLM_VOTE}

    @property
    def deterministic_score(self) -> float:
        """Weighted mean over the APPLICABLE measured signals.

        A signal that cannot discriminate is excluded, not scored as neutral.
        Scoring type_fit = 1.0 for a plain-string target against every column would
        give irrelevant fields a free baseline and drown the real evidence.
        """
        det = self.deterministic_signals
        weight = sum(self.weights.get(s, 0.0) for s in det)
        if weight <= 0:
            return 0.0
        return sum(self.weights.get(s, 0.0) * v for s, v in det.items()) / weight

    @property
    def score(self) -> float:
        """Deterministic evidence, with the model's vote blended in under a hard cap.

        The model's weight is fixed against the FULL scale rather than renormalised
        alongside the measured signals. That is what makes the cap structural: a
        mapping with no deterministic support at all cannot exceed LLM_VOTE_CAP, so
        it can never reach an auto-apply threshold no matter how certain the model
        sounds. Renormalising the vote too would have let its share reach ~87% on a
        target field that declares no constraints.
        """
        if self.vetoes:
            return 0.0
        det = self.deterministic_score
        if Signal.LLM_VOTE not in self.signals:
            return round(det, 4)
        vote = self.signals[Signal.LLM_VOTE]
        return round((1.0 - LLM_VOTE_CAP) * det + LLM_VOTE_CAP * vote, 4)

    @property
    def llm_share(self) -> float:
        """The model's contribution on the 0-1 scale. Bounded by LLM_VOTE_CAP."""
        if Signal.LLM_VOTE not in self.signals or self.vetoes:
            return 0.0
        return round(LLM_VOTE_CAP * self.signals[Signal.LLM_VOTE], 4)

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

    auto_apply: float = 0.75
    gap: float = 0.15
    review_floor: float = 0.50
    calibrated: bool = True
    calibration_ref: str = "scripts/eval_mapping.py --sweep, Phase 1 corpus (35 columns)"


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
