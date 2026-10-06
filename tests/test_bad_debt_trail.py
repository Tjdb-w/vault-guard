"""按来源结算流水号串联坏账轨迹 ``bad_debt_trail`` 的公开行为测试。

覆盖：首次坏账取原结算 uncovered_bad_debt、回收 / 核销按命中明细求和、
按审计顺序与事件内顺序保留操作、同一债权多次部分处理不合并不覆盖、
remaining 为该债权处理后余额、存续额与 outstanding_bad_debts 同来源一致、
拒绝 / 无坏账放行结算的零额空明细轨迹、回收 / 核销流水号不替代查询、
精确匹配不去空白、未命中返回 None、参数校验、空台账、跨币种隔离、
只读幂等与不可变结构。
"""

import unittest
from decimal import Decimal

from vault_guard import (
    BadDebtOperation,
    BadDebtTrail,
    ClearingEngine,
)

D = Decimal


def make_engine_with_trails() -> ClearingEngine:
    """构造跨来源、多次部分回收 / 核销交织的坏账台账。

    T1 USD 放行：a30 / b30，池内 50 -> a 无坏账，b 坏账 10。
    T2 USD 放行：b25 / c15，池内 10 -> b 坏账 15，c 坏账 15。
    ZR0 / ZW0：零额回收与零额核销（空明细事件，不进轨迹）。
    R1 回收 20：T1 的 b 清零 10，T2 的 b 冲减 10（余 5）。
    W1 核销 3：T2 的 b 核销 3（余 2）。
    R2 回收 17：T2 的 b 清零 2，T2 的 c 清零 15。
    T3 USD 放行：x50 / y50，池内 0 -> x、y 各坏账 50。
    R3 回收 60：T3 的 x 清零 50，y 冲减 10（余 40）。
    """
    engine = ClearingEngine()
    engine.process(
        "T1", "USD", D("100"), D("50"), D("0"), D("100"), D("0"),
        [("a", D("30")), ("b", D("30"))],
    )
    engine.process(
        "T2", "USD", D("100"), D("10"), D("0"), D("100"), D("0"),
        [("b", D("25")), ("c", D("15"))],
    )
    engine.process_recovery("ZR0", "USD", D("0"))
    engine.process_writeoff("ZW0", "USD", D("0"))
    engine.process_recovery("R1", "USD", D("20"))
    engine.process_writeoff("W1", "USD", D("3"))
    engine.process_recovery("R2", "USD", D("17"))
    engine.process(
        "T3", "USD", D("0"), D("0"), D("0"), D("100"), D("0"),
        [("x", D("50")), ("y", D("50"))],
    )
    engine.process_recovery("R3", "USD", D("60"))
    return engine


