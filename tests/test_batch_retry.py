"""批次重试与断点恢复（process_batch_with_retry）公开行为测试。

覆盖：首次执行输出原有清算结果；相同内容重试（无论执行标识）返回完全
相同结果且不重复扣减 / 归因 / 追加审计；中断后从未完步骤继续；同批次
标识不同内容返回唯一冲突结果且无副作用；标识缺失 / 为空 / 摘要不可
计算返回唯一无效标识结果；并发提交同一批次只有一个推进；既有校验
异常与业务拒绝口径不变。
"""

import threading
import unittest
from decimal import Decimal

from vault_guard import (
    BatchRetryConflictResult,
    BatchSettlementResult,
    ClearingEngine,
    DuplicateTransactionError,
    EmptyBatchError,
    EmptyCreditorListError,
    InvalidBatchIdentifierResult,
    InvalidCurrencyError,
    InvalidRiskFactorError,
    MixedCurrencyError,
)

D = Decimal


def req(tid, amount, exposure="0", limit="1000", factor="0",
        creditors=(("senior", "60"), ("equity", "40")), capital="0"):
    normalized = []
    for item in creditors:
        if len(item) == 2:
            name, claim = item
            normalized.append((name, D(claim)))
        else:
            name, claim, ccy = item
            normalized.append((name, D(claim), ccy))
    return {
        "transaction_id": tid,
        "settlement_amount": D(amount),
        "notional_exposure": D(exposure),
        "base_limit": D(limit),
        "risk_factor": D(factor),
        "creditors": normalized,
        "supplementary_capital": D(capital),
    }


def three_requests():
    return [req("B1", "40"), req("B2", "30"), req("B3", "10")]


class FirstExecutionTests(unittest.TestCase):
    def test_first_success_matches_plain_batch_result(self):
        retry_engine = ClearingEngine()
        plain_engine = ClearingEngine()
        batch = retry_engine.process_batch_with_retry(
            "BATCH-1", "EXEC-1", "USD", D("100"), three_requests()
        )
        plain = plain_engine.process_batch("USD", D("100"), three_requests())
        self.assertIsInstance(batch, BatchSettlementResult)
        self.assertEqual(batch, plain)
        self.assertEqual(batch.validated_available_balance, D("20"))
        self.assertEqual(
            [e.event_id for e in retry_engine.audit_log],
            [e.event_id for e in plain_engine.audit_log],
        )

    def test_business_rejection_keeps_original_shape(self):
        engine = ClearingEngine()
        batch = engine.process_batch_with_retry(
            "BATCH-1", "EXEC-1", "USD", D("50"),
            [req("B1", "40"), req("B2", "20"), req("B3", "10")],
        )
        self.assertIsInstance(batch, BatchSettlementResult)
        r1, r2, r3 = batch.results
        self.assertTrue(r1.approved)
        self.assertFalse(r2.approved)
        self.assertEqual(
            r2.rejection_reason, "SETTLEMENT_EXCEEDS_AVAILABLE_BALANCE"
        )
        self.assertTrue(r3.approved)

    def test_generator_requests_are_supported(self):
        engine = ClearingEngine()
        batch = engine.process_batch_with_retry(
            "BATCH-1", "EXEC-1", "USD", D("100"),
            (r for r in three_requests()),
        )
        self.assertEqual(batch.validated_available_balance, D("20"))


