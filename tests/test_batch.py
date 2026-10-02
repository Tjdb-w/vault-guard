"""多笔批次结算（process_batch / process_settlement_batch）公开行为测试。

覆盖：滚动余额、限额拒绝不改余额、批次整体校验顺序与异常、失败回退
（不生成事件 / 不占流水号 / 不改余额）、审计序号递增、结果结构不可变、
模块级一次性入口不跨批次去重。
"""

import unittest
from dataclasses import FrozenInstanceError
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


class BatchSettlementTests(unittest.TestCase):
    def test_rolling_balance_across_requests(self):
        engine = ClearingEngine()
        batch = engine.process_batch(
            currency="USD",
            opening_pool_balance=D("100"),
            requests=[req("B1", "40"), req("B2", "30"), req("B3", "10")],
        )
        self.assertIsInstance(batch, BatchSettlementResult)
        # 各笔即时余额：100-40=60，60-30=30，30-10=20。
        self.assertEqual(
            [r.validated_available_balance for r in batch.results],
            [D("60"), D("30"), D("20")],
        )
        self.assertEqual(batch.validated_available_balance, D("20"))
        self.assertEqual(len(batch.results), 3)
        self.assertTrue(all(r.approved for r in batch.results))

    def test_results_and_event_ids_align_with_requests(self):
        engine = ClearingEngine()
        batch = engine.process_batch(
            "USD", D("100"),
            [req("B1", "10"), req("B2", "10"), req("B3", "10")],
        )
        self.assertEqual(
            [r.transaction_id for r in batch.results], ["B1", "B2", "B3"]
        )
        self.assertEqual(len(batch.event_ids), 3)
        for result, event_id in zip(batch.results, batch.event_ids, strict=True):
            self.assertEqual(result.event_id, event_id)
            self.assertEqual(
                engine.get_event(result.transaction_id).event_id, event_id
            )

    def test_rejection_keeps_balance_and_continues(self):
        engine = ClearingEngine()
        batch = engine.process_batch(
            "USD", D("50"),
            [
                req("B1", "40"),                    # 通过，余额 10
                req("B2", "20"),                    # 超余额，拒绝
                req("B3", "10", limit="1000"),      # 通过，余额 0
            ],
        )
        r1, r2, r3 = batch.results
        self.assertTrue(r1.approved)
        self.assertFalse(r2.approved)
        self.assertEqual(
            r2.rejection_reason, "SETTLEMENT_EXCEEDS_AVAILABLE_BALANCE"
        )
        # 拒绝不动资金、不确认坏账，余额保持 10。
        self.assertEqual(r2.validated_available_balance, D("10"))
        self.assertEqual(r2.uncovered_bad_debt, D("0"))
        self.assertEqual(r2.total_pool_allocated, D("0"))
        self.assertTrue(r3.approved)
        self.assertEqual(batch.validated_available_balance, D("0"))

    def test_risk_occupancy_rejection_in_batch(self):
        engine = ClearingEngine()
        batch = engine.process_batch(
            "USD", D("100"),
            [req("B1", "40", exposure="100", limit="90", factor="0.6")],
        )
        (result,) = batch.results
        self.assertFalse(result.approved)
        self.assertEqual(
            result.rejection_reason, "RISK_OCCUPANCY_EXCEEDS_BASE_LIMIT"
        )
        self.assertEqual(batch.validated_available_balance, D("100"))

    def test_waterfall_and_capital_per_request(self):
        engine = ClearingEngine()
        batch = engine.process_batch(
            "USD", D("100"),
            [req("B1", "50", capital="40")],
        )
        (result,) = batch.results
        self.assertEqual(result.pool_allocations, (D("50"), D("0")))
        self.assertEqual(result.capital_allocations, (D("10"), D("30")))
        self.assertEqual(result.uncovered_bad_debt, D("10"))
        self.assertEqual(result.validated_available_balance, D("50"))

    def test_batch_appends_one_event_per_request_in_sequence(self):
        engine = ClearingEngine()
        engine.process(  # 既有单笔事件，序号为 1
            "S0", "USD", D("10"), D("10"), D("0"), D("100"), D("0"),
            [("c", D("10"))],
        )
        engine.process_batch(
            "USD", D("100"), [req("B1", "10"), req("B2", "20")]
        )
        self.assertEqual([e.sequence for e in engine.audit_log], [1, 2, 3])
        self.assertEqual(
            [e.transaction_id for e in engine.audit_log], ["S0", "B1", "B2"]
        )

    def test_batch_result_is_immutable(self):
        batch = process_settlement_batch("USD", D("10"), [req("B1", "5")])
        self.assertIsInstance(batch.results, tuple)
        self.assertIsInstance(batch.event_ids, tuple)
        with self.assertRaises(FrozenInstanceError):
            batch.validated_available_balance = D("0")


