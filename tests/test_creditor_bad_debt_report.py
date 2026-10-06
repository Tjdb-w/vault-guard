"""债权人维度坏账报告 ``creditor_bad_debt_report`` 的公开行为测试。

覆盖：多来源合并、三类交易标识按事件顺序去重、零额事件不归属、
Decimal 恒等式与存续余额一致、全额结清留行、零坏账债权人剔除、
默认排序、名单过滤与未命中、币种 / 名单校验、空台账与无坏账币种、
跨币种隔离、只读幂等与不可变性。
"""

import unittest
from decimal import Decimal

from vault_guard import (
    ClearingEngine,
    CreditorBadDebtSummary,
    InvalidCurrencyError,
)

D = Decimal


def make_engine_with_multi_source_debt() -> ClearingEngine:
    """构造跨事件、跨来源的坏账台账。

    T1 USD 放行：a30 / b30，池内 40 -> a 无坏账，b 坏账 20。
    T2 USD 放行：b25 / c15，池内 10 -> b 坏账 15，c 坏账 15。
    ZR0 / ZW0：零额回收与零额核销（空明细事件，不归属债权人）。
    R1 回收 20：T1 的 b 清零 20。
    R2 回收 20：T2 的 b 清零 15，T2 的 c 冲减 5。
    W1 核销 10：T2 的 c 核销 10，c 全额结清。
    T3 USD 放行：d10 / d10，池内 0 -> d 合并坏账 20（来源号只计一次）。
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
    return engine


class CreditorBadDebtReportTests(unittest.TestCase):
    def test_merges_multi_source_records_in_default_order(self):
        engine = make_engine_with_multi_source_debt()
        rows = engine.creditor_bad_debt_report("USD")

        self.assertEqual(
            [row.creditor for row in rows], ["b", "c", "d"]
        )
        b_row, c_row, d_row = rows

        # b：跨 T1 / T2 两个来源，被 R1、R2 两笔回收全额结清。
        self.assertEqual(
            b_row,
            CreditorBadDebtSummary(
                creditor="b",
                source_transaction_ids=("T1", "T2"),
                recovery_transaction_ids=("R1", "R2"),
                writeoff_transaction_ids=(),
                initial_bad_debt=D("35"),
                recovered_amount=D("35"),
                written_off_amount=D("0"),
                outstanding_bad_debt=D("0"),
            ),
        )
        # c：单来源 T2，R2 部分回收、W1 核销结清。
        self.assertEqual(
            c_row,
            CreditorBadDebtSummary(
                creditor="c",
                source_transaction_ids=("T2",),
                recovery_transaction_ids=("R2",),
                writeoff_transaction_ids=("W1",),
                initial_bad_debt=D("15"),
                recovered_amount=D("5"),
                written_off_amount=D("10"),
                outstanding_bad_debt=D("0"),
            ),
        )
        # d：同一结算中两条归因合并为一行，来源流水号去重。
        self.assertEqual(
            d_row,
            CreditorBadDebtSummary(
                creditor="d",
                source_transaction_ids=("T3",),
                recovery_transaction_ids=(),
                writeoff_transaction_ids=(),
                initial_bad_debt=D("20"),
                recovered_amount=D("0"),
                written_off_amount=D("0"),
                outstanding_bad_debt=D("20"),
            ),
        )

    def test_zero_amount_recovery_and_writeoff_not_attributed(self):
        engine = make_engine_with_multi_source_debt()
        rows = engine.creditor_bad_debt_report("USD")
        for row in rows:
            self.assertNotIn("ZR0", row.recovery_transaction_ids)
            self.assertNotIn("ZW0", row.writeoff_transaction_ids)
        # 空明细事件确实存在于审计台账，只是不归属债权人。
        event_ids = [event.event_id for event in engine.events()]
        self.assertIn("EVT-ZR0-recovery", event_ids)
        self.assertIn("EVT-ZW0-writeoff", event_ids)

    def test_fully_settled_creditors_keep_rows(self):
        engine = make_engine_with_multi_source_debt()
        rows = engine.creditor_bad_debt_report("USD")
        settled = {row.creditor: row for row in rows}
        # b、c 存续余额为零仍保留行。
        self.assertIn("b", settled)
        self.assertIn("c", settled)
        self.assertEqual(settled["b"].outstanding_bad_debt, D("0"))
        self.assertEqual(settled["c"].outstanding_bad_debt, D("0"))

    def test_creditor_without_bad_debt_is_excluded(self):
        engine = make_engine_with_multi_source_debt()
        rows = engine.creditor_bad_debt_report("USD")
        # a 在 T1 中全额受偿（bad_debt=0），不入表。
        self.assertNotIn("a", [row.creditor for row in rows])

    def test_decimal_identity_matches_ledger(self):
        engine = make_engine_with_multi_source_debt()
        outstanding = {}
        for item in engine.outstanding_bad_debts("USD"):
            outstanding[item.creditor] = (
                outstanding.get(item.creditor, D("0")) + item.balance
            )
        for row in engine.creditor_bad_debt_report("USD"):
            # 金额字段全部为 Decimal。
            for field_name in (
                "initial_bad_debt",
                "recovered_amount",
                "written_off_amount",
                "outstanding_bad_debt",
            ):
                self.assertIsInstance(getattr(row, field_name), Decimal)
            self.assertEqual(
                row.initial_bad_debt
                - row.recovered_amount
                - row.written_off_amount,
                row.outstanding_bad_debt,
            )
            # 与 outstanding_bad_debts 该债权人来源余额合计一致。
            self.assertEqual(
                row.outstanding_bad_debt,
                outstanding.get(row.creditor, D("0")),
            )

    def test_filter_returns_only_hits_in_default_order(self):
        engine = make_engine_with_multi_source_debt()
        # 名单顺序不改变结果的默认排序；未命中不报错。
        rows = engine.creditor_bad_debt_report("USD", ("c", "b", "zzz"))
        self.assertEqual([row.creditor for row in rows], ["b", "c"])

        only_d = engine.creditor_bad_debt_report("USD", ["d"])
        self.assertEqual(len(only_d), 1)
        self.assertEqual(only_d[0].creditor, "d")

        # 名称按去首尾空白后匹配；重复名称不产生重复行。
        spaced = engine.creditor_bad_debt_report(" USD ", ("  c ", "c"))
        self.assertEqual([row.creditor for row in spaced], ["c"])

        miss = engine.creditor_bad_debt_report("USD", ("nobody",))
        self.assertEqual(miss, ())

    def test_none_returns_all_and_empty_sequence_returns_empty(self):
        engine = make_engine_with_multi_source_debt()
        self.assertEqual(
            engine.creditor_bad_debt_report("USD", None),
            engine.creditor_bad_debt_report("USD"),
        )
        self.assertEqual(engine.creditor_bad_debt_report("USD", ()), ())
        self.assertEqual(engine.creditor_bad_debt_report("USD", []), ())

    def test_currency_validation_uses_invalid_currency_error(self):
        engine = make_engine_with_multi_source_debt()
        for bad in (None, "", "   ", 7, b"USD"):
            with self.subTest(bad=bad):
                with self.assertRaises(InvalidCurrencyError):
                    engine.creditor_bad_debt_report(bad)
        # 币种校验先于名单校验。
        with self.assertRaises(InvalidCurrencyError):
            engine.creditor_bad_debt_report("", 123)
        # 匹配时去除首尾空白。
        self.assertEqual(
            engine.creditor_bad_debt_report("  USD "),
            engine.creditor_bad_debt_report("USD"),
        )

    def test_creditor_names_validation(self):
        engine = make_engine_with_multi_source_debt()
        # 非 list / tuple：裸字符串、集合、字典、生成器均拒绝。
        for bad in ("abc", {"b"}, {"b": 1}, (name for name in ("b",))):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    engine.creditor_bad_debt_report("USD", bad)
        # 元素非字符串或空白字符串。
        for bad in (["b", 7], ["b", None], [""], ["   "], ["b", " c ", ""]):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    engine.creditor_bad_debt_report("USD", bad)

    def test_empty_ledger_and_currency_without_bad_debt(self):
        self.assertEqual(ClearingEngine().creditor_bad_debt_report("USD"), ())

        engine = ClearingEngine()
        # 只有拒绝事件：不确认坏账，无命中行。
        engine.process(
            "X1", "USD", D("10"), D("50"), D("0"), D("100"), D("0"),
            [("z", D("50"))],
        )
        self.assertEqual(engine.creditor_bad_debt_report("USD"), ())
        # 从未出现的币种同样为空。
        self.assertEqual(engine.creditor_bad_debt_report("JPY"), ())

    def test_currency_isolation(self):
        engine = make_engine_with_multi_source_debt()
        engine.process(
            "E1", "EUR", D("100"), D("5"), D("0"), D("100"), D("0"),
            [("eur-co", D("12"))],
        )
        usd = engine.creditor_bad_debt_report("USD")
        eur = engine.creditor_bad_debt_report("EUR")
        self.assertEqual([row.creditor for row in usd], ["b", "c", "d"])
        self.assertEqual(len(eur), 1)
        self.assertEqual(
            eur[0],
            CreditorBadDebtSummary(
                creditor="eur-co",
                source_transaction_ids=("E1",),
                recovery_transaction_ids=(),
                writeoff_transaction_ids=(),
                initial_bad_debt=D("7"),
                recovered_amount=D("0"),
                written_off_amount=D("0"),
                outstanding_bad_debt=D("7"),
            ),
        )

    def test_recovery_and_writeoff_ids_follow_event_order(self):
        engine = ClearingEngine()
        engine.process(
            "S1", "USD", D("0"), D("0"), D("0"), D("100"), D("0"),
            [("q", D("100"))],
        )
        engine.process_recovery("RA", "USD", D("10"))
        engine.process_writeoff("WA", "USD", D("20"))
        engine.process_recovery("RB", "USD", D("30"))
        engine.process_writeoff("WB", "USD", D("40"))
        rows = engine.creditor_bad_debt_report("USD")
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row.recovery_transaction_ids, ("RA", "RB"))
        self.assertEqual(row.writeoff_transaction_ids, ("WA", "WB"))
        self.assertEqual(row.recovered_amount, D("40"))
        self.assertEqual(row.written_off_amount, D("60"))
        self.assertEqual(row.outstanding_bad_debt, D("0"))

    def test_query_is_read_only_and_repeatable(self):
        engine = make_engine_with_multi_source_debt()
        events_before = engine.events()
        sequences_before = [event.sequence for event in events_before]
        outstanding_before = engine.outstanding_bad_debts("USD")
        t3_result = engine.result_of("T3")
        reconciliation_before = engine.audit_reconciliation("USD")

        first = engine.creditor_bad_debt_report("USD")
        second = engine.creditor_bad_debt_report("USD")
        filtered = engine.creditor_bad_debt_report("USD", ("b", "d"))
        again = engine.creditor_bad_debt_report("USD", ("b", "d"))
        self.assertEqual(first, second)
        self.assertEqual(filtered, again)
        self.assertEqual(len(filtered), 2)

        self.assertEqual(engine.events(), events_before)
        self.assertEqual(
            [event.sequence for event in engine.events()], sequences_before
        )
        self.assertEqual(
            engine.outstanding_bad_debts("USD"), outstanding_before
        )
        self.assertIs(engine.result_of("T3"), t3_result)
        self.assertEqual(
            engine.audit_reconciliation("USD"), reconciliation_before
        )
        # 不占流水号：查询所用名称仍可作为业务流水号提交（此处仅核对未占用）。
        self.assertFalse(engine.has_transaction("creditor_bad_debt_report"))

    def test_result_is_immutable(self):
        engine = make_engine_with_multi_source_debt()
        rows = engine.creditor_bad_debt_report("USD")
        self.assertIsInstance(rows, tuple)
        with self.assertRaises(Exception):
            rows[0].creditor = "hacked"

    def test_field_order_matches_spec(self):
        self.assertEqual(
            CreditorBadDebtSummary._fields,
            (
                "creditor",
                "source_transaction_ids",
                "recovery_transaction_ids",
                "writeoff_transaction_ids",
                "initial_bad_debt",
                "recovered_amount",
                "written_off_amount",
                "outstanding_bad_debt",
            ),
        )


if __name__ == "__main__":
    unittest.main()