class RetryReplayTests(unittest.TestCase):
    def test_retry_with_other_execution_id_returns_identical_result(self):
        engine = ClearingEngine()
        first = engine.process_batch_with_retry(
            "BATCH-1", "EXEC-1", "USD", D("100"), three_requests()
        )
        events_after_first = engine.audit_log
        retry = engine.process_batch_with_retry(
            "BATCH-1", "EXEC-2", "USD", D("100"), three_requests()
        )
        self.assertIs(retry, first)
        # 不重复追加审计记录。
        self.assertEqual(engine.audit_log, events_after_first)
        self.assertEqual(len(engine.audit_log), 3)

    def test_retry_with_same_execution_id_returns_identical_result(self):
        engine = ClearingEngine()
        first = engine.process_batch_with_retry(
            "BATCH-1", "EXEC-1", "USD", D("100"), three_requests()
        )
        again = engine.process_batch_with_retry(
            "BATCH-1", "EXEC-1", "USD", D("100"), three_requests()
        )
        self.assertIs(again, first)

    def test_retry_does_not_double_count_bad_debt_or_reconciliation(self):
        engine = ClearingEngine()
        engine.process_batch_with_retry(
            "BATCH-1", "EXEC-1", "USD", D("100"),
            [req("B1", "50", capital="40")],  # 产生 10 未覆盖坏账
        )
        engine.process_batch_with_retry(
            "BATCH-1", "EXEC-2", "USD", D("100"),
            [req("B1", "50", capital="40")],
        )
        summary = engine.audit_reconciliation("USD")
        self.assertEqual(summary.settlement_count, 1)
        self.assertEqual(summary.initial_bad_debt, D("10"))
        self.assertEqual(summary.outstanding_bad_debt, D("10"))
        outstanding = engine.outstanding_bad_debts("USD")
        self.assertEqual(len(outstanding), 1)
        self.assertEqual(outstanding[0].balance, D("10"))

    def test_equivalent_numeric_writings_are_same_content(self):
        engine = ClearingEngine()
        first = engine.process_batch_with_retry(
            "BATCH-1", "EXEC-1", "USD", D("100"),
            [req("B1", "40", capital="0")],
        )
        # int / float / 不同精度 Decimal 写法与首次内容等价。
        equivalent = [{
            "transaction_id": "B1",
            "settlement_amount": 40,
            "notional_exposure": 0.0,
            "base_limit": D("1000.0"),
            "risk_factor": D("0.00"),
            "creditors": [("senior", 60), ("equity", D("40.00"))],
            "supplementary_capital": D("-0"),
        }]
        retry = engine.process_batch_with_retry(
            "BATCH-1", "EXEC-2", "USD", D("100.00"), equivalent
        )
        self.assertIs(retry, first)
        self.assertEqual(len(engine.audit_log), 1)

    def test_retry_result_is_deterministic_regardless_of_execution_id(self):
        engine = ClearingEngine()
        results = [
            engine.process_batch_with_retry(
                "BATCH-1", f"EXEC-{i}", "USD", D("100"), three_requests()
            )
            for i in range(5)
        ]
        self.assertTrue(all(r is results[0] for r in results))
        self.assertEqual(len(engine.audit_log), 3)


class ResumeTests(unittest.TestCase):
    def _interrupting_engine(self, fail_at):
        """构造一个在执行第 fail_at 笔（0 起）时抛出一次的引擎。"""
        engine = ClearingEngine()
        original = engine._execute
        state = {"calls": 0, "failed": False}

        def flaky(request):
            calls = state["calls"]
            state["calls"] = calls + 1
            if calls == fail_at and not state["failed"]:
                state["failed"] = True
                raise RuntimeError("simulated interruption")
            return original(request)

        engine._execute = flaky
        return engine

    def test_interrupted_batch_resumes_from_next_step(self):
        engine = self._interrupting_engine(fail_at=1)
        with self.assertRaises(RuntimeError):
            engine.process_batch_with_retry(
                "BATCH-1", "EXEC-1", "USD", D("100"), three_requests()
            )
        # 中断时第一笔已完成：恰好一条事件，余额与流水号已占用。
        self.assertEqual(len(engine.audit_log), 1)
        self.assertTrue(engine.has_transaction("B1"))
        self.assertFalse(engine.has_transaction("B2"))

        batch = engine.process_batch_with_retry(
            "BATCH-1", "EXEC-2", "USD", D("100"), three_requests()
        )
        self.assertIsInstance(batch, BatchSettlementResult)
        self.assertEqual(len(batch.results), 3)
        # 已完成步骤不重复产生副作用：事件总数为 3，序号 1..3 各一条。
        self.assertEqual(len(engine.audit_log), 3)
        self.assertEqual([e.sequence for e in engine.audit_log], [1, 2, 3])
        self.assertEqual(
            [e.transaction_id for e in engine.audit_log], ["B1", "B2", "B3"]
        )
        # 金额分配与一次性执行完全一致：不会得到两套分配。
        plain = ClearingEngine().process_batch("USD", D("100"), three_requests())
        self.assertEqual(batch, plain)

    def test_resume_after_interruption_at_first_step(self):
        engine = self._interrupting_engine(fail_at=0)
        with self.assertRaises(RuntimeError):
            engine.process_batch_with_retry(
                "BATCH-1", "EXEC-1", "USD", D("100"), three_requests()
            )
        self.assertEqual(len(engine.audit_log), 0)
        batch = engine.process_batch_with_retry(
            "BATCH-1", "EXEC-2", "USD", D("100"), three_requests()
        )
        self.assertEqual(batch.validated_available_balance, D("20"))
        self.assertEqual(len(engine.audit_log), 3)

    def test_retry_after_completion_is_full_replay(self):
        engine = self._interrupting_engine(fail_at=2)
        with self.assertRaises(RuntimeError):
            engine.process_batch_with_retry(
                "BATCH-1", "EXEC-1", "USD", D("100"), three_requests()
            )
        resumed = engine.process_batch_with_retry(
            "BATCH-1", "EXEC-2", "USD", D("100"), three_requests()
        )
        replayed = engine.process_batch_with_retry(
            "BATCH-1", "EXEC-3", "USD", D("100"), three_requests()
        )
        self.assertIs(replayed, resumed)
        self.assertEqual(len(engine.audit_log), 3)


