"""存续坏账回收的公开行为测试。

覆盖：跨事件按顺序冲减、部分覆盖、零额回收、回收审计事件、只读查询、
历史结果不回写、失败路径不产生状态变更，以及各类输入异常。
"""

import unittest
from decimal import Decimal

from vault_guard import (
    ClearingEngine,
    DuplicateTransactionError,
    InvalidCurrencyError,
    NoOutstandingBadDebtError,
    RecoveryAmountExceedsOutstandingError,
)

D = Decimal


def make_engine_with_bad_debt() -> ClearingEngine:
    """构造两笔已放行结算，留下跨事件、跨债权的存续坏账。

    T1：债权 a 30 / b 30，池内 40 -> b 坏账 20。
    T2：债权 c 25，池内 10 -> c 坏账 15。
    """
    engine = ClearingEngine()
    engine.process(
        "T1", "USD", D("100"), D("40"), D("0"), D("100"), D("0"),
        [("a", D("30")), ("b", D("30"))],
    )
    engine.process(
        "T2", "USD", D("100"), D("10"), D("0"), D("100"), D("0"),
        [("c", D("25"))],
    )
    return engine


class RecoveryExecutionTests(unittest.TestCase):
    def test_recovery_allocates_in_event_then_creditor_order(self):
        engine = make_engine_with_bad_debt()
        result = engine.process_recovery("R1", "USD", D("25"))

        self.assertEqual(result.recovery_transaction_id, "R1")
        self.assertEqual(result.currency, "USD")
        self.assertEqual(result.recovery_amount, D("25"))
        self.assertEqual(result.total_recovered, D("25"))
        self.assertEqual(result.event_id, "EVT-R1-recovery")
        # 先冲减 T1 的 b（20 清零），再冲减 T2 的 c（部分覆盖 5）。
        self.assertEqual(len(result.allocations), 2)
        first, second = result.allocations
        self.assertEqual(first.source_transaction_id, "T1")
        self.assertEqual(first.creditor, "b")
        self.assertEqual(first.recovered_amount, D("20"))
        self.assertEqual(first.remaining_bad_debt, D("0"))
        self.assertEqual(second.source_transaction_id, "T2")
        self.assertEqual(second.creditor, "c")
        self.assertEqual(second.recovered_amount, D("5"))
        self.assertEqual(second.remaining_bad_debt, D("10"))
        # 回收后该币种存续坏账总额。
        self.assertEqual(result.outstanding_bad_debt, D("10"))

    def test_recovery_partial_coverage_of_single_debt(self):
        engine = make_engine_with_bad_debt()
        result = engine.process_recovery("R1", "USD", D("8"))
        self.assertEqual(len(result.allocations), 1)
        allocation = result.allocations[0]
        self.assertEqual(allocation.creditor, "b")
        self.assertEqual(allocation.recovered_amount, D("8"))
        self.assertEqual(allocation.remaining_bad_debt, D("12"))
        self.assertEqual(result.outstanding_bad_debt, D("27"))

    def test_recovery_amount_normalization(self):
        engine = make_engine_with_bad_debt()
        result = engine.process_recovery("R1", "USD", 0.6)
        self.assertEqual(result.recovery_amount, D("0.6"))
        self.assertEqual(result.total_recovered, D("0.6"))

    def test_zero_recovery_with_outstanding_debt_creates_event(self):
        engine = make_engine_with_bad_debt()
        events_before = len(engine.events())
        result = engine.process_recovery("R0", "USD", D("0"))
        self.assertEqual(result.allocations, ())
        self.assertEqual(result.total_recovered, D("0"))
        self.assertEqual(result.outstanding_bad_debt, D("35"))
        self.assertEqual(len(engine.events()), events_before + 1)
        event = engine.events()[-1]
        self.assertEqual(event.event_id, "EVT-R0-recovery")
        self.assertEqual(event.recovery_allocations, ())

    def test_recovery_appends_audit_event_with_detail(self):
        engine = make_engine_with_bad_debt()
        sequence_before = engine.events()[-1].sequence
        result = engine.process_recovery("R1", "USD", D("25"))

        event = engine.events()[-1]
        self.assertEqual(event.event_id, "EVT-R1-recovery")
        self.assertEqual(event.transaction_id, "R1")
        self.assertEqual(event.sequence, sequence_before + 1)
        self.assertEqual(event.currency, "USD")
        # 事件内明细与结果同额同序。
        self.assertEqual(
            event.recovery_allocations,
            (
                ("T1", "b", D("20"), D("0")),
                ("T2", "c", D("5"), D("10")),
            ),
        )
        self.assertEqual(event.uncovered_bad_debt, result.outstanding_bad_debt)

    def test_settlement_events_keep_empty_recovery_allocations(self):
        engine = make_engine_with_bad_debt()
        engine.process_recovery("R1", "USD", D("25"))
        settlement_events = [
            e for e in engine.events() if e.transaction_id in ("T1", "T2")
        ]
        self.assertEqual(len(settlement_events), 2)
        for event in settlement_events:
            self.assertEqual(event.recovery_allocations, ())

    def test_recovery_does_not_rewrite_historical_results(self):
        engine = make_engine_with_bad_debt()
        before_t1 = engine.result_of("T1")
        before_t2 = engine.result_of("T2")
        engine.process_recovery("R1", "USD", D("25"))
        # 历史 SettlementResult 不回写。
        self.assertIs(engine.result_of("T1"), before_t1)
        self.assertIs(engine.result_of("T2"), before_t2)
        self.assertEqual(engine.result_of("T1").uncovered_bad_debt, D("20"))
        self.assertEqual(engine.result_of("T2").uncovered_bad_debt, D("15"))

    def test_rejected_settlement_leaves_no_recoverable_debt(self):
        engine = ClearingEngine()
        result = engine.process(
            "T1", "USD", D("10"), D("50"), D("0"), D("100"), D("0"),
            [("a", D("50"))],
        )
        self.assertFalse(result.approved)
        self.assertEqual(engine.outstanding_bad_debts("USD"), ())
        with self.assertRaises(NoOutstandingBadDebtError):
            engine.process_recovery("R1", "USD", D("1"))

    def test_recovery_of_roundtrip(self):
        engine = make_engine_with_bad_debt()
        self.assertIsNone(engine.recovery_of("R1"))
        result = engine.process_recovery("R1", "USD", D("25"))
        self.assertIs(engine.recovery_of("R1"), result)
        self.assertIsNone(engine.recovery_of("R-unknown"))

    def test_outstanding_bad_debts_detail(self):
        engine = make_engine_with_bad_debt()
        engine.process_recovery("R1", "USD", D("25"))
        outstanding = engine.outstanding_bad_debts("USD")
        self.assertEqual(len(outstanding), 1)
        item = outstanding[0]
        self.assertEqual(item.source_transaction_id, "T2")
        self.assertEqual(item.creditor, "c")
        self.assertEqual(item.currency, "USD")
        self.assertEqual(item.balance, D("10"))

    def test_queries_do_not_trigger_clearing(self):
        engine = make_engine_with_bad_debt()
        events_before = engine.events()
        engine.outstanding_bad_debts("USD")
        engine.recovery_of("R1")
        self.assertEqual(engine.events(), events_before)

    def test_other_currency_unaffected(self):
        engine = make_engine_with_bad_debt()
        engine.process(
            "T3", "EUR", D("100"), D("5"), D("0"), D("100"), D("0"),
            [("e", D("12"))],
        )
        engine.process_recovery("R1", "USD", D("35"))
        # USD 全部回收；EUR 坏账不受影响。
        self.assertEqual(engine.outstanding_bad_debts("USD"), ())
        eur = engine.outstanding_bad_debts("EUR")
        self.assertEqual(len(eur), 1)
        self.assertEqual(eur[0].balance, D("7"))
        with self.assertRaises(NoOutstandingBadDebtError):
            engine.process_recovery("R2", "USD", D("1"))

    def test_multiple_recoveries_consume_debt_in_order(self):
        engine = make_engine_with_bad_debt()
        engine.process_recovery("R1", "USD", D("20"))
        result = engine.process_recovery("R2", "USD", D("15"))
        # 第二笔从 T2 的 c 继续冲减。
        self.assertEqual(len(result.allocations), 1)
        self.assertEqual(result.allocations[0].creditor, "c")
        self.assertEqual(result.allocations[0].recovered_amount, D("15"))
        self.assertEqual(result.outstanding_bad_debt, D("0"))
        with self.assertRaises(NoOutstandingBadDebtError):
            engine.process_recovery("R3", "USD", D("1"))


