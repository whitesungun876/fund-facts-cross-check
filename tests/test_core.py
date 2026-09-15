"""Behavioral regressions for the frozen contract; no live network or credentials."""
import asyncio
import copy
import io
import json
import os
from pathlib import Path
import socket
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch
from uuid import uuid4

import httpx
from pydantic import ValidationError

import compare
import evaluate
import evidence
import main
import providers
from schemas import (Claim, CrossCheckRequest, CrossCheckResponse, FeeValue, ModelAnswer,
                     ModelRun, NormalizedFee, load_json)

ROOT = Path(__file__).resolve().parents[1]
SETTINGS = providers.Settings("unit-test-only-token", "test/model-a", "test/model-b")
FAST = providers.Budget(attempt_seconds=0.2, total_seconds=0.8, backoff_min=0, backoff_max=0)


def valid_answer():
    case = next(c for c in evaluate.cases() if c.id == "E15-valid")
    return copy.deepcopy(case.model_inputs["answer"])


def fee_claim():
    return Claim.model_validate(valid_answer()["claims"][0])


def envelope(answer=None, *, usage=True, finish="stop"):
    body = {"choices": [{"finish_reason": finish, "message": {"content": json.dumps(answer if answer is not None else valid_answer())}}]}
    if usage:
        body["usage"] = {"prompt_tokens": 10, "completion_tokens": 20}
    return body


def good_response():
    answer = ModelAnswer.model_validate(valid_answer())
    return main.assemble_response(CrossCheckRequest(), main.load_corpus("synthetic-v1"),
        [(evaluate.fixture_run("a"), answer), (evaluate.fixture_run("b"), answer)], str(uuid4()), 0)


class ContractTests(unittest.TestCase):
    def test_rejects_coercion_and_invalid_claim_values(self):
        for value in [True, 0.3, -1, "-1", "NaN", "Infinity", "1e2", "01.0", "0.1234567"]:
            with self.subTest(value=value), self.assertRaises(ValidationError):
                FeeValue(amount=value, unit="percent")

    def test_rejects_wrong_value_type_extra_fields_and_bad_date(self):
        base = fee_claim().model_dump()
        for update in [{"value": {"label": "guaranteed"}}, {"effective_date": "2026-02-30"}, {"evidence_status": "supported"}, {"citations": []}]:
            with self.subTest(update=update), self.assertRaises(ValidationError):
                Claim.model_validate({**base, **update})

    def test_unknown_is_not_missing_or_boolean_false(self):
        base = fee_claim().model_dump()
        for update in [{"answer_status": "not_stated"}, {"answer_status": "abstained", "value": None}]:
            with self.assertRaises(ValidationError):
                Claim.model_validate({**base, **update})
        valid = Claim.model_validate({**base, "answer_status": "not_stated", "value": None, "citations": []})
        self.assertIsNone(valid.value)

    def test_duplicate_claim_and_json_members_rejected(self):
        raw = valid_answer()
        raw["claims"][1] = copy.deepcopy(raw["claims"][0])
        with self.assertRaises(ValidationError):
            ModelAnswer.model_validate(raw)
        for text in ['{"claims":[],"claims":[]}', '{"x":NaN}', '{"x":Infinity}']:
            with self.assertRaises(ValueError):
                load_json(text)

    def test_request_cannot_expand_scope(self):
        for fields in [[], ["capital_guarantee", "capital_guarantee"], ["total_fees"]]:
            with self.assertRaises(ValidationError):
                CrossCheckRequest(fields=fields)
        for question in [" ", "x\0y", "x" * 1001]:
            with self.assertRaises(ValidationError):
                CrossCheckRequest(question=question)
        with self.assertRaises(ValidationError):
            CrossCheckRequest(corpus_id="../../private")

    def test_final_schema_rejects_missing_rows_and_false_completeness(self):
        raw = good_response().model_dump()
        for mutate in [lambda r: r["rows"].pop(), lambda r: r.update(status="partial"),
                       lambda r: r["models"][1].update(model_id=r["models"][0]["model_id"]),
                       lambda r: r.update(request_id="bad"), lambda r: r.update(created_at="2026-01-01")]:
            changed = copy.deepcopy(raw)
            mutate(changed)
            with self.assertRaises(ValidationError):
                CrossCheckResponse.model_validate(changed)

    def test_final_schema_rejects_bad_slot_and_citation_shape(self):
        raw = good_response().model_dump()
        raw["rows"][0]["a"]["presence"] = "missing"
        with self.assertRaises(ValidationError):
            CrossCheckResponse.model_validate(raw)
        raw = good_response().model_dump()
        raw["rows"][0]["a"]["assessment"]["citation_checks"] = []
        with self.assertRaises(ValidationError):
            CrossCheckResponse.model_validate(raw)

    def test_decimal_max_precision_and_zero(self):
        self.assertEqual(compare.normalize_fee(FeeValue(amount="999999999999999999.999999", unit="percent")).amount, "99999999999999999999.9999")
        self.assertEqual(compare.normalize_fee(FeeValue(amount="0.000001", unit="percent")).amount, "0.0001")
        self.assertEqual(compare.normalize_fee(FeeValue(amount="0.000000", unit="percent")).amount, "0")