class ConflictTests(unittest.TestCase):
    def test_same_batch_id_with_different_content_is_conflict(self):
        engine = ClearingEngine()
        engine.process_batch_with_retry(
            "BATCH-1", "EXEC-1", "USD", D("100"), three_requests()
        )
        events_before = engine.audit_log
        conflict = engine.process_batch_with_retry(
            "BATCH-1", "EXEC-2", "USD", D("100"),
            [req("B1", "40"), req("B2", "30")],
        )
        self.assertIsInstance(conflict, BatchRetryConflictResult)
        self.assertEqual(conflict.batch_id, "BATCH-1")
        self.assertTrue(conflict.original_request_digest)
        self.assertTrue(conflict.incoming_request_digest)
        self.assertNotEqual(
            conflict.original_request_digest,
            conflict.incoming_request_digest,
        )
        # 冲突在产生任何资金或审计副作用之前拒绝。
        self.assertEqual(engine.audit_log, events_before)

    def test_conflict_observable_before_any_side_effect_on_fresh_amounts(self):
        engine = ClearingEngine()
        engine.process_batch_with_retry(
            "BATCH-1", "EXEC-1", "USD", D("100"), three_requests()
        )
        conflict = engine.process_batch_with_retry(
            "BATCH-1", "EXEC-2", "USD", D("200"), three_requests()
        )
        self.assertIsInstance(conflict, BatchRetryConflictResult)
        self.assertEqual(len(engine.audit_log), 3)

    def test_conflict_after_interruption_does_not_extend_partial_state(self):
        engine = ClearingEngine()
        original = engine._execute
        state = {"calls": 0}

        def flaky(request):
            state["calls"] += 1
            if state["calls"] == 2:
                raise RuntimeError("simulated interruption")
            return original(request)

        engine._execute = flaky
        with self.assertRaises(RuntimeError):
            engine.process_batch_with_retry(
                "BATCH-1", "EXEC-1", "USD", D("100"), three_requests()
            )
        self.assertEqual(len(engine.audit_log), 1)
        conflict = engine.process_batch_with_retry(
            "BATCH-1", "EXEC-2", "USD", D("100"),
            [req("B1", "40"), req("B2", "99")],
        )
        self.assertIsInstance(conflict, BatchRetryConflictResult)
        self.assertEqual(len(engine.audit_log), 1)

    def test_different_batch_id_processes_independently(self):
        engine = ClearingEngine()
        first = engine.process_batch_with_retry(
            "BATCH-1", "EXEC-1", "USD", D("100"), [req("B1", "40")]
        )
        second = engine.process_batch_with_retry(
            "BATCH-2", "EXEC-1", "USD", D("100"), [req("C1", "40")]
        )
        self.assertIsInstance(second, BatchSettlementResult)
        self.assertIsNot(second, first)
        self.assertEqual(len(engine.audit_log), 2)


class InvalidIdentifierTests(unittest.TestCase):
    def test_missing_or_empty_batch_id(self):
        engine = ClearingEngine()
        for bad in (None, "", "   ", 123):
            with self.subTest(bad=bad):
                result = engine.process_batch_with_retry(
                    bad, "EXEC-1", "USD", D("100"), three_requests()
                )
                self.assertIsInstance(result, InvalidBatchIdentifierResult)
                self.assertEqual(result.reason, "MISSING_OR_EMPTY_BATCH_ID")
        self.assertEqual(len(engine.audit_log), 0)

    def test_missing_or_empty_execution_id(self):
        engine = ClearingEngine()
        for bad in (None, "", "   ", 7):
            with self.subTest(bad=bad):
                result = engine.process_batch_with_retry(
                    "BATCH-1", bad, "USD", D("100"), three_requests()
                )
                self.assertIsInstance(result, InvalidBatchIdentifierResult)
                self.assertEqual(
                    result.reason, "MISSING_OR_EMPTY_EXECUTION_ID"
                )
        self.assertEqual(len(engine.audit_log), 0)

    def test_uncomputable_digest(self):
        engine = ClearingEngine()
        bad_payloads = [
            None,                       # 请求清单缺失
            42,                         # 请求清单不可迭代
            ["not-a-mapping"],          # 单项不是映射
            [{"transaction_id": "B1", "settlement_amount": "abc"}],
            [{"transaction_id": "B1", "settlement_amount": D("1"),
              "notional_exposure": D("0"), "base_limit": D("10"),
              "risk_factor": D("0"), "creditors": [("c", "x")]}],
        ]
        for payload in bad_payloads:
            with self.subTest(payload=payload):
                result = engine.process_batch_with_retry(
                    "BATCH-1", "EXEC-1", "USD", D("100"), payload
                )
                self.assertIsInstance(result, InvalidBatchIdentifierResult)
                self.assertEqual(
                    result.reason, "REQUEST_DIGEST_UNCOMPUTABLE"
                )
        self.assertEqual(len(engine.audit_log), 0)

    def test_invalid_identifier_result_echoes_identifiers(self):
        engine = ClearingEngine()
        result = engine.process_batch_with_retry(
            None, "EXEC-9", "USD", D("100"), three_requests()
        )
        self.assertIsNone(result.batch_id)
        self.assertEqual(result.execution_id, "EXEC-9")

    def test_invalid_results_do_not_consume_batch_id(self):
        engine = ClearingEngine()
        engine.process_batch_with_retry(
            "BATCH-1", None, "USD", D("100"), three_requests()
        )
        batch = engine.process_batch_with_retry(
            "BATCH-1", "EXEC-1", "USD", D("100"), three_requests()
        )
        self.assertIsInstance(batch, BatchSettlementResult)


