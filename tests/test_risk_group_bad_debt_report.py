"""风险组坏账责任报告 ``risk_group_bad_debt_report`` 的公开行为测试。

覆盖：按风险组字典序汇总、放行 / 拒绝计数与风险占用口径、三类流水号
首次出现顺序去重、回收 / 核销 / 定向操作回溯来源风险组与债权人、
空明细零额事件不计、组内债权人字典序与恒等式、无风险组记录排除、
跨币种隔离、币种校验、空台账与只读幂等。
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


def make_engine_with_risk_groups() -> ClearingEngine:
    """构造带风险组的多来源坏账台账（USD）。

    T1 G1 放行：a30 / b30，池内 40 -> b 坏账 20，风险占用 40。
    T2 G2 放行：b25 / c15，池内 10 -> b 坏账 15、c 坏账 15，风险占用 10。
    T3 G1 拒绝：风险占用 50 超单笔限额（仅计数，不确认坏账）。
    T4 无风险组放行：d10，池内 5 -> d 坏账 5（不参与报告）。
    ZR0 / ZW0：零额回收与零额核销（空明细事件，不计入）。
    R1 回收 20：T1 的 b 清零 20（归属 G1）。
    W1 核销 10：T2 的 b 核销 10（归属 G2），b 余 5。
    TR1 定向回收 5：T2 的 c 冲减 5（归属 G2），c 余 10。
    """
    engine = ClearingEngine()
    engine.process_risk_group_batch(
        "USD",
        D("100"),
        {"G1": D("1000"), "G2": D("1000")},
        [
            {
                "transaction_id": "T1",
                "risk_group_id": "G1",
                "settlement_amount": D("40"),
                "notional_exposure": D("0"),
                "base_limit": D("100"),
                "risk_factor": D("0"),
                "creditors": [("a", D("30")), ("b", D("30"))],
            },
            {
                "transaction_id": "T2",
                "risk_group_id": "G2",
                "settlement_amount": D("10"),
                "notional_exposure": D("0"),
                "base_limit": D("100"),
                "risk_factor": D("0"),
                "creditors": [("b", D("25")), ("c", D("15"))],
            },
            {
                "transaction_id": "T3",
                "risk_group_id": "G1",
                "settlement_amount": D("50"),
                "notional_exposure": D("0"),
                "base_limit": D("10"),
                "risk_factor": D("0"),
                "creditors": [("a", D("50"))],
            },
            {
                "transaction_id": "T4",
                "settlement_amount": D("5"),
                "notional_exposure": D("0"),
                "base_limit": D("100"),
                "risk_factor": D("0"),
                "creditors": [("d", D("10"))],
            },
        ],
    )
    engine.process_recovery("ZR0", "USD", D("0"))
    engine.process_writeoff("ZW0", "USD", D("0"))
    engine.process_recovery("R1", "USD", D("20"))
    engine.process_writeoff("W1", "USD", D("10"))
    engine.process_targeted_recovery("TR1", "USD", "T2", "c", D("5"))
    return engine


class RiskGroupBadDebtReportTests(unittest.TestCase):
    def test_groups_sorted_by_id_with_expected_rows(self):
        engine = make_engine_with_risk_groups()
        rows = engine.risk_group_bad_debt_report("USD")

        self.assertEqual([row.risk_group_id for row in rows], ["G1", "G2"])
        g1_row, g2_row = rows

        self.assertEqual(
            g1_row,
            RiskGroupBadDebtSummary(
                risk_group_id="G1",
                settlement_count=2,
                approved_count=1,
                rejected_count=1,
                risk_occupancy=D("40"),
                initial_bad_debt=D("20"),
                recovered_amount=D("20"),
                written_off_amount=D("0"),
                outstanding_bad_debt=D("0"),
                source_transaction_ids=("T1",),
                recovery_transaction_ids=("R1",),
                writeoff_transaction_ids=(),
                creditor_summaries=(
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
            g2_row,
            RiskGroupBadDebtSummary(
                risk_group_id="G2",
                settlement_count=1,
                approved_count=1,
                rejected_count=0,
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

    def test_zero_amount_operations_not_counted(self):
        engine = make_engine_with_risk_groups()
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

    def test_group_identities_and_outstanding_totals(self):
        engine = make_engine_with_risk_groups()
        rows = engine.risk_group_bad_debt_report("USD")
        for row in rows:
            # 首次坏账 - 已回收 - 已核销 == 存续坏账。
            self.assertEqual(
                row.initial_bad_debt
                - row.recovered_amount
                - row.written_off_amount,
                row.outstanding_bad_debt,
            )
            # 组内债权人存续坏账之和等于该组值。
            self.assertEqual(
                sum(
                    (c.outstanding_bad_debt for c in row.creditor_summaries),
                    D("0"),
                ),
                row.outstanding_bad_debt,
            )
        # 全部组之和等于来源带风险组的存续坏账合计（本场景即全部）。
        ledger_total = sum(
            (item.balance for item in engine.outstanding_bad_debts("USD")),
            D("0"),
        )
        group_total = sum((row.outstanding_bad_debt for row in rows), D("0"))
        self.assertEqual(group_total, ledger_total - D("5"))  # T4 无风险组

    def test_all_grouped_sources_match_outstanding_total(self):
        # 全部结算都带风险组时，各组存续之和等于该币种存续坏账总额。
        engine = ClearingEngine()
        engine.process_risk_group_batch(
            "USD",
            D("10"),
            {"G1": D("100"), "G2": D("100")},
            [
                {
                    "transaction_id": "T1",
                    "risk_group_id": "G1",
                    "settlement_amount": D("10"),
                    "notional_exposure": D("0"),
                    "base_limit": D("100"),
                    "risk_factor": D("0"),
                    "creditors": [("a", D("15"))],
                },
                {
                    "transaction_id": "T2",
                    "risk_group_id": "G2",
                    "settlement_amount": D("0"),
                    "notional_exposure": D("0"),
                    "base_limit": D("100"),
                    "risk_factor": D("0"),
                    "creditors": [("b", D("7"))],
                },
            ],
        )
        rows = engine.risk_group_bad_debt_report("USD")
        ledger_total = sum(
            (item.balance for item in engine.outstanding_bad_debts("USD")),
            D("0"),
        )
        self.assertEqual(
            sum((row.outstanding_bad_debt for row in rows), D("0")),
            ledger_total,
        )
        self.assertEqual(ledger_total, D("12"))

    def test_settlements_without_risk_group_excluded(self):
        engine = ClearingEngine()
        engine.process(
            "T1", "USD", D("10"), D("5"), D("0"), D("100"), D("0"),
            [("a", D("8"))],
        )
        self.assertEqual(engine.risk_group_bad_debt_report("USD"), ())

    def test_empty_ledger_and_unknown_currency_return_empty(self):
        engine = ClearingEngine()
        self.assertEqual(engine.risk_group_bad_debt_report("USD"), ())
        engine = make_engine_with_risk_groups()
        self.assertEqual(engine.risk_group_bad_debt_report("EUR"), ())

    def test_currency_validation(self):
        engine = make_engine_with_risk_groups()
        for bad in (None, 123, "", "   "):
            with self.assertRaises(InvalidCurrencyError):
                engine.risk_group_bad_debt_report(bad)
        # 去首尾空白后匹配台账。
        rows = engine.risk_group_bad_debt_report("  USD  ")
        self.assertEqual([row.risk_group_id for row in rows], ["G1", "G2"])

    def test_report_is_read_only_and_repeatable(self):
        engine = make_engine_with_risk_groups()
        events_before = engine.events()
        outstanding_before = engine.outstanding_bad_debts("USD")
        first = engine.risk_group_bad_debt_report("USD")
        second = engine.risk_group_bad_debt_report("USD")
        self.assertEqual(first, second)
        self.assertEqual(engine.events(), events_before)
        self.assertEqual(engine.outstanding_bad_debts("USD"), outstanding_before)
        self.assertIsInstance(first, tuple)
        for row in first:
            self.assertIsInstance(row.source_transaction_ids, tuple)
            self.assertIsInstance(row.recovery_transaction_ids, tuple)
            self.assertIsInstance(row.writeoff_transaction_ids, tuple)
            self.assertIsInstance(row.creditor_summaries, tuple)
            self.assertIsInstance(row.risk_occupancy, Decimal)
            self.assertIsInstance(row.initial_bad_debt, Decimal)
            self.assertIsInstance(row.outstanding_bad_debt, Decimal)
            self.assertIsInstance(row.settlement_count, int)

    def test_cross_currency_isolation(self):
        engine = make_engine_with_risk_groups()
        engine.process_risk_group_batch(
            "EUR",
            D("50"),
            {"G1": D("1000")},
            [
                {
                    "transaction_id": "E1",
                    "risk_group_id": "G1",
                    "settlement_amount": D("10"),
                    "notional_exposure": D("0"),
                    "base_limit": D("100"),
                    "risk_factor": D("0"),
                    "creditors": [("x", D("12"))],
                },
            ],
        )
        eur_rows = engine.risk_group_bad_debt_report("EUR")
        self.assertEqual(len(eur_rows), 1)
        self.assertEqual(eur_rows[0].risk_group_id, "G1")
        self.assertEqual(eur_rows[0].settlement_count, 1)
        self.assertEqual(eur_rows[0].initial_bad_debt, D("2"))
        # USD 报告不受 EUR 台账影响。
        usd_rows = engine.risk_group_bad_debt_report("USD")
        self.assertEqual([row.risk_group_id for row in usd_rows], ["G1", "G2"])
        self.assertEqual(usd_rows[0].settlement_count, 2)


if __name__ == "__main__":
    unittest.main()
