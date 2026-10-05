"""单币种只读审计核对快照的公开行为测试。

覆盖：事件分类计数、放行口径的风险占用与各层分配、首次坏账 / 回收冲减 /
存续坏账恒等式、原因码排序与零次省略、台账顺序 event_ids、币种隔离、
未知币种与空台账、币种校验、不可变结构，以及只读幂等（不改变序号、
事件、结果索引、风险组额度、坏账与回收状态）。
"""

import unittest
from dataclasses import FrozenInstanceError
from decimal import Decimal
from types import MappingProxyType

from vault_guard import (
    ClearingEngine,
    CurrencyAuditSummary,
    InvalidCurrencyError,
)

D = Decimal


def build_engine() -> ClearingEngine:
    """构造跨放行 / 拒绝 / 风险组 / 回收的混合台账。

    USD 事件顺序：

    - T1 放行：池内 40 分配 a 30 / b 10，b 首次坏账 20，风险占用 40。
    - T-EUR 放行（EUR，用于币种隔离）。
    - T2 放行：池内 10 + 资本 5 覆盖 c 的 25，c 首次坏账 10，风险占用 60。
    - T3 拒绝：余额不足 SETTLEMENT_EXCEEDS_AVAILABLE_BALANCE。
    - T4 拒绝：风险占用超限 RISK_OCCUPANCY_EXCEEDS_BASE_LIMIT。
    - 风险组批次 T5 拒绝：GROUP_LIMIT_EXCEEDED（组上限 0）。
    - R1 回收 USD 25：先冲 b 20，再冲 c 5。
    """
    engine = ClearingEngine()
    engine.process(
        "T1", "USD", D("100"), D("40"), D("0"), D("100"), D("0"),
        [("a", D("30")), ("b", D("30"))],
    )
    engine.process(
        "T-EUR", "EUR", D("100"), D("10"), D("0"), D("100"), D("0"),
        [("x", D("10"))],
    )
    engine.process(
        "T2", "USD", D("100"), D("10"), D("100"), D("100"), D("0.5"),
        [("c", D("25"))],
        supplementary_capital=D("5"),
    )
    engine.process(
        "T3", "USD", D("100"), D("200"), D("0"), D("1000"), D("0"),
        [("a", D("10"))],
    )
    engine.process(
        "T4", "USD", D("100"), D("10"), D("200"), D("100"), D("1"),
        [("a", D("10"))],
    )
    engine.process_risk_group_batch(
        "USD",
        D("1000"),
        {"G1": D("0")},
        [
            {
                "transaction_id": "T5",
                "risk_group_id": "G1",
                "settlement_amount": D("10"),
                "notional_exposure": D("0"),
                "base_limit": D("100"),
                "risk_factor": D("0"),
                "creditors": [("d", D("10"))],
            }
        ],
    )
    engine.process_recovery("R1", "USD", D("25"))
    return engine


class AuditReconciliationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = build_engine()
        self.summary = self.engine.audit_reconciliation("USD")

    def test_returns_immutable_currency_audit_summary(self):
        self.assertIsInstance(self.summary, CurrencyAuditSummary)
        with self.assertRaises(FrozenInstanceError):
            self.summary.settlement_count = 0  # type: ignore[misc]

    def test_event_counts_by_category(self):
        self.assertEqual(self.summary.currency, "USD")
        # T1 / T2 放行，T3 / T4 / T5 拒绝，R1 回收。
        self.assertEqual(self.summary.settlement_count, 5)
        self.assertEqual(self.summary.approved_count, 2)
        self.assertEqual(self.summary.rejected_count, 3)
        self.assertEqual(self.summary.recovery_count, 1)

    def test_approved_only_amounts(self):
        # 风险占用：T1=40，T2=10+100*0.5=60；拒绝三笔均不累计。
        self.assertEqual(self.summary.approved_risk_occupancy, D("100"))
        # 池内：T1 40（30+10）+ T2 10；资本：仅 T2 的 5。
        self.assertEqual(self.summary.pool_allocated, D("50"))
        self.assertEqual(self.summary.capital_allocated, D("5"))

    def test_bad_debt_and_recovery_reconciliation(self):
        # 首次坏账：b 20 + c 10。
        self.assertEqual(self.summary.initial_bad_debt, D("30"))
        # 回收冲减按明细求和：b 20 + c 5。
        self.assertEqual(self.summary.recovered_amount, D("25"))
        # 恒等式：首次坏账 - 回收冲减 = 存续坏账。
        self.assertEqual(
            self.summary.initial_bad_debt - self.summary.recovered_amount,
            self.summary.outstanding_bad_debt,
        )
        self.assertEqual(self.summary.outstanding_bad_debt, D("5"))
        # 与 outstanding_bad_debts(currency) 余额合计一致。
        ledger_total = sum(
            (item.balance for item in self.engine.outstanding_bad_debts("USD")),
            D("0"),
        )
        self.assertEqual(self.summary.outstanding_bad_debt, ledger_total)

    def test_amounts_are_decimal(self):
        for field in (
            "approved_risk_occupancy",
            "pool_allocated",
            "capital_allocated",
            "initial_bad_debt",
            "recovered_amount",
            "outstanding_bad_debt",
        ):
            self.assertIsInstance(getattr(self.summary, field), Decimal)

    def test_rejection_counts_sorted_and_zero_omitted(self):
        counts = self.summary.rejection_counts
        self.assertIsInstance(counts, MappingProxyType)
        self.assertEqual(
            dict(counts),
            {
                "GROUP_LIMIT_EXCEEDED": 1,
                "RISK_OCCUPANCY_EXCEEDS_BASE_LIMIT": 1,
                "SETTLEMENT_EXCEEDS_AVAILABLE_BALANCE": 1,
            },
        )
        # 按原因码字典序排列。
        self.assertEqual(
            list(counts),
            [
                "GROUP_LIMIT_EXCEEDED",
                "RISK_OCCUPANCY_EXCEEDS_BASE_LIMIT",
                "SETTLEMENT_EXCEEDS_AVAILABLE_BALANCE",
            ],
        )
        with self.assertRaises(TypeError):
            counts["GROUP_LIMIT_EXCEEDED"] = 2  # type: ignore[index]

    def test_rejection_counts_accumulates_repeated_reasons(self):
        engine = ClearingEngine()
        for tid in ("A1", "A2"):
            engine.process(
                tid, "USD", D("5"), D("50"), D("0"), D("100"), D("0"),
                [("a", D("10"))],
            )
        summary = engine.audit_reconciliation("USD")
        self.assertEqual(
            dict(summary.rejection_counts),
            {"SETTLEMENT_EXCEEDS_AVAILABLE_BALANCE": 2},
        )

    def test_event_ids_keep_ledger_order(self):
        self.assertEqual(
            self.summary.event_ids,
            (
                "EVT-T1-approved",
                "EVT-T2-approved",
                "EVT-T3-rejected",
                "EVT-T4-rejected",
                "EVT-T5-rejected",
                "EVT-R1-recovery",
            ),
        )
        self.assertIsInstance(self.summary.event_ids, tuple)

    def test_currency_isolation(self):
        eur = self.engine.audit_reconciliation("EUR")
        self.assertEqual(eur.currency, "EUR")
        self.assertEqual(eur.settlement_count, 1)
        self.assertEqual(eur.approved_count, 1)
        self.assertEqual(eur.rejected_count, 0)
        self.assertEqual(eur.recovery_count, 0)
        self.assertEqual(eur.approved_risk_occupancy, D("10"))
        self.assertEqual(eur.pool_allocated, D("10"))
        self.assertEqual(eur.capital_allocated, D("0"))
        self.assertEqual(eur.initial_bad_debt, D("0"))
        self.assertEqual(eur.recovered_amount, D("0"))
        self.assertEqual(eur.outstanding_bad_debt, D("0"))
        self.assertEqual(dict(eur.rejection_counts), {})
        self.assertEqual(eur.event_ids, ("EVT-T-EUR-approved",))

    def test_whitespace_is_stripped_on_match(self):
        padded = self.engine.audit_reconciliation("  USD ")
        self.assertEqual(padded.currency, "USD")
        self.assertEqual(padded, self.summary)

    def test_unknown_currency_is_all_zero(self):
        summary = self.engine.audit_reconciliation("JPY")
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
        self.assertEqual(dict(summary.rejection_counts), {})
        self.assertEqual(summary.event_ids, ())

    def test_empty_ledger_result_is_deterministic(self):
        engine = ClearingEngine()
        first = engine.audit_reconciliation("USD")
        second = engine.audit_reconciliation("USD")
        self.assertEqual(first, second)
        self.assertEqual(first.settlement_count, 0)
        self.assertEqual(first.event_ids, ())
        self.assertEqual(dict(first.rejection_counts), {})

    def test_invalid_currency_raises(self):
        for bad in (None, 42, D("7"), "", "   ", "\t\n"):
            with self.subTest(bad=bad):
                with self.assertRaises(InvalidCurrencyError):
                    self.engine.audit_reconciliation(bad)  # type: ignore[arg-type]

    def test_zero_amount_recovery_event_counted(self):
        engine = ClearingEngine()
        engine.process(
            "Z1", "USD", D("100"), D("10"), D("0"), D("100"), D("0"),
            [("a", D("30"))],
        )
        engine.process_recovery("RZ", "USD", D("0"))
        summary = engine.audit_reconciliation("USD")
        self.assertEqual(summary.recovery_count, 1)
        self.assertEqual(summary.recovered_amount, D("0"))
        self.assertEqual(summary.event_ids[-1], "EVT-RZ-recovery")
        # 无冲减明细：首次坏账全部存续。
        self.assertEqual(summary.initial_bad_debt, D("20"))
        self.assertEqual(summary.outstanding_bad_debt, D("20"))

    def test_multicurrency_batch_events_counted_per_currency(self):
        engine = ClearingEngine()
        engine.process_multicurrency_batch(
            {"GBP": D("100"), "USD": D("100")},
            [
                {
                    "transaction_id": "M1",
                    "currency": "GBP",
                    "settlement_amount": D("40"),
                    "notional_exposure": D("0"),
                    "base_limit": D("100"),
                    "risk_factor": D("0"),
                    "creditors": [("g", D("60"))],
                },
                {
                    "transaction_id": "M2",
                    "currency": "USD",
                    "settlement_amount": D("200"),
                    "notional_exposure": D("0"),
                    "base_limit": D("100"),
                    "risk_factor": D("0"),
                    "creditors": [("u", D("10"))],
                },
            ],
        )
        gbp = engine.audit_reconciliation("GBP")
        self.assertEqual(gbp.settlement_count, 1)
        self.assertEqual(gbp.approved_count, 1)
        self.assertEqual(gbp.pool_allocated, D("40"))
        self.assertEqual(gbp.initial_bad_debt, D("20"))
        self.assertEqual(gbp.outstanding_bad_debt, D("20"))
        usd = engine.audit_reconciliation("USD")
        self.assertEqual(usd.settlement_count, 1)
        self.assertEqual(usd.rejected_count, 1)
        self.assertEqual(usd.approved_count, 0)
        self.assertEqual(
            dict(usd.rejection_counts),
            {"SETTLEMENT_EXCEEDS_AVAILABLE_BALANCE": 1},
        )


