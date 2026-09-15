"""CLI and application orchestration. The corpus is fixed and local."""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import html
import json
import logging
import sys
import time
from contextlib import AsyncExitStack
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import httpx

import compare
import evidence
import providers
from schemas import (
    ClaimKey, ComparisonRow, CrossCheckRequest, CrossCheckResponse, Diagnostic,
    Document, DocumentRef, ErrorDetail, ModelRun, ModelSlot,
)

ROOT = Path(__file__).resolve().parent
VERIFIER_VERSION = "controlled-v1"


def load_corpus(corpus_id: str) -> dict[str, Document]:
    if corpus_id != "synthetic-v1":
        raise ValueError("unknown corpus")
    docs = [evidence.parse_document((ROOT / "fixtures" / name).read_bytes()) for name in ["fund_alpha.md", "fund_beta.md"]]
    if len({d.document_id for d in docs}) != 2 or len({d.scope() for d in docs}) != 2:
        raise ValueError("duplicate document ID or scope")
    return {d.document_id: d for d in docs}


def as_key(claim):
    return ClaimKey(fund_id=claim.fund_id, share_class=claim.share_class,
                    effective_date=claim.effective_date, field=claim.field)


def assemble_response(request, documents, results, request_id, duration_ms):
    keys = {(*d.scope(), f): ClaimKey(fund_id=d.fund_id, share_class=d.share_class,
                                    effective_date=d.effective_date, field=f)
            for d in documents.values() for f in request.fields}
    indexed, diagnostics = {}, []
    models = []
    for slot, (run, answer) in zip(["a", "b"], results, strict=True):
        models.append(run)
        indexed[slot] = {}
        if run.slot != slot or (run.status != "ok" and answer is not None):
            raise ValueError("invalid provider boundary result")
        if answer is not None:
            for claim in answer.claims:
                if claim.key() not in keys:
                    diagnostics.append(Diagnostic(slot=slot, code="SCOPE_MISMATCH", message="Claim scope is outside the requested matrix.", claim_key=as_key(claim)))
                else:
                    indexed[slot][claim.key()] = evidence.assess_claim(claim, documents)
        for key in sorted(keys):
            if key not in indexed[slot]:
                diagnostics.append(Diagnostic(slot=slot, code="MISSING_CLAIM", message="No valid claim in this model slot.", claim_key=keys[key]))
    rows = []
    for key in sorted(keys):
        a, b = indexed["a"].get(key), indexed["b"].get(key)
        rows.append(ComparisonRow(
            key=keys[key], a=ModelSlot(presence="present" if a else "missing", assessment=a),
            b=ModelSlot(presence="present" if b else "missing", assessment=b), relation=compare.compare_pair(a, b),
        ))
    present = sum(len(value) for value in indexed.values())
    status = "failed" if not present else "complete" if present == 2 * len(keys) else "partial"
    errors = [m.error for m in models if m.error]
    if not present:
        errors.append(ErrorDetail(code="NO_VALID_CLAIMS", message="No valid claims for the requested scope.", retryable=False))
    response = CrossCheckResponse(
        schema_version="1.0", request_id=request_id, created_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        status=status, request=request, prompt_version=providers.PROMPT_VERSION, verifier_version=VERIFIER_VERSION,
        documents=[DocumentRef(**{k: getattr(d, k) for k in DocumentRef.model_fields}) for d in sorted(documents.values(), key=lambda d: d.document_id)],
        models=models, rows=rows, diagnostics=diagnostics, errors=errors, duration_ms=duration_ms,
        synthetic=True, decision_boundary="research_only_never_auto_act",
    )
    return CrossCheckResponse.model_validate(response.model_dump())


async def run_cross_check(request: CrossCheckRequest, *, settings=None, client=None,
                          budget=providers.Budget(), model_call=None, trace=None) -> CrossCheckResponse:
    request = CrossCheckRequest.model_validate(request)
    settings = settings or providers.Settings.from_env()
    if not settings.api_key.strip() or not settings.model_a.strip() or not settings.model_b.strip() or settings.model_a == settings.model_b:
        raise ValueError("two distinct models and a key required")
    documents = load_corpus(request.corpus_id)
    started, request_id = time.monotonic(), str(uuid4())
    deadline = started + budget.total_seconds
    messages = providers.build_messages(request, documents)
    # Metadata is needed even without raw traces, including interrupted attempts.
    records = []
    providers.event("request_started", request_id=request_id)
    async with AsyncExitStack() as stack:
        if client is None:
            client = await stack.enter_async_context(httpx.AsyncClient(timeout=providers.TIMEOUT, follow_redirects=False))
        invoke = model_call or providers.call_model
        tasks = [asyncio.create_task(invoke(slot, model, messages, deadline, client=client,
                  api_key=settings.api_key, request_id=request_id, budget=budget, trace=records, save_raw=trace is not None))
                 for slot, model in [("a", settings.model_a), ("b", settings.model_b)]]
        try:
            _, pending = await asyncio.wait(tasks, timeout=max(0, deadline - time.monotonic()))
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            results = []
            for slot, model, task in zip(["a", "b"], [settings.model_a, settings.model_b], tasks, strict=True):
                if task in pending:
                    attempts = max((r["attempt"] for r in records or [] if r["slot"] == slot), default=0)
                    results.append((ModelRun(slot=slot, model_id=model, status="timeout", attempts=attempts,
                        duration_ms=int((time.monotonic() - started) * 1000), prompt_tokens=None,
                        completion_tokens=None, usage_complete=False,
                        error=ErrorDetail(code="DEADLINE_EXCEEDED", message="Model stage deadline exceeded.", retryable=True)), None))
                else:
                    results.append(task.result())
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
    response = assemble_response(request, documents, results, request_id, int((time.monotonic() - started) * 1000))
    providers.event("verification_finished", request_id=request_id, rows=len(response.rows))
    providers.event("request_finished", request_id=request_id, status=response.status, duration_ms=response.duration_ms)
    if trace is not None:
        trace.update({"kind": "live_provider_trace", "request_id": request_id,
            "messages_sha256": hashlib.sha256(json.dumps(messages, ensure_ascii=False).encode()).hexdigest(),
            "initial_messages": messages, "prompt_version": providers.PROMPT_VERSION,
            "verifier_version": VERIFIER_VERSION, "documents": [d.model_dump() for d in response.documents],
            "budget": {"total_seconds": budget.total_seconds, "attempt_seconds": budget.attempt_seconds, "max_attempts_per_model": 2},
            "attempts": records})
    return response