class EvidenceTests(unittest.TestCase):
    def setUp(self):
        self.documents = main.load_corpus("synthetic-v1")
        self.alpha = (ROOT / "fixtures/fund_alpha.md").read_bytes()

    def test_all_labeled_regressions(self):
        for case in evaluate.cases():
            with self.subTest(case=case.id):
                actual = evaluate.run_case(case)
                for key, expected in case.expected.items():
                    self.assertEqual(actual[key], expected)

    def test_no_network_required_for_eval_and_mutations(self):
        with patch.object(socket, "create_connection", side_effect=AssertionError("network forbidden")), patch.object(httpx.AsyncClient, "send", side_effect=AssertionError("network forbidden")):
            report = evaluate.offline_report()
            mutations = evaluate.mutation_report()
        self.assertTrue(report["passed"])
        self.assertEqual(report["evidence"]["false_support_count"], 0)
        self.assertEqual(report["evidence"]["retention"], 1)
        self.assertTrue(mutations["passed"])
        self.assertIn("E01", mutations["caught_by"]["normalization_disabled"])
        self.assertIn("E05", mutations["caught_by"]["evidence_gate_disabled"])
        self.assertTrue(evaluate.offline_report()["passed"], "mutations must restore originals")

    def test_empty_metric_denominator_is_null(self):
        self.assertIsNone(evaluate.ratio(0, 0))

    def test_mutating_source_value_invalidates_old_claim(self):
        changed = evidence.parse_document(self.alpha.replace(b"0.30%", b"2.137%"))
        raw = fee_claim().model_dump()
        raw["citations"][0]["quote"] = changed.sections["management_fee"].text
        result = evidence.assess_claim(Claim.model_validate(raw), {changed.document_id: changed})
        self.assertEqual(result.evidence_status, "contradicted")
        self.assertNotEqual(changed.sha256, self.documents["alpha-v1"].sha256)

    def test_negation_substring_is_not_positive_support(self):
        raw = valid_answer()["claims"][1]
        raw["value"] = {"label": "guaranteed"}
        raw["citations"][0]["quote"] = "guaranteed."
        result = evidence.assess_claim(Claim.model_validate(raw), self.documents)
        self.assertEqual(result.evidence_status, "insufficient")

    def test_unknown_grammar_cannot_establish_absence(self):
        raw = (ROOT / "fixtures/fund_beta.md").read_text()
        for addition in ["\n[unrecognized]\nCapital is guaranteed.\n", "\n[risk_extra]\nUnrecognized prose.\n"]:
            document = evidence.parse_document((raw + addition).encode())
            result = evidence.assess_claim(Claim.model_validate(valid_answer()["claims"][3]), {document.document_id: document})
            self.assertEqual(result.evidence_status, "unverifiable")

    def test_guarantee_conditions_fail_closed(self):
        for sentence in ["Capital is guaranteed only if held for ten years.", "Capital is not not guaranteed.", "Capital is guaranteed. Except in insolvency."]:
            raw = self.alpha.decode().replace("Capital is not guaranteed. Investors may lose part or all of their investment.", sentence)
            document = evidence.parse_document(raw.encode())
            claim = valid_answer()["claims"][1]
            claim["citations"][0]["quote"] = sentence
            result = evidence.assess_claim(Claim.model_validate(claim), {document.document_id: document})
            self.assertEqual(result.evidence_status, "unverifiable")

    def test_unicode_whitespace_offsets_and_all_occurrences(self):
        sentence = "Capital is not guaranteed."
        raw = self.alpha.decode().replace("Capital is not guaranteed. Investors may lose part or all of their investment.", "Capital is\nnot guaranteed.\nCapital is\tnot guaranteed.")
        document = evidence.parse_document(raw.encode())
        claim = valid_answer()["claims"][1]
        claim["citations"][0]["quote"] = sentence
        result = evidence.assess_claim(Claim.model_validate(claim), {document.document_id: document})
        self.assertEqual(result.evidence_status, "supported")
        spans = result.citation_checks[0].spans
        self.assertEqual(len(spans), 2)
        for span in spans:
            self.assertEqual(evidence.clean(document.raw_text[span.start:span.end]), sentence)

    def test_document_header_duplicates_and_non_synthetic_rejected(self):
        for raw in [self.alpha.replace(b"Document ID: alpha-v1", b"Document ID: alpha-v1\nDocument ID: other"),
                    self.alpha.replace(b"[management_fee]", b"[management_fee]\nvalid\n[management_fee]"),
                    self.alpha.replace(b"SYNTHETIC FIXTURE", b"REAL FIXTURE"),
                    self.alpha.replace(b"Format version: controlled-factsheet-v1", b"Format version: unknown")]:
            with self.assertRaises(ValueError):
                evidence.parse_document(raw)

    def test_unknown_and_abstention_preserve_relation(self):
        fee = evidence.assess_claim(fee_claim(), self.documents)
        raw = fee_claim().model_dump()
        raw.update(answer_status="abstained", value=None, citations=[])
        unknown = evidence.assess_claim(Claim.model_validate(raw), self.documents)
        self.assertEqual(compare.compare_pair(fee, unknown), "one_unknown")
        self.assertEqual(compare.compare_pair(unknown, unknown), "both_unknown")
        self.assertEqual(compare.compare_pair(fee, None), "missing")

    def test_scope_mismatch_is_not_repaired_by_pipeline(self):
        a, b = valid_answer(), valid_answer()
        b["claims"][0]["effective_date"] = "2025-01-01"
        response = main.assemble_response(CrossCheckRequest(), self.documents,
            [(evaluate.fixture_run("a"), ModelAnswer.model_validate(a)), (evaluate.fixture_run("b"), ModelAnswer.model_validate(b))], str(uuid4()), 0)
        self.assertEqual(response.status, "partial")
        row = next(r for r in response.rows if r.key.field == "annual_management_fee" and r.key.fund_id == "alpha")
        self.assertEqual(row.relation, "missing")
        self.assertEqual(row.b.presence, "missing")
        self.assertTrue(any(d.code == "SCOPE_MISMATCH" for d in response.diagnostics))


