"""批次重试与断点恢复（process_batch_retry 等三个可重试入口）公开行为测试。

覆盖：
- 首次结果与既有正式入口逐字段一致，审计内容相同；
- 相同批次标识 + 相同内容、不同执行标识重试返回同一结果，不重复扣减 /
  归因 / 追加审计（台账、流水号、风险组、坏账只产生一次）；
- 相同批次标识 + 不同内容在任何副作用前返回唯一 BatchRetryConflict；
- 标识缺失 / 为空 / 摘要不可计算返回唯一 BatchIdentifierInvalid；
- 既有校验异常仍按原口径抛出，失败不登记批次、可修正后重提；
- 中断后续跑从首个未完成步骤继续，已完成步骤不重复产生副作用；
- 并发提交同一批次只允许一个请求推进，其余等待并取得同一最终结果；
- 风险组与多币种变体、坏账 / 回收 / 审计核对在重试后仍自洽。
"""

import threading
import unittest
from dataclasses import FrozenInstanceError, asdict
from decimal import Decimal
from unittest.mock import patch

from vault_guard import (
    BATCH_INVALID_REASON_MISSING_BATCH_ID,
    BATCH_INVALID_REASON_MISSING_EXECUTION_ID,
    BATCH_INVALID_REASON_UNCOMPUTABLE_DIGEST,
    BATCH_OUTCOME_INVALID_IDENTIFIER,
    BATCH_OUTCOME_RETRY_CONFLICT,
    BatchIdentifierInvalid,
    BatchRetryConflict,
    ClearingEngine,
    DuplicateTransactionError,
    EmptyBatchError,
    InvalidCurrencyError,
)

D = Decimal


def req(tid, amount, exposure="0", limit="1000", factor="0",
        creditors=(("senior", "60"), ("equity", "40")), capital="0",
        currency=None, group=None):
    normalized = []
    for item in creditors:
        if len(item) == 2:
            name, claim = item
            normalized.append((name, D(claim)))
        else:
            name, claim, ccy = item
            normalized.append((name, D(claim), ccy))
    data = {
        "transaction_id": tid,
        "settlement_amount": D(amount),
        "notional_exposure": D(exposure),
        "base_limit": D(limit),
        "risk_factor": D(factor),
        "creditors": normalized,
        "supplementary_capital": D(capital),
    }
    if currency is not None:
        data["currency"] = currency
    if group is not None:
        data["risk_group_id"] = group
    return data


REQS = [req("T1", "40"), req("T2", "30"), req("T3", "10")]
REQS_WITH_REJECTION = [req("A1", "40"), req("A2", "70")]


class FirstRunParityTests(unittest.TestCase):
    def test_first_run_matches_process_batch_field_by_field(self):
        plain = ClearingEngine().process_batch("USD", D("100"), REQS)
        retried = ClearingEngine().process_batch_retry(
            "B1", "exec-1", "USD", D("100"), REQS
        )
        self.assertEqual(asdict(plain), asdict(retried))

    def test_first_run_with_business_rejection_matches_plain_entry(self):
        plain = ClearingEngine().process_batch(
            "USD", D("100"), REQS_WITH_REJECTION
        )
        retried = ClearingEngine().process_batch_retry(
            "B2", "exec-1", "USD", D("100"), REQS_WITH_REJECTION
        )
        self.assertEqual(asdict(plain), asdict(retried))
        self.assertFalse(retried.results[1].approved)
        self.assertEqual(
            retried.results[1].rejection_reason,
            "SETTLEMENT_EXCEEDS_AVAILABLE_BALANCE",
        )

    def test_audit_events_identical_to_plain_batch(self):
        engine_plain = ClearingEngine()
        engine_plain.process_batch("USD", D("100"), REQS)
        engine_retry = ClearingEngine()
        engine_retry.process_batch_retry(
            "B3", "exec-1", "USD", D("100"), REQS
        )
        plain_events = [asdict(e) for e in engine_plain.audit_log]
        retry_events = [asdict(e) for e in engine_retry.audit_log]
        self.assertEqual(plain_events, retry_events)


