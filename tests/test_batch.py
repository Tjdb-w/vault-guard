"""多笔批量结算的公开行为测试。

覆盖：滚动余额清算、批内限额拒绝不阻断后续、结果同序与事件 ID、
批次不建事件、序号递增、整批校验失败的原子回退（不生成事件 / 不占
流水号 / 不改余额）、异常优先级、与单笔入口混用同一台账、以及一次性
便捷函数不跨批次去重。
"""

import unittest
from decimal import Decimal

from vault_guard import (
    BatchSettlementResult,
    ClearingEngine,
    DuplicateTransactionError,
    EmptyBatchError,
    EmptyCreditorListError,
    InvalidCurrencyError,
    InvalidRiskFactorError,
    MixedCurrencyError,
    process_settlement_batch,
)

D = Decimal


def req(
    transaction_id,
    settlement_amount,
    creditors,
    *,
    notional_exposure="0",
    base_limit="1000",
    risk_factor="0",
    supplementary_capital=None,
):
    """构造批次内单笔请求映射（默认限额宽松，聚焦余额滚动）。"""
    data = {
        "transaction_id": transaction_id,
        "settlement_amount": D(settlement_amount),
        "notional_exposure": D(notional_exposure),
        "base_limit": D(base_limit),
        "risk_factor": D(risk_factor),
        "creditors": creditors,
    }
    if supplementary_capital is not None:
        data["supplementary_capital"] = D(supplementary_capital)
    return data


class BatchSuccessTests(unittest.TestCase):
    def test_results_same_order_as_requests(self):
        engine = ClearingEngine()
        requests = [
            req("B1", "30", [("a", D("30"))]),
            req("B2", "20", [("b", D("20"))]),
            req("B3", "50", [("c", D("50"))]),
        ]
        batch = engine.process_batch("USD", D("100"), requests)
        self.assertIsInstance(batch, BatchSettlementResult)
        self.assertEqual(
            tuple(r.transaction_id for r in batch.results), ("B1", "B2", "B3")
        )
        self.assertTrue(all(r.approved for r in batch.results))
        self.assertEqual(len(batch.results), len(batch.event_ids))
        self.assertEqual(
            batch.event_ids, tuple(r.event_id for r in batch.results)
        )

    def test_rolling_balance_deducted_by_approved_requests(self):
        engine = ClearingEngine()
        batch = engine.process_batch(
            "USD",
            D("100"),
            [
                req("B1", "30", [("a", D("30"))]),
                req("B2", "45", [("b", D("45"))]),
            ],
        )
        # 第一笔后即时余额 70，第二笔后 25；批次最终余额 25。
        self.assertEqual(
            batch.results[0].validated_available_balance, D("70")
        )
        self.assertEqual(
            batch.results[1].validated_available_balance, D("25")
        )
        self.assertEqual(batch.validated_available_balance, D("25"))

    def test_rejected_request_does_not_move_funds_or_block_next(self):
        engine = ClearingEngine()
        batch = engine.process_batch(
            "USD",
            D("100"),
            [
                req("B1", "30", [("a", D("30"))]),
                # 滚动余额 70：拟清算 80 超余额，拒绝、不动资金。
                req("B2", "80", [("b", D("80"))]),
                # 后续请求仍按 70 余额继续处理，本笔 20 放行至 50。
                req("B3", "20", [("c", D("20"))]),
            ],
        )
        r1, r2, r3 = batch.results
        self.assertTrue(r1.approved)
        self.assertFalse(r2.approved)
        self.assertEqual(
            r2.rejection_reason, "SETTLEMENT_EXCEEDS_AVAILABLE_BALANCE"
        )
        self.assertEqual(r2.validated_available_balance, D("70"))
        self.assertEqual(r2.total_pool_allocated, D("0"))
        self.assertEqual(r2.uncovered_bad_debt, D("0"))
        self.assertTrue(all(a.bad_debt == D("0") for a in r2.attributions))
        self.assertTrue(r3.approved)
        self.assertEqual(r3.validated_available_balance, D("50"))
        self.assertEqual(batch.validated_available_balance, D("50"))

    def test_risk_occupancy_rejection_in_batch(self):
        engine = ClearingEngine()
        # 风险占用 = 40 + 100 * 0.6 = 100 > 90 -> 风险拒绝。
        batch = engine.process_batch(
            "USD",
            D("100"),
            [
                req(
                    "B1",
                    "40",
                    [("a", D("40"))],
                    notional_exposure="100",
                    base_limit="90",
                    risk_factor="0.6",
                )
            ],
        )
        result = batch.results[0]
        self.assertFalse(result.approved)
        self.assertEqual(
            result.rejection_reason, "RISK_OCCUPANCY_EXCEEDS_BASE_LIMIT"
        )
        self.assertEqual(result.risk_occupancy, D("100"))
        # 拒绝不动余额，批次最终余额仍是期初值。
        self.assertEqual(result.validated_available_balance, D("100"))
        self.assertEqual(batch.validated_available_balance, D("100"))

    def test_supplementary_capital_fills_residual_in_batch(self):
        engine = ClearingEngine()
        batch = engine.process_batch(
            "USD",
            D("100"),
            [
                req(
                    "B1",
                    "50",
                    [("senior", D("60")), ("mezz", D("30"))],
                    supplementary_capital="40",
                )
            ],
        )
        result = batch.results[0]
        self.assertTrue(result.approved)
        # 池内 senior 50；资本补 senior 10、mezz 30；无坏账。
        self.assertEqual(result.pool_allocations, (D("50"), D("0")))
        self.assertEqual(result.capital_allocations, (D("10"), D("30")))
        self.assertEqual(result.uncovered_bad_debt, D("0"))
        # 资本不扣池余额：100 - 50 = 50。
        self.assertEqual(result.validated_available_balance, D("50"))

    def test_events_appended_per_request_with_increasing_sequence(self):
        engine = ClearingEngine()
        batch = engine.process_batch(
            "USD",
            D("100"),
            [
                req("B1", "10", [("a", D("10"))]),
                req("B2", "90", [("b", D("90"))]),  # 超滚动余额 90? ==90 放行
            ],
        )
        # 第二笔拟清算 90，滚动余额 90，边界取等号放行。
        self.assertTrue(batch.results[1].approved)
        events = engine.audit_log
        self.assertEqual(len(events), 2)
        self.assertEqual([e.sequence for e in events], [1, 2])
        self.assertEqual(
            tuple(e.event_id for e in events), batch.event_ids
        )
        # 批次不另建事件：事件数严格等于请求数。
        self.assertEqual(len(events), len(batch.results))


