"""Strict wire contracts. No networking or verification policy lives here."""
from __future__ import annotations

import json
from datetime import date, datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

FieldName = Literal["annual_management_fee", "capital_guarantee"]
Relation = Literal["agreement", "disagreement", "missing", "incomparable", "one_unknown", "both_unknown"]
EvidenceStatus = Literal["supported", "contradicted", "insufficient", "unverifiable"]
Slot = Literal["a", "b"]
Identifier = Annotated[str, Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")]
ClassName = Annotated[str, Field(min_length=1, max_length=32, pattern=r"^[A-Za-z0-9_-]+$")]
DecimalString = Annotated[str, Field(pattern=r"^(0|[1-9][0-9]{0,17})(\.[0-9]{1,6})?$")]
Nonnegative = Annotated[int, Field(ge=0)]
DateString = Annotated[str, Field(pattern=r"^\d{4}-\d{2}-\d{2}$")]
DEFAULT_QUESTION = "比较这两只基金的年度管理费和本金保证声明。每项结论附上原文依据，资料没有说明的部分标为未知。"


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, revalidate_instances="always")


def load_json(text: str):
    """Reject duplicate object members and non-JSON numeric constants."""
    def pairs(items):
        obj = {}
        for key, value in items:
            if key in obj:
                raise ValueError("duplicate JSON member")
            obj[key] = value
        return obj

    def invalid_constant(_):
        raise ValueError("non-finite JSON constant")

    return json.loads(text, object_pairs_hook=pairs, parse_constant=invalid_constant)


class CrossCheckRequest(StrictModel):
    corpus_id: Literal["synthetic-v1"] = "synthetic-v1"
    question: str = Field(default=DEFAULT_QUESTION, min_length=1, max_length=1000)
    fields: list[FieldName] = Field(default_factory=lambda: ["annual_management_fee", "capital_guarantee"], min_length=1, max_length=2)

    @field_validator("question")
    @classmethod
    def question_text(cls, value):
        if not value.strip() or "\0" in value:
            raise ValueError("question must be nonblank and contain no NUL")
        return value

    @field_validator("fields")
    @classmethod
    def unique_fields(cls, value):
        if len(set(value)) != len(value):
            raise ValueError("duplicate fields")
        return value


class Scope(StrictModel):
    fund_id: Identifier
    share_class: ClassName
    effective_date: DateString

    @field_validator("effective_date")
    @classmethod
    def valid_date(cls, value):
        date.fromisoformat(value)
        return value

    def scope(self):
        return self.fund_id, self.share_class, self.effective_date


class ClaimKey(Scope):
    field: FieldName

    def key(self):
        return (*self.scope(), self.field)


class FeeValue(StrictModel):
    amount: DecimalString
    unit: Literal["percent", "bps"]


class NormalizedFee(StrictModel):
    amount: str = Field(pattern=r"^(0|[1-9][0-9]{0,19})(\.[0-9]{1,6})?$")
    unit: Literal["bps"] = "bps"


class GuaranteeValue(StrictModel):
    label: Literal["guaranteed", "not_guaranteed"]


class Citation(StrictModel):
    document_id: Identifier
    section_id: Identifier
    quote: str = Field(min_length=1, max_length=1000)

    @field_validator("quote")
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError("blank quote")
        return value


class Claim(ClaimKey):
    answer_status: Literal["stated", "not_stated", "abstained"]
    value: FeeValue | GuaranteeValue | None
    citations: list[Citation] = Field(max_length=3)

    @model_validator(mode="after")
    def field_contract(self):
        if self.answer_status == "stated":
            expected = FeeValue if self.field == "annual_management_fee" else GuaranteeValue
            if not isinstance(self.value, expected) or not self.citations:
                raise ValueError("stated requires matching value and citations")
        elif self.value is not None or self.citations:
            raise ValueError("unknown requires null value and no citations")
        return self


class ModelAnswer(StrictModel):
    schema_version: Literal["1.0"]
    claims: list[Claim] = Field(max_length=4)

    @model_validator(mode="after")
    def no_duplicate_claims(self):
        if len({c.key() for c in self.claims}) != len(self.claims):
            raise ValueError("duplicate claim key")
        return self


class Span(StrictModel):
    start: Nonnegative
    end: Nonnegative

    @model_validator(mode="after")
    def ordered(self):
        if self.start >= self.end:
            raise ValueError("empty or reversed span")
        return self


class Section(Span):
    section_id: Identifier
    text: str


class DocumentRef(Scope):
    document_id: Identifier
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class Document(DocumentRef):
    synthetic: Literal[True]
    format_version: Literal["controlled-factsheet-v1"]
    raw_text: str = Field(min_length=1, max_length=12000)
    sections: dict[str, Section]

    @model_validator(mode="after")
    def indexed_text(self):
        for key, sec in self.sections.items():
            if key != sec.section_id or sec.end > len(self.raw_text) or self.raw_text[sec.start:sec.end] != sec.text:
                raise ValueError("invalid section index")
        return self


class SectionRef(StrictModel):
    document_id: Identifier
    section_id: Identifier


