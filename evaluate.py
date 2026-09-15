"""One eval entry point: deterministic cases, mutation gates and explicit live mode."""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from typing import Any, Literal
from unittest.mock import patch
from uuid import uuid4

import compare
import evidence
import main
from schemas import (Claim, CrossCheckRequest, ErrorDetail, ModelAnswer, ModelRun,
                     NormalizedFee, StrictModel, load_json)

ROOT = Path(__file__).resolve().parent


class DocumentInput(StrictModel):
    document_id: str
    text: str


class EvalCase(StrictModel):
    id: str
    tags: list[str]
    stage: Literal["schema", "assessment", "comparison", "pipeline"]
    documents: list[DocumentInput]
    model_inputs: dict[str, Any]
    expected: dict[str, Any]


def cases():
    result = [EvalCase.model_validate(c) for c in load_json((ROOT / "evals/cases.json").read_text())]
    if len({c.id for c in result}) != len(result):
        raise ValueError("duplicate eval ID")
    return result


def fixture_run(slot, status="ok"):
    return ModelRun(slot=slot, model_id=f"fixture/model-{slot}", status=status, attempts=1, duration_ms=0,
                    prompt_tokens=None, completion_tokens=None, usage_complete=False,
                    error=None if status == "ok" else ErrorDetail(code="INJECTED_FAILURE", message="Explicit test fixture.", retryable=True))


def run_case(case: EvalCase):
    documents = main.load_corpus("synthetic-v1")
    for override in case.documents:
        document = evidence.parse_document(override.text.encode())
        if document.document_id != override.document_id:
            raise ValueError("fixture document identity mismatch")
        documents[override.document_id] = document
    inputs = case.model_inputs
    if case.stage == "schema":
        try:
            raw = load_json(inputs["answer"]) if isinstance(inputs["answer"], str) else inputs["answer"]
            ModelAnswer.model_validate(raw)
            return {"valid": True}
        except ValueError:
            return {"valid": False}
    if case.stage == "pipeline":
        results = []
        for slot in ["a", "b"]:
            status = inputs.get("status_" + slot, "ok")
            answer = ModelAnswer.model_validate(inputs[slot]) if status == "ok" else None
            results.append((fixture_run(slot, status), answer))
        response = main.assemble_response(CrossCheckRequest(), documents, results, str(uuid4()), 0)
        return {"status": response.status, "row_count": len(response.rows),
                "missing_slots": sum(s.presence == "missing" for r in response.rows for s in [r.a, r.b])}
    assessments = {slot: evidence.assess_claim(Claim.model_validate(inputs[slot]), documents) if inputs.get(slot) is not None else None for slot in ["a", "b"]}
    observation = {}
    for slot, assessment in assessments.items():
        if assessment:
            observation["evidence_" + slot] = assessment.evidence_status
            observation["reason_" + slot] = assessment.reason_code
    if case.stage == "comparison":
        observation["relation"] = compare.compare_pair(assessments["a"], assessments["b"])
    return observation


def ratio(numerator, denominator):
    return numerator / denominator if denominator else None


def offline_report():
    details = []
    tp = fp = fn = tn = 0
    evidence_total = correct = supported_total = retained = nonsupported = false_support = 0
    for case in cases():
        observed = run_case(case)
        if not case.expected:
            raise ValueError("empty expected labels")
        passed = all(observed.get(key) == value for key, value in case.expected.items())
        details.append({"id": case.id, "passed": passed, "expected": case.expected, "observed": observed})
        expected_relation = case.expected.get("relation")
        if expected_relation in {"agreement", "disagreement"}:
            positive, predicted = expected_relation == "disagreement", observed.get("relation") == "disagreement"
            tp += positive and predicted
            fp += not positive and predicted
            fn += positive and not predicted
            tn += not positive and not predicted
        for slot in ["a", "b"]:
            key = "evidence_" + slot
            if key not in case.expected:
                continue
            expected, predicted = case.expected[key], observed.get(key)
            evidence_total += 1
            correct += expected == predicted
            supported_total += expected == "supported"
            retained += expected == "supported" and predicted == "supported"
            nonsupported += expected != "supported"
            false_support += expected != "supported" and predicted == "supported"
    return {"mode": "offline_synthetic_injected", "passed": all(d["passed"] for d in details),
            "case_count": len(details), "passed_cases": sum(d["passed"] for d in details),
            "disagreement": {"tp": tp, "fp": fp, "fn": fn, "tn": tn,
                "precision": ratio(tp, tp + fp), "recall": ratio(tp, tp + fn), "f1": ratio(2 * tp, 2 * tp + fp + fn)},
            "evidence": {"count": evidence_total, "accuracy": ratio(correct, evidence_total),
                "false_support_count": false_support, "false_support_rate": ratio(false_support, nonsupported),
                "supported_count": supported_total, "retention": ratio(retained, supported_total)},
            "cases": details}