class RetryIdempotencyTests(unittest.TestCase):
    def test_retry_with_different_execution_id_returns_same_result(self):
        engine = ClearingEngine()
        first = engine.process_batch_retry(
            "BATCH", "exec-1", "USD", D("100"), REQS
        )
        second = engine.process_batch_retry(
            "BATCH", "exec-2", "USD", D("100"), REQS
        )
        self.assertIs(second, first)

    def test_retry_does_not_duplicate_side_effects(self):
        engine = ClearingEngine()
        engine.process_batch_retry(
            "BATCH", "exec-1", "USD", D("100"), REQS
        )
        self.assertEqual(len(engine.audit_log), 3)
        self.assertEqual(
            engine.audit_reconciliation("USD").settlement_count, 3
        )
        engine.process_batch_retry(
            "BATCH", "exec-2", "USD", D("100"), REQS
        )
        engine.process_batch_retry(
            "BATCH", "exec-3", "USD", D("100"), REQS
        )
        # 不重复事件、不重复占序号、余额不因重试再扣减。
        self.assertEqual(len(engine.audit_log), 3)
        self.assertEqual([e.sequence for e in engine.audit_log], [1, 2, 3])
        summary = engine.audit_reconciliation("USD")
        self.assertEqual(summary.settlement_count, 3)
        self.assertEqual(summary.approved_count, 3)
        self.assertEqual(summary.pool_allocated, D("80"))
        self.assertTrue(all(engine.has_transaction(t) for t in ("T1", "T2", "T3")))

    def test_same_content_equivalent_numeric_forms_are_consistent(self):
        engine = ClearingEngine()

        def call(exec_id, opening, amount, claim):
            return engine.process_batch_retry(
                "NUM", exec_id, "USD", opening,
                [{
                    "transaction_id": "N1",
                    "settlement_amount": amount,
                    "notional_exposure": D("0"),
                    "base_limit": D("1000"),
                    "risk_factor": D("0"),
                    "creditors": [("senior", claim)],
                    "supplementary_capital": 0,
                }],
            )

        first = call("e1", D("100"), D("40"), D("60"))
        for opening, amount, claim in (
            (100, 40, 60),
            (100.0, 40.0, 60.0),
            (D("100.0"), D("40.00"), D("60.000")),
        ):
            with self.subTest(open=opening, amount=amount, claim=claim):
                self.assertIs(
                    call("eX", opening, amount, claim), first
                )
        self.assertEqual(len(engine.audit_log), 1)

    def test_retry_after_business_rejections_counts_events_once(self):
        engine = ClearingEngine()
        first = engine.process_batch_retry(
            "BATCH", "e1", "USD", D("100"), REQS_WITH_REJECTION
        )
        second = engine.process_batch_retry(
            "BATCH", "e2", "USD", D("100"), REQS_WITH_REJECTION
        )
        self.assertIs(second, first)
        summary = engine.audit_reconciliation("USD")
        self.assertEqual(summary.settlement_count, 2)
        self.assertEqual(summary.approved_count, 1)
        self.assertEqual(summary.rejected_count, 1)
        self.assertEqual(
            summary.rejection_counts,
            {"SETTLEMENT_EXCEEDS_AVAILABLE_BALANCE": 1},
        )


