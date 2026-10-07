"""风险组坏账责任报告 ``risk_group_bad_debt_report`` 的公开行为测试。

覆盖：多组汇总与字典序、放行 / 拒绝计数与风险占用口径、三类流水号去重、
回收 / 核销 / 定向操作回溯来源风险组与债权人、空明细零额操作不归属、
组内债权人字典序与金额恒等式、无风险组记录排除、空台账 / 未知币种 /
仅无风险组记录返回空元组、币种校验、只读幂等与不可变性。
"""

import unittest
from decimal import Decimal

from vault_guard import (
    ClearingEngine,
    CreditorBadDebtSummary,
    InvalidCurrencyError,
    RiskGroupBadDebtSummary,
)

D = Decimal


def make_engine_with_grouped_debt() -> ClearingEngine:
    """构造带两个风险组与一条无组记录的坏账台账（币种 USD，期初 100）。

    T1 GA 放行：a30 / b30，池内 40 -> b 坏账 20，风险占用 40。
    T2 GB 放行：b25 / c15，池内 10 -> b 坏账 15、c 坏账 15，风险占用 10。
    T3 无组放行：z20，池内 5 -> z 坏账 15（不进入报告）。
    T4 GA 放行：a5，池内 0 -> a 坏账 5，风险占用 0。
    T5 GB 拒绝：风险占用 10 超单笔限额 5（仅计数，不占额度、无坏账）。
    R1 回收 20：T1 的 b 清零（GA）。
    W1 核销 10：T2 的 b 核销 10（GB）。
    TR1 定向回收：T2 的 c 冲减 5（GB）。
    TW1 定向核销：T4 的 a 核销 5，全额结清（GA）。
    TU1 定向回收：T3 的 z 冲减 5（无组来源，不进入报告）。
    ZR0 / ZW0：零额回收与零额核销（空明细事件，不归属任何组）。
    """
    engine = ClearingEngine()
    engine.process_risk_group_batch(
        "USD",
        D("100"),
        {"GB": D("1000"), "GA": D("1000")},
        [
            {
                "transaction_id": "T1",
                "risk_group_id": "GA",
                "settlement_amount": D("40"),
                "notional_exposure": D("0"),
                "base_limit": D("1000"),
                "risk_factor": D("0"),
                "creditors": [("a", D("30")), ("b", D("30"))],
            },
            {
                "transaction_id": "T2",
                "risk_group_id": "GB",
                "settlement_amount": D("10"),
                "notional_exposure": D("0"),
                "base_limit": D("1000"),
                "risk_factor": D("0"),
                "creditors": [("b", D("25")), ("c", D("15"))],
            },
            {
                "transaction_id": "T3",
                "settlement_amount": D("5"),
                "notional_exposure": D("0"),
                "base_limit": D("1000"),
                "risk_factor": D("0"),
                "creditors": [("z", D("20"))],
            },
            {
                "transaction_id": "T4",
                "risk_group_id": "GA",
                "settlement_amount": D("0"),
                "notional_exposure": D("0"),
                "base_limit": D("1000"),
                "risk_factor": D("0"),
                "creditors": [("a", D("5"))],
            },
            {
                "transaction_id": "T5",
                "risk_group_id": "GB",
                "settlement_amount": D("10"),
                "notional_exposure": D("0"),
                "base_limit": D("5"),
                "risk_factor": D("0"),
                "creditors": [("b", D("10"))],
            },
        ],
    )
    engine.process_recovery("R1", "USD", D("20"))
    engine.process_writeoff("W1", "USD", D("10"))
    engine.process_targeted_recovery("TR1", "USD", "T2", "c", D("5"))
    engine.process_targeted_writeoff("TW1", "USD", "T4", "a", D("5"))
    engine.process_targeted_recovery("TU1", "USD", "T3", "z", D("5"))
    engine.process_recovery("ZR0", "USD", D("0"))
    engine.process_writeoff("ZW0", "USD", D("0"))
    return engine


