# Five-minute walkthrough — recorded live run

This is a written walkthrough of a real OpenRouter execution on 2026-09-15. The input documents are deliberately fictional, but both model calls, schema validation, evidence checks and comparison results are real. The saved record is `demo/live_run.json`; it contains no credential.

## 0:00–0:45 — the task and safety boundary

Open `fixtures/fund_alpha.md` and `fixtures/fund_beta.md`. Alpha states a 0.30% annual management fee and says capital is not guaranteed. Beta states a 45-basis-point fee and contains no capital-guarantee statement. Both files say `SYNTHETIC FIXTURE — NOT A REAL FUND`.

The request asks two models to compare the annual management fee and capital-guarantee statement, quote the source for stated facts, and mark missing information as unknown. The program can only research these bundled fixtures. It has no trade, recommendation, upload or money-movement path.

## 0:45–1:35 — one prompt, two models

Run:

```bash
uv run --frozen --env-file .env python evaluate.py --mode live
```

`main.py` builds one initial message and one Pydantic JSON schema. `providers.py` sends the same message and schema concurrently to:

- Model A: `openai/gpt-4.1-nano`
- Model B: `google/gemini-2.5-flash-lite`

The recorded request ID is `5a72ed5d-720d-448a-b795-d6c02be7ff85`. Both calls completed on their first attempt. Model A used 999 prompt and 299 completion tokens; Model B used 694 prompt and 562 completion tokens.

## 1:35–2:30 — strict structure before comparison

Open `demo/live_run.json`. Each model returned exactly four keyed claims: two funds multiplied by two fields. Each claim includes fund ID, share class, effective date, field, answer status, typed value and citations. Pydantic rejects wrong schema versions, float coercion, extra fields, duplicate claim keys and stated answers without citations.

This matters because a malformed response never enters comparison. A missing or failed model remains visible as a missing slot rather than being silently replaced.

## 2:30–3:35 — agreement and evidence are separate

For Alpha's fee, both models returned `0.30 percent`; the verifier located the exact sentence and normalized the value to 30 bps. For Beta's fee, both returned 45 bps. These are agreements supported by the source.

For Alpha's guarantee field, both returned `not_guaranteed` and quoted the full negative sentence. For Beta, both returned `not_stated`; the verifier checked the complete controlled document before accepting that absence. The run therefore finished with 100% field coverage and 8/8 supported model/field checks.

The separate injected example in `demo/injected_answers.json` shows the more important failure case: two models agree that Alpha is guaranteed, but `demo/injected_report.md` marks both claims contradicted. Consensus never overrides evidence.

## 3:35–4:35 — the regression gate

Run:

```bash
uv run --frozen python evaluate.py --mode offline --mutations
```

The harness evaluates 27 labeled cases. It then introduces two deliberate regressions. First it disables percent-to-bps normalization; existing equivalence cases fail. Then it bypasses the evidence gate; shared hallucinations and bad citations are incorrectly accepted, and existing cases fail. The command exits nonzero if either mutation escapes detection, so it tests behavior rather than merely checking that code runs.

## 4:35–5:00 — limits and reproducibility

Run the 43-test suite with:

```bash
uv run --frozen python -m unittest discover -s tests -v
```

The live run proves compatibility for one date, corpus and model pair. It does not estimate accuracy on arbitrary fund documents. The evidence checker intentionally accepts only the controlled grammar documented in `contract.md`; unfamiliar or conditional language fails closed as `unverifiable`. Timeouts, two-attempt limits, request IDs, source hashes and token accounting make failures inspectable without exposing the API key.
