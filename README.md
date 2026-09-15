# Fund Facts Cross-Check

Two models read the same synthetic fund factsheets. The application compares their claims and separately checks whether the quoted source supports each claim. A shared mistake remains a mistake even when both models agree.

**Status:** standalone CLI implemented and verified with 43 unit tests, 27 fixed evaluation cases, two mutation gates, and one recorded live OpenRouter run. The live run used `openai/gpt-4.1-nano` and `google/gemini-2.5-flash-lite`; both returned all four requested claims and all eight model/field checks passed. No HTTP server or authentication endpoint is implemented. This is a controlled-language prototype, not a financial document verifier for arbitrary inputs.

## Start here

Python 3.12 and uv are required. From this project directory:

```bash
uv sync --frozen
uv run python -m unittest discover -s tests -v
uv run python evaluate.py --mode offline --mutations
```

After dependencies are installed, the unit suite and offline eval require no credentials or network. `uv.lock` pins the tested dependency resolution. Offline eval exits nonzero if any expected result fails or either deliberately broken implementation escapes detection.

For real model calls, set `OPENROUTER_API_KEY`, `MODEL_A_ID`, and `MODEL_B_ID` in your process environment. Use two distinct model IDs that support JSON-schema structured output. `.env.example` contains the two model IDs used in the recorded run; the application does not auto-load `.env` or search other projects for keys. Models are not silently substituted if configuration or capabilities are invalid.

```bash
uv run python main.py --corpus synthetic-v1 --output results/ --save-trace
uv run python evaluate.py --mode live
```

These commands contact OpenRouter and can consume the configured account's model credits. Each run produces a new request-ID directory. Live eval only passes if both models return all expected facts with supported evidence and the correct fixture values. Failed/missing calls cannot produce a passing live result.

For explicit local-file configuration, copy `.env.example` to `.env` only if `.env` does not already exist, then fill in your key locally. GPT-4.1 Nano and Gemini 2.5 Flash-Lite passed the recorded live run on 2026-09-15; provider behavior can still change. Keep the key out of chat and version control. uv can explicitly load the file into the process environment:

```bash
uv run --frozen --env-file .env python evaluate.py --mode live
```

The application itself still does not auto-discover credentials or `.env` files.

## Recorded live run

The saved walkthrough records a real OpenRouter execution over the two synthetic documents, not injected model output. Request `5a72ed5d-720d-448a-b795-d6c02be7ff85` completed with 100% field coverage: both models agreed on Alpha's 0.30% fee and explicit lack of capital guarantee, agreed on Beta's 45 bps fee, and correctly returned `not_stated` for Beta's missing guarantee statement. Every claim passed the independent evidence check.

See [the five-minute walkthrough](demo/walkthrough.md) and [the compact live-run record](demo/live_run.json). The record contains model IDs, token usage, claims, citations, relation labels and evidence results, but no credentials.

## Adversarial example: agreement with bad evidence

This table describes the **explicitly injected** example in [demo/injected_answers.json](demo/injected_answers.json), not a live model transcript:

| Scope | Model A | Model B | Relation | Evidence |
| --- | --- | --- | --- | --- |
| Alpha management fee | 0.30% | 30 bps | agreement | both supported |
| Alpha capital guarantee | guaranteed | guaranteed | agreement | both contradicted by the source |
| Beta management fee | 45 bps | 45 bps | agreement | both supported |
| Beta capital guarantee | not_stated | not_stated | both_unknown | no statement in checked scope |

See [the generated adversarial report](demo/injected_report.md). It uses the real parser, verifier, comparator and renderer with labeled synthetic responses and demonstrates the regression that model consensus alone would miss.

## Architecture and frozen boundaries

| File | Responsibility |
| --- | --- |
| `schemas.py` | Strict input, model and final-output schemas |
| `compare.py` | Exact Decimal unit normalization and independent relation classification |
| `evidence.py` | Document parsing, citation offsets and controlled-language evidence rules |
| `providers.py` | Parallel-call adapter, structured-output request, shared retry budget and safe errors |
| `main.py` | Fixed corpus, request matrix, partial results, CLI and file output |
| `evaluate.py` | Labeled regression evaluation, mutation gates and live evaluation |
| `tests/test_core.py` | Unit tests plus mocked transport/pipeline/CLI behavior |

