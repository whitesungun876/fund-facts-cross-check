"""Deterministic verifier for controlled-factsheet-v1, not general entailment."""
import hashlib
import re

import compare
from schemas import (
    Citation, CitationCheck, Claim, ClaimAssessment, Document, FeeValue,
    GuaranteeValue, Section, SectionRef, Span,
)

MARKER = "SYNTHETIC FIXTURE — NOT A REAL FUND"
HEADER = {
    "Document ID": "document_id", "Fund ID": "fund_id", "Share class": "share_class",
    "Terms effective from": "effective_date", "Format version": "format_version",
}
HEADINGS = re.compile(r"^\[([A-Za-z0-9_-]+)\][ \t]*\r?$", re.M)
NUMBER = r"(?:0|[1-9][0-9]{0,17})(?:\.[0-9]{1,6})?"
FEE = re.compile(r"The annual management fee for Class ([A-Za-z0-9_-]+) is (" + NUMBER + r")\s*(%|percent|bps|basis points)\.")
GUARANTEE = re.compile(r"Capital is not guaranteed\.(?: Investors may lose part or all of their investment\.)?|Capital is guaranteed\.")
RISK = "The fund is exposed to market risk."
REASONS = {
    "VALUE_MATCH": "原文支持该字段值。",
    "VALUE_MISMATCH": "模型数值与原文不同。",
    "NEGATION_CONFLICT": "模型的本金保证结论与原文相反。",
    "SOURCE_NOT_FOUND": "引用的文档不存在。",
    "SECTION_NOT_FOUND": "引用的章节不存在。",
    "QUOTE_NOT_FOUND": "引文无法在指定章节精确定位。",
    "SCOPE_MISMATCH": "文档与论断的基金、份额或日期不一致。",
    "QUOTE_INSUFFICIENT": "引文真实，但未提供足够的字段和值信息。",
    "NOT_STATED_IN_SCOPE": "检查范围内未找到声明。",
    "SOURCE_HAS_STATEMENT": "原文有明确声明，不能标为资料未说明。",
    "MODEL_ABSTAINED": "模型无法判断；这不表示资料未说明。",
    "UNSUPPORTED_EXPRESSION": "来源含有超出受控语法的内容，需人工核查。",
    "SOURCE_CONFLICT": "来源自身包含相互矛盾的声明。",
}


def parse_document(raw: bytes) -> Document:
    text = raw.decode("utf-8")
    if len(text) > 12000 or "\0" in text:
        raise ValueError("invalid document length or NUL")
    headings = list(HEADINGS.finditer(text))
    if not headings:
        raise ValueError("document requires sections")
    lines = [line.strip() for line in text[:headings[0].start()].splitlines() if line.strip()]
    if not lines or lines[0] != MARKER:
        raise ValueError("synthetic marker missing")
    metadata = {}
    for line in lines[1:]:
        name, sep, value = line.partition(":")
        if not sep or name not in HEADER or HEADER[name] in metadata:
            raise ValueError("unknown or duplicate header")
        metadata[HEADER[name]] = value.strip()
    if set(metadata) != set(HEADER.values()):
        raise ValueError("missing metadata")
    sections = {}
    for i, heading in enumerate(headings):
        name = heading.group(1)
        if name in sections:
            raise ValueError("duplicate section")
        start = heading.end()
        end = headings[i + 1].start() if i + 1 < len(headings) else len(text)
        while start < end and text[start].isspace():
            start += 1
        while end > start and text[end - 1].isspace():
            end -= 1
        if start == end:
            raise ValueError("empty section; omit optional sections instead")
        sections[name] = Section(section_id=name, text=text[start:end], start=start, end=end)
    if "management_fee" not in sections:
        raise ValueError("management fee section required")
    return Document(**metadata, synthetic=True, raw_text=text,
                    sha256=hashlib.sha256(raw).hexdigest(), sections=sections)


def normalized_text(text: str) -> tuple[str, list[tuple[int, int]]]:
    """Collapse whitespace and keep a map to original character ranges."""
    chars, offsets = [], []
    for match in re.finditer(r"\s+|\S", text):
        chars.append(" " if match.group().isspace() else match.group())
        offsets.append(match.span())
    if chars and chars[0] == " ":
        chars.pop(0)
        offsets.pop(0)
    if chars and chars[-1] == " ":
        chars.pop()
        offsets.pop()
    return "".join(chars), offsets


def clean(text: str) -> str:
    return normalized_text(text)[0]