class RetryConflictTests(unittest.TestCase):
    def test_different_content_returns_unique_conflict_before_side_effects(self):
        engine = ClearingEngine()
        first = engine.process_batch_retry(
            "BATCH", "exec-1", "USD", D("100"), REQS
        )
        modified = [req("T1", "41"), req("T2", "30"), req("T3", "10")]
        conflict = engine.process_batch_retry(
            "BATCH", "exec-2", "USD", D("100"), modified
        )
        self.assertIsInstance(conflict, BatchRetryConflict)
        self.assertEqual(conflict.outcome, BATCH_OUTCOME_RETRY_CONFLICT)
        self.assertEqual(conflict.batch_id, "BATCH")
        self.assertEqual(conflict.original_execution_id, "exec-1")
        self.assertNotEqual(
            conflict.original_request_digest, conflict.incoming_request_digest
        )
        self.assertEqual(len(conflict.original_request_digest), 64)
        self.assertEqual(len(conflict.incoming_request_digest), 64)
        # 冲突不产生任何额外副作用。
        self.assertEqual(len(engine.audit_log), 3)
        self.assertEqual(first.validated_available_balance, D("20"))

    def test_conflict_is_immutable(self):
        conflict = BatchRetryConflict(
            batch_id="B",
            original_request_digest="a" * 64,
            incoming_request_digest="b" * 64,
            original_execution_id="e1",
        )
        with self.assertRaises(FrozenInstanceError):
            conflict.batch_id = "X"

    def test_conflict_observed_with_changed_opening_balance(self):
        engine = ClearingEngine()
        engine.process_batch_retry(
            "BATCH", "e1", "USD", D("100"), REQS
        )
        conflict = engine.process_batch_retry(
            "BATCH", "e2", "USD", D("99"), REQS
        )
        self.assertIsInstance(conflict, BatchRetryConflict)
        self.assertEqual(len(engine.audit_log), 3)

    def test_conflict_observed_with_changed_currency(self):
        engine = ClearingEngine()
        engine.process_batch_retry(
            "BATCH", "e1", "USD", D("100"),
            [req("T1", "40", creditors=(("senior", "60"),))],
        )
        conflict = engine.process_batch_retry(
            "BATCH", "e2", "EUR", D("100"),
            [req("T1", "40", creditors=(("senior", "60"),))],
        )
        self.assertIsInstance(conflict, BatchRetryConflict)
        # 冲突在原口径校验（币种一致性等）之前返回，无任何事件。
        self.assertEqual(len(engine.audit_log), 1)

    def test_conflict_then_same_content_still_returns_first_result(self):
        engine = ClearingEngine()
        first = engine.process_batch_retry(
            "BATCH", "e1", "USD", D("100"), REQS
        )
        conflict = engine.process_batch_retry(
            "BATCH", "e2", "USD", D("100"), [req("T1", "1")]
        )
        self.assertIsInstance(conflict, BatchRetryConflict)
        again = engine.process_batch_retry(
            "BATCH", "e3", "USD", D("100"), REQS
        )
        self.assertIs(again, first)
        self.assertEqual(len(engine.audit_log), 3)

    def test_different_batch_ids_are_independent_batches(self):
        engine = ClearingEngine()
        one = engine.process_batch_retry(
            "B1", "e1", "USD", D("100"),
            [req("T1", "10", creditors=(("senior", "10"),))],
        )
        two = engine.process_batch_retry(
            "B2", "e1", "USD", D("100"),
            [req("T2", "10", creditors=(("senior", "10"),))],
        )
        self.assertIsNot(one, two)
        self.assertEqual(len(engine.audit_log), 2)

    def test_whitespace_in_batch_id_is_stripped_for_identity(self):
        engine = ClearingEngine()
        first = engine.process_batch_retry(
            "  BATCH  ", "e1", "USD", D("100"),
            [req("T1", "10", creditors=(("senior", "10"),))],
        )
        second = engine.process_batch_retry(
            "BATCH", "e2", "USD", D("100"),
            [req("T1", "10", creditors=(("senior", "10"),))],
        )
        self.assertIs(second, first)


