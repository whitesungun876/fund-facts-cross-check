"""Exact normalization and model agreement, independent of evidence support."""
from decimal import Decimal, localcontext

from schemas import ClaimAssessment, FeeValue, GuaranteeValue, NormalizedFee, Relation


def normalize_fee(value: FeeValue) -> NormalizedFee:
    with localcontext() as context:
        context.prec = 40
        amount = Decimal(value.amount) * (100 if value.unit == "percent" else 1)
        text = format(amount, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return NormalizedFee(amount=text, unit="bps")


def normalize_value(value):
    return normalize_fee(value) if isinstance(value, FeeValue) else value


def compare_pair(a: ClaimAssessment | None, b: ClaimAssessment | None) -> Relation:
    if a is not None and b is not None and a.claim.key() != b.claim.key():
        return "incomparable"
    if a is None or b is None:
        return "missing"
    unknown_a, unknown_b = a.claim.answer_status != "stated", b.claim.answer_status != "stated"
    if unknown_a and unknown_b:
        return "both_unknown"
    if unknown_a or unknown_b:
        return "one_unknown"
    # Recompute from typed raw claims; don't trust caller-supplied normalized values.
    av, bv = normalize_value(a.claim.value), normalize_value(b.claim.value)
    if isinstance(av, NormalizedFee) and isinstance(bv, NormalizedFee):
        equal = Decimal(av.amount) == Decimal(bv.amount)
    else:
        equal = isinstance(av, GuaranteeValue) and isinstance(bv, GuaranteeValue) and av.label == bv.label
    return "agreement" if equal else "disagreement"
