"""坏账核销的公开行为测试。

覆盖：跨事件按顺序核销、部分核销、零额核销、核销审计事件、只读查询、
与回收的顺序衔接、历史结果不回写、只减坏账不改其他状态、失败路径不产生
状态变更，以及各类输入异常。
"""

import unittest
from decimal import Decimal

from vault_guard import (
    ClearingEngine,
    DuplicateTransactionError,
    InvalidCurrencyError,
    NoOutstandingBadDebtError,
    WriteoffAllocation,
    WriteoffAmountExceedsOutstandingError,
    WriteoffResult,
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

        self.assertIsInstance(result, WriteoffResult)
        self.assertEqual(result.writeoff_transaction_id, "W1")
        self.assertEqual(result.currency, "USD")
        self.assertEqual(result.writeoff_amount, D("25"))
        self.assertEqual(result.total_written_off, D("25"))
        self.assertEqual(result.event_id, "EVT-W1-writeoff")
        # 先核销 T1 的 b（20 清零），再核销 T2 的 c（部分核销 5）。
        self.assertEqual(len(result.allocations), 2)
        first, second = result.allocations
        self.assertIsInstance(first, WriteoffAllocation)
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

    def test_other_events_keep_empty_writeoff_allocations(self):
        engine = make_engine_with_bad_debt()
        engine.process_recovery("R1", "USD", D("5"))
        engine.process_writeoff("W1", "USD", D("5"))
        # 结算与回收事件的 writeoff_allocations 恒为空。
        for transaction_id in ("T1", "T2"):
            self.assertEqual(
                engine.get_event(transaction_id).writeoff_allocations, ()
            )
        recovery_event = engine.get_event("R1")
        self.assertEqual(recovery_event.writeoff_allocations, ())
        # 核销事件的 recovery_allocations 恒为空。
        writeoff_event = engine.get_event("W1")
        self.assertEqual(writeoff_event.recovery_allocations, ())
        # 回收事件自身的 recovery_allocations 不变。
        self.assertEqual(
            recovery_event.recovery_allocations,
            (("T1", "b", D("5"), D("15")),),
        )

    def test_writeoff_after_recovery_continues_from_remaining(self):
        engine = make_engine_with_bad_debt()
        engine.process_recovery("R1", "USD", D("25"))
        # 回收后仅剩 T2 的 c 10；核销 7 为部分核销。
        result = engine.process_writeoff("W1", "USD", D("7"))
        self.assertEqual(len(result.allocations), 1)
        allocation = result.allocations[0]
        self.assertEqual(allocation.source_transaction_id, "T2")
        self.assertEqual(allocation.creditor, "c")
        self.assertEqual(allocation.written_off_amount, D("7"))
        self.assertEqual(allocation.remaining_bad_debt, D("3"))
        self.assertEqual(result.outstanding_bad_debt, D("3"))

    def test_writeoff_does_not_rewrite_historical_results(self):
        engine = make_engine_with_bad_debt()
        before_t1 = engine.result_of("T1")
        before_t2 = engine.result_of("T2")
        engine.process_writeoff("W1", "USD", D("25"))
        # 历史 SettlementResult 不回写。
        self.assertIs(engine.result_of("T1"), before_t1)
        self.assertIs(engine.result_of("T2"), before_t2)
        self.assertEqual(engine.result_of("T1").uncovered_bad_debt, D("20"))
        self.assertEqual(engine.result_of("T2").uncovered_bad_debt, D("15"))

    def test_writeoff_only_reduces_bad_debt(self):
        engine = make_engine_with_bad_debt()
        engine.process(
            "T3", "EUR", D("100"), D("5"), D("0"), D("100"), D("0"),
            [("e", D("12"))],
        )
        balance_before = engine.result_of("T2").validated_available_balance
        events_before = engine.events()
        engine.process_writeoff("W1", "USD", D("35"))
        # 资金池余额等历史状态不因核销改变；事件只新增一条核销事件。
        self.assertEqual(
            engine.result_of("T2").validated_available_balance, balance_before
        )
        self.assertEqual(engine.events()[:-1], events_before)
        self.assertEqual(engine.outstanding_bad_debts("USD"), ())
        # 其他币种坏账不受影响。
        eur = engine.outstanding_bad_debts("EUR")
        self.assertEqual(len(eur), 1)
        self.assertEqual(eur[0].balance, D("7"))

    def test_writeoff_of_roundtrip(self):
        engine = make_engine_with_bad_debt()
        self.assertIsNone(engine.writeoff_of("W1"))
        result = engine.process_writeoff("W1", "USD", D("25"))
        self.assertIs(engine.writeoff_of("W1"), result)
        self.assertIsNone(engine.writeoff_of("W-unknown"))

    def test_outstanding_bad_debts_detail(self):
        engine = make_engine_with_bad_debt()
        engine.process_writeoff("W1", "USD", D("25"))
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
        engine.writeoff_of("W1")
        self.assertEqual(engine.events(), events_before)

    def test_multiple_writeoffs_consume_debt_in_order(self):
        engine = make_engine_with_bad_debt()
        engine.process_writeoff("W1", "USD", D("20"))
        result = engine.process_writeoff("W2", "USD", D("15"))
        # 第二笔从 T2 的 c 继续核销。
        self.assertEqual(len(result.allocations), 1)
        self.assertEqual(result.allocations[0].creditor, "c")
        self.assertEqual(result.allocations[0].written_off_amount, D("15"))
        self.assertEqual(result.outstanding_bad_debt, D("0"))
        with self.assertRaises(NoOutstandingBadDebtError):
            engine.process_writeoff("W3", "USD", D("1"))

    def test_result_is_immutable(self):
        engine = make_engine_with_bad_debt()
        result = engine.process_writeoff("W1", "USD", D("5"))
        with self.assertRaises(Exception):
            result.writeoff_amount = D("99")


class WriteoffValidationTests(unittest.TestCase):
    def test_missing_or_blank_transaction_id(self):
        engine = make_engine_with_bad_debt()
        for bad in (None, "", "   ", 7):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    engine.process_writeoff(bad, "USD", D("5"))

    def test_duplicate_writeoff_transaction_id(self):
        engine = make_engine_with_bad_debt()
        engine.process_writeoff("W1", "USD", D("5"))
        with self.assertRaises(DuplicateTransactionError):
            engine.process_writeoff("W1", "USD", D("5"))

    def test_writeoff_id_conflicts_with_settlement_or_recovery_id(self):
        engine = make_engine_with_bad_debt()
        engine.process_recovery("R1", "USD", D("5"))
        with self.assertRaises(DuplicateTransactionError):
            engine.process_writeoff("T1", "USD", D("5"))
        with self.assertRaises(DuplicateTransactionError):
            engine.process_writeoff("R1", "USD", D("5"))

    def test_missing_currency(self):
        engine = make_engine_with_bad_debt()
        for bad in ("", "   ", None, 7):
            with self.assertRaises(InvalidCurrencyError):
                engine.process_writeoff("W1", bad, D("5"))

    def test_currency_matching_strips_whitespace(self):
        engine = make_engine_with_bad_debt()
        result = engine.process_writeoff("W1", "  USD ", D("5"))
        self.assertEqual(result.currency, "USD")

    def test_invalid_amounts(self):
        engine = make_engine_with_bad_debt()
        for bad in (D("-1"), -2, float("nan"), float("inf"), "5", None):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    engine.process_writeoff("W1", "USD", bad)

    def test_no_outstanding_bad_debt(self):
        engine = ClearingEngine()
        with self.assertRaises(NoOutstandingBadDebtError):
            engine.process_writeoff("W1", "USD", D("5"))

    def test_zero_writeoff_without_bad_debt_rejected(self):
        engine = ClearingEngine()
        with self.assertRaises(NoOutstandingBadDebtError):
            engine.process_writeoff("W1", "USD", D("0"))

    def test_amount_exceeds_outstanding(self):
        engine = make_engine_with_bad_debt()
        with self.assertRaises(WriteoffAmountExceedsOutstandingError):
            engine.process_writeoff("W1", "USD", D("35.01"))
        # 恰好等于存续坏账总额：允许。
        result = engine.process_writeoff("W1", "USD", D("35"))
        self.assertEqual(result.outstanding_bad_debt, D("0"))

    def test_failed_writeoff_leaves_no_trace(self):
        engine = make_engine_with_bad_debt()
        events_before = engine.events()
        outstanding_before = engine.outstanding_bad_debts("USD")
        sequence_before = engine.events()[-1].sequence
        for call in (
            lambda: engine.process_writeoff("W1", "USD", D("100")),
            lambda: engine.process_writeoff("W2", "EUR", D("1")),
            lambda: engine.process_writeoff("W3", "USD", D("-1")),
            lambda: engine.process_writeoff("W4", "", D("1")),
            lambda: engine.process_writeoff("", "USD", D("1")),
            lambda: engine.process_writeoff("T1", "USD", D("1")),
        ):
            with self.assertRaises(Exception):
                call()
        # 失败不生成事件、不占流水号、不改状态。
        self.assertEqual(engine.events(), events_before)
        self.assertEqual(engine.outstanding_bad_debts("USD"), outstanding_before)
        self.assertEqual(engine.events()[-1].sequence, sequence_before)
        for tid in ("W1", "W2", "W3", "W4"):
            self.assertFalse(engine.has_transaction(tid))
            self.assertIsNone(engine.writeoff_of(tid))
        # 流水号未被占用：相同标识随后可成功核销。
        engine.process_writeoff("W1", "USD", D("5"))
        self.assertTrue(engine.has_transaction("W1"))


if __name__ == "__main__":
    unittest.main()
