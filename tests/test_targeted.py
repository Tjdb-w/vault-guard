"""定向坏账回收与核销的公开行为测试。

覆盖：唯一定向明细命中、部分覆盖、债权人名称去首尾空白、来源精确匹配、
零额处理空明细事件、审计事件与序号、只读查询口径（recovery_of /
writeoff_of / outstanding_bad_debts / audit_reconciliation /
creditor_bad_debt_report / bad_debt_trail）、互不影响同来源其他债权人与
其他来源同名债权、校验顺序与各类输入异常、失败不产生状态变更。
"""

import unittest
from decimal import Decimal

from vault_guard import (
    ClearingEngine,
    DuplicateTransactionError,
    InvalidCurrencyError,
    NoOutstandingBadDebtError,
    RecoveryAmountExceedsOutstandingError,
    WriteoffAmountExceedsOutstandingError,
)

D = Decimal


def make_engine() -> ClearingEngine:
    """构造两笔已放行结算，留下跨来源、含同名债权的存续坏账。

    T1（USD）：债权 a 30 / b 30，池内 40 -> a 受偿 30，b 受偿 10，b 坏账 20。
    T2（USD）：债权 b 25 / c 10，池内 10 -> b 受偿 10，b 坏账 15，c 坏账 10。
    """
    engine = ClearingEngine()
    engine.process(
        "T1", "USD", D("100"), D("40"), D("0"), D("100"), D("0"),
        [("a", D("30")), ("b", D("30"))],
    )
    engine.process(
        "T2", "USD", D("100"), D("10"), D("0"), D("100"), D("0"),
        [("b", D("25")), ("c", D("10"))],
    )
    return engine


class TargetedRecoveryTests(unittest.TestCase):
    def test_hits_unique_entry_and_partially_covers(self):
        engine = make_engine()
        result = engine.process_targeted_recovery(
            "R1", "USD", "T2", "b", D("6")
        )
        self.assertEqual(result.recovery_transaction_id, "R1")
        self.assertEqual(result.currency, "USD")
        self.assertEqual(result.recovery_amount, D("6"))
        self.assertEqual(result.total_recovered, D("6"))
        self.assertEqual(result.event_id, "EVT-R1-recovery")
        # allocations 只含一条命中明细。
        self.assertEqual(len(result.allocations), 1)
        allocation = result.allocations[0]
        self.assertEqual(allocation.source_transaction_id, "T2")
        self.assertEqual(allocation.creditor, "b")
        self.assertEqual(allocation.recovered_amount, D("6"))
        self.assertEqual(allocation.remaining_bad_debt, D("9"))
        # 该币种处理后的存续余额：20 + 15 + 10 - 6 = 39。
        self.assertEqual(result.outstanding_bad_debt, D("39"))

    def test_does_not_touch_same_source_other_creditor_or_same_name_other_source(self):
        engine = make_engine()
        engine.process_targeted_recovery("R1", "USD", "T2", "b", D("15"))
        outstanding = {
            (item.source_transaction_id, item.creditor): item.balance
            for item in engine.outstanding_bad_debts("USD")
        }
        # T2 的 b 清零；T1 的 b 与 T2 的 c 不受影响。
        self.assertEqual(
            outstanding,
            {("T1", "b"): D("20"), ("T2", "c"): D("10")},
        )

    def test_creditor_name_is_stripped_source_is_exact(self):
        engine = make_engine()
        result = engine.process_targeted_recovery(
            "R1", "USD", "T1", "  b  ", D("5")
        )
        self.assertEqual(result.allocations[0].creditor, "b")
        self.assertEqual(result.allocations[0].remaining_bad_debt, D("15"))
        # 来源流水号精确匹配：带空白的来源不命中。
        with self.assertRaises(NoOutstandingBadDebtError):
            engine.process_targeted_recovery("R2", "USD", " T1 ", "b", D("1"))

    def test_zero_amount_with_outstanding_target_creates_empty_event(self):
        engine = make_engine()
        events_before = len(engine.events())
        result = engine.process_targeted_recovery(
            "R0", "USD", "T1", "b", D("0")
        )
        self.assertEqual(result.allocations, ())
        self.assertEqual(result.total_recovered, D("0"))
        self.assertEqual(result.outstanding_bad_debt, D("45"))
        self.assertEqual(len(engine.events()), events_before + 1)
        event = engine.events()[-1]
        self.assertEqual(event.event_id, "EVT-R0-recovery")
        self.assertEqual(event.validation_result, "RECOVERY")
        self.assertEqual(event.recovery_allocations, ())

    def test_audit_event_appended_with_sequence(self):
        engine = make_engine()
        sequence_before = engine.events()[-1].sequence
        result = engine.process_targeted_recovery(
            "R1", "USD", "T2", "b", D("6")
        )
        event = engine.events()[-1]
        self.assertEqual(event.event_id, "EVT-R1-recovery")
        self.assertEqual(event.transaction_id, "R1")
        self.assertEqual(event.sequence, sequence_before + 1)
        self.assertEqual(event.currency, "USD")
        self.assertEqual(
            event.recovery_allocations, (("T2", "b", D("6"), D("9")),)
        )
        self.assertEqual(event.uncovered_bad_debt, result.outstanding_bad_debt)

    def test_queries_reflect_targeted_recovery(self):
        engine = make_engine()
        engine.process_targeted_recovery("R1", "USD", "T2", "b", D("6"))

        self.assertEqual(
            engine.recovery_of("R1").total_recovered, D("6")
        )
        summary = engine.audit_reconciliation("USD")
        self.assertEqual(summary.recovery_count, 1)
        self.assertEqual(summary.recovered_amount, D("6"))
        self.assertEqual(summary.outstanding_bad_debt, D("39"))
        self.assertIn("EVT-R1-recovery", summary.event_ids)

        report = {
            row.creditor: row for row in engine.creditor_bad_debt_report("USD")
        }
        self.assertEqual(report["b"].recovered_amount, D("6"))
        self.assertEqual(report["b"].recovery_transaction_ids, ("R1",))
        self.assertEqual(report["b"].outstanding_bad_debt, D("29"))
        self.assertEqual(
            report["b"].initial_bad_debt
            - report["b"].recovered_amount
            - report["b"].written_off_amount,
            report["b"].outstanding_bad_debt,
        )

        trail = engine.bad_debt_trail("T2")
        self.assertEqual(trail.recovered_amount, D("6"))
        self.assertEqual(len(trail.recoveries), 1)
        self.assertEqual(trail.recoveries[0].operation_id, "R1")
        self.assertEqual(trail.recoveries[0].creditor, "b")
        self.assertEqual(trail.recoveries[0].remaining, D("9"))
        self.assertEqual(trail.outstanding_bad_debt, D("19"))
        # T1 的轨迹不受 T2 定向回收影响。
        self.assertEqual(engine.bad_debt_trail("T1").recovered_amount, D("0"))