class AuditReconciliationReadOnlyTests(unittest.TestCase):
    def test_repeated_calls_do_not_change_state(self):
        engine = build_engine()

        events_before = engine.events()
        sequences_before = tuple(event.sequence for event in events_before)
        results_before = {
            event.transaction_id: engine.result_of(event.transaction_id)
            for event in events_before
        }
        recovery_before = engine.recovery_of("R1")
        debts_before = engine.outstanding_bad_debts("USD")

        first = engine.audit_reconciliation("USD")
        second = engine.audit_reconciliation("  USD ")
        third = engine.audit_reconciliation("EUR")
        self.assertEqual(first, second)

        events_after = engine.events()
        self.assertEqual(events_after, events_before)
        self.assertEqual(
            tuple(event.sequence for event in events_after), sequences_before
        )
        for transaction_id, result in results_before.items():
            self.assertIs(engine.result_of(transaction_id), result)
        self.assertIs(engine.recovery_of("R1"), recovery_before)
        self.assertEqual(engine.outstanding_bad_debts("USD"), debts_before)

    def test_invalid_currency_does_not_change_state(self):
        engine = build_engine()
        events_before = engine.events()
        for bad in (None, 1, "", " "):
            with self.assertRaises(InvalidCurrencyError):
                engine.audit_reconciliation(bad)  # type: ignore[arg-type]
        self.assertEqual(engine.events(), events_before)
        self.assertEqual(
            engine.audit_reconciliation("USD").event_ids,
            (
                "EVT-T1-approved",
                "EVT-T2-approved",
                "EVT-T3-rejected",
                "EVT-T4-rejected",
                "EVT-T5-rejected",
                "EVT-R1-recovery",
            ),
        )


if __name__ == "__main__":
    unittest.main()