The project has seven Python source files, including the unit suite, and no dependency on its surrounding workspace. Copy this directory to run it elsewhere.

- [contract.md](contract.md) freezes interfaces, source grammar, status semantics and scope.
- [agent.md](agent.md) contains development rules. It must be read explicitly by tools that only auto-discover `AGENTS.md`.
- [spec.md](spec.md) preserves the broader design. Its optional HTTP/Bearer section is deferred.
- JSON schema can be obtained with `ModelAnswer.model_json_schema()` and `CrossCheckResponse.model_json_schema()` from `schemas.py`.

## What the verifier actually checks

1. Claim scope matches fund, share class, effective date and field.
2. Every quote exists in the specified document section; Unicode positions map back to the original text.
3. The full source follows the narrow grammar in contract.md. Unknown prose, conditional fees and conflicting statements are unverifiable.
4. A quote includes enough field/value information; a real but incomplete quote is insufficient.
5. Values match the source after exact conversion (`percent × 100 = bps`), including explicit negative guarantee statements.

The verifier parses numbers from the text. It does not look up expected answers by fund ID. Test E20 changes the source to a previously unseen fee. Expected labels live only in evaluation code/data.

No statement is different from a model abstaining. To support `not_stated`, every relevant part of the controlled document must be readable and no statement may exist. Unrecognized wording cannot establish absence. The tool checks claims about the given text, not the accuracy or legal meaning of real fund documents.

## Failures, output and secrets

- `complete` means every expected model/field slot exists; it does not mean every statement is true.
- `partial` keeps available claims and explicitly marks missing slots. `failed` means no usable requested claims.
- Two attempts maximum per model, 25-second per-attempt wall time, 60-second model-stage budget. Transport and JSON repair share this budget. Factual disagreement is never retried away.
- Every result includes request ID, model IDs, source hashes, prompt/verifier versions and model-call status. Unknown token usage is null; failed attempts are not forgotten.
- Raw content is persisted only with `--save-trace`. Normal logs contain metadata, not quotes, questions or credentials.
- CLI uses OS user permissions; model API keys remain server-side/process-local. There is no user/account authentication system.
- There are no external-document, recommendation, trade or money-movement paths. All inputs bundled here are fictional.

CLI exits: 0 complete; 2 input/configuration error; 3 partial; 4 failed; 5 internal/write error. For eval, 0 passes, 1 fails a quality/regression gate, 2 cannot execute.

## Verification

Local verification covers **43 unit tests** and **27 fixed cases**. The unit tests also exercise malformed model responses, rate limits, timeout cancellation, retry ceilings, same-prompt parallel calls, output roundtrips and credential-safe errors through `httpx.MockTransport`.

Both mutations are caught by existing assertions:

- Disable unit normalization: E01 and E20 fail.
- Bypass the evidence gate: shared-hallucination cases, among others, fail.

See [demo/offline_eval.json](demo/offline_eval.json) for the measured report. Its disagreement binary subset contains only five cases, including one positive disagreement. Its scores are regression results for these fixtures, not an estimate of real-world accuracy. The separate recorded live run is one compatibility check, not an accuracy benchmark.

## References and AI use

Design references: [LLM Council orchestration](https://github.com/karpathy/llm-council/blob/master/backend/council.py), [Instructor exact citations](https://github.com/567-labs/instructor/blob/main/docs/examples/exact_citations.md), [ALCE citation evaluation](https://github.com/princeton-nlp/ALCE/blob/main/eval.py). This implementation was drafted specifically for this project; these repositories were references, not copied application code or inherited test results.

Read [AI_USE.md](AI_USE.md) before submitting. It distinguishes what Codex drafted and executed from what the candidate must personally inspect; agent-run checks are never presented as candidate hand-verification.