class ProviderTests(unittest.IsolatedAsyncioTestCase):
    async def invoke(self, handler, **kwargs):
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await providers.call_model("a", SETTINGS.model_a, [{"role": "user", "content": "synthetic"}],
                time.monotonic() + kwargs.pop("seconds", 1), client=client, api_key=SETTINGS.api_key,
                request_id=str(uuid4()), budget=kwargs.pop("budget", FAST), **kwargs)

    async def test_schema_repair_bounded_and_usage_aggregated(self):
        requests = []
        async def handler(request):
            requests.append(json.loads(request.content))
            invalid = {"schema_version": "1.0", "claims": [], "extra": "wrong"}
            return httpx.Response(200, json=envelope(invalid if len(requests) == 1 else valid_answer()))
        run, answer = await self.invoke(handler)
        self.assertEqual(run.status, "ok")
        self.assertEqual(run.attempts, 2)
        self.assertEqual((run.prompt_tokens, run.completion_tokens), (20, 40))
        self.assertTrue(run.usage_complete)
        self.assertEqual(len(answer.claims), 4)
        self.assertEqual(len(requests[1]["messages"]), 3)
        self.assertTrue(requests[0]["response_format"]["json_schema"]["strict"])
        self.assertTrue(requests[0]["provider"]["require_parameters"])

    async def test_schema_errors_stop_after_two_attempts(self):
        count = 0
        async def handler(request):
            nonlocal count
            count += 1
            return httpx.Response(200, json=envelope({"claims": []}))
        run, answer = await self.invoke(handler)
        self.assertEqual(count, 2)
        self.assertEqual(run.status, "invalid_output")
        self.assertIsNone(answer)

    async def test_auth_and_capability_errors_do_not_retry_or_leak(self):
        for status in [400, 401, 403, 404]:
            count = 0
            async def handler(request):
                nonlocal count
                count += 1
                return httpx.Response(status, text="secret provider detail " + SETTINGS.api_key)
            with self.assertLogs("fundcheck", level="INFO") as logs:
                run, _ = await self.invoke(handler)
            self.assertEqual(count, 1)
            self.assertFalse(run.error.retryable)
            self.assertNotIn(SETTINGS.api_key, run.model_dump_json() + "".join(logs.output))
            self.assertNotIn("secret provider detail", "".join(logs.output))

    async def test_retry_after_beyond_budget_is_not_ignored(self):
        count = 0
        async def handler(request):
            nonlocal count
            count += 1
            return httpx.Response(429, headers={"Retry-After": "90"})
        run, _ = await self.invoke(handler, seconds=0.1)
        self.assertEqual(count, 1)
        self.assertEqual(run.status, "rate_limited")

    async def test_transient_error_then_success_keeps_usage_unknown(self):
        count = 0
        async def handler(request):
            nonlocal count
            count += 1
            return httpx.Response(503) if count == 1 else httpx.Response(200, json=envelope())
        run, _ = await self.invoke(handler)
        self.assertEqual(run.status, "ok")
        self.assertEqual(run.attempts, 2)
        self.assertIsNone(run.prompt_tokens)
        self.assertIsNone(run.completion_tokens)
        self.assertFalse(run.usage_complete)

    async def test_per_attempt_timeout_has_bounded_retries(self):
        async def handler(request):
            await asyncio.sleep(1)
            return httpx.Response(200, json=envelope())
        started = time.monotonic()
        budget = providers.Budget(attempt_seconds=0.01, total_seconds=0.1, backoff_min=0, backoff_max=0)
        run, _ = await self.invoke(handler, budget=budget)
        self.assertEqual(run.status, "timeout")
        self.assertEqual(run.attempts, 2)
        self.assertLess(time.monotonic() - started, 0.5)

    async def test_empty_deadline_makes_no_http_request(self):
        async def handler(request):
            self.fail("should not call provider")
        run, answer = await self.invoke(handler, seconds=-1)
        self.assertEqual(run.attempts, 0)
        self.assertEqual(run.status, "timeout")
        self.assertIsNone(answer)

    async def test_truncation_refusal_and_malformed_envelopes_fail(self):
        bodies = [envelope(finish="length"), {"choices": []}, [],
                  {"choices": [{"finish_reason": "stop", "message": {"refusal": "no", "content": "{}"}}]}]
        for body in bodies:
            async def handler(request):
                return httpx.Response(200, json=body)
            run, answer = await self.invoke(handler)
            self.assertEqual(run.status, "invalid_output")
            self.assertIsNone(answer)

    async def test_evidence_error_does_not_trigger_retry(self):
        injected = load_json((ROOT / "demo/injected_answers.json").read_text())["a"]
        count = 0
        async def handler(request):
            nonlocal count
            count += 1
            return httpx.Response(200, json=envelope(injected))
        run, answer = await self.invoke(handler)
        self.assertEqual(count, 1)
        self.assertEqual(run.status, "ok")
        assessment = evidence.assess_claim(answer.claims[1], main.load_corpus("synthetic-v1"))
        self.assertEqual(assessment.evidence_status, "contradicted")

    async def test_input_messages_not_mutated_by_repair(self):
        messages = [{"role": "user", "content": "synthetic"}]
        async def handler(request):
            return httpx.Response(200, json=envelope({"wrong": True}))
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            await providers.call_model("a", SETTINGS.model_a, messages, time.monotonic() + 1,
                client=client, api_key=SETTINGS.api_key, request_id=str(uuid4()), budget=FAST)
        self.assertEqual(messages, [{"role": "user", "content": "synthetic"}])

    async def test_transport_exception_is_safe_and_bounded(self):
        count = 0
        async def handler(request):
            nonlocal count
            count += 1
            raise httpx.ConnectError("do not expose " + SETTINGS.api_key)
        run, answer = await self.invoke(handler)
        self.assertEqual(count, 2)
        self.assertEqual(run.error.code, "TRANSPORT_ERROR")
        self.assertNotIn(SETTINGS.api_key, run.model_dump_json())
        self.assertIsNone(answer)

    async def test_raw_content_is_opt_in(self):
        async def handler(request):
            return httpx.Response(200, json=envelope())
        for enabled in [False, True]:
            records = []
            await self.invoke(handler, trace=records, save_raw=enabled)
            self.assertEqual("raw_content" in records[0], enabled)

    async def test_oversized_response_is_not_accepted(self):
        async def handler(request):
            return httpx.Response(200, content=b"x" * 262145)
        run, answer = await self.invoke(handler)
        self.assertEqual(run.status, "invalid_output")
        self.assertIsNone(answer)


