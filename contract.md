# Fund Facts Cross-Check — frozen contract v1.0

Frozen: 2026-09-15. This is the implementation contract; `schemas.py` is its machine-readable representation. The accompanying `spec.md` records the wider design. Changes to the following boundary or wire format require an explicit version change and matching regression tests.

## 1. Delivery boundary

- Standalone Python 3.12 project: five runtime modules, one eval entry point, one unit-test file (seven source files total).
- CLI and Python interfaces only. HTTP, Bearer authentication, web UI, PDF ingestion, arbitrary file/URL input, trading and cloud deployment are deferred. CLI authentication is the local OS user; live model credentials come only from the process environment.
- Two synthetic, controlled English factsheets; two fields: `annual_management_fee`, `capital_guarantee`.
- Two distinct configurable OpenRouter model IDs receive identical initial messages and JSON schema. No model voting, third-model judge, or automatic correction of factual mistakes.
- Pydantic strict validation, exact Decimal conversion, explicit evidence results, bounded retries, JSON and Markdown output.
- Offline unit/eval results do not certify live provider compatibility. Live verification is a separate command requiring credentials and two model IDs. Never substitute recorded or fabricated answers in live mode.

## 2. Public CLI

```bash
uv sync --frozen
uv run python main.py --corpus synthetic-v1 --output results/
uv run python main.py --fields annual_management_fee --output results/
uv run python main.py --save-trace --output results/
uv run python evaluate.py --mode offline --mutations
uv run python evaluate.py --mode live
uv run python -m unittest discover -s tests -v
```

`--question` accepts 1–1,000 nonblank characters without NUL. It cannot expand the field scope or trigger tools. `--fields` accepts one or both unique field names; default is both. `--corpus` accepts only `synthetic-v1`. No CLI option accepts model URLs, secrets or arbitrary documents.

Outputs: `<output>/<request_id>/result.json`, `report.md`, optionally `trace.json`. A new request-ID directory is created without overwriting existing data; individual files use atomic rename. CLI exit codes: 0 complete; 2 configuration/input error; 3 partial; 4 failed; 5 internal/write error. Eval exit codes: 0 passes, 1 regression/live quality failure, 2 cannot execute/configuration error.

## 3. Public Python interfaces

```python
load_corpus(corpus_id: str) -> dict[str, Document]
parse_document(raw: bytes) -> Document
assess_claim(claim: Claim, documents: dict[str, Document]) -> ClaimAssessment
compare_pair(a: ClaimAssessment | None, b: ClaimAssessment | None) -> Relation
async run_cross_check(request: CrossCheckRequest) -> CrossCheckResponse
render_markdown(response: CrossCheckResponse) -> str
```

Transport/client/clock injection is an internal testing seam, never a CLI or HTTP configuration surface. `run_cross_check` builds the expected keys from the fixed corpus. Unknown scopes appear in diagnostics and leave missing slots; they are never silently repaired.

## 4. Wire format

All objects forbid extra keys and use strict scalar types. `schemas.py` exports models and JSON schema through `ModelAnswer.model_json_schema()` and `CrossCheckResponse.model_json_schema()`. JSON examples in `demo/` must validate through these same models.

Request: `{corpus_id, question, fields}`. Defaults: `synthetic-v1`, the comparison question, both fields. Dates are valid `YYYY-MM-DD` strings. IDs are nonblank, bounded strings. Values are nonnegative decimal strings, max 18 integral digits and 6 fractional digits, no exponents/NaN/infinity; float/bool coercion is prohibited.

Claim key: `(fund_id, share_class, effective_date, field)`.

```text
ModelAnswer = {schema_version: "1.0", claims: Claim[0..4]}
Claim = {fund_id, share_class, effective_date, field,
         answer_status: "stated" | "not_stated" | "abstained",
         value: FeeValue | GuaranteeValue | null, citations: Citation[0..3]}
FeeValue = {amount: DecimalString, unit: "percent" | "bps"}
GuaranteeValue = {label: "guaranteed" | "not_guaranteed"}
Citation = {document_id, section_id, quote: nonblank string[1..1000]}
```

Stated requires the field-appropriate value and at least one citation. Other statuses require null and no citations. Duplicate keys invalidate the whole model response; missing claims remain missing. JSON duplicate object members and non-finite JSON constants are rejected before model validation.

```text
ClaimAssessment = {claim, normalized_value, citation_checks,
                   evidence_status, reason_code, reason, checked_sections}
CitationCheck = {citation, status: "located" | "invalid", reason_code,
                 spans: [{start, end}]}
SectionRef = {document_id, section_id}
ModelSlot = {presence: "present" | "missing", assessment: ClaimAssessment | null}
ComparisonRow = {key: ClaimKey, a: ModelSlot, b: ModelSlot, relation}
```

Offsets are left-inclusive/right-exclusive Unicode character offsets into decoded original text, not UTF-8 bytes. Spaces/newlines may be normalized for matching; numbers, punctuation and negation may not. All matching occurrences are returned. Invalid citations yield no spans.