class RiskGroupBadDebtReportTests(unittest.TestCase):
    def test_groups_sorted_by_group_id_with_expected_rows(self):
        engine = make_engine_with_grouped_debt()
        rows = engine.risk_group_bad_debt_report("USD")

        self.assertEqual([row.risk_group_id for row in rows], ["GA", "GB"])
        ga_row, gb_row = rows

        self.assertEqual(
            ga_row,
            RiskGroupBadDebtSummary(
                risk_group_id="GA",
                settlement_count=2,
                approved_count=2,
                rejected_count=0,
                risk_occupancy=D("40"),
                initial_bad_debt=D("25"),
                recovered_amount=D("20"),
                written_off_amount=D("5"),
                outstanding_bad_debt=D("0"),
                source_transaction_ids=("T1", "T4"),
                recovery_transaction_ids=("R1",),
                writeoff_transaction_ids=("TW1",),
                creditor_summaries=(
                    CreditorBadDebtSummary(
                        creditor="a",
                        source_transaction_ids=("T4",),
                        recovery_transaction_ids=(),
                        writeoff_transaction_ids=("TW1",),
                        initial_bad_debt=D("5"),
                        recovered_amount=D("0"),
                        written_off_amount=D("5"),
                        outstanding_bad_debt=D("0"),
                    ),
                    CreditorBadDebtSummary(
                        creditor="b",
                        source_transaction_ids=("T1",),
                        recovery_transaction_ids=("R1",),
                        writeoff_transaction_ids=(),
                        initial_bad_debt=D("20"),
                        recovered_amount=D("20"),
                        written_off_amount=D("0"),
                        outstanding_bad_debt=D("0"),
                    ),
                ),
            ),
        )
        self.assertEqual(
            gb_row,
            RiskGroupBadDebtSummary(
                risk_group_id="GB",
                settlement_count=2,
                approved_count=1,
                rejected_count=1,
                risk_occupancy=D("10"),
                initial_bad_debt=D("30"),
                recovered_amount=D("5"),
                written_off_amount=D("10"),
                outstanding_bad_debt=D("15"),
                source_transaction_ids=("T2",),
                recovery_transaction_ids=("TR1",),
                writeoff_transaction_ids=("W1",),
                creditor_summaries=(
                    CreditorBadDebtSummary(
                        creditor="b",
                        source_transaction_ids=("T2",),
                        recovery_transaction_ids=(),
                        writeoff_transaction_ids=("W1",),
                        initial_bad_debt=D("15"),
                        recovered_amount=D("0"),
                        written_off_amount=D("10"),
                        outstanding_bad_debt=D("5"),
                    ),
                    CreditorBadDebtSummary(
                        creditor="c",
                        source_transaction_ids=("T2",),
                        recovery_transaction_ids=("TR1",),
                        writeoff_transaction_ids=(),
                        initial_bad_debt=D("15"),
                        recovered_amount=D("5"),
                        written_off_amount=D("0"),
                        outstanding_bad_debt=D("10"),
                    ),
                ),
            ),
        )

    def test_ungrouped_records_excluded(self):
        engine = make_engine_with_grouped_debt()
        rows = engine.risk_group_bad_debt_report("USD")
        for row in rows:
            self.assertNotIn("T3", row.source_transaction_ids)
            self.assertNotIn("TU1", row.recovery_transaction_ids)
            for creditor_row in row.creditor_summaries:
                self.assertNotEqual(creditor_row.creditor, "z")

    def test_zero_amount_operations_not_attributed(self):
        engine = make_engine_with_grouped_debt()
        rows = engine.risk_group_bad_debt_report("USD")
        for row in rows:
            self.assertNotIn("ZR0", row.recovery_transaction_ids)
            self.assertNotIn("ZW0", row.writeoff_transaction_ids)
            for creditor_row in row.creditor_summaries:
                self.assertNotIn("ZR0", creditor_row.recovery_transaction_ids)
                self.assertNotIn("ZW0", creditor_row.writeoff_transaction_ids)
        # 空明细事件确实存在于审计台账，只是不归属任何风险组。
        event_ids = [event.event_id for event in engine.events()]
        self.assertIn("EVT-ZR0-recovery", event_ids)
        self.assertIn("EVT-ZW0-writeoff", event_ids)

    def test_amount_invariants_and_decimal_types(self):
        engine = make_engine_with_grouped_debt()
        rows = engine.risk_group_bad_debt_report("USD")
        for row in rows:
            self.assertEqual(
                row.initial_bad_debt
                - row.recovered_amount
                - row.written_off_amount,
                row.outstanding_bad_debt,
            )
            self.assertEqual(
                sum(
                    (c.outstanding_bad_debt for c in row.creditor_summaries),
                    D("0"),
                ),
                row.outstanding_bad_debt,
            )
            self.assertIsInstance(row.risk_occupancy, Decimal)
            self.assertIsInstance(row.initial_bad_debt, Decimal)
            self.assertIsInstance(row.recovered_amount, Decimal)
            self.assertIsInstance(row.written_off_amount, Decimal)
            self.assertIsInstance(row.outstanding_bad_debt, Decimal)
            self.assertIsInstance(row.settlement_count, int)
            self.assertIsInstance(row.approved_count, int)
            self.assertIsInstance(row.rejected_count, int)

    def test_group_outstanding_sums_to_currency_outstanding(self):
        # 全部坏账均来自带风险组的结算时，各组存续之和等于币种存续总额。
        engine = ClearingEngine()
        engine.process_risk_group_batch(
            "USD",
            D("10"),
            {"G1": D("100"), "G2": D("100")},
            [
                {
                    "transaction_id": "S1",
                    "risk_group_id": "G1",
                    "settlement_amount": D("10"),
                    "notional_exposure": D("0"),
                    "base_limit": D("100"),
                    "risk_factor": D("0"),
                    "creditors": [("a", D("20"))],
                },
                {
                    "transaction_id": "S2",
                    "risk_group_id": "G2",
                    "settlement_amount": D("0"),
                    "notional_exposure": D("0"),
                    "base_limit": D("100"),
                    "risk_factor": D("0"),
                    "creditors": [("b", D("7"))],
                },
            ],
        )
        engine.process_recovery("RC", "USD", D("4"))
        rows = engine.risk_group_bad_debt_report("USD")
        total = sum((row.outstanding_bad_debt for row in rows), D("0"))
        ledger_total = sum(
            (item.balance for item in engine.outstanding_bad_debts("USD")),
            D("0"),
        )
        self.assertEqual(total, ledger_total)
        self.assertEqual(total, D("13"))

    def test_empty_ledger_unknown_currency_and_ungrouped_only(self):
        engine = ClearingEngine()
        self.assertEqual(engine.risk_group_bad_debt_report("USD"), ())

        # 仅无风险组记录：返回空元组。
        engine.process(
            "T1", "USD", D("10"), D("5"), D("0"), D("100"), D("0"),
            [("a", D("20"))],
        )
        self.assertEqual(engine.risk_group_bad_debt_report("USD"), ())
        # 币种不存在（台账只有 USD 记录）。
        self.assertEqual(engine.risk_group_bad_debt_report("EUR"), ())

    def test_currency_validation(self):
        engine = make_engine_with_grouped_debt()
        for bad in (None, "", "   ", 123, 1.5, b"USD"):
            with self.assertRaises(InvalidCurrencyError):
                engine.risk_group_bad_debt_report(bad)
        # 去首尾空白后匹配台账。
        rows = engine.risk_group_bad_debt_report("  USD  ")
        self.assertEqual([row.risk_group_id for row in rows], ["GA", "GB"])

    def test_read_only_and_idempotent(self):
        engine = make_engine_with_grouped_debt()
        events_before = engine.events()
        first = engine.risk_group_bad_debt_report("USD")
        second = engine.risk_group_bad_debt_report("USD")
        self.assertEqual(first, second)
        self.assertEqual(engine.events(), events_before)
        # 不占用流水号：报告后同名新结算仍可正常登记之外的查询不受影响，
        # 审计序号与事件数不变。
        self.assertEqual(len(engine.events()), len(events_before))

    def test_immutability(self):
        engine = make_engine_with_grouped_debt()
        row = engine.risk_group_bad_debt_report("USD")[0]
        self.assertIsInstance(row, tuple)
        self.assertIsInstance(row.creditor_summaries, tuple)
        self.assertIsInstance(row.source_transaction_ids, tuple)
        with self.assertRaises(AttributeError):
            row.outstanding_bad_debt = D("999")

    def test_group_without_bad_debt_still_listed(self):
        # 带组结算全部全额受偿：组仍在报告中，金额为零、流水号为空。
        engine = ClearingEngine()
        engine.process_risk_group_batch(
            "USD",
            D("100"),
            {"G1": D("100")},
            [
                {
                    "transaction_id": "S1",
                    "risk_group_id": "G1",
                    "settlement_amount": D("10"),
                    "notional_exposure": D("2"),
                    "base_limit": D("100"),
                    "risk_factor": D("1"),
                    "creditors": [("a", D("10"))],
                },
            ],
        )
        rows = engine.risk_group_bad_debt_report("USD")
        self.assertEqual(len(rows), 1)
        (row,) = rows
        self.assertEqual(row.risk_group_id, "G1")
        self.assertEqual(row.settlement_count, 1)
        self.assertEqual(row.approved_count, 1)
        self.assertEqual(row.risk_occupancy, D("12"))
        self.assertEqual(row.initial_bad_debt, D("0"))
        self.assertEqual(row.outstanding_bad_debt, D("0"))
        self.assertEqual(row.source_transaction_ids, ())
        self.assertEqual(row.creditor_summaries, ())


if __name__ == "__main__":
    unittest.main()
