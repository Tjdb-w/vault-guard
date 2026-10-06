"""存续坏账核销的公开行为测试。

覆盖：跨事件按顺序核销、部分核销、零额核销、核销审计事件、只读查询、
历史结果不回写、失败路径不产生状态变更、各类输入异常，以及核销在
审计核对快照中的口径。
"""

import unittest
from decimal import Decimal

from vault_guard import (
    ClearingEngine,
    DuplicateTransactionError,
    InvalidCurrencyError,
    NoOutstandingBadDebtError,
    WriteoffAmountExceedsOutstandingError,
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


class WriteoffExecutionTests(unittest.TestCase):
    def test_writeoff_allocates_in_event_then_creditor_order(self):
        engine = make_engine_with_bad_debt()
        result = engine.process_writeoff("W1", "USD", D("25"))

        self.assertEqual(result.writeoff_transaction_id, "W1")
        self.assertEqual(result.currency, "USD")
        self.assertEqual(result.writeoff_amount, D("25"))
        self.assertEqual(result.total_written_off, D("25"))
        self.assertEqual(result.event_id, "EVT-W1-writeoff")
        # 先核销 T1 的 b（20 清零），再核销 T2 的 c（部分核销 5）。
        self.assertEqual(len(result.allocations), 2)
        first, second = result.allocations
        self.assertEqual(first.source_transaction_id, "T1")
        self.assertEqual(first.creditor, "b")
        self.assertEqual(first.written_off_amount, D("20"))
        self.assertEqual(first.remaining_bad_debt, D("0"))
        self.assertEqual(second.source_transaction_id, "T2")
        self.assertEqual(second.creditor, "c")
        self.assertEqual(second.written_off_amount, D("5"))
        self.assertEqual(second.remaining_bad_debt, D("10"))
        # 核销后该币种存续坏账总额。
        self.assertEqual(result.outstanding_bad_debt, D("10"))

    def test_writeoff_partial_coverage_of_single_debt(self):
        engine = make_engine_with_bad_debt()
        result = engine.process_writeoff("W1", "USD", D("8"))
        self.assertEqual(len(result.allocations), 1)
        allocation = result.allocations[0]
        self.assertEqual(allocation.creditor, "b")
        self.assertEqual(allocation.written_off_amount, D("8"))
        self.assertEqual(allocation.remaining_bad_debt, D("12"))
        self.assertEqual(result.outstanding_bad_debt, D("27"))

    def test_writeoff_amount_normalization(self):
        engine = make_engine_with_bad_debt()
        result = engine.process_writeoff("W1", "USD", 0.6)
        self.assertEqual(result.writeoff_amount, D("0.6"))
        self.assertEqual(result.total_written_off, D("0.6"))

    def test_writeoff_full_amount_clears_outstanding(self):
        engine = make_engine_with_bad_debt()
        result = engine.process_writeoff("W1", "USD", D("35"))
        self.assertEqual(result.outstanding_bad_debt, D("0"))
        self.assertEqual(engine.outstanding_bad_debts("USD"), ())

    def test_writeoff_only_touches_matching_currency(self):
        engine = make_engine_with_bad_debt()
        engine.process(
            "T3", "EUR", D("100"), D("0"), D("0"), D("100"), D("0"),
            [("x", D("7"))],
        )
        engine.process_writeoff("W1", "USD", D("35"))
        outstanding_eur = engine.outstanding_bad_debts("EUR")
        self.assertEqual(len(outstanding_eur), 1)
        self.assertEqual(outstanding_eur[0].balance, D("7"))

    def test_zero_writeoff_with_outstanding_debt_creates_event(self):
        engine = make_engine_with_bad_debt()
        events_before = len(engine.events())
        result = engine.process_writeoff("W0", "USD", D("0"))
        self.assertEqual(result.allocations, ())
        self.assertEqual(result.total_written_off, D("0"))
        self.assertEqual(result.outstanding_bad_debt, D("35"))
        self.assertEqual(len(engine.events()), events_before + 1)
        event = engine.events()[-1]
        self.assertEqual(event.event_id, "EVT-W0-writeoff")
        self.assertEqual(event.writeoff_allocations, ())

    def test_writeoff_appends_audit_event_with_detail(self):
        engine = make_engine_with_bad_debt()
        sequence_before = engine.events()[-1].sequence
        result = engine.process_writeoff("W1", "USD", D("25"))

        event = engine.events()[-1]
        self.assertEqual(event.event_id, "EVT-W1-writeoff")
        self.assertEqual(event.transaction_id, "W1")
        self.assertEqual(event.sequence, sequence_before + 1)
        self.assertEqual(event.currency, "USD")
        self.assertTrue(event.approved)
        self.assertEqual(event.validation_result, "WRITEOFF")
        self.assertEqual(event.risk_occupancy, D("0"))
        # 事件内明细与结果同额同序。
        self.assertEqual(
            event.writeoff_allocations,
            (
                ("T1", "b", D("20"), D("0")),
                ("T2", "c", D("5"), D("10")),
            ),
        )
        self.assertEqual(event.uncovered_bad_debt, result.outstanding_bad_debt)

    def test_settlement_and_recovery_events_keep_empty_writeoff_allocations(self):
        engine = make_engine_with_bad_debt()
        engine.process_recovery("R1", "USD", D("5"))
        engine.process_writeoff("W1", "USD", D("5"))
        for event in engine.events():
            if event.validation_result == "WRITEOFF":
                self.assertEqual(event.recovery_allocations, ())
            else:
                self.assertEqual(event.writeoff_allocations, ())

    def test_writeoff_does_not_rewrite_historical_results(self):
        engine = make_engine_with_bad_debt()
        before = engine.result_of("T1")
        engine.process_writeoff("W1", "USD", D("20"))
        self.assertIs(engine.result_of("T1"), before)
        self.assertEqual(before.uncovered_bad_debt, D("20"))

    def test_writeoff_result_is_immutable(self):
        engine = make_engine_with_bad_debt()
        result = engine.process_writeoff("W1", "USD", D("5"))
        with self.assertRaises(Exception):
            result.currency = "EUR"


class WriteoffQueryTests(unittest.TestCase):
    def test_writeoff_of_roundtrip(self):
        engine = make_engine_with_bad_debt()
        self.assertIsNone(engine.writeoff_of("W1"))
        result = engine.process_writeoff("W1", "USD", D("5"))
        self.assertIs(engine.writeoff_of("W1"), result)
        self.assertIsNone(engine.writeoff_of("W-unknown"))

    def test_writeoff_of_is_read_only(self):
        engine = make_engine_with_bad_debt()
        engine.process_writeoff("W1", "USD", D("5"))
        events_before = engine.events()
        engine.writeoff_of("W1")
        engine.writeoff_of("W-unknown")
        self.assertEqual(engine.events(), events_before)


class WriteoffValidationTests(unittest.TestCase):
    def assert_no_state_change(self, engine, events_before, tid):
        self.assertEqual(engine.events(), events_before)
        self.assertFalse(engine.has_transaction(tid))
        self.assertIsNone(engine.writeoff_of(tid))

    def test_missing_or_blank_transaction_id(self):
        engine = make_engine_with_bad_debt()
        events_before = engine.events()
        for bad_id in (None, "", "   ", 123):
            with self.assertRaises(ValueError):
                engine.process_writeoff(bad_id, "USD", D("1"))
        self.assertEqual(engine.events(), events_before)

    def test_duplicate_transaction_id(self):
        engine = make_engine_with_bad_debt()
        engine.process_writeoff("W1", "USD", D("5"))
        events_before = engine.events()
        with self.assertRaises(DuplicateTransactionError):
            engine.process_writeoff("W1", "USD", D("1"))
        # 与结算 / 回收流水号同样冲突。
        with self.assertRaises(DuplicateTransactionError):
            engine.process_writeoff("T1", "USD", D("1"))
        self.assertEqual(engine.events(), events_before)

    def test_invalid_currency(self):
        engine = make_engine_with_bad_debt()
        events_before = engine.events()
        for bad_currency in (None, "", "   ", 123):
            with self.assertRaises(InvalidCurrencyError):
                engine.process_writeoff("W1", bad_currency, D("1"))
        self.assert_no_state_change(engine, events_before, "W1")

    def test_invalid_amount(self):
        engine = make_engine_with_bad_debt()
        events_before = engine.events()
        for bad_amount in (D("-1"), -1, float("nan"), float("inf"), "x", None):
            with self.assertRaises(ValueError):
                engine.process_writeoff("W1", "USD", bad_amount)
        self.assert_no_state_change(engine, events_before, "W1")

    def test_no_outstanding_bad_debt(self):
        engine = ClearingEngine()
        events_before = engine.events()
        with self.assertRaises(NoOutstandingBadDebtError):
            engine.process_writeoff("W1", "USD", D("1"))
        # 无存续坏账时零额核销同样拒绝。
        with self.assertRaises(NoOutstandingBadDebtError):
            engine.process_writeoff("W2", "USD", D("0"))
        self.assertEqual(engine.events(), events_before)
        self.assertFalse(engine.has_transaction("W1"))
        self.assertFalse(engine.has_transaction("W2"))

    def test_amount_exceeds_outstanding(self):
        engine = make_engine_with_bad_debt()
        events_before = engine.events()
        with self.assertRaises(WriteoffAmountExceedsOutstandingError):
            engine.process_writeoff("W1", "USD", D("36"))
        self.assert_no_state_change(engine, events_before, "W1")
        # 修正后可用同一流水号重新提交。
        result = engine.process_writeoff("W1", "USD", D("35"))
        self.assertEqual(result.outstanding_bad_debt, D("0"))


class WriteoffReconciliationTests(unittest.TestCase):
    def test_reconciliation_counts_and_identity(self):
        engine = make_engine_with_bad_debt()
        engine.process_recovery("R1", "USD", D("5"))
        engine.process_writeoff("W1", "USD", D("10"))
        engine.process_writeoff("W2", "USD", D("0"))

        summary = engine.audit_reconciliation("USD")
        self.assertEqual(summary.settlement_count, 2)
        self.assertEqual(summary.recovery_count, 1)
        self.assertEqual(summary.writeoff_count, 2)
        self.assertEqual(summary.initial_bad_debt, D("35"))
        self.assertEqual(summary.recovered_amount, D("5"))
        self.assertEqual(summary.written_off_amount, D("10"))
        self.assertEqual(summary.outstanding_bad_debt, D("20"))
        self.assertEqual(
            summary.initial_bad_debt
            - summary.recovered_amount
            - summary.written_off_amount,
            summary.outstanding_bad_debt,
        )
        self.assertEqual(
            summary.event_ids,
            (
                "EVT-T1-approved",
                "EVT-T2-approved",
                "EVT-R1-recovery",
                "EVT-W1-writeoff",
                "EVT-W2-writeoff",
            ),
        )

    def test_reconciliation_without_writeoff_is_unchanged(self):
        engine = make_engine_with_bad_debt()
        engine.process_recovery("R1", "USD", D("5"))
        summary = engine.audit_reconciliation("USD")
        self.assertEqual(summary.writeoff_count, 0)
        self.assertEqual(summary.written_off_amount, D("0"))
        self.assertEqual(summary.recovered_amount, D("5"))
        self.assertEqual(summary.outstanding_bad_debt, D("30"))

    def test_reconciliation_unknown_currency_is_zero(self):
        engine = make_engine_with_bad_debt()
        engine.process_writeoff("W1", "USD", D("5"))
        summary = engine.audit_reconciliation("CHF")
        self.assertEqual(summary.writeoff_count, 0)
        self.assertEqual(summary.written_off_amount, D("0"))
        self.assertEqual(summary.event_ids, ())


if __name__ == "__main__":
    unittest.main()
