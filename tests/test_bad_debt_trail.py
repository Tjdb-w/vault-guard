"""坏账处理轨迹 ``bad_debt_trail`` 的公开行为测试。

覆盖：仅命中已登记结算流水号（精确匹配）、未命中与回收 / 核销流水号
不替代查询、拒绝与零坏账放行的零额空轨迹、回收 / 核销明细按审计顺序
与事件内顺序仅保留命中来源、remaining 为处理后余额、同债权多次部分
处理不合并不覆盖、金额恒等式与 outstanding_bad_debts 同来源余额一致、
参数校验、跨批次登记来源、只读幂等与不可变性、字段顺序与公开导出。
"""

import unittest
from dataclasses import fields
from decimal import Decimal

from vault_guard import (
    BadDebtOperation,
    BadDebtTrail,
    ClearingEngine,
)

D = Decimal


def make_engine_with_multi_source_debt() -> ClearingEngine:
    """构造跨事件、跨来源的坏账台账（同债权人报告夹具口径）。

    T1 USD 放行：a30 / b30，池内 40 -> a 无坏账，b 坏账 20。
    T2 USD 放行：b25 / c15，池内 10 -> b 坏账 15，c 坏账 15。
    ZR0 / ZW0：零额回收与零额核销（空明细事件）。
    R1 回收 20：T1 的 b 清零 20。
    R2 回收 20：T2 的 b 清零 15，T2 的 c 冲减 5。
    W1 核销 10：T2 的 c 核销 10，c 全额结清。
    T3 USD 放行：d10 / d10，池内 0 -> 两条 d 各坏账 10。
    R3 回收 15：T3 的第一条 d 清零 10，第二条 d 冲减 5（余 5）。
    """
    engine = ClearingEngine()
    engine.process(
        "T1", "USD", D("100"), D("40"), D("0"), D("100"), D("0"),
        [("a", D("30")), ("b", D("30"))],
    )
    engine.process(
        "T2", "USD", D("100"), D("10"), D("0"), D("100"), D("0"),
        [("b", D("25")), ("c", D("15"))],
    )
    engine.process_recovery("ZR0", "USD", D("0"))
    engine.process_writeoff("ZW0", "USD", D("0"))
    engine.process_recovery("R1", "USD", D("20"))
    engine.process_recovery("R2", "USD", D("20"))
    engine.process_writeoff("W1", "USD", D("10"))
    engine.process(
        "T3", "USD", D("0"), D("0"), D("0"), D("100"), D("0"),
        [("d", D("10")), ("d", D("10"))],
    )
    engine.process_recovery("R3", "USD", D("15"))
    return engine