`CrossCheckResponse` contains: schema_version, request_id (UUID), created_at (UTC RFC3339), status, request, prompt_version, verifier_version, documents (IDs/scope/SHA-256), models (exactly a/b), rows (all expected keys), diagnostics, errors, duration_ms, synthetic=true, decision_boundary=`research_only_never_auto_act`.

`ModelRun` contains slot, model_id, status, attempts (0..2), duration_ms, prompt_tokens, completion_tokens, usage_complete, error. Status: ok/timeout/rate_limited/upstream_error/invalid_output. Missing usage is null, not zero; usage is complete only if all attempts reported both token counts. Error is `{code,message,retryable}` and excludes upstream response bodies. UUID/time/hash and row/model relationships are revalidated before serialization.

## 5. Independent state machines

Evidence: supported / contradicted / insufficient / unverifiable. Relation: agreement / disagreement / missing / incomparable / one_unknown / both_unknown. Relation never depends on evidence status.

1. Two non-null different keys -> incomparable (direct comparator).
2. Either missing -> missing.
3. Both not_stated/abstained -> both_unknown; preserve the actual statuses.
4. One stated and one unknown -> one_unknown.
5. Two stated -> compare exact normalized values.

Complete: all expected slots have valid claims and both runs are ok. Partial: at least one valid expected claim but an expected slot is missing. Failed: zero valid expected claims. Complete describes execution completeness, never factual correctness. Statements with failed evidence still occupy a slot and remain visible.

Evidence decision priority: scope validity; citation validity; complete-source grammar validity; abstention/absence; quoted field content; value comparison. An invalid attached citation invalidates the claim even if another citation is good. A real but incomplete quotation is insufficient. An unsupported or internally contradictory source is unverifiable. No evaluation labels are accessible to the runtime.

## 6. Controlled source language (explicitly narrow)

Header: synthetic marker; Document ID; Fund ID; Share class; Terms effective from; Format version (`controlled-factsheet-v1`). Required unique metadata; no extra prose in the header. Section headings are `[management_fee]`, optional `[capital_guarantee]`, optional `[risk]`. Unknown sections remain readable but make source evidence unverifiable.

- management_fee must contain one complete sentence: `The annual management fee for Class <class> is <decimal><unit>.` Unit: `%`, `percent`, `bps`, `basis points`. Whitespace may vary. Missing or empty section is a corpus format error. Unknown contents are accepted as data but cannot be verified.
- capital_guarantee, if present: `Capital is guaranteed.` or `Capital is not guaranteed.`; the latter may append `Investors may lose part or all of their investment.` Repeated identical supported sentences are allowed for citation-location tests; opposite declarations are source conflict. Conditional/extra/unrecognized sentences are unverifiable.
- risk, if present: exactly `The fund is exposed to market risk.` It does not establish a guarantee.
- To support not_stated, all sections must satisfy this grammar and no guarantee declaration can exist. No keyword-match failure can establish absence. The narrow grammar is a deliberate limitation, not general financial-language entailment.
- percent -> bps multiplies by Decimal(100). The verifier reads the supplied source number, never a fund-ID lookup or hidden expected value.

## 7. Reliability and credentials

Required for live: OPENROUTER_API_KEY, MODEL_A_ID, MODEL_B_ID; model IDs must differ. Fixed HTTPS gateway. Two attempts per model maximum (transport and structural repairs share the limit), 25s per-attempt wall time, 60s model-stage deadline, 2,000 output tokens. HTTPX connect/read/write/pool timeouts: 5/20/5/5s. Retry only transient HTTP/network failures or invalid structured output; never retry evidence disagreement. Respect Retry-After within remaining budget; otherwise stop. No nested automatic retries. Provider cancellation must release transport resources and preserve completed results.

Logs use request IDs, slot, attempt, status, duration and available token counts. Do not log credentials, questions, citations or raw upstream errors. Raw attempt output only goes to explicitly enabled synthetic trace. No .env auto-discovery, no API key search in other projects, no credential fallback.

## 8. Regression gates and delivery evidence

Unit tests cover schemas, evidence, comparison, pipeline, CLI output and mocked HTTP provider boundaries. One evaluate.py aggregates cases, metrics and live mode. Offline eval forbids external network. E01–E20 scenarios from spec are represented in cases/tests, including new source values, scope mismatch, shared hallucination, invalid citations, absence, abstention, conditional clauses, timeout and schema retry.

Report disagreement precision/recall/F1 plus confusion counts on comparable stated pairs; evidence accuracy, false-support rate and supported-answer retention on labeled assessments. Empty denominators are null. Mutation gate: disabling normalization and bypassing evidence each must break pre-existing regressions; restore originals afterward.

The implementation is verified by local tests, contract artifacts and runnable modules. A live-provider check completed on 2026-09-15 with `openai/gpt-4.1-nano` and `google/gemini-2.5-flash-lite`; `demo/fund-facts-demo.mp4` explains its inputs, outputs and verification steps using animated panels and synthetic narration. This is one compatibility observation, not a general accuracy result. README and AI_USE.md must distinguish agent-executed checks from candidate hand-verification; never infer the latter from the former.