class IdentifierInvalidTests(unittest.TestCase):
    def test_missing_batch_id(self):
        for bad in (None, "", "   ", 123, b"B"):
            with self.subTest(bad=bad):
                result = ClearingEngine().process_batch_retry(
                    bad, "e1", "USD", D("100"),
                    [req("T1", "1", creditors=(("senior", "1"),))],
                )
                self.assertIsInstance(result, BatchIdentifierInvalid)
                self.assertEqual(result.outcome, BATCH_OUTCOME_INVALID_IDENTIFIER)
                self.assertEqual(
                    result.reason, BATCH_INVALID_REASON_MISSING_BATCH_ID
                )
                self.assertIsNone(result.batch_id)

    def test_missing_execution_id(self):
        for bad in (None, "", "   ", 9):
            with self.subTest(bad=bad):
                result = ClearingEngine().process_batch_retry(
                    "B1", bad, "USD", D("100"),
                    [req("T1", "1", creditors=(("senior", "1"),))],
                )
                self.assertIsInstance(result, BatchIdentifierInvalid)
                self.assertEqual(
                    result.reason, BATCH_INVALID_REASON_MISSING_EXECUTION_ID
                )
                self.assertEqual(result.batch_id, "B1")

    def test_identifier_invalid_has_no_side_effects(self):
        engine = ClearingEngine()
        result = engine.process_batch_retry(
            None, None, "USD", D("100"), REQS
        )
        self.assertIsInstance(result, BatchIdentifierInvalid)
        self.assertEqual(len(engine.audit_log), 0)
        # 标识无效不占批次名：补全标识后可正常首次提交。
        ok = engine.process_batch_retry(
            "B1", "e1", "USD", D("100"), REQS
        )
        self.assertEqual(ok.validated_available_balance, D("20"))

    def test_uncomputable_request_digest(self):
        engine = ClearingEngine()
        bad_requests = [{
            "transaction_id": "T1",
            "settlement_amount": D("1"),
            "notional_exposure": D("0"),
            "base_limit": D("1000"),
            "risk_factor": D("0"),
            # 集合无法确定性序列化 -> 摘要不可计算。
            "creditors": [("senior", D("1")), frozenset({"x"})],
            "supplementary_capital": D("0"),
        }]
        result = engine.process_batch_retry(
            "B1", "e1", "USD", D("100"), bad_requests
        )
        self.assertIsInstance(result, BatchIdentifierInvalid)
        self.assertEqual(
            result.reason, BATCH_INVALID_REASON_UNCOMPUTABLE_DIGEST
        )
        self.assertEqual(result.batch_id, "B1")
        self.assertEqual(len(engine.audit_log), 0)

    def test_invalid_result_is_immutable(self):
        invalid = BatchIdentifierInvalid(
            reason=BATCH_INVALID_REASON_MISSING_BATCH_ID
        )
        with self.assertRaises(FrozenInstanceError):
            invalid.reason = "other"


class ExistingValidationSemanticsTests(unittest.TestCase):
    def test_empty_batch_still_raises(self):
        for bad in ([], None, ()):
            with self.subTest(bad=bad):
                with self.assertRaises(EmptyBatchError):
                    ClearingEngine().process_batch_retry(
                        "B1", "e1", "USD", D("100"), bad
                    )

    def test_missing_currency_still_raises(self):
        with self.assertRaises(InvalidCurrencyError):
            ClearingEngine().process_batch_retry(
                "B1", "e1", None, D("100"),
                [req("T1", "1", creditors=(("senior", "1"),))],
            )

    def test_duplicate_transaction_still_raises_and_batch_not_registered(self):
        engine = ClearingEngine()
        with self.assertRaises(DuplicateTransactionError):
            engine.process_batch_retry(
                "B1", "e1", "USD", D("100"),
                [req("T1", "1", creditors=(("senior", "1"),)),
                 req("T1", "2", creditors=(("senior", "2"),))],
            )
        self.assertEqual(len(engine.audit_log), 0)
        # 校验失败不登记批次：修正后可用同一 batch_id 首次提交。
        result = engine.process_batch_retry(
            "B1", "e2", "USD", D("100"),
            [req("T1", "1", creditors=(("senior", "1"),)),
             req("T2", "2", creditors=(("senior", "2"),))],
        )
        self.assertEqual(len(result.results), 2)

    def test_duplicate_against_existing_ledger_still_raises(self):
        engine = ClearingEngine()
        engine.process(
            "T1", "USD", D("10"), D("1"), D("0"), D("100"), D("0"),
            [("c", D("1"))],
        )
        with self.assertRaises(DuplicateTransactionError):
            engine.process_batch_retry(
                "B1", "e1", "USD", D("100"),
                [req("T1", "1", creditors=(("senior", "1"),))],
            )
        # 失败不登记：另一批次标识内容不变时仍可提交不同流水号。
        result = engine.process_batch_retry(
            "B2", "e1", "USD", D("100"),
            [req("T9", "1", creditors=(("senior", "1"),))],
        )
        self.assertTrue(result.results[0].approved)

    def test_invalid_values_still_raise_value_error(self):
        with self.assertRaises(ValueError):
            ClearingEngine().process_batch_retry(
                "B1", "e1", "USD", D("-1"),
                [req("T1", "1", creditors=(("senior", "1"),))],
            )