class BadDebtTrailTests(unittest.TestCase):
    def test_trail_follows_audit_and_intra_event_order(self):
        engine = make_engine_with_multi_source_debt()

        trail = engine.bad_debt_trail("T2")
        self.assertIsInstance(trail, BadDebtTrail)
        self.assertEqual(trail.transaction_id, "T2")
        # 首次坏账取原结算 uncovered_bad_debt，历史结果不回写。
        self.assertEqual(trail.initial_bad_debt, D("30"))
        self.assertEqual(engine.result_of("T2").uncovered_bad_debt, D("30"))

        # R2 事件内按明细顺序：b 清零 15 后 c 冲减 5（余 10）。
        self.assertEqual(
            trail.recoveries,
            (
                BadDebtOperation(
                    operation_id="R2", creditor="b",
                    amount=D("15"), remaining=D("0"),
                ),
                BadDebtOperation(
                    operation_id="R2", creditor="c",
                    amount=D("5"), remaining=D("10"),
                ),
            ),
        )
        self.assertEqual(
            trail.writeoffs,
            (
                BadDebtOperation(
                    operation_id="W1", creditor="c",
                    amount=D("10"), remaining=D("0"),
                ),
            ),
        )
        self.assertEqual(trail.recovered_amount, D("20"))
        self.assertEqual(trail.written_off_amount, D("10"))
        self.assertEqual(trail.outstanding_bad_debt, D("0"))

        # 零额空明细事件不出现在轨迹中；只命中来源 T2，R1 / R3 排除。
        op_ids = {op.operation_id for op in trail.recoveries} | {
            op.operation_id for op in trail.writeoffs
        }
        self.assertEqual(op_ids, {"R2", "W1"})

    def test_other_sources_and_zero_events_excluded(self):
        engine = make_engine_with_multi_source_debt()
        trail = engine.bad_debt_trail("T1")
        self.assertEqual(trail.initial_bad_debt, D("20"))
        self.assertEqual(
            trail.recoveries,
            (
                BadDebtOperation(
                    operation_id="R1", creditor="b",
                    amount=D("20"), remaining=D("0"),
                ),
            ),
        )
        self.assertEqual(trail.writeoffs, ())
        self.assertEqual(trail.recovered_amount, D("20"))
        self.assertEqual(trail.written_off_amount, D("0"))
        self.assertEqual(trail.outstanding_bad_debt, D("0"))
        # 零额事件 ZR0 / ZW0 不产生任何操作明细。
        self.assertNotIn(
            "ZR0", [op.operation_id for op in trail.recoveries]
        )
        self.assertNotIn(
            "ZW0", [op.operation_id for op in trail.writeoffs]
        )

    def test_same_creditor_duplicate_rows_not_merged_or_overwritten(self):
        engine = make_engine_with_multi_source_debt()
        trail = engine.bad_debt_trail("T3")
        self.assertEqual(trail.initial_bad_debt, D("20"))
        # 同一事件对同名债权的两行坏账逐条保留，不合并。
        self.assertEqual(
            trail.recoveries,
            (
                BadDebtOperation(
                    operation_id="R3", creditor="d",
                    amount=D("10"), remaining=D("0"),
                ),
                BadDebtOperation(
                    operation_id="R3", creditor="d",
                    amount=D("5"), remaining=D("5"),
                ),
            ),
        )
        self.assertEqual(trail.recovered_amount, D("15"))
        self.assertEqual(trail.written_off_amount, D("0"))
        self.assertEqual(trail.outstanding_bad_debt, D("5"))

    def test_partial_treatments_keep_separate_operations_in_audit_order(self):
        engine = ClearingEngine()
        engine.process(
            "S1", "USD", D("0"), D("0"), D("0"), D("100"), D("0"),
            [("q", D("100"))],
        )
        engine.process_recovery("RA", "USD", D("10"))
        engine.process_writeoff("WA", "USD", D("20"))
        engine.process_recovery("RB", "USD", D("30"))

        trail = engine.bad_debt_trail("S1")
        self.assertEqual(trail.initial_bad_debt, D("100"))
        # 回收序列只按审计顺序收录回收事件（RA 在 WA 前，RB 在 WA 后），
        # 同一债权 q 的多次部分处理逐条保留、remaining 不被覆盖。
        self.assertEqual(
            trail.recoveries,
            (
                BadDebtOperation(
                    operation_id="RA", creditor="q",
                    amount=D("10"), remaining=D("90"),
                ),
                BadDebtOperation(
                    operation_id="RB", creditor="q",
                    amount=D("30"), remaining=D("40"),
                ),
            ),
        )
        self.assertEqual(
            trail.writeoffs,
            (
                BadDebtOperation(
                    operation_id="WA", creditor="q",
                    amount=D("20"), remaining=D("70"),
                ),
            ),
        )
        self.assertEqual(trail.recovered_amount, D("40"))
        self.assertEqual(trail.written_off_amount, D("20"))
        self.assertEqual(trail.outstanding_bad_debt, D("40"))

    def test_outstanding_matches_ledger_same_source_total(self):
        engine = make_engine_with_multi_source_debt()
        for source_id in ("T1", "T2", "T3"):
            with self.subTest(source_id=source_id):
                trail = engine.bad_debt_trail(source_id)
                ledger_total = sum(
                    (
                        item.balance
                        for item in engine.outstanding_bad_debts("USD")
                        if item.source_transaction_id == source_id
                    ),
                    D("0"),
                )
                self.assertEqual(
                    trail.initial_bad_debt
                    - trail.recovered_amount
                    - trail.written_off_amount,
                    trail.outstanding_bad_debt,
                )
                self.assertEqual(trail.outstanding_bad_debt, ledger_total)
                for amount in (
                    trail.initial_bad_debt,
                    trail.recovered_amount,
                    trail.written_off_amount,
                    trail.outstanding_bad_debt,
                ):
                    self.assertIsInstance(amount, Decimal)
                for op in trail.recoveries + trail.writeoffs:
                    self.assertIsInstance(op.amount, Decimal)
                    self.assertIsInstance(op.remaining, Decimal)

    def test_rejected_settlement_returns_zero_empty_trail(self):
        engine = ClearingEngine()
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
            "P1", "USD", D("100"), D("50"), D("0"), D("100"), D("0"),
            [("z", D("50"))],
        )
        trail = engine.bad_debt_trail("P1")
        self.assertEqual(trail.initial_bad_debt, D("0"))
        self.assertEqual(trail.recovered_amount, D("0"))
        self.assertEqual(trail.written_off_amount, D("0"))
        self.assertEqual(trail.outstanding_bad_debt, D("0"))
        self.assertEqual(trail.recoveries, ())
        self.assertEqual(trail.writeoffs, ())

    def test_recovery_and_writeoff_ids_are_not_fallback_queries(self):
        engine = make_engine_with_multi_source_debt()
        # 已登记的回收 / 核销流水号查不到来源轨迹：返回 None，不替代查询。
        self.assertIsNone(engine.bad_debt_trail("R1"))
        self.assertIsNone(engine.bad_debt_trail("R2"))
        self.assertIsNone(engine.bad_debt_trail("R3"))
        self.assertIsNone(engine.bad_debt_trail("W1"))
        # 空明细事件的流水号同样不命中。
        self.assertIsNone(engine.bad_debt_trail("ZR0"))
        self.assertIsNone(engine.bad_debt_trail("ZW0"))
        # recovery_of / writeoff_of 仍可按各自流水号取到结果。
        self.assertIsNotNone(engine.recovery_of("R1"))
        self.assertIsNotNone(engine.writeoff_of("W1"))

    def test_unknown_id_and_empty_ledger_return_none(self):
        self.assertIsNone(ClearingEngine().bad_debt_trail("NOBODY"))
        engine = make_engine_with_multi_source_debt()
        self.assertIsNone(engine.bad_debt_trail("NOBODY"))

    def test_exact_match_uses_raw_registered_id(self):
        engine = ClearingEngine()
        # 流水号登记时不归一化：带空格的原值可精确命中，去空白形式不命中。
        engine.process(
            " T1 ", "USD", D("0"), D("0"), D("0"), D("100"), D("0"),
            [("b", D("20"))],
        )
        trail = engine.bad_debt_trail(" T1 ")
        self.assertIsNotNone(trail)
        self.assertEqual(trail.transaction_id, " T1 ")
        self.assertEqual(trail.initial_bad_debt, D("20"))
        self.assertIsNone(engine.bad_debt_trail("T1"))
        self.assertIsNone(engine.bad_debt_trail("  T1  "))

    def test_invalid_transaction_id_raises_value_error(self):
        engine = make_engine_with_multi_source_debt()
        for bad in (None, "", "   ", 7, 1.5, b"T1", ["T1"], ("T1",)):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    engine.bad_debt_trail(bad)

    def test_source_registered_via_batch_is_supported(self):
        engine = ClearingEngine()
        engine.process_batch(
            "USD",
            D("10"),
            [
                {
                    "transaction_id": "B1",
                    "settlement_amount": D("10"),
                    "notional_exposure": D("0"),
                    "base_limit": D("100"),
                    "risk_factor": D("0"),
                    "creditors": [("g", D("30"))],
                },
            ],
        )
        # 池内 10 -> g 坏账 20；批次登记的来源同样可串联轨迹。
        trail = engine.bad_debt_trail("B1")
        self.assertEqual(trail.initial_bad_debt, D("20"))
        self.assertEqual(trail.outstanding_bad_debt, D("20"))

    def test_multicurrency_operations_isolated_by_source(self):
        engine = ClearingEngine()
        engine.process_multicurrency_batch(
            {"USD": D("0"), "EUR": D("0")},
            [
                {
                    "transaction_id": "U1",
                    "currency": "USD",
                    "settlement_amount": D("0"),
                    "notional_exposure": D("0"),
                    "base_limit": D("100"),
                    "risk_factor": D("0"),
                    "creditors": [("u", D("10"))],
                },
                {
                    "transaction_id": "E1",
                    "currency": "EUR",
                    "settlement_amount": D("0"),
                    "notional_exposure": D("0"),
                    "base_limit": D("100"),
                    "risk_factor": D("0"),
                    "creditors": [("e", D("20"))],
                },
            ],
        )
        engine.process_recovery("RU", "USD", D("4"))
        engine.process_recovery("RE", "EUR", D("8"))
        u_trail = engine.bad_debt_trail("U1")
        e_trail = engine.bad_debt_trail("E1")
        self.assertEqual(
            [op.operation_id for op in u_trail.recoveries], ["RU"]
        )
        self.assertEqual(
            [op.operation_id for op in e_trail.recoveries], ["RE"]
        )
        self.assertEqual(u_trail.outstanding_bad_debt, D("6"))
        self.assertEqual(e_trail.outstanding_bad_debt, D("12"))

    def test_query_is_read_only_and_repeatable(self):
        engine = make_engine_with_multi_source_debt()
        events_before = engine.events()
        sequences_before = [event.sequence for event in events_before]
        outstanding_before = engine.outstanding_bad_debts("USD")
        t2_result = engine.result_of("T2")
        reconciliation_before = engine.audit_reconciliation("USD")
        recoveries_before = tuple(
            event.event_id for event in events_before
            if event.validation_result == "RECOVERY"
        )

        first = engine.bad_debt_trail("T2")
        second = engine.bad_debt_trail("T2")
        missed = engine.bad_debt_trail("NOBODY")
        invalid_probe = None
        try:
            engine.bad_debt_trail("   ")
        except ValueError:
            invalid_probe = "raised"
        self.assertEqual(invalid_probe, "raised")

        self.assertEqual(first, second)
        self.assertIsNot(first, second)
        self.assertIsNone(missed)

        self.assertEqual(engine.events(), events_before)
        self.assertEqual(
            [event.sequence for event in engine.events()], sequences_before
        )
        self.assertEqual(
            engine.outstanding_bad_debts("USD"), outstanding_before
        )
        self.assertIs(engine.result_of("T2"), t2_result)
        self.assertEqual(
            engine.audit_reconciliation("USD"), reconciliation_before
        )
        # 不占流水号：查询字符串未进入已登记集合。
        self.assertFalse(engine.has_transaction("NOBODY"))
        self.assertFalse(engine.has_transaction("bad_debt_trail"))
        # 回收 / 核销结果索引不变。
        self.assertEqual(
            tuple(
                event.event_id
                for event in engine.events()
                if event.validation_result == "RECOVERY"
            ),
            recoveries_before,
        )

    def test_results_are_immutable(self):
        engine = make_engine_with_multi_source_debt()
        trail = engine.bad_debt_trail("T2")
        self.assertIsInstance(trail, BadDebtTrail)
        self.assertIsInstance(trail.recoveries, tuple)
        self.assertIsInstance(trail.writeoffs, tuple)
        with self.assertRaises(Exception):
            trail.initial_bad_debt = D("999")
        with self.assertRaises(Exception):
            trail.recoveries[0].amount = D("999")
        with self.assertRaises(TypeError):
            trail.recoveries[0] = BadDebtOperation(
                "RX", "x", D("1"), D("1")
            )

    def test_field_order_matches_spec(self):
        self.assertEqual(
            [field.name for field in fields(BadDebtTrail)],
            [
                "transaction_id",
                "initial_bad_debt",
                "recovered_amount",
                "written_off_amount",
                "outstanding_bad_debt",
                "recoveries",
                "writeoffs",
            ],
        )
        self.assertEqual(
            [field.name for field in fields(BadDebtOperation)],
            ["operation_id", "creditor", "amount", "remaining"],
        )

    def test_models_are_publicly_exported(self):
        import vault_guard

        self.assertIs(vault_guard.BadDebtTrail, BadDebtTrail)
        self.assertIs(vault_guard.BadDebtOperation, BadDebtOperation)
        self.assertIn("BadDebtTrail", vault_guard.__all__)
        self.assertIn("BadDebtOperation", vault_guard.__all__)


if __name__ == "__main__":
    unittest.main()