class RecoveryValidationTests(unittest.TestCase):
    def test_duplicate_recovery_transaction_id(self):
        engine = make_engine_with_bad_debt()
        engine.process_recovery("R1", "USD", D("5"))
        with self.assertRaises(DuplicateTransactionError):
            engine.process_recovery("R1", "USD", D("5"))

    def test_recovery_id_conflicts_with_settlement_id(self):
        engine = make_engine_with_bad_debt()
        with self.assertRaises(DuplicateTransactionError):
            engine.process_recovery("T1", "USD", D("5"))

    def test_missing_currency(self):
        engine = make_engine_with_bad_debt()
        for bad in ("", "   ", None, 7):
            with self.assertRaises(InvalidCurrencyError):
                engine.process_recovery("R1", bad, D("5"))

    def test_invalid_amounts(self):
        engine = make_engine_with_bad_debt()
        for bad in (D("-1"), -2, float("nan"), float("inf"), "5", None):
            with self.assertRaises(ValueError, msg=repr(bad)):
                engine.process_recovery("R1", "USD", bad)

    def test_no_outstanding_bad_debt(self):
        engine = ClearingEngine()
        with self.assertRaises(NoOutstandingBadDebtError):
            engine.process_recovery("R1", "USD", D("5"))

    def test_zero_recovery_without_bad_debt_rejected(self):
        engine = ClearingEngine()
        with self.assertRaises(NoOutstandingBadDebtError):
            engine.process_recovery("R1", "USD", D("0"))

    def test_amount_exceeds_outstanding(self):
        engine = make_engine_with_bad_debt()
        with self.assertRaises(RecoveryAmountExceedsOutstandingError):
            engine.process_recovery("R1", "USD", D("35.01"))
        # 恰好等于存续坏账总额：允许。
        result = engine.process_recovery("R1", "USD", D("35"))
        self.assertEqual(result.outstanding_bad_debt, D("0"))

    def test_failed_recovery_leaves_no_trace(self):
        engine = make_engine_with_bad_debt()
        events_before = engine.events()
        outstanding_before = engine.outstanding_bad_debts("USD")
        for call in (
            lambda: engine.process_recovery("R1", "USD", D("100")),
            lambda: engine.process_recovery("R2", "EUR", D("1")),
            lambda: engine.process_recovery("R3", "USD", D("-1")),
            lambda: engine.process_recovery("R4", "", D("1")),
        ):
            with self.assertRaises(Exception):
                call()
        # 失败不生成事件、不占流水号、不改状态。
        self.assertEqual(engine.events(), events_before)
        self.assertEqual(engine.outstanding_bad_debts("USD"), outstanding_before)
        for tid in ("R1", "R2", "R3", "R4"):
            self.assertFalse(engine.has_transaction(tid))
            self.assertIsNone(engine.recovery_of(tid))
        # 流水号未被占用：相同标识随后可成功回收。
        engine.process_recovery("R1", "USD", D("5"))
        self.assertTrue(engine.has_transaction("R1"))


if __name__ == "__main__":
    unittest.main()