class BadDebtTrailTests(unittest.TestCase):
    def test_trail_for_fully_recovered_single_creditor_source(self):
        engine = make_engine_with_trails()
        trail = engine.bad_debt_trail("T1")
        self.assertEqual(
            trail,
            BadDebtTrail(
                transaction_id="T1",
                initial_bad_debt=D("10"),
                recovered_amount=D("10"),
                written_off_amount=D("0"),
                outstanding_bad_debt=D("0"),
                recoveries=(
                    BadDebtOperation(
                        operation_id="R1",
                        creditor="b",
                        amount=D("10"),
                        remaining=D("0"),
                    ),
                ),
                writeoffs=(),
            ),
        )

    def test_trail_interleaved_partial_recoveries_and_writeoff(self):
        engine = make_engine_with_trails()
        trail = engine.bad_debt_trail("T2")

        # 同一债权 b 的三次部分处理（R1 / W1 / R2）不合并、不覆盖；
        # 事件内按明细顺序，R2 中 b 先于 c。
        self.assertEqual(
            trail,
            BadDebtTrail(
                transaction_id="T2",
                initial_bad_debt=D("30"),
                recovered_amount=D("27"),
                written_off_amount=D("3"),
                outstanding_bad_debt=D("0"),
                recoveries=(
                    BadDebtOperation("R1", "b", D("10"), D("5")),
                    BadDebtOperation("R2", "b", D("2"), D("0")),
                    BadDebtOperation("R2", "c", D("15"), D("0")),
                ),
                writeoffs=(
                    BadDebtOperation("W1", "b", D("3"), D("2")),
                ),
            ),
        )

    def test_trail_within_event_order_and_remaining_balance(self):
        engine = make_engine_with_trails()
        trail = engine.bad_debt_trail("T3")
        self.assertEqual(
            trail,
            BadDebtTrail(
                transaction_id="T3",
                initial_bad_debt=D("100"),
                recovered_amount=D("60"),
                written_off_amount=D("0"),
                outstanding_bad_debt=D("40"),
                recoveries=(
                    BadDebtOperation("R3", "x", D("50"), D("0")),
                    BadDebtOperation("R3", "y", D("10"), D("40")),
                ),
                writeoffs=(),
            ),
        )
        # 存续额与 outstanding_bad_debts 的同来源余额合计一致。
        source_outstanding = sum(
            (
                item.balance
                for item in engine.outstanding_bad_debts("USD")
                if item.source_transaction_id == "T3"
            ),
            D("0"),
        )
        self.assertEqual(trail.outstanding_bad_debt, source_outstanding)

    def test_amounts_are_decimal_and_identity_holds(self):
        engine = make_engine_with_trails()
        for tid in ("T1", "T2", "T3"):
            trail = engine.bad_debt_trail(tid)
            for field_name in (
                "initial_bad_debt",
                "recovered_amount",
                "written_off_amount",
                "outstanding_bad_debt",
            ):
                self.assertIsInstance(getattr(trail, field_name), Decimal)
            self.assertEqual(
                trail.initial_bad_debt
                - trail.recovered_amount
                - trail.written_off_amount,
                trail.outstanding_bad_debt,
            )
            for operation in trail.recoveries + trail.writeoffs:
                self.assertIsInstance(operation.amount, Decimal)
                self.assertIsInstance(operation.remaining, Decimal)
            # 金额字段等于各自明细之和。
            self.assertEqual(
                trail.recovered_amount,
                sum((op.amount for op in trail.recoveries), D("0")),
            )
            self.assertEqual(
                trail.written_off_amount,
                sum((op.amount for op in trail.writeoffs), D("0")),
            )

    def test_zero_amount_events_do_not_produce_operations(self):
        engine = make_engine_with_trails()
        for tid in ("T1", "T2", "T3"):
            trail = engine.bad_debt_trail(tid)
            self.assertNotIn(
                "ZR0", [op.operation_id for op in trail.recoveries]
            )
            self.assertNotIn(
                "ZW0", [op.operation_id for op in trail.writeoffs]
            )

    def test_rejected_settlement_returns_zero_empty_trail(self):
        engine = ClearingEngine()
        # 拟清算金额超过余额：拒绝，不确认坏账。
        result = engine.process(
            "X1", "USD", D("10"), D("50"), D("0"), D("100"), D("0"),
            [("z", D("50"))],
        )
        self.assertFalse(result.approved)
        trail = engine.bad_debt_trail("X1")
        self.assertEqual(
            trail,
            BadDebtTrail(
                transaction_id="X1",
                initial_bad_debt=D("0"),
                recovered_amount=D("0"),
                written_off_amount=D("0"),
                outstanding_bad_debt=D("0"),
                recoveries=(),
                writeoffs=(),
            ),
        )

    def test_approved_settlement_without_bad_debt_returns_zero_empty_trail(self):
        engine = ClearingEngine()
        engine.process(
            "A1", "USD", D("100"), D("60"), D("0"), D("100"), D("0"),
            [("p", D("60"))],
        )
        trail = engine.bad_debt_trail("A1")
        self.assertEqual(trail.transaction_id, "A1")
        self.assertEqual(trail.initial_bad_debt, D("0"))
        self.assertEqual(trail.outstanding_bad_debt, D("0"))
        self.assertEqual(trail.recoveries, ())
        self.assertEqual(trail.writeoffs, ())

    def test_recovery_and_writeoff_ids_are_not_alternative_queries(self):
        engine = make_engine_with_trails()
        # 已登记的回收 / 核销流水号不回退为操作流水号查询，一律未命中。
        self.assertIsNone(engine.bad_debt_trail("R1"))
        self.assertIsNone(engine.bad_debt_trail("R2"))
        self.assertIsNone(engine.bad_debt_trail("R3"))
        self.assertIsNone(engine.bad_debt_trail("W1"))
        self.assertIsNone(engine.bad_debt_trail("ZR0"))
        self.assertIsNone(engine.bad_debt_trail("ZW0"))

    def test_unknown_and_exact_match_semantics(self):
        engine = make_engine_with_trails()
        self.assertIsNone(engine.bad_debt_trail("NO-SUCH-ID"))
        # 精确匹配：不去除首尾空白。
        self.assertIsNone(engine.bad_debt_trail(" T1"))
        self.assertIsNone(engine.bad_debt_trail("T1 "))
        self.assertIsNotNone(engine.bad_debt_trail("T1"))

    def test_empty_ledger_returns_none(self):
        engine = ClearingEngine()
        self.assertIsNone(engine.bad_debt_trail("ANY"))

    def test_invalid_transaction_id_raises_value_error(self):
        engine = make_engine_with_trails()
        for bad in (None, "", "   ", 7, b"T1", 1.5, ("T1",)):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    engine.bad_debt_trail(bad)

    def test_currency_isolation(self):
        engine = make_engine_with_trails()
        engine.process(
            "E1", "EUR", D("100"), D("5"), D("0"), D("100"), D("0"),
            [("eur-co", D("12"))],
        )
        trail = engine.bad_debt_trail("E1")
        self.assertEqual(
            trail,
            BadDebtTrail(
                transaction_id="E1",
                initial_bad_debt=D("7"),
                recovered_amount=D("0"),
                written_off_amount=D("0"),
                outstanding_bad_debt=D("7"),
                recoveries=(),
                writeoffs=(),
            ),
        )
        # USD 的回收 / 核销事件不串入 EUR 轨迹。
        usd_trail = engine.bad_debt_trail("T1")
        self.assertEqual(len(usd_trail.recoveries), 1)

    def test_query_is_read_only_and_repeatable(self):
        engine = make_engine_with_trails()
        events_before = engine.events()
        sequences_before = [event.sequence for event in events_before]
        outstanding_before = engine.outstanding_bad_debts("USD")
        t1_result = engine.result_of("T1")
        reconciliation_before = engine.audit_reconciliation("USD")

        first = engine.bad_debt_trail("T1")
        second = engine.bad_debt_trail("T1")
        miss_first = engine.bad_debt_trail("UNKNOWN")
        miss_second = engine.bad_debt_trail("UNKNOWN")
        invalid_lookups = 0
        for bad in (None, "", " "):
            try:
                engine.bad_debt_trail(bad)
            except ValueError:
                invalid_lookups += 1
        self.assertEqual(invalid_lookups, 3)

        self.assertEqual(first, second)
        self.assertIsNone(miss_first)
        self.assertIsNone(miss_second)

        # 台账、序号、结果索引、存续坏账与审计核对均不变。
        self.assertEqual(engine.events(), events_before)
        self.assertEqual(
            [event.sequence for event in engine.events()], sequences_before
        )
        self.assertEqual(
            engine.outstanding_bad_debts("USD"), outstanding_before
        )
        self.assertIs(engine.result_of("T1"), t1_result)
        self.assertEqual(
            engine.audit_reconciliation("USD"), reconciliation_before
        )
        # 未命中的查询不占流水号。
        self.assertFalse(engine.has_transaction("UNKNOWN"))
        self.assertFalse(engine.has_transaction("bad_debt_trail"))

    def test_results_are_immutable_and_field_order_fixed(self):
        engine = make_engine_with_trails()
        trail = engine.bad_debt_trail("T2")
        self.assertIsInstance(trail, BadDebtTrail)
        self.assertIsInstance(trail.recoveries, tuple)
        self.assertIsInstance(trail.writeoffs, tuple)
        self.assertIsInstance(trail.recoveries[0], BadDebtOperation)
        self.assertEqual(
            BadDebtTrail._fields,
            (
                "transaction_id",
                "initial_bad_debt",
                "recovered_amount",
                "written_off_amount",
                "outstanding_bad_debt",
                "recoveries",
                "writeoffs",
            ),
        )
        self.assertEqual(
            BadDebtOperation._fields,
            ("operation_id", "creditor", "amount", "remaining"),
        )
        with self.assertRaises(Exception):
            trail.initial_bad_debt = D("999")
        with self.assertRaises(Exception):
            trail.recoveries[0].amount = D("999")


if __name__ == "__main__":
    unittest.main()