class TargetedWriteoffTests(unittest.TestCase):
    def test_hits_unique_entry_and_partially_covers(self):
        engine = make_engine()
        result = engine.process_targeted_writeoff(
            "W1", "USD", "T1", "b", D("8")
        )
        self.assertEqual(result.writeoff_transaction_id, "W1")
        self.assertEqual(result.writeoff_amount, D("8"))
        self.assertEqual(result.total_written_off, D("8"))
        self.assertEqual(result.event_id, "EVT-W1-writeoff")
        self.assertEqual(len(result.allocations), 1)
        allocation = result.allocations[0]
        self.assertEqual(allocation.source_transaction_id, "T1")
        self.assertEqual(allocation.creditor, "b")
        self.assertEqual(allocation.written_off_amount, D("8"))
        self.assertEqual(allocation.remaining_bad_debt, D("12"))
        self.assertEqual(result.outstanding_bad_debt, D("37"))

    def test_zero_amount_with_outstanding_target_creates_empty_event(self):
        engine = make_engine()
        result = engine.process_targeted_writeoff(
            "W0", "USD", "T2", "c", D("0")
        )
        self.assertEqual(result.allocations, ())
        self.assertEqual(result.total_written_off, D("0"))
        self.assertEqual(result.outstanding_bad_debt, D("45"))
        event = engine.events()[-1]
        self.assertEqual(event.event_id, "EVT-W0-writeoff")
        self.assertEqual(event.validation_result, "WRITEOFF")
        self.assertEqual(event.writeoff_allocations, ())

    def test_queries_reflect_targeted_writeoff(self):
        engine = make_engine()
        engine.process_targeted_writeoff("W1", "USD", "T1", "b", D("8"))

        self.assertEqual(engine.writeoff_of("W1").total_written_off, D("8"))
        summary = engine.audit_reconciliation("USD")
        self.assertEqual(summary.writeoff_count, 1)
        self.assertEqual(summary.written_off_amount, D("8"))
        self.assertEqual(summary.outstanding_bad_debt, D("37"))

        report = {
            row.creditor: row for row in engine.creditor_bad_debt_report("USD")
        }
        self.assertEqual(report["b"].written_off_amount, D("8"))
        self.assertEqual(report["b"].writeoff_transaction_ids, ("W1",))
        self.assertEqual(report["b"].outstanding_bad_debt, D("27"))

        trail = engine.bad_debt_trail("T1")
        self.assertEqual(trail.written_off_amount, D("8"))
        self.assertEqual(trail.writeoffs[0].operation_id, "W1")
        self.assertEqual(trail.writeoffs[0].remaining, D("12"))
        self.assertEqual(trail.outstanding_bad_debt, D("12"))

    def test_targeted_recovery_and_writeoff_compose_with_plain_ones(self):
        engine = make_engine()
        engine.process_targeted_recovery("R1", "USD", "T1", "b", D("20"))
        engine.process_targeted_writeoff("W1", "USD", "T2", "b", D("15"))
        # 定向结清后，普通回收只冲减剩余的 T2 的 c。
        result = engine.process_recovery("R2", "USD", D("4"))
        self.assertEqual(len(result.allocations), 1)
        self.assertEqual(result.allocations[0].creditor, "c")
        self.assertEqual(result.outstanding_bad_debt, D("6"))