class ResumeFromInterruptionTests(unittest.TestCase):
    def _crash_on_execute_call(self, engine, failing_call):
        """让引擎在第 failing_call 次执行单笔步骤时崩溃。"""
        original = ClearingEngine._execute
        calls = {"n": 0}

        def flaky(self, request):
            calls["n"] += 1
            if calls["n"] == failing_call:
                raise RuntimeError("simulated interruption")
            return original(self, request)

        return patch.object(ClearingEngine, "_execute", flaky)

    def test_resume_continues_from_incomplete_step(self):
        engine = ClearingEngine()
        with self._crash_on_execute_call(engine, failing_call=2):
            with self.assertRaises(RuntimeError):
                engine.process_batch_retry(
                    "BK", "exec-1", "USD", D("100"), REQS
                )

        # 第 1 步保留；崩溃的第 2 步连同其事件 / 流水号一起回滚。
        self.assertEqual(
            [e.transaction_id for e in engine.audit_log], ["T1"]
        )
        self.assertTrue(engine.has_transaction("T1"))
        self.assertFalse(engine.has_transaction("T2"))
        self.assertIsNone(engine.result_of("T2"))

        resumed = engine.process_batch_retry(
            "BK", "exec-2", "USD", D("100"), REQS
        )
        self.assertEqual(
            [r.transaction_id for r in resumed.results], ["T1", "T2", "T3"]
        )
        self.assertEqual(
            [r.validated_available_balance for r in resumed.results],
            [D("60"), D("30"), D("20")],
        )
        # 只有一条完整审计轨迹，序号连续、无重复。
        self.assertEqual(
            [e.transaction_id for e in engine.audit_log], ["T1", "T2", "T3"]
        )
        self.assertEqual([e.sequence for e in engine.audit_log], [1, 2, 3])

        again = engine.process_batch_retry(
            "BK", "exec-3", "USD", D("100"), REQS
        )
        self.assertIs(again, resumed)
        self.assertEqual(len(engine.audit_log), 3)

    def test_crash_before_any_step_leaves_clean_resume(self):
        engine = ClearingEngine()
        with self._crash_on_execute_call(engine, failing_call=1):
            with self.assertRaises(RuntimeError):
                engine.process_batch_retry(
                    "BK", "exec-1", "USD", D("100"), REQS
                )
        self.assertEqual(len(engine.audit_log), 0)
        resumed = engine.process_batch_retry(
            "BK", "exec-2", "USD", D("100"), REQS
        )
        self.assertEqual(resumed.validated_available_balance, D("20"))
        self.assertEqual([e.sequence for e in engine.audit_log], [1, 2, 3])

    def test_resume_after_repeated_crashes(self):
        engine = ClearingEngine()
        with self._crash_on_execute_call(engine, failing_call=1):
            with self.assertRaises(RuntimeError):
                engine.process_batch_retry(
                    "BK", "exec-1", "USD", D("100"), REQS
                )
        with self._crash_on_execute_call(engine, failing_call=2):
            with self.assertRaises(RuntimeError):
                engine.process_batch_retry(
                    "BK", "exec-2", "USD", D("100"), REQS
                )
        # 第一次崩溃在 T1 前；第二次调用从 T1 开始，崩溃在 T2，故 T1 保留。
        self.assertEqual(
            [e.transaction_id for e in engine.audit_log], ["T1"]
        )
        resumed = engine.process_batch_retry(
            "BK", "exec-3", "USD", D("100"), REQS
        )
        self.assertEqual(
            [r.transaction_id for r in resumed.results], ["T1", "T2", "T3"]
        )
        self.assertEqual(len(engine.audit_log), 3)


