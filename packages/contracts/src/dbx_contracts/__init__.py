"""Shared contracts for the migration pipeline.

Nothing in this package knows what a person, an employee or a department is. It
defines a schema *language* and the vocabulary of evidence, escalation and audit.
"""

from .audit import Actor, AuditEvent
from .mapping import (
    DEFAULT_WEIGHTS,
    LLM_VOTE_CAP,
    Candidate,
    ColumnMapping,
    Decision,
    Evidence,
    Signal,
    Thresholds,
    Veto,
)
from .profile import ColumnProfile, FileProfile, PatternMask, TypeCandidate, ValueCount
from .provenance import FieldProvenance, SourceRef, Transformation
from .records import (
    CanonicalRecord,
    ExtractedRecord,
    RecordState,
    Severity,
    ValidationIssue,
    ValidationResult,
)
from .review import (
    Action,
    CaseState,
    Checked,
    EscalationClass,
    HumanDecision,
    Option,
    ReviewCase,
)
from .schema import (
    FieldSpec,
    FieldType,
    LookupSpec,
    MatchingSpec,
    MigrationSchema,
    to_strftime,
)
from .schema_import import SchemaShapeError, explain_schema_errors, normalise_schema

__all__ = [
    "DEFAULT_WEIGHTS",
    "LLM_VOTE_CAP",
    "Action",
    "Actor",
    "AuditEvent",
    "Candidate",
    "CanonicalRecord",
    "CaseState",
    "Checked",
    "ColumnMapping",
    "ColumnProfile",
    "Decision",
    "EscalationClass",
    "Evidence",
    "ExtractedRecord",
    "FieldProvenance",
    "FieldSpec",
    "FieldType",
    "FileProfile",
    "HumanDecision",
    "LookupSpec",
    "MatchingSpec",
    "MigrationSchema",
    "Option",
    "PatternMask",
    "RecordState",
    "ReviewCase",
    "SchemaShapeError",
    "Severity",
    "Signal",
    "SourceRef",
    "Thresholds",
    "Transformation",
    "TypeCandidate",
    "ValidationIssue",
    "ValidationResult",
    "ValueCount",
    "Veto",
    "explain_schema_errors",
    "normalise_schema",
    "to_strftime",
]