class ExistingSemanticsTests(unittest.TestCase):
    """既有失败口径不被新拒绝结果混入。"""

    def test_empty_batch_still_raises(self):
        with self.assertRaises(EmptyBatchError):
            ClearingEngine().process_batch_with_retry(
                "BATCH-1", "EXEC-1", "USD", D("100"), []
            )

    def test_missing_currency_still_raises(self):
        with self.assertRaises(InvalidCurrencyError):
            ClearingEngine().process_batch_with_retry(
                "BATCH-1", "EXEC-1", "  ", D("100"), three_requests()
            )

    def test_mixed_currency_still_raises(self):
        with self.assertRaises(MixedCurrencyError):
            ClearingEngine().process_batch_with_retry(
                "BATCH-1", "EXEC-1", "USD", D("100"),
                [req("B1", "1", creditors=(("f", "1", "EUR"),))],
            )

    def test_duplicate_transaction_still_raises(self):
        with self.assertRaises(DuplicateTransactionError):
            ClearingEngine().process_batch_with_retry(
                "BATCH-1", "EXEC-1", "USD", D("100"),
                [req("B1", "1"), req("B1", "2")],
            )

    def test_empty_creditor_list_still_raises(self):
        with self.assertRaises(EmptyCreditorListError):
            ClearingEngine().process_batch_with_retry(
                "BATCH-1", "EXEC-1", "USD", D("100"),
                [req("B1", "1", creditors=())],
            )

    def test_risk_factor_out_of_bounds_still_raises(self):
        with self.assertRaises(InvalidRiskFactorError):
            ClearingEngine().process_batch_with_retry(
                "BATCH-1", "EXEC-1", "USD", D("100"),
                [req("B1", "1", factor="1.01")],
            )

    def test_negative_amount_still_raises_value_error(self):
        with self.assertRaises(ValueError):
            ClearingEngine().process_batch_with_retry(
                "BATCH-1", "EXEC-1", "USD", D("100"), [req("B1", "-1")]
            )

    def test_failed_validation_does_not_pin_batch_id(self):
        engine = ClearingEngine()
        with self.assertRaises(EmptyCreditorListError):
            engine.process_batch_with_retry(
                "BATCH-1", "EXEC-1", "USD", D("100"),
                [req("B1", "1", creditors=())],
            )
        # 校验失败未产生副作用：修正后可用同一批次标识重新提交。
        batch = engine.process_batch_with_retry(
            "BATCH-1", "EXEC-2", "USD", D("100"), [req("B1", "1")]
        )
        self.assertIsInstance(batch, BatchSettlementResult)


class ConcurrencyTests(unittest.TestCase):
    def test_concurrent_same_batch_single_execution(self):
        engine = ClearingEngine()
        barrier = threading.Barrier(8)
        outcomes = []
        errors = []

        def submit(i):
            try:
                barrier.wait(timeout=5)
                outcomes.append(
                    engine.process_batch_with_retry(
                        "BATCH-1", f"EXEC-{i}", "USD", D("100"),
                        three_requests(),
                    )
                )
            except Exception as exc:  # pragma: no cover - 便于诊断
                errors.append(exc)

        threads = [threading.Thread(target=submit, args=(i,)) for i in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(errors, [])
        self.assertEqual(len(outcomes), 8)
        # 所有并发请求取得同一最终结果，审计轨迹只有一套。
        self.assertTrue(all(o is outcomes[0] for o in outcomes))
        self.assertEqual(len(engine.audit_log), 3)
        self.assertEqual([e.sequence for e in engine.audit_log], [1, 2, 3])


if __name__ == "__main__":
    unittest.main()