class BatchAuditIntegrationTests(unittest.TestCase):
    def test_batch_shares_sequence_with_single_entry(self):
        engine = ClearingEngine()
        engine.process(
            "S0", "USD", D("100"), D("10"), D("0"), D("1000"), D("0"),
            [("a", D("10"))],
        )
        engine.process_batch(
            "USD",
            D("90"),
            [
                req("B1", "20", [("a", D("20"))]),
                req("B2", "30", [("a", D("30"))]),
            ],
        )
        self.assertEqual(
            [e.sequence for e in engine.audit_log], [1, 2, 3]
        )
        # 批次期初余额独立给出；单笔后的台账不影响批次期初值。
        self.assertEqual(
            engine.audit_log[-1].validated_available_balance, D("40")
        )

    def test_results_registered_in_ledger(self):
        engine = ClearingEngine()
        engine.process_batch(
            "USD", D("100"), [req("B1", "10", [("a", D("10"))])]
        )
        self.assertIsNotNone(engine.result_of("B1"))
        self.assertIsNotNone(engine.get_event("B1"))
        self.assertTrue(engine.has_transaction("B1"))

    def test_batch_rejects_transaction_already_in_ledger(self):
        engine = ClearingEngine()
        engine.process(
            "DUP", "USD", D("100"), D("10"), D("0"), D("1000"), D("0"),
            [("a", D("10"))],
        )
        with self.assertRaises(DuplicateTransactionError):
            engine.process_batch(
                "USD", D("100"), [req("DUP", "10", [("a", D("10"))])]
            )
        # 台账仍只有单笔那一条事件。
        self.assertEqual(len(engine.audit_log), 1)