def check_citation(citation: Citation, claim: Claim, documents: dict[str, Document]) -> CitationCheck:
    def invalid(code):
        return CitationCheck(citation=citation, status="invalid", reason_code=code, spans=[])

    doc = documents.get(citation.document_id)
    if doc is None:
        return invalid("SOURCE_NOT_FOUND")
    if doc.scope() != claim.scope():
        return invalid("SCOPE_MISMATCH")
    section = doc.sections.get(citation.section_id)
    if section is None:
        return invalid("SECTION_NOT_FOUND")
    source, mapping = normalized_text(section.text)
    quote = clean(citation.quote)
    spans = []
    position = 0
    while (index := source.find(quote, position)) != -1:
        start = section.start + mapping[index][0]
        end = section.start + mapping[index + len(quote) - 1][1]
        if clean(doc.raw_text[start:end]) != quote:
            raise RuntimeError("citation offset mapping invariant failed")
        spans.append(Span(start=start, end=end))
        position = index + 1
    if not spans:
        return invalid("QUOTE_NOT_FOUND")
    return CitationCheck(citation=citation, status="located", reason_code="EXACT_MATCH", spans=spans)


def source_facts(doc: Document):
    """Read values from text; reject anything outside the documented language."""
    if set(doc.sections) - {"management_fee", "capital_guarantee", "risk"}:
        return None, "UNSUPPORTED_EXPRESSION"
    fee_match = FEE.fullmatch(clean(doc.sections["management_fee"].text))
    if fee_match is None or fee_match[1] != doc.share_class:
        return None, "UNSUPPORTED_EXPRESSION"
    fee = FeeValue(amount=fee_match[2], unit="percent" if fee_match[3] in {"%", "percent"} else "bps")
    guarantee = None
    if section := doc.sections.get("capital_guarantee"):
        text = clean(section.text)
        matches = list(GUARANTEE.finditer(text))
        if not matches or GUARANTEE.sub("", text).strip():
            return None, "UNSUPPORTED_EXPRESSION"
        labels = {"not_guaranteed" if m.group().startswith("Capital is not") else "guaranteed" for m in matches}
        if len(labels) > 1:
            return None, "SOURCE_CONFLICT"
        # Matches must start after sentence boundaries, not inside unknown prose.
        guarantee = GuaranteeValue(label=labels.pop())
    if section := doc.sections.get("risk"):
        if clean(section.text) != RISK:
            return None, "UNSUPPORTED_EXPRESSION"
    return {"annual_management_fee": fee, "capital_guarantee": guarantee}, None


def assess_claim(claim: Claim, documents: dict[str, Document]) -> ClaimAssessment:
    checks = [check_citation(c, claim, documents) for c in claim.citations]
    docs = [d for d in documents.values() if d.scope() == claim.scope()]
    checked = []

    def result(status, code):
        return ClaimAssessment(
            claim=claim, normalized_value=compare.normalize_value(claim.value), citation_checks=checks,
            evidence_status=status, reason_code=code, reason=REASONS[code], checked_sections=checked,
        )

    if len(docs) != 1:
        return result("unverifiable", "SCOPE_MISMATCH")
    if bad := next((c for c in checks if c.status == "invalid"), None):
        return result("unverifiable", bad.reason_code)
    doc = docs[0]
    checked = [SectionRef(document_id=doc.document_id, section_id=name) for name in sorted(doc.sections)]
    facts, error = source_facts(doc)
    if error:
        return result("unverifiable", error)
    expected = facts[claim.field]
    if claim.answer_status == "abstained":
        return result("insufficient", "MODEL_ABSTAINED")
    if claim.answer_status == "not_stated":
        return result("supported", "NOT_STATED_IN_SCOPE") if expected is None else result("contradicted", "SOURCE_HAS_STATEMENT")
    if expected is None:
        return result("insufficient", "QUOTE_INSUFFICIENT")
    relevant = "management_fee" if claim.field == "annual_management_fee" else "capital_guarantee"
    quotes = [clean(c.quote) for c in claim.citations if c.section_id == relevant]
    # A real but incomplete quotation cannot borrow support from uncited text.
    enough = any(FEE.search(q) for q in quotes) if relevant == "management_fee" else any(GUARANTEE.search(q) for q in quotes)
    if not enough:
        return result("insufficient", "QUOTE_INSUFFICIENT")
    if compare.normalize_value(expected) == compare.normalize_value(claim.value):
        return result("supported", "VALUE_MATCH")
    return result("contradicted", "VALUE_MISMATCH" if relevant == "management_fee" else "NEGATION_CONFLICT")
