"""债权人维度坏账汇总的公开行为测试。

覆盖：多来源合并、三类交易标识按事件顺序去重、零额回收 / 核销不归属、
全额结清留行、Decimal 恒等式与 outstanding_bad_debts 对齐、默认行序、
债权名过滤（None / 空序列 / 命中 / 未命中 / 重复）、币种与名称校验、
空台账与无坏账币种、多币种隔离、不可变性、只读幂等与既有入口不变。
"""

import unittest
from decimal import Decimal

from vault_guard import (
    ClearingEngine,
    CreditorBadDebtSummary,
    InvalidCurrencyError,
)

D = Decimal


def make_engine() -> ClearingEngine:
    """构造跨多来源、回收、核销与零额事件的 USD / EUR 台账。

    事件（审计）顺序：

    T1 USD 放行：池内 40 -> a30 / b10，b 坏账 20。
    T2 USD 放行：池内 10 -> c10，c 坏账 15。
    R1 USD 拒绝：SETTLEMENT_EXCEEDS_AVAILABLE_BALANCE，不确认坏账。
    T3 EUR 放行：e 坏账 7。
    T6 USD 放行：零池内，同名债权 s 两笔各坏账 10（台账两条明细，
                 排在 T4 / T5 之前以保证后续 RC3 可命中 s）。
    T4 USD 放行：池内 5 -> b5，b 再坏账 20（同债权人第二来源）。
    T5 USD 放行：零池内，p / q 各坏账 10（同事件债权清单顺序）。
    T7 USD 放行：z 全额受偿，坏账 0（不入表）。
    RC1 USD 回收 25：b(T1) 清零 20，c 冲减 5（c 余 10）。
    WO1 USD 核销 5：c(T2) 10 -> 5。
    RC2 USD 零额回收：空明细事件，不归属任何债权人。
    WO2 USD 核销 10：c 5 -> 0，s 首条 10 -> 5。
    RC3 USD 回收 15：s 首条 5 -> 0、s 次条 10 -> 0（同一事件两条
                     同债权人分配，标识只记一次）。
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
    engine.process(
        "R1", "USD", D("40"), D("50"), D("0"), D("1000"), D("0"),
        [("x", D("50"))],
    )
    engine.process(
        "T3", "EUR", D("100"), D("5"), D("0"), D("100"), D("0"),
        [("e", D("12"))],
    )
    engine.process(
        "T6", "USD", D("100"), D("0"), D("0"), D("100"), D("0"),
        [("s", D("10")), ("s", D("10"))],
    )
    engine.process(
        "T4", "USD", D("100"), D("5"), D("0"), D("100"), D("0"),
        [("b", D("25"))],
    )
    engine.process(
        "T5", "USD", D("100"), D("0"), D("0"), D("100"), D("0"),
        [("p", D("10")), ("q", D("10"))],
    )
    engine.process(
        "T7", "USD", D("100"), D("5"), D("0"), D("100"), D("0"),
        [("z", D("5"))],
    )
    engine.process_recovery("RC1", "USD", D("25"))
    engine.process_writeoff("WO1", "USD", D("5"))
    engine.process_recovery("RC2", "USD", D("0"))
    engine.process_writeoff("WO2", "USD", D("10"))
    engine.process_recovery("RC3", "USD", D("15"))
    return engine


def by_creditor(rows):
    return {row.creditor: row for row in rows}


class CreditorBadDebtReportTests(unittest.TestCase):
    def test_rows_merge_multiple_sources_in_default_order(self):
        engine = make_engine()
        rows = engine.creditor_bad_debt_report("USD")
        self.assertEqual(
            [row.creditor for row in rows],
            ["b", "c", "s", "p", "q"],
        )
        for row in rows:
            self.assertIsInstance(row, CreditorBadDebtSummary)
            self.assertIsInstance(row.initial_bad_debt, Decimal)
            self.assertIsInstance(row.recovered_amount, Decimal)
            self.assertIsInstance(row.written_off_amount, Decimal)
            self.assertIsInstance(row.outstanding_bad_debt, Decimal)

    def test_source_ids_deduped_in_event_order(self):
        rows = by_creditor(make_engine().creditor_bad_debt_report("USD"))
        # b 的坏账来自 T1 与 T4 两个来源。
        self.assertEqual(rows["b"].source_transaction_ids, ("T1", "T4"))
        self.assertEqual(rows["c"].source_transaction_ids, ("T2",))
        self.assertEqual(rows["p"].source_transaction_ids, ("T5",))
        # 同事件内同名债权两笔：来源流水号只出现一次。
        self.assertEqual(rows["s"].source_transaction_ids, ("T6",))

    def test_recovery_and_writeoff_ids_deduped_in_event_order(self):
        rows = by_creditor(make_engine().creditor_bad_debt_report("USD"))
        self.assertEqual(rows["b"].recovery_transaction_ids, ("RC1",))
        self.assertEqual(rows["b"].writeoff_transaction_ids, ())
        self.assertEqual(rows["c"].recovery_transaction_ids, ("RC1",))
        self.assertEqual(
            rows["c"].writeoff_transaction_ids, ("WO1", "WO2")
        )
        # WO2 与 RC3 均命中 s；RC3 在同一回收事件内对 s 有两条分配，
        # 标识只记一次。
        self.assertEqual(rows["s"].recovery_transaction_ids, ("RC3",))
        self.assertEqual(rows["s"].writeoff_transaction_ids, ("WO2",))
        # 零额空明细回收 RC2 不归属任何债权人。
        for row in rows.values():
            self.assertNotIn("RC2", row.recovery_transaction_ids)
        # p / q 无任何回收核销标识。
        self.assertEqual(rows["p"].recovery_transaction_ids, ())
        self.assertEqual(rows["q"].writeoff_transaction_ids, ())

    def test_amounts_and_decimal_identity(self):
        rows = by_creditor(make_engine().creditor_bad_debt_report("USD"))
        # b：初始 20 + 20 = 40；回收 20；无核销；存续 20。
        self.assertEqual(rows["b"].initial_bad_debt, D("40"))
        self.assertEqual(rows["b"].recovered_amount, D("20"))
        self.assertEqual(rows["b"].written_off_amount, D("0"))
        self.assertEqual(rows["b"].outstanding_bad_debt, D("20"))
        # c：初始 15；回收 5；核销 5 + 5 = 10；全额结清仍留行。
        self.assertEqual(rows["c"].initial_bad_debt, D("15"))
        self.assertEqual(rows["c"].recovered_amount, D("5"))
        self.assertEqual(rows["c"].written_off_amount, D("10"))
        self.assertEqual(rows["c"].outstanding_bad_debt, D("0"))
        # s：初始 20；WO2 核销 5；RC3 回收 5 + 10 = 15；全额结清。
        self.assertEqual(rows["s"].initial_bad_debt, D("20"))
        self.assertEqual(rows["s"].recovered_amount, D("15"))
        self.assertEqual(rows["s"].written_off_amount, D("5"))
        self.assertEqual(rows["s"].outstanding_bad_debt, D("0"))
        # p / q 未被回收核销。
        for name in ("p", "q"):
            row = rows[name]
            self.assertEqual(row.initial_bad_debt, D("10"))
            self.assertEqual(row.recovered_amount, D("0"))
            self.assertEqual(row.written_off_amount, D("0"))
            self.assertEqual(row.outstanding_bad_debt, D("10"))
        for row in rows.values():
            self.assertEqual(
                row.initial_bad_debt
                - row.recovered_amount
                - row.written_off_amount,
                row.outstanding_bad_debt,
            )

    def test_outstanding_matches_outstanding_bad_debts_per_creditor(self):
        engine = make_engine()
        rows = by_creditor(engine.creditor_bad_debt_report("USD"))
        balances: dict[str, Decimal] = {}
        for item in engine.outstanding_bad_debts("USD"):
            balances[item.creditor] = balances.get(item.creditor, D("0")) + (
                item.balance
            )
        # 全额结清的 c / s 不在存续明细中，但报告留行且余额为 0。
        for name, row in rows.items():
            self.assertEqual(
                row.outstanding_bad_debt, balances.get(name, D("0"))
            )
        # 存续明细总额与报告余额合计一致。
        self.assertEqual(
            sum((row.outstanding_bad_debt for row in rows.values()), D("0")),
            sum((item.balance for item in engine.outstanding_bad_debts("USD")), D("0")),
        )

    def test_fully_settled_creditors_keep_rows(self):
        rows = by_creditor(make_engine().creditor_bad_debt_report("USD"))
        self.assertIn("c", rows)
        self.assertIn("s", rows)
        self.assertEqual(rows["c"].outstanding_bad_debt, D("0"))
        self.assertEqual(rows["s"].outstanding_bad_debt, D("0"))
        # 全额受偿、无坏账归因的 z 从不出现在报告中。
        self.assertNotIn("z", rows)
        # 被拒绝请求中的 x 不确认坏账，不出现在报告中。
        self.assertNotIn("x", rows)

    def test_same_event_creditors_follow_list_order(self):
        rows = make_engine().creditor_bad_debt_report("USD")
        names = [row.creditor for row in rows]
        self.assertLess(names.index("p"), names.index("q"))

    def test_creditor_names_filter(self):
        engine = make_engine()
        # None 与缺省等价，返回全部命中行。
        self.assertEqual(
            engine.creditor_bad_debt_report("USD", None),
            engine.creditor_bad_debt_report("USD"),
        )
        # 过滤不改变默认行序：即使按 (c, b) 传入，仍是 b 在前。
        filtered = engine.creditor_bad_debt_report("USD", ("c", "b"))
        self.assertEqual([row.creditor for row in filtered], ["b", "c"])
        # 未命中不报错，返回空元组。
        self.assertEqual(
            engine.creditor_bad_debt_report("USD", ("nobody",)), ()
        )
        # 部分命中只返回命中行。
        part = engine.creditor_bad_debt_report("USD", ("s", "ghost"))
        self.assertEqual([row.creditor for row in part], ["s"])
        # 重复名称只返回一行。
        dup = engine.creditor_bad_debt_report("USD", ("b", "b"))
        self.assertEqual([row.creditor for row in dup], ["b"])
        # 无坏账归因的名称即使显式查询也不命中。
        self.assertEqual(engine.creditor_bad_debt_report("USD", ("z",)), ())
        # 名称去首尾空白后匹配。
        spaced = engine.creditor_bad_debt_report("USD", (" c ",))
        self.assertEqual([row.creditor for row in spaced], ["c"])

    def test_empty_creditor_names_returns_empty_tuple(self):
        engine = make_engine()
        self.assertEqual(engine.creditor_bad_debt_report("USD", ()), ())
        self.assertEqual(engine.creditor_bad_debt_report("USD", []), ())
        self.assertIsInstance(
            engine.creditor_bad_debt_report("USD", ()), tuple
        )

    def test_invalid_creditor_names_raises_value_error(self):
        engine = make_engine()
        for bad in (
            {"b"},
            "b",
            123,
            3.5,
            object(),
            ["b", 7],
            ["b", None],
            ["b", ""],
            ["b", "   "],
        ):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    engine.creditor_bad_debt_report("USD", bad)

    def test_invalid_currency_raises(self):
        engine = make_engine()
        for bad in (None, "", "   ", 7, b"USD"):
            with self.subTest(bad=bad):
                with self.assertRaises(InvalidCurrencyError):
                    engine.creditor_bad_debt_report(bad)
        # 币种校验先于债权名清单校验。
        with self.assertRaises(InvalidCurrencyError):
            engine.creditor_bad_debt_report(None, ["b"])

    def test_currency_matching_strips_whitespace(self):
        engine = make_engine()
        self.assertEqual(
            engine.creditor_bad_debt_report(" USD "),
            engine.creditor_bad_debt_report("USD"),
        )

    def test_empty_ledger_and_no_bad_debt_currency(self):
        self.assertEqual(ClearingEngine().creditor_bad_debt_report("USD"), ())
        engine = ClearingEngine()
        # 全额受偿：放行但零坏账，该币种无坏账行。
        engine.process(
            "T1", "USD", D("100"), D("50"), D("0"), D("100"), D("0"),
            [("z", D("50"))],
        )
        self.assertEqual(engine.creditor_bad_debt_report("USD"), ())
        # 仅有拒绝事件的币种同样为空。
        engine.process(
            "R1", "EUR", D("40"), D("50"), D("0"), D("1000"), D("0"),
            [("x", D("50"))],
        )
        self.assertEqual(engine.creditor_bad_debt_report("EUR"), ())
        # 台账从未出现的币种。
        self.assertEqual(engine.creditor_bad_debt_report("JPY"), ())

    def test_currency_isolation(self):
        engine = make_engine()
        eur = engine.creditor_bad_debt_report("EUR")
        self.assertEqual([row.creditor for row in eur], ["e"])
        self.assertEqual(eur[0].initial_bad_debt, D("7"))
        self.assertEqual(eur[0].outstanding_bad_debt, D("7"))
        self.assertEqual(eur[0].source_transaction_ids, ("T3",))
        self.assertEqual(eur[0].recovery_transaction_ids, ())
        self.assertEqual(eur[0].writeoff_transaction_ids, ())
        usd = engine.creditor_bad_debt_report("USD")
        self.assertTrue(all(row.creditor != "e" for row in usd))

    def test_batch_settlements_feed_same_ledger(self):
        engine = ClearingEngine()
        engine.process_batch(
            "USD",
            D("100"),
            [
                {
                    "transaction_id": "B1",
                    "settlement_amount": D("10"),
                    "notional_exposure": D("0"),
                    "base_limit": D("100"),
                    "risk_factor": D("0"),
                    "creditors": [("g", D("30"))],
                },
                {
                    "transaction_id": "B2",
                    "settlement_amount": D("0"),
                    "notional_exposure": D("0"),
                    "base_limit": D("100"),
                    "risk_factor": D("0"),
                    "creditors": [("g", D("5"))],
                },
            ],
        )
        rows = engine.creditor_bad_debt_report("USD")
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row.creditor, "g")
        # B1 池内 10 -> g 坏账 20；B2 零池内 -> g 坏账 5。
        self.assertEqual(row.source_transaction_ids, ("B1", "B2"))
        self.assertEqual(row.initial_bad_debt, D("25"))
        self.assertEqual(row.outstanding_bad_debt, D("25"))

    def test_query_is_read_only_repeatable_and_does_not_emit_events(self):
        engine = make_engine()
        events_before = engine.events()
        sequences_before = [event.sequence for event in events_before]
        outstanding_before = engine.outstanding_bad_debts("USD")
        result_before = engine.result_of("T1")
        recovery_before = engine.recovery_of("RC1")
        writeoff_before = engine.writeoff_of("WO1")

        first = engine.creditor_bad_debt_report("USD")
        second = engine.creditor_bad_debt_report("USD")
        third = engine.creditor_bad_debt_report("USD", ("b", "c"))
        fourth = engine.creditor_bad_debt_report("JPY")
        self.assertEqual(first, second)
        self.assertEqual(len(third), 2)
        self.assertEqual(fourth, ())

        self.assertEqual(engine.events(), events_before)
        self.assertEqual(
            [event.sequence for event in engine.events()], sequences_before
        )
        self.assertEqual(len(engine.audit_log), len(events_before))
        self.assertEqual(
            engine.outstanding_bad_debts("USD"), outstanding_before
        )
        self.assertIs(engine.result_of("T1"), result_before)
        self.assertIs(engine.recovery_of("RC1"), recovery_before)
        self.assertIs(engine.writeoff_of("WO1"), writeoff_before)
        # 既有流水号去重状态不变：查询不占用新流水号。
        self.assertTrue(engine.has_transaction("T1"))

    def test_rows_and_sequences_are_immutable(self):
        row = make_engine().creditor_bad_debt_report("USD", ("b",))[0]
        with self.assertRaises(Exception):
            row.creditor = "other"
        with self.assertRaises(TypeError):
            row.source_transaction_ids[0] = "TX-X"
        with self.assertRaises(AttributeError):
            row.recovery_transaction_ids.append("RC-X")

    def test_field_order_matches_spec(self):
        self.assertEqual(
            list(CreditorBadDebtSummary.__dataclass_fields__),
            [
                "creditor",
                "source_transaction_ids",
                "recovery_transaction_ids",
                "writeoff_transaction_ids",
                "initial_bad_debt",
                "recovered_amount",
                "written_off_amount",
                "outstanding_bad_debt",
            ],
        )


if __name__ == "__main__":
    unittest.main()