class BatchValidationTests(unittest.TestCase):
    def test_empty_batch(self):
        for bad in ([], None, ()):
            with self.subTest(bad=bad):
                with self.assertRaises(EmptyBatchError):
                    ClearingEngine().process_batch("USD", D("100"), bad)

    def test_missing_currency(self):
        for bad in (None, "", "   "):
            with self.subTest(bad=bad):
                with self.assertRaises(InvalidCurrencyError):
                    ClearingEngine().process_batch(bad, D("100"), [req("B1", "1")])

    def test_empty_batch_checked_before_currency(self):
        with self.assertRaises(EmptyBatchError):
            ClearingEngine().process_batch(None, D("100"), [])

    def test_mixed_currency_before_duplicate_check(self):
        engine = ClearingEngine()
        engine.process(
            "DUP", "USD", D("10"), D("1"), D("0"), D("100"), D("0"),
            [("c", D("1"))],
        )
        # 第一笔与台账重复、第二笔债权币种不一致：先抛 MixedCurrencyError。
        with self.assertRaises(MixedCurrencyError):
            engine.process_batch(
                "USD", D("100"),
                [req("DUP", "1"), req("B2", "1", creditors=(("f", "1", "EUR"),))],
            )

    def test_mixed_currency_rejected(self):
        with self.assertRaises(MixedCurrencyError):
            ClearingEngine().process_batch(
                "USD", D("100"),
                [req("B1", "1", creditors=(("foreign", "1", "EUR"),))],
            )

    def test_duplicate_within_batch(self):
        with self.assertRaises(DuplicateTransactionError):
            ClearingEngine().process_batch(
                "USD", D("100"), [req("B1", "1"), req("B1", "2")]
            )

    def test_duplicate_against_ledger(self):
        engine = ClearingEngine()
        engine.process(
            "B1", "USD", D("10"), D("1"), D("0"), D("100"), D("0"),
            [("c", D("1"))],
        )
        with self.assertRaises(DuplicateTransactionError):
            engine.process_batch("USD", D("100"), [req("B1", "1")])

    def test_empty_creditor_list(self):
        with self.assertRaises(EmptyCreditorListError):
            ClearingEngine().process_batch(
                "USD", D("100"), [req("B1", "1", creditors=())]
            )

    def test_risk_factor_out_of_bounds(self):
        with self.assertRaises(InvalidRiskFactorError):
            ClearingEngine().process_batch(
                "USD", D("100"), [req("B1", "1", factor="1.01")]
            )

    def test_invalid_values_raise_value_error(self):
        engine = ClearingEngine()
        with self.assertRaises(ValueError):
            engine.process_batch("USD", D("-1"), [req("B1", "1")])
        with self.assertRaises(ValueError):
            engine.process_batch("USD", D("100"), [req("B1", "-1")])
        with self.assertRaises(ValueError):
            engine.process_batch("USD", D("100"), [req("B1", "1", capital="-2")])
        with self.assertRaises(ValueError):
            engine.process_batch("USD", D("100"), [{"transaction_id": "B1"}])
        with self.assertRaises(ValueError):
            engine.process_batch("USD", D("100"), ["not-a-mapping"])

    def test_failure_rolls_back_batch_state(self):
        engine = ClearingEngine()
        with self.assertRaises(DuplicateTransactionError):
            engine.process_batch(
                "USD", D("100"), [req("B1", "10"), req("B1", "20")]
            )
        # 不生成事件、不占流水号、不改余额：修正后可整体重提。
        self.assertEqual(len(engine.audit_log), 0)
        self.assertFalse(engine.has_transaction("B1"))
        batch = engine.process_batch(
            "USD", D("100"), [req("B1", "10"), req("B2", "20")]
        )
        self.assertEqual(len(engine.audit_log), 2)
        self.assertEqual(batch.validated_available_balance, D("70"))

    def test_failed_batch_leaves_ledger_usable(self):
        engine = ClearingEngine()
        with self.assertRaises(ValueError):
            engine.process_batch(
                "USD", D("100"), [req("B1", "10"), req("B2", "-1")]
            )
        self.assertEqual(len(engine.audit_log), 0)
        self.assertIsNone(engine.result_of("B1"))
        # 单笔入口不受影响。
        result = engine.process(
            "B1", "USD", D("100"), D("10"), D("0"), D("100"), D("0"),
            [("c", D("10"))],
        )
        self.assertTrue(result.approved)


class ModuleLevelBatchTests(unittest.TestCase):
    def test_process_settlement_batch_one_shot(self):
        batch = process_settlement_batch(
            "USD", D("100"), [req("M1", "40"), req("M2", "30")]
        )
        self.assertEqual(batch.validated_available_balance, D("30"))
        self.assertEqual(len(batch.event_ids), 2)

    def test_no_cross_batch_dedup(self):
        # 一次性引擎：相同流水号可在不同批次重复使用。
        r1 = process_settlement_batch("USD", D("100"), [req("M1", "10")])
        r2 = process_settlement_batch("USD", D("100"), [req("M1", "10")])
        self.assertTrue(r1.results[0].approved)
        self.assertTrue(r2.results[0].approved)


if __name__ == "__main__":
    unittest.main()