class TargetedValidationTests(unittest.TestCase):
    def test_invalid_operation_id_and_duplicate(self):
        engine = make_engine()
        events_before = len(engine.events())
        sequence_before = engine.events()[-1].sequence
        for bad in (None, 123, "", "   "):
            with self.assertRaises(ValueError):
                engine.process_targeted_recovery(bad, "USD", "T1", "b", D("1"))
            with self.assertRaises(ValueError):
                engine.process_targeted_writeoff(bad, "USD", "T1", "b", D("1"))
        # 与已登记流水号（含结算流水号）重复。
        with self.assertRaises(DuplicateTransactionError):
            engine.process_targeted_recovery("T1", "USD", "T1", "b", D("1"))
        with self.assertRaises(DuplicateTransactionError):
            engine.process_targeted_writeoff("T1", "USD", "T1", "b", D("1"))
        engine.process_targeted_recovery("R1", "USD", "T1", "b", D("1"))
        with self.assertRaises(DuplicateTransactionError):
            engine.process_targeted_writeoff("R1", "USD", "T1", "b", D("1"))
        self.assertFalse(engine.has_transaction("X1"))
        # 仅成功的 R1（回收 1）产生状态变更；失败路径不留痕迹。
        self.assertEqual(len(engine.events()), events_before + 1)
        self.assertEqual(engine.events()[-1].sequence, sequence_before + 1)
        outstanding = tuple(
            (item.source_transaction_id, item.creditor, item.balance)
            for item in engine.outstanding_bad_debts("USD")
        )
        self.assertEqual(
            outstanding,
            (("T1", "b", D("19")), ("T2", "b", D("15")), ("T2", "c", D("10"))),
        )

    def test_invalid_currency(self):
        engine = make_engine()
        for bad in (None, "", "   ", 7):
            with self.assertRaises(InvalidCurrencyError):
                engine.process_targeted_recovery("R1", bad, "T1", "b", D("1"))
            with self.assertRaises(InvalidCurrencyError):
                engine.process_targeted_writeoff("W1", bad, "T1", "b", D("1"))
        self.assertFalse(engine.has_transaction("R1"))
        self.assertFalse(engine.has_transaction("W1"))

    def test_invalid_source_and_creditor(self):
        engine = make_engine()
        for bad in (None, 5, "", "  "):
            with self.assertRaises(ValueError):
                engine.process_targeted_recovery("R1", "USD", bad, "b", D("1"))
            with self.assertRaises(ValueError):
                engine.process_targeted_recovery("R1", "USD", "T1", bad, D("1"))
            with self.assertRaises(ValueError):
                engine.process_targeted_writeoff("W1", "USD", bad, "b", D("1"))
            with self.assertRaises(ValueError):
                engine.process_targeted_writeoff("W1", "USD", "T1", bad, D("1"))
        self.assertFalse(engine.has_transaction("R1"))
        self.assertFalse(engine.has_transaction("W1"))

    def test_invalid_amount(self):
        engine = make_engine()
        for bad in (D("-1"), -0.5, float("nan"), float("inf"), "1", None):
            with self.assertRaises(ValueError):
                engine.process_targeted_recovery("R1", "USD", "T1", "b", bad)
            with self.assertRaises(ValueError):
                engine.process_targeted_writeoff("W1", "USD", "T1", "b", bad)
        self.assertFalse(engine.has_transaction("R1"))
        self.assertFalse(engine.has_transaction("W1"))

    def test_no_matching_entry(self):
        engine = make_engine()
        events_before = len(engine.events())
        sequence_before = engine.events()[-1].sequence
        # 来源不存在、债权人不符、币种不符、已结清明细均不命中。
        engine.process_targeted_writeoff("W0", "USD", "T2", "c", D("10"))
        for args in (
            ("R1", "USD", "T9", "b"),
            ("R2", "USD", "T1", "c"),
            ("R3", "EUR", "T1", "b"),
            ("R4", "USD", "T2", "c"),
        ):
            with self.assertRaises(NoOutstandingBadDebtError):
                engine.process_targeted_recovery(*args, D("1"))
        for args in (("W1", "USD", "T2", "c"), ("W2", "USD", "T1", "a")):
            with self.assertRaises(NoOutstandingBadDebtError):
                engine.process_targeted_writeoff(*args, D("1"))
        # 零额处理在目标无余额时同样失败。
        with self.assertRaises(NoOutstandingBadDebtError):
            engine.process_targeted_recovery("R5", "USD", "T2", "c", D("0"))
        for tid in ("R1", "R2", "R3", "R4", "R5", "W1", "W2"):
            self.assertFalse(engine.has_transaction(tid))
        self.assertEqual(len(engine.events()), events_before + 1)
        self.assertEqual(
            engine.events()[-1].sequence, sequence_before + 1
        )

    def test_amount_exceeds_entry_balance(self):
        engine = make_engine()
        with self.assertRaises(RecoveryAmountExceedsOutstandingError):
            # T1 的 b 余额 20；即使币种总额 45 足够，定向超额仍拒绝。
            engine.process_targeted_recovery("R1", "USD", "T1", "b", D("21"))
        with self.assertRaises(WriteoffAmountExceedsOutstandingError):
            engine.process_targeted_writeoff("W1", "USD", "T2", "c", D("11"))
        self.assertFalse(engine.has_transaction("R1"))
        self.assertFalse(engine.has_transaction("W1"))
        # 等于余额上限合法。
        result = engine.process_targeted_recovery(
            "R2", "USD", "T1", "b", D("20")
        )
        self.assertEqual(result.allocations[0].remaining_bad_debt, D("0"))

    def test_validation_order(self):
        engine = make_engine()
        # 操作流水号错误优先于币种错误。
        with self.assertRaises(ValueError):
            engine.process_targeted_recovery("", None, "T1", "b", D("1"))
        # 重复流水号优先于币种错误。
        with self.assertRaises(DuplicateTransactionError):
            engine.process_targeted_recovery("T1", None, "T1", "b", D("1"))
        # 币种错误优先于来源 / 债权人错误。
        with self.assertRaises(InvalidCurrencyError):
            engine.process_targeted_recovery("R1", "", None, None, D("1"))
        # 来源 / 债权人错误优先于金额错误。
        with self.assertRaises(ValueError):
            engine.process_targeted_recovery(
                "R1", "USD", "", "", D("-1")
            )
        # 金额错误优先于目标明细查找。
        with self.assertRaises(ValueError):
            engine.process_targeted_recovery(
                "R1", "USD", "T9", "x", D("-1")
            )
        # 目标缺失优先于余额上限（超额判断以命中明细为前提）。
        with self.assertRaises(NoOutstandingBadDebtError):
            engine.process_targeted_recovery(
                "R1", "USD", "T9", "x", D("100")
            )

    def test_amount_normalization_uses_decimal(self):
        engine = make_engine()
        result = engine.process_targeted_recovery(
            "R1", "USD", "T1", "b", 0.6
        )
        self.assertEqual(result.recovery_amount, D("0.6"))
        self.assertEqual(result.total_recovered, D("0.6"))
        self.assertEqual(result.allocations[0].remaining_bad_debt, D("19.4"))


if __name__ == "__main__":
    unittest.main()