class PipelineTests(unittest.IsolatedAsyncioTestCase):
    async def test_parallel_same_prompt_structured_end_to_end(self):
        requests = []
        gate = asyncio.Event()
        async def handler(request):
            requests.append(json.loads(request.content))
            if len(requests) == 2:
                gate.set()
            await asyncio.wait_for(gate.wait(), 0.5)
            return httpx.Response(200, json=envelope())
        trace = {}
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            response = await main.run_cross_check(CrossCheckRequest(), settings=SETTINGS, client=client, budget=FAST, trace=trace)
        self.assertEqual(response.status, "complete")
        self.assertEqual(len(response.rows), 4)
        self.assertEqual(requests[0]["messages"], requests[1]["messages"])
        self.assertNotEqual(requests[0]["model"], requests[1]["model"])
        self.assertEqual(requests[0]["response_format"], requests[1]["response_format"])
        self.assertEqual(len(trace["attempts"]), 2)
        self.assertTrue(evaluate.live_report(response)["passed"])
        self.assertEqual(CrossCheckResponse.model_validate(load_json(response.model_dump_json())), response)

    async def test_deadline_preserves_finished_model_and_cancels_other(self):
        cancelled = asyncio.Event()
        async def invoke(slot, model, *args, **kwargs):
            if slot == "a":
                run = evaluate.fixture_run("a").model_copy(update={"model_id": model})
                return run, ModelAnswer.model_validate(valid_answer())
            try:
                await asyncio.sleep(10)
            finally:
                cancelled.set()
        budget = providers.Budget(total_seconds=0.03)
        response = await main.run_cross_check(CrossCheckRequest(), settings=SETTINGS, budget=budget, model_call=invoke)
        self.assertTrue(cancelled.is_set())
        self.assertEqual(response.status, "partial")
        self.assertTrue(all(r.relation == "missing" for r in response.rows))
        self.assertEqual(response.models[1].error.code, "DEADLINE_EXCEEDED")

    async def test_both_provider_failures_are_failed(self):
        async def handler(request):
            return httpx.Response(401)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            response = await main.run_cross_check(CrossCheckRequest(), settings=SETTINGS, client=client, budget=FAST)
        self.assertEqual(response.status, "failed")
        self.assertEqual(len(response.rows), 4)
        self.assertTrue(all(r.a.assessment is None and r.b.assessment is None for r in response.rows))
        self.assertFalse(evaluate.live_report(response)["passed"])

    async def test_real_transport_cancellation_keeps_attempt_count_without_raw_trace(self):
        async def handler(request):
            await asyncio.sleep(10)
            return httpx.Response(200, json=envelope())
        budget = providers.Budget(attempt_seconds=1, total_seconds=0.02)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            response = await main.run_cross_check(CrossCheckRequest(), settings=SETTINGS, client=client, budget=budget)
        self.assertEqual(response.status, "failed")
        self.assertEqual([m.attempts for m in response.models], [1, 1])
        self.assertTrue(all(m.prompt_tokens is None for m in response.models))

    async def test_internal_bug_is_not_a_successful_model_failure(self):
        async def invoke(*args, **kwargs):
            raise RuntimeError("programming bug")
        with self.assertRaisesRegex(RuntimeError, "programming bug"):
            await main.run_cross_check(CrossCheckRequest(), settings=SETTINGS, model_call=invoke)

    async def test_selected_field_builds_only_requested_matrix(self):
        async def handler(request):
            raw = valid_answer()
            raw["claims"] = [c for c in raw["claims"] if c["field"] == "annual_management_fee"]
            return httpx.Response(200, json=envelope(raw))
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            response = await main.run_cross_check(CrossCheckRequest(fields=["annual_management_fee"]), settings=SETTINGS, client=client, budget=FAST)
        self.assertEqual(response.status, "complete")
        self.assertEqual(len(response.rows), 2)