class CitationCheck(StrictModel):
    citation: Citation
    status: Literal["located", "invalid"]
    reason_code: str
    spans: list[Span]

    @model_validator(mode="after")
    def located_has_spans(self):
        if (self.status == "located") != bool(self.spans):
            raise ValueError("citation status/spans mismatch")
        return self


class ClaimAssessment(StrictModel):
    claim: Claim
    normalized_value: NormalizedFee | GuaranteeValue | None
    citation_checks: list[CitationCheck]
    evidence_status: EvidenceStatus
    reason_code: str
    reason: str
    checked_sections: list[SectionRef]

    @model_validator(mode="after")
    def assessment_shape(self):
        if [x.citation for x in self.citation_checks] != self.claim.citations:
            raise ValueError("every citation must be checked in order")
        expected = NormalizedFee if self.claim.field == "annual_management_fee" else GuaranteeValue
        if self.claim.answer_status == "stated":
            if not isinstance(self.normalized_value, expected):
                raise ValueError("normalization type mismatch")
        elif self.normalized_value is not None:
            raise ValueError("unknown has no normalized value")
        if self.evidence_status == "supported" and any(c.status == "invalid" for c in self.citation_checks):
            raise ValueError("invalid citation cannot be supported")
        return self


class ErrorDetail(StrictModel):
    code: str
    message: str
    retryable: bool


class ModelRun(StrictModel):
    slot: Slot
    model_id: str = Field(min_length=1)
    status: Literal["ok", "timeout", "rate_limited", "upstream_error", "invalid_output"]
    attempts: int = Field(ge=0, le=2)
    duration_ms: Nonnegative
    prompt_tokens: Nonnegative | None
    completion_tokens: Nonnegative | None
    usage_complete: bool
    error: ErrorDetail | None

    @model_validator(mode="after")
    def run_invariants(self):
        if (self.status == "ok") != (self.error is None):
            raise ValueError("run status/error mismatch")
        if self.usage_complete != (self.prompt_tokens is not None and self.completion_tokens is not None):
            raise ValueError("incomplete usage must have null totals")
        return self


class ModelSlot(StrictModel):
    presence: Literal["present", "missing"]
    assessment: ClaimAssessment | None

    @model_validator(mode="after")
    def presence_contract(self):
        if (self.presence == "present") != (self.assessment is not None):
            raise ValueError("presence mismatch")
        return self


class ComparisonRow(StrictModel):
    key: ClaimKey
    a: ModelSlot
    b: ModelSlot
    relation: Relation

    @model_validator(mode="after")
    def same_keys(self):
        for slot in [self.a, self.b]:
            if slot.assessment and slot.assessment.claim.key() != self.key.key():
                raise ValueError("row key mismatch")
        return self


class Diagnostic(StrictModel):
    slot: Slot | None
    code: str
    message: str
    claim_key: ClaimKey | None


class CrossCheckResponse(StrictModel):
    schema_version: Literal["1.0"]
    request_id: str
    created_at: str
    status: Literal["complete", "partial", "failed"]
    request: CrossCheckRequest
    prompt_version: str
    verifier_version: str
    documents: list[DocumentRef] = Field(min_length=2, max_length=2)
    models: list[ModelRun] = Field(min_length=2, max_length=2)
    rows: list[ComparisonRow]
    diagnostics: list[Diagnostic]
    errors: list[ErrorDetail]
    duration_ms: Nonnegative
    synthetic: Literal[True]
    decision_boundary: Literal["research_only_never_auto_act"]

    @field_validator("request_id")
    @classmethod
    def uuid_string(cls, value):
        if str(UUID(value)) != value:
            raise ValueError("noncanonical UUID")
        return value

    @field_validator("created_at")
    @classmethod
    def utc_timestamp(cls, value):
        parsed = datetime.fromisoformat(value)
        if not value.endswith("Z") or parsed.utcoffset() is None or parsed.utcoffset().total_seconds() != 0:
            raise ValueError("UTC RFC3339 timestamp ending in Z required")
        return value

    @model_validator(mode="after")
    def complete_matrix(self):
        if [m.slot for m in self.models] != ["a", "b"] or self.models[0].model_id == self.models[1].model_id:
            raise ValueError("two distinct ordered model slots required")
        if len({d.document_id for d in self.documents}) != 2 or len({d.scope() for d in self.documents}) != 2:
            raise ValueError("unique documents/scopes required")
        expected = sorted((*d.scope(), f) for d in self.documents for f in self.request.fields)
        if [r.key.key() for r in self.rows] != expected:
            raise ValueError("all expected rows required in canonical order")
        present = sum(s.presence == "present" for r in self.rows for s in [r.a, r.b])
        for i, name in enumerate(["a", "b"]):
            if self.models[i].status != "ok" and any(getattr(r, name).assessment for r in self.rows):
                raise ValueError("failed model cannot supply valid claims")
        expected_status = "failed" if present == 0 else "complete" if present == 2 * len(expected) else "partial"
        if self.status != expected_status:
            raise ValueError("top-level completeness mismatch")
        return self