def display_cell(value):
    return html.escape(str(value), quote=True).replace("|", "&#124;").replace("\r", " ").replace("\n", " / ")


def render_markdown(response: CrossCheckResponse) -> str:
    response = CrossCheckResponse.model_validate(response)
    lines = ["# Fund Facts Cross-Check", "", "Synthetic documents only. Research only; never auto-act.", "",
             f"Request: {response.request_id} | Execution: {response.status}", "",
             "Execution completeness and model agreement are independent of evidence support.", "",
             "| Fund / class / effective date | Field | Model A | Model B | Relation | A evidence | B evidence |",
             "| --- | --- | --- | --- | --- | --- | --- |"]

    def value(slot):
        if slot.assessment is None:
            return "missing"
        assessment = slot.assessment
        return json.dumps(assessment.claim.value.model_dump(), ensure_ascii=False) if assessment.claim.value else assessment.claim.answer_status

    def support(slot):
        if slot.assessment is None:
            return "No claim"
        a = slot.assessment
        quotes = "; ".join(f"{c.citation.document_id}/{c.citation.section_id}: {c.citation.quote}" for c in a.citation_checks)
        scope = ", ".join(f"{s.document_id}/{s.section_id}" for s in a.checked_sections)
        return f"{a.evidence_status}: {a.reason} Sources: {quotes or 'none'}; checked: {scope or 'none'}"

    for row in response.rows:
        cells = [" / ".join(row.key.scope()), row.key.field, value(row.a), value(row.b), row.relation, support(row.a), support(row.b)]
        lines.append("| " + " | ".join(display_cell(x) for x in cells) + " |")
    lines += ["", "## Model calls", ""]
    for model in response.models:
        lines.append(f"- {display_cell(model.slot)}: {display_cell(model.model_id)} — {model.status}; attempts={model.attempts}; tokens={model.prompt_tokens}/{model.completion_tokens} (null = unavailable)")
    if response.diagnostics or response.errors:
        lines += ["", "## Diagnostics", ""]
        for diag in response.diagnostics:
            lines.append(f"- {display_cell(diag.slot)} {display_cell(diag.code)}: {display_cell(diag.message)}")
        for error in response.errors:
            lines.append(f"- {display_cell(error.code)}: {display_cell(error.message)}")
    return "\n".join(lines) + "\n"


def save_result(response, output: Path, trace=None):
    response = CrossCheckResponse.model_validate(response)
    directory = output / response.request_id
    directory.mkdir(parents=True, exist_ok=False)
    files = {"result.json": response.model_dump_json(indent=2) + "\n", "report.md": render_markdown(response)}
    if trace is not None:
        files["trace.json"] = json.dumps(trace, ensure_ascii=False, indent=2) + "\n"
    for name, text in files.items():
        temporary = directory / (name + ".tmp")
        temporary.write_text(text, encoding="utf-8")
        temporary.replace(directory / name)
    return directory


def cli(argv=None):
    parser = argparse.ArgumentParser(description="Cross-check synthetic fund facts using two models.")
    parser.add_argument("--corpus", default="synthetic-v1")
    parser.add_argument("--question", default=CrossCheckRequest().question)
    parser.add_argument("--fields", nargs="+", default=CrossCheckRequest().fields)
    parser.add_argument("--output", type=Path, default=Path("results"))
    parser.add_argument("--save-trace", action="store_true")
    args = parser.parse_args(argv)
    try:
        request = CrossCheckRequest(corpus_id=args.corpus, question=args.question, fields=args.fields)
        settings = providers.Settings.from_env()
        load_corpus(request.corpus_id)
    except (ValueError, OSError):
        print("Invalid input, corpus or live configuration. Check contract.md and environment variable names.", file=sys.stderr)
        return 2
    trace = {} if args.save_trace else None
    try:
        response = asyncio.run(run_cross_check(request, settings=settings, trace=trace))
        directory = save_result(response, args.output, trace)
    except Exception:
        print("Internal execution or output error; no success result claimed.", file=sys.stderr)
        return 5
    print(f"{response.status}: {directory}")
    return {"complete": 0, "partial": 3, "failed": 4}[response.status]


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    raise SystemExit(cli())