class OutputTests(unittest.TestCase):
    def test_result_files_roundtrip_and_no_overwrite(self):
        response = good_response()
        with tempfile.TemporaryDirectory() as tmp:
            directory = main.save_result(response, Path(tmp))
            restored = CrossCheckResponse.model_validate(load_json((directory / "result.json").read_text()))
            self.assertEqual(restored, response)
            self.assertFalse((directory / "trace.json").exists())
            self.assertFalse(list(directory.glob("*.tmp")))
            with self.assertRaises(FileExistsError):
                main.save_result(response, Path(tmp))

    def test_markdown_is_escaped_and_preserves_unknown_and_sources(self):
        self.assertEqual(main.display_cell('<script>|\nx'), '&lt;script&gt;&#124; / x')
        report = main.render_markdown(good_response())
        self.assertIn("both_unknown", report)
        self.assertIn("检查范围内未找到声明", report)
        self.assertIn("alpha-v1/management_fee", report)
        self.assertIn("never auto-act", report)

    def test_missing_live_configuration_never_falls_back_to_mock(self):
        with patch.dict(os.environ, {}, clear=True), redirect_stderr(io.StringIO()), patch.object(httpx.AsyncClient, "send", side_effect=AssertionError("no network")):
            self.assertEqual(main.cli([]), 2)
            with redirect_stdout(io.StringIO()):
                self.assertEqual(evaluate.cli(["--mode", "live"]), 2)

    def test_cli_all_runtime_status_exit_codes(self):
        async def fake_run(*args, **kwargs):
            return response
        for status, code in [("complete", 0), ("partial", 3), ("failed", 4)]:
            raw = good_response().model_dump()
            if status != "complete":
                for row in raw["rows"]:
                    row["b"] = {"presence": "missing", "assessment": None}
                    if status == "failed":
                        row["a"] = {"presence": "missing", "assessment": None}
                    row["relation"] = "missing"
                raw["status"] = status
            response = CrossCheckResponse.model_validate(raw)
            with tempfile.TemporaryDirectory() as tmp, patch.object(providers.Settings, "from_env", return_value=SETTINGS), patch.object(main, "run_cross_check", side_effect=fake_run), redirect_stdout(io.StringIO()):
                self.assertEqual(main.cli(["--output", tmp]), code)

    def test_duplicate_models_and_credentials_repr(self):
        with patch.dict(os.environ, {"OPENROUTER_API_KEY": "test", "MODEL_A_ID": "same", "MODEL_B_ID": "same"}, clear=True):
            with self.assertRaises(ValueError):
                providers.Settings.from_env()
        self.assertNotIn(SETTINGS.api_key, repr(SETTINGS))


if __name__ == "__main__":
    unittest.main()
