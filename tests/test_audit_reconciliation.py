"""单币种审计核对快照的公开行为测试。

覆盖：放行 / 拒绝 / 回收的统一口径汇总、风险占用仅计放行、池内 / 资本 /
首次坏账 / 回收冲减按明细求和、坏账恒等式、拒绝原因码排序与省略零次、
事件标识台账顺序、未出现币种与空台账、币种校验、不可变性与只读幂等。
"""

import unittest
from decimal import Decimal
from types import MappingProxyType

from vault_guard import (
    ClearingEngine,
    CurrencyAuditSummary,
    InvalidCurrencyError,
)

D = Decimal


def make_engine_with_mixed_events() -> ClearingEngine:
    """构造跨放行、拒绝、回收的台账。

    T1 USD 放行：池内 40 -> a30 / b30，b 坏账 20。
    T2 USD 放行：池内 10 -> c25，c 坏账 15。
    R1 USD 拒绝：SETTLEMENT_EXCEEDS_AVAILABLE_BALANCE。
    R2 USD 拒绝：RISK_OCCUPANCY_EXCEEDS_BASE_LIMIT。
    T3 EUR 放行：池内 5 -> e12，e 坏账 7。
    RC1 USD 回收 25：b 清零 20，c 冲减 5。
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
        "R2", "USD", D("100"), D("40"), D("100"), D("90"), D("0.6"),
        [("y", D("40"))],
    )
    engine.process(
        "T3", "EUR", D("100"), D("5"), D("0"), D("100"), D("0"),
        [("e", D("12"))],
    )
    engine.process_recovery("RC1", "USD", D("25"))
    return engine


class AuditReconciliationTests(unittest.TestCase):
    def test_counts_and_event_ids_in_ledger_order(self):
        engine = make_engine_with_mixed_events()
        summary = engine.audit_reconciliation("USD")
        self.assertIsInstance(summary, CurrencyAuditSummary)
        self.assertEqual(summary.currency, "USD")
        self.assertEqual(summary.settlement_count, 4)
        self.assertEqual(summary.approved_count, 2)
        self.assertEqual(summary.rejected_count, 2)
        self.assertEqual(summary.recovery_count, 1)
        self.assertEqual(
            summary.event_ids,
            (
                "EVT-T1-approved",
                "EVT-T2-approved",
                "EVT-R1-rejected",
                "EVT-R2-rejected",
                "EVT-RC1-recovery",
            ),
        )

    def test_amounts_summed_from_details(self):
        engine = make_engine_with_mixed_events()
        summary = engine.audit_reconciliation("USD")
        # 风险占用仅累计放行：40 + 10。
        self.assertEqual(summary.approved_risk_occupancy, D("50"))
        # 池内分配：40 + 10；两笔均无补充资本。
        self.assertEqual(summary.pool_allocated, D("50"))
        self.assertEqual(summary.capital_allocated, D("0"))
        # 首次坏账：b 20 + c 15；回收冲减 25。
        self.assertEqual(summary.initial_bad_debt, D("35"))
        self.assertEqual(summary.recovered_amount, D("25"))
        self.assertEqual(summary.outstanding_bad_debt, D("10"))

    def test_bad_debt_identity_holds(self):
        engine = make_engine_with_mixed_events()
        summary = engine.audit_reconciliation("USD")
        self.assertEqual(
            summary.initial_bad_debt - summary.recovered_amount,
            summary.outstanding_bad_debt,
        )
        self.assertEqual(
            summary.outstanding_bad_debt,
            sum(
                (item.balance for item in engine.outstanding_bad_debts("USD")),
                D("0"),
            ),
        )

    def test_rejection_counts_sorted_and_zero_omitted(self):
        engine = make_engine_with_mixed_events()
        summary = engine.audit_reconciliation("USD")
        self.assertEqual(
            dict(summary.rejection_counts),
            {
                "RISK_OCCUPANCY_EXCEEDS_BASE_LIMIT": 1,
                "SETTLEMENT_EXCEEDS_AVAILABLE_BALANCE": 1,
            },
        )
        # 按原因码排序。
        reasons = list(summary.rejection_counts)
        self.assertEqual(reasons, sorted(reasons))

    def test_currency_matching_strips_surrounding_whitespace(self):
        engine = make_engine_with_mixed_events()
        self.assertEqual(
            engine.audit_reconciliation("  USD "),
            engine.audit_reconciliation("USD"),
        )
        eur = engine.audit_reconciliation(" EUR ")
        self.assertEqual(eur.currency, "EUR")
        self.assertEqual(
            (
                eur.settlement_count,
                eur.approved_count,
                eur.rejected_count,
                eur.recovery_count,
            ),
            (1, 1, 0, 0),
        )
        self.assertEqual(eur.initial_bad_debt, D("7"))
        self.assertEqual(eur.recovered_amount, D("0"))
        self.assertEqual(eur.outstanding_bad_debt, D("7"))
        self.assertEqual(eur.event_ids, ("EVT-T3-approved",))
        self.assertEqual(dict(eur.rejection_counts), {})

    def test_unseen_currency_is_all_zero_with_empty_sequences(self):
        engine = make_engine_with_mixed_events()
        summary = engine.audit_reconciliation("JPY")
        self.assertEqual(summary.currency, "JPY")
        self.assertEqual(summary.settlement_count, 0)
        self.assertEqual(summary.approved_count, 0)
        self.assertEqual(summary.rejected_count, 0)
        self.assertEqual(summary.recovery_count, 0)
        self.assertEqual(summary.approved_risk_occupancy, D("0"))
        self.assertEqual(summary.pool_allocated, D("0"))
        self.assertEqual(summary.capital_allocated, D("0"))
        self.assertEqual(summary.initial_bad_debt, D("0"))
        self.assertEqual(summary.recovered_amount, D("0"))
        self.assertEqual(summary.outstanding_bad_debt, D("0"))
        self.assertEqual(summary.rejection_counts, MappingProxyType({}))
        self.assertEqual(summary.event_ids, ())

    def test_empty_ledger_result_is_deterministic(self):
        first = ClearingEngine().audit_reconciliation("USD")
        second = ClearingEngine().audit_reconciliation("USD")
        self.assertEqual(first, second)
        self.assertEqual(first.currency, "USD")
        self.assertEqual(first.settlement_count, 0)
        self.assertEqual(first.recovery_count, 0)
        self.assertEqual(first.event_ids, ())
        self.assertEqual(dict(first.rejection_counts), {})
        for field in (
            "approved_risk_occupancy",
            "pool_allocated",
            "capital_allocated",
            "initial_bad_debt",
            "recovered_amount",
            "outstanding_bad_debt",
        ):
            self.assertEqual(getattr(first, field), D("0"))

    def test_invalid_currency_raises(self):
        engine = make_engine_with_mixed_events()
        for bad in (None, "", "   ", 7, b"USD"):
            with self.subTest(bad=bad):
                with self.assertRaises(InvalidCurrencyError):
                    engine.audit_reconciliation(bad)

    def test_capital_allocation_and_risk_occupancy_summed(self):
        engine = ClearingEngine()
        engine.process(
            "C1", "USD", D("100"), D("50"), D("0"), D("200"), D("0"),
            [("senior", D("60")), ("mezz", D("30")), ("eq", D("10"))],
            supplementary_capital=D("40"),
        )
        summary = engine.audit_reconciliation("USD")
        self.assertEqual(summary.pool_allocated, D("50"))
        self.assertEqual(summary.capital_allocated, D("40"))
        self.assertEqual(summary.initial_bad_debt, D("10"))
        self.assertEqual(summary.approved_risk_occupancy, D("50"))
        self.assertEqual(summary.outstanding_bad_debt, D("10"))

    def test_zero_recovery_event_counted_without_amount(self):
        engine = ClearingEngine()
        engine.process(
            "T1", "USD", D("100"), D("0"), D("0"), D("0"), D("0"),
            [("a", D("10"))],
        )
        engine.process_recovery("Z1", "USD", D("0"))
        summary = engine.audit_reconciliation("USD")
        self.assertEqual(summary.recovery_count, 1)
        self.assertEqual(summary.recovered_amount, D("0"))
        self.assertEqual(summary.event_ids[-1], "EVT-Z1-recovery")
        self.assertEqual(
            summary.initial_bad_debt - summary.recovered_amount,
            summary.outstanding_bad_debt,
        )

    def test_group_limit_rejection_reason_counted(self):
        engine = ClearingEngine()
        engine.process_risk_group_batch(
            "USD",
            D("100"),
            {"G1": D("10")},
            [
                {
                    "transaction_id": "G1A",
                    "settlement_amount": D("10"),
                    "notional_exposure": D("0"),
                    "base_limit": D("100"),
                    "risk_factor": D("0"),
                    "creditors": [("c", D("10"))],
                    "risk_group_id": "G1",
                },
                {
                    "transaction_id": "G1B",
                    "settlement_amount": D("1"),
                    "notional_exposure": D("0"),
                    "base_limit": D("100"),
                    "risk_factor": D("0"),
                    "creditors": [("c", D("1"))],
                    "risk_group_id": "G1",
                },
            ],
        )
        summary = engine.audit_reconciliation("USD")
        self.assertEqual(summary.settlement_count, 2)
        self.assertEqual(summary.approved_count, 1)
        self.assertEqual(summary.rejected_count, 1)
        self.assertEqual(
            dict(summary.rejection_counts),
            {"GROUP_LIMIT_EXCEEDED": 1},
        )
        self.assertEqual(summary.approved_risk_occupancy, D("10"))

    def test_partial_recoveries_keep_identity(self):
        engine = make_engine_with_mixed_events()
        engine.process_recovery("RC2", "USD", D("5"))
        summary = engine.audit_reconciliation("USD")
        self.assertEqual(summary.recovery_count, 2)
        self.assertEqual(summary.recovered_amount, D("30"))
        self.assertEqual(summary.outstanding_bad_debt, D("5"))
        self.assertEqual(
            summary.initial_bad_debt - summary.recovered_amount,
            summary.outstanding_bad_debt,
        )

    def test_writeoff_counts_and_identity(self):
        engine = make_engine_with_mixed_events()
        # RC1 已回收 25（b 清零 20，c 剩 10）；再核销 c 的 6 与 4。
        engine.process_writeoff("W1", "USD", D("6"))
        zero = engine.process_writeoff("W0", "USD", D("0"))
        self.assertEqual(zero.allocations, ())
        engine.process_writeoff("W2", "USD", D("4"))
        summary = engine.audit_reconciliation("USD")
        self.assertEqual(summary.writeoff_count, 3)
        self.assertEqual(summary.written_off_amount, D("10"))
        # recovered_amount 只计回收，不含核销。
        self.assertEqual(summary.recovered_amount, D("25"))
        self.assertEqual(summary.recovery_count, 1)
        self.assertEqual(summary.outstanding_bad_debt, D("0"))
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
                "EVT-R1-rejected",
                "EVT-R2-rejected",
                "EVT-RC1-recovery",
                "EVT-W1-writeoff",
                "EVT-W0-writeoff",
                "EVT-W2-writeoff",
            ),
        )

    def test_zero_writeoff_counted_without_amount(self):
        engine = ClearingEngine()
        engine.process(
            "T1", "USD", D("100"), D("0"), D("0"), D("0"), D("0"),
            [("a", D("10"))],
        )
        engine.process_writeoff("Z1", "USD", D("0"))
        summary = engine.audit_reconciliation("USD")
        self.assertEqual(summary.writeoff_count, 1)
        self.assertEqual(summary.written_off_amount, D("0"))
        self.assertEqual(summary.event_ids[-1], "EVT-Z1-writeoff")
        self.assertEqual(
            summary.initial_bad_debt
            - summary.recovered_amount
            - summary.written_off_amount,
            summary.outstanding_bad_debt,
        )

    def test_writeoff_does_not_change_recovered_amount(self):
        engine = make_engine_with_mixed_events()
        before = engine.audit_reconciliation("USD")
        engine.process_writeoff("W1", "USD", D("10"))
        after = engine.audit_reconciliation("USD")
        self.assertEqual(after.recovered_amount, before.recovered_amount)
        self.assertEqual(after.recovery_count, before.recovery_count)
        self.assertEqual(after.written_off_amount, D("10"))

    def test_query_is_read_only_and_idempotent(self):
        engine = make_engine_with_mixed_events()
        events_before = engine.events()
        sequences_before = [event.sequence for event in events_before]
        outstanding_before = engine.outstanding_bad_debts("USD")
        recovery_before = engine.recovery_of("RC1")

        first = engine.audit_reconciliation("USD")
        second = engine.audit_reconciliation("USD")
        third = engine.audit_reconciliation(" JPY ")
        self.assertEqual(first, second)
        self.assertEqual(third.settlement_count, 0)

        # 审计序号、事件、坏账与回收状态均不变。
        self.assertEqual(engine.events(), events_before)
        self.assertEqual(
            [event.sequence for event in engine.events()], sequences_before
        )
        self.assertEqual(engine.outstanding_bad_debts("USD"), outstanding_before)
        self.assertIs(engine.recovery_of("RC1"), recovery_before)
        self.assertEqual(len(engine.audit_log), 6)

    def test_snapshot_is_immutable(self):
        engine = make_engine_with_mixed_events()
        summary = engine.audit_reconciliation("USD")
        with self.assertRaises(Exception):
            summary.settlement_count = 99
        with self.assertRaises(TypeError):
            summary.rejection_counts["NEW_REASON"] = 1

    def test_field_order_matches_spec(self):
        self.assertEqual(
            list(CurrencyAuditSummary.__dataclass_fields__),
            [
                "currency",
                "settlement_count",
                "approved_count",
                "rejected_count",
                "recovery_count",
                "writeoff_count",
                "approved_risk_occupancy",
                "pool_allocated",
                "capital_allocated",
                "initial_bad_debt",
                "recovered_amount",
                "written_off_amount",
                "outstanding_bad_debt",
                "rejection_counts",
                "event_ids",
            ],
        )


if __name__ == "__main__":
    unittest.main()