def mutation_report():
    original_assess = evidence.assess_claim

    def always_supported(*args, **kwargs):
        # Intentionally bypass validation too, simulating removal of the evidence gate.
        return original_assess(*args, **kwargs).model_copy(update={"evidence_status": "supported"})

    mutations = {}
    with patch.object(compare, "normalize_fee", lambda v: NormalizedFee(amount=v.amount, unit="bps")):
        mutated = offline_report()
        mutations["normalization_disabled"] = [c["id"] for c in mutated["cases"] if not c["passed"]]
    with patch.object(evidence, "assess_claim", always_supported):
        # Restrict to field/comparison stages: pipeline schema must not obscure which assertion caught this mutation.
        failures = []
        for case in cases():
            if case.stage in {"assessment", "comparison"}:
                observed = run_case(case)
                if any(observed.get(k) != v for k, v in case.expected.items()):
                    failures.append(case.id)
        mutations["evidence_gate_disabled"] = failures
    return {"passed": all(bool(v) for v in mutations.values()), "caught_by": mutations}


def live_report(response):
    # Independent fixture labels, not derived from source_facts or normalized runtime results.
    labels = {
        ("alpha", "annual_management_fee"): {"amount": "30", "unit": "bps"},
        ("alpha", "capital_guarantee"): {"label": "not_guaranteed"},
        ("beta", "annual_management_fee"): {"amount": "45", "unit": "bps"},
        ("beta", "capital_guarantee"): None,
    }
    observations, count = [], 0
    for row in response.rows:
        expected = labels[(row.key.fund_id, row.key.field)]
        for name in ["a", "b"]:
            assessment = getattr(row, name).assessment
            count += assessment is not None
            correct = assessment is not None and assessment.evidence_status == "supported"
            if correct:
                value = assessment.normalized_value.model_dump() if assessment.normalized_value else None
                correct = value == expected and assessment.claim.answer_status == ("not_stated" if expected is None else "stated")
            observations.append({"key": row.key.model_dump(), "slot": name, "passed": bool(correct)})
    return {"mode": "live", "passed": response.status == "complete" and all(c["passed"] for c in observations),
            "request_id": response.request_id, "field_coverage": ratio(count, 2 * len(response.rows)),
            "models": [m.model_dump() for m in response.models], "checks": observations}


def cli(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=["offline", "live"], default="offline")
    parser.add_argument("--mutations", action="store_true")
    parser.add_argument("--output", type=Path, default=Path("results"))
    args = parser.parse_args(argv)
    try:
        if args.mode == "offline":
            report = offline_report()
            if args.mutations:
                report["mutations"] = mutation_report()
                report["passed"] = report["passed"] and report["mutations"]["passed"]
        else:
            if args.mutations:
                raise ValueError("mutations only available offline")
            trace = {}
            response = asyncio.run(main.run_cross_check(CrossCheckRequest(), trace=trace))
            directory = main.save_result(response, args.output, trace)
            report = live_report(response)
            (directory / "eval.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if report["passed"] else 1
    except (ValueError, OSError):
        print(json.dumps({"passed": False, "error": "Evaluation could not execute; check configuration and fixture contracts."}))
        return 2


if __name__ == "__main__":
    raise SystemExit(cli())