class BatchAtomicValidationTests(unittest.TestCase):
    def _engine_with_two_valid_requests(self):
        return ClearingEngine(), [
            req("B1", "10", [("a", D("10"))]),
            req("B2", "10", [("b", D("10"))]),
        ]

    def test_empty_batch(self):
        for bad in ([], None):
            with self.subTest(bad=bad):
                with self.assertRaises(EmptyBatchError):
                    ClearingEngine().process_batch("USD", D("100"), bad)

    def test_missing_batch_currency(self):
        for bad in (None, "", "   "):
            with self.subTest(bad=bad):
                with self.assertRaises(InvalidCurrencyError):
                    ClearingEngine().process_batch(
                        bad, D("100"), [req("B1", "10", [("a", D("10"))])]
                    )

    def test_creditor_currency_mismatch(self):
        with self.assertRaises(MixedCurrencyError):
            ClearingEngine().process_batch(
                "USD",
                D("100"),
                [req("B1", "10", [("foreign", D("10"), "EUR")])],
            )

    def test_request_currency_mismatch_with_batch(self):
        request = req("B1", "10", [("a", D("10"))])
        request["currency"] = "EUR"
        with self.assertRaises(MixedCurrencyError):
            ClearingEngine().process_batch("USD", D("100"), [request])

    def test_duplicate_within_batch(self):
        engine = ClearingEngine()
        with self.assertRaises(DuplicateTransactionError):
            engine.process_batch(
                "USD",
                D("100"),
                [
                    req("B1", "10", [("a", D("10"))]),
                    req("B1", "20", [("b", D("20"))]),
                ],
            )
        self.assertEqual(len(engine.audit_log), 0)

    def test_empty_creditor_list(self):
        with self.assertRaises(EmptyCreditorListError):
            ClearingEngine().process_batch(
                "USD", D("100"), [req("B1", "10", [])]
            )

    def test_risk_factor_out_of_bounds(self):
        with self.assertRaises(InvalidRiskFactorError):
            ClearingEngine().process_batch(
                "USD",
                D("100"),
                [
                    req(
                        "B1",
                        "10",
                        [("a", D("10"))],
                        risk_factor="1.01",
                    )
                ],
            )

    def test_negative_amounts_raise_value_error(self):
        # 批次期初余额为负。
        with self.assertRaises(ValueError):
            ClearingEngine().process_batch(
                "USD", D("-1"), [req("B1", "10", [("a", D("10"))])]
            )
        # 单笔拟清算金额为负。
        with self.assertRaises(ValueError):
            ClearingEngine().process_batch(
                "USD", D("100"), [req("B1", "-1", [("a", D("10"))])]
            )

    def test_missing_required_field_raises_value_error(self):
        bad = {"transaction_id": "B1", "creditors": [("a", D("10"))]}
        with self.assertRaises(ValueError):
            ClearingEngine().process_batch("USD", D("100"), [bad])

    def test_pool_balance_in_request_rejected(self):
        request = req("B1", "10", [("a", D("10"))])
        request["pool_balance"] = D("500")
        with self.assertRaises(ValueError):
            ClearingEngine().process_batch("USD", D("100"), [request])

    def test_failed_batch_leaves_no_state_at_all(self):
        engine = ClearingEngine()
        # 第一笔合法、第二笔风险系数越界：整批必须回退。
        with self.assertRaises(InvalidRiskFactorError):
            engine.process_batch(
                "USD",
                D("100"),
                [
                    req("B1", "10", [("a", D("10"))]),
                    req(
                        "B2",
                        "10",
                        [("b", D("10"))],
                        risk_factor="2",
                    ),
                ],
            )
        # 无事件、无结果、流水号未占用。
        self.assertEqual(len(engine.audit_log), 0)
        self.assertIsNone(engine.result_of("B1"))
        self.assertFalse(engine.has_transaction("B1"))
        self.assertFalse(engine.has_transaction("B2"))
        # 同批流水号修正后可原样重新提交成功。
        batch = engine.process_batch(
            "USD",
            D("100"),
            [
                req("B1", "10", [("a", D("10"))]),
                req("B2", "10", [("b", D("10"))]),
            ],
        )
        self.assertTrue(all(r.approved for r in batch.results))
        self.assertEqual(batch.validated_available_balance, D("80"))
        self.assertEqual([e.sequence for e in engine.audit_log], [1, 2])

    def test_error_priority_empty_before_currency(self):
        # 空批次先于币种缺失判定。
        with self.assertRaises(EmptyBatchError):
            ClearingEngine().process_batch("", D("100"), [])

    def test_error_priority_duplicate_before_risk_factor(self):
        # 批内重复流水号先于后笔风险系数越界暴露。
        with self.assertRaises(DuplicateTransactionError):
            ClearingEngine().process_batch(
                "USD",
                D("100"),
                [
                    req("B1", "10", [("a", D("10"))]),
                    req(
                        "B1",
                        "10",
                        [("b", D("10"))],
                        risk_factor="2",
                    ),
                ],
            )


class ModuleLevelBatchTests(unittest.TestCase):
    def test_one_shot_engine_returns_batch_result(self):
        batch = process_settlement_batch(
            "USD",
            D("100"),
            [
                req("M1", "60", [("a", D("60"))]),
                req("M2", "30", [("b", D("30"))]),
            ],
        )
        self.assertIsInstance(batch, BatchSettlementResult)
        self.assertTrue(all(r.approved for r in batch.results))
        self.assertEqual(batch.validated_available_balance, D("10"))

    def test_no_dedup_across_batches(self):
        requests = [req("X1", "10", [("a", D("10"))])]
        first = process_settlement_batch("USD", D("100"), requests)
        second = process_settlement_batch("USD", D("100"), requests)
        # 两次独立一次性引擎：同一流水号互不冲突，各自成立。
        self.assertTrue(first.results[0].approved)
        self.assertTrue(second.results[0].approved)
        self.assertEqual(
            first.event_ids, second.event_ids
        )


class BatchImmutabilityTests(unittest.TestCase):
    def test_batch_result_is_frozen(self):
        batch = process_settlement_batch(
            "USD", D("100"), [req("I1", "10", [("a", D("10"))])]
        )
        with self.assertRaises(Exception):
            batch.results = ()  # type: ignore[misc]
        with self.assertRaises(Exception):
            batch.validated_available_balance = D("0")  # type: ignore[misc]


if __name__ == "__main__":
    unittest.main()