class ConcurrentSubmissionTests(unittest.TestCase):
    def test_concurrent_same_batch_single_progression(self):
        engine = ClearingEngine()
        thread_count = 8
        barrier = threading.Barrier(thread_count)
        outcomes = []
        outcomes_lock = threading.Lock()

        def worker(index):
            barrier.wait()
            result = engine.process_batch_retry(
                "PARALLEL", f"exec-{index}", "USD", D("100"),
                [req(f"P{index_item}", "1", creditors=(("senior", "1"),))
                 for index_item in range(5)],
            )
            with outcomes_lock:
                outcomes.append(result)

        threads = [
            threading.Thread(target=worker, args=(i,))
            for i in range(thread_count)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(len(outcomes), thread_count)
        self.assertEqual(len({id(r) for r in outcomes}), 1)
        self.assertEqual(len(engine.audit_log), 5)
        self.assertEqual([e.sequence for e in engine.audit_log], [1, 2, 3, 4, 5])


class RiskGroupRetryTests(unittest.TestCase):
    LIMITS = {"GR": D("80")}

    def test_risk_group_retry_returns_first_result_without_double_occupancy(self):
        engine = ClearingEngine()
        requests = [
            req("G1", "60", group="GR"),
            req("G2", "60", group="GR"),
        ]
        first = engine.process_risk_group_batch_retry(
            "RG", "e1", "USD", D("100"), self.LIMITS, requests
        )
        self.assertTrue(first.results[0].approved)
        # 第二笔超资金池被拒（既有原因码），不占组额度。
        self.assertFalse(first.results[1].approved)
        self.assertEqual(first.risk_groups["GR"].used, D("60"))

        second = engine.process_risk_group_batch_retry(
            "RG", "e2", "USD", D("100"), self.LIMITS, requests
        )
        self.assertIs(second, first)
        self.assertEqual(len(engine.audit_log), 2)
        self.assertEqual(
            engine.process_risk_group_batch_retry(
                "RG", "e3", "USD", D("100"), self.LIMITS, requests
            ).risk_groups["GR"].used,
            D("60"),
        )

    def test_risk_group_conflict_before_side_effects(self):
        engine = ClearingEngine()
        requests = [req("G1", "60", group="GR")]
        engine.process_risk_group_batch_retry(
            "RG", "e1", "USD", D("100"), self.LIMITS, requests
        )
        conflict = engine.process_risk_group_batch_retry(
            "RG", "e2", "USD", D("100"), {"GR": D("81")}, requests
        )
        self.assertIsInstance(conflict, BatchRetryConflict)
        self.assertEqual(len(engine.audit_log), 1)

    def test_risk_group_resume_keeps_single_occupancy(self):
        engine = ClearingEngine()
        requests = [
            req("G1", "10", group="GR"),
            req("G2", "10", group="GR"),
            req("G3", "10", group="GR"),
        ]
        original = ClearingEngine._execute
        state = {"n": 0}

        def flaky(self, request):
            state["n"] += 1
            if state["n"] == 2:
                raise RuntimeError("boom")
            return original(self, request)

        with patch.object(ClearingEngine, "_execute", flaky):
            with self.assertRaises(RuntimeError):
                engine.process_risk_group_batch_retry(
                    "RG", "e1", "USD", D("100"), self.LIMITS, requests
                )
        resumed = engine.process_risk_group_batch_retry(
            "RG", "e2", "USD", D("100"), self.LIMITS, requests
        )
        self.assertTrue(all(r.approved for r in resumed.results))
        # 组已用额度只累计一次，无重复占用。
        self.assertEqual(resumed.risk_groups["GR"].used, D("30"))
        self.assertEqual(len(engine.audit_log), 3)


class MulticurrencyRetryTests(unittest.TestCase):
    OPENING = {"USD": D("100"), "EUR": D("50")}

    def test_multicurrency_retry_returns_first_result(self):
        engine = ClearingEngine()
        requests = [
            req("M1", "40", currency="USD"),
            req("M2", "60", currency="EUR"),
        ]
        first = engine.process_multicurrency_batch_retry(
            "MC", "e1", self.OPENING, requests
        )
        self.assertTrue(first.results[0].approved)
        self.assertFalse(first.results[1].approved)
        self.assertEqual(
            dict(first.validated_available_balances),
            {"USD": D("60"), "EUR": D("50")},
        )
        second = engine.process_multicurrency_batch_retry(
            "MC", "e2", self.OPENING, requests
        )
        self.assertIs(second, first)
        self.assertEqual(len(engine.audit_log), 2)

    def test_multicurrency_conflict(self):
        engine = ClearingEngine()
        requests = [req("M1", "40", currency="USD")]
        engine.process_multicurrency_batch_retry(
            "MC", "e1", self.OPENING, requests
        )
        conflict = engine.process_multicurrency_batch_retry(
            "MC", "e2", {"USD": D("101")}, requests
        )
        self.assertIsInstance(conflict, BatchRetryConflict)
        self.assertEqual(len(engine.audit_log), 1)

    def test_multicurrency_resume_keeps_per_currency_balances(self):
        engine = ClearingEngine()
        requests = [
            req("M1", "40", currency="USD"),
            req("M2", "30", currency="EUR"),
            req("M3", "20", currency="USD"),
        ]
        original = ClearingEngine._execute
        state = {"n": 0}

        def flaky(self, request):
            state["n"] += 1
            if state["n"] == 2:
                raise RuntimeError("boom")
            return original(self, request)

        with patch.object(ClearingEngine, "_execute", flaky):
            with self.assertRaises(RuntimeError):
                engine.process_multicurrency_batch_retry(
                    "MC", "e1", self.OPENING, requests
                )
        resumed = engine.process_multicurrency_batch_retry(
            "MC", "e2", self.OPENING, requests
        )
        self.assertEqual(
            dict(resumed.validated_available_balances),
            {"USD": D("40"), "EUR": D("20")},
        )
        self.assertEqual(len(engine.audit_log), 3)
        self.assertEqual(
            [e.transaction_id for e in engine.audit_log], ["M1", "M2", "M3"]
        )


class BadDebtAndRecoveryWithRetryTests(unittest.TestCase):
    def test_bad_debt_recorded_once_and_recovery_still_works(self):
        engine = ClearingEngine()
        requests = [req("D1", "50", capital="40")]  # 10 未覆盖坏账
        first = engine.process_batch_retry(
            "BD", "e1", "USD", D("100"), requests
        )
        self.assertEqual(first.results[0].uncovered_bad_debt, D("10"))
        engine.process_batch_retry(
            "BD", "e2", "USD", D("100"), requests
        )
        # 坏账台账只入账一次。
        outstanding = engine.outstanding_bad_debts("USD")
        self.assertEqual(len(outstanding), 1)
        self.assertEqual(outstanding[0].balance, D("10"))

        summary = engine.audit_reconciliation("USD")
        self.assertEqual(summary.initial_bad_debt, D("10"))
        self.assertEqual(summary.outstanding_bad_debt, D("10"))

        # 既有回收流程不受影响。
        recovery = engine.process_recovery("RC-1", "USD", D("4"))
        self.assertEqual(recovery.total_recovered, D("4"))
        self.assertEqual(
            engine.outstanding_bad_debts("USD")[0].balance, D("6")
        )
        # 再重试批次不回写、不重复坏账；回收事件保留。
        again = engine.process_batch_retry(
            "BD", "e3", "USD", D("100"), requests
        )
        self.assertIs(again, first)
        self.assertEqual(len(engine.audit_log), 2)
        self.assertEqual(
            engine.outstanding_bad_debts("USD")[0].balance, D("6")
        )


if __name__ == "__main__":
    unittest.main()
