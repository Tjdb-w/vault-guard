"""Vault Guard 公开行为测试。"""

from __future__ import annotations

import unittest
from decimal import Decimal

from vault_guard import (
    AuditLedger,
    ClearanceEngine,
    Creditor,
    DuplicateTransactionError,
    EmptyCreditorListError,
    InvalidCurrencyError,
    InvalidRiskFactorError,
    MixedCurrencyError,
    REJECT_INSUFFICIENT_BALANCE,
    REJECT_RISK_LIMIT_EXCEEDED,
    SettlementRequest,
    process_settlement,
)

D = Decimal


def request(
    transaction_id="TX-1",
    currency="USD",
    pool_balance=D("1000"),
    settlement_amount=D("100"),
    notional_exposure=D("0"),
    base_limit=D("1000"),
    risk_factor=D("0"),
    creditors=None,
    supplementary_capital=D("0"),
):
    if creditors is None:
        creditors = [Creditor("A", D("60"), "USD")]
    return SettlementRequest(
        transaction_id=transaction_id,
        currency=currency,
        pool_balance=pool_balance,
        settlement_amount=settlement_amount,
        notional_exposure=notional_exposure,
        base_limit=base_limit,
        risk_factor=risk_factor,
        creditors=creditors,
        supplementary_capital=supplementary_capital,
    )


class FullCoverageTests(unittest.TestCase):
    def test_pool_covers_all_in_priority_order(self):
        creditors = [
            Creditor("senior", D("60"), "USD"),
            Creditor("mezz", D("30"), "USD"),
            Creditor("junior", D("10"), "USD"),
        ]
        result = process_settlement(
            "TX-1", "USD", D("1000"), D("100"), D("0"), D("1000"), D("0"),
            creditors,
        )
        self.assertTrue(result.approved)
        self.assertEqual(result.verified_balance, D("900"))
        self.assertEqual(result.total_pool_allocated, D("100"))
        self.assertEqual(result.total_capital_allocated, D("0"))
        self.assertEqual(result.uncovered_bad_debt, D("0"))
        names = [a.name for a in result.attributions]
        self.assertEqual(names, ["senior", "mezz", "junior"])
        for a, amount in zip(result.attributions, [D("60"), D("30"), D("10")]):
            self.assertEqual(a.pool_allocation, amount)
            self.assertEqual(a.capital_allocation, D("0"))
            self.assertEqual(a.bad_debt, D("0"))
            # 归因守恒
            self.assertEqual(
                a.pool_allocation + a.capital_allocation + a.bad_debt,
                a.claim_amount,
            )

    def test_partial_pool_then_capital_then_bad_debt(self):
        creditors = [
            Creditor("C1", D("50"), "USD"),
            Creditor("C2", D("50"), "USD"),
            Creditor("C3", D("50"), "USD"),
        ]
        # 池内 60：C1 全额、C2 受偿 10；资本 30：C2 补足 40 不足，受偿 30；
        # C2 坏账 10，C3 坏账 50。
        result = process_settlement(
            "TX-2", "USD", D("1000"), D("60"), D("0"), D("1000"), D("0"),
            creditors, supplementary_capital=D("30"),
        )
        self.assertTrue(result.approved)
        a1, a2, a3 = result.attributions
        self.assertEqual(
            (a1.pool_allocation, a1.capital_allocation, a1.bad_debt),
            (D("50"), D("0"), D("0")),
        )
        self.assertEqual(
            (a2.pool_allocation, a2.capital_allocation, a2.bad_debt),
            (D("10"), D("30"), D("10")),
        )
        self.assertEqual(
            (a3.pool_allocation, a3.capital_allocation, a3.bad_debt),
            (D("0"), D("0"), D("50")),
        )
        self.assertEqual(result.total_pool_allocated, D("60"))
        self.assertEqual(result.total_capital_allocated, D("30"))
        self.assertEqual(result.uncovered_bad_debt, D("60"))
        self.assertEqual(
            sum(a.bad_debt for a in result.attributions),
            result.uncovered_bad_debt,
        )
        self.assertEqual(result.verified_balance, D("940"))

    def test_priority_means_higher_creditor_never_loses_to_lower(self):
        # 资金极度稀缺：第一顺位应被全额清偿，后续全部坏账。
        creditors = [
            Creditor("first", D("100"), "USD"),
            Creditor("second", D("100"), "USD"),
        ]
        result = process_settlement(
            "TX-3", "USD", D("1000"), D("100"), D("0"), D("1000"), D("0"),
            creditors, supplementary_capital=D("0"),
        )
        a1, a2 = result.attributions
        self.assertEqual(a1.pool_allocation, D("100"))
        self.assertEqual(a1.bad_debt, D("0"))
        self.assertEqual(a2.pool_allocation, D("0"))
        self.assertEqual(a2.bad_debt, D("100"))

    def test_capital_does_not_displace_pool_priority(self):
        # 池内仅够覆盖第一顺位的一半；资本不得让第二顺位插队。
        creditors = [
            Creditor("first", D("100"), "USD"),
            Creditor("second", D("100"), "USD"),
        ]
        result = process_settlement(
            "TX-4", "USD", D("1000"), D("50"), D("0"), D("1000"), D("0"),
            creditors, supplementary_capital=D("200"),
        )
        a1, a2 = result.attributions
        self.assertEqual(
            (a1.pool_allocation, a1.capital_allocation, a1.bad_debt),
            (D("50"), D("50"), D("0")),
        )
        self.assertEqual(
            (a2.pool_allocation, a2.capital_allocation, a2.bad_debt),
            (D("0"), D("100"), D("0")),
        )
        self.assertEqual(result.uncovered_bad_debt, D("0"))


class RiskAndLimitTests(unittest.TestCase):
    def test_risk_usage_formula(self):
        # 风险占用 = 拟清算金额 + 名义敞口 * 风险系数
        result = process_settlement(
            "TX-5", "USD", D("1000"), D("100"), D("200"), D("150"),
            D("0.25"), [Creditor("A", D("100"), "USD")],
        )
        self.assertTrue(result.approved)
        self.assertEqual(result.risk_usage, D("150.00"))

    def test_reject_when_settlement_exceeds_pool_balance(self):
        engine = ClearanceEngine()
        result = engine.submit(request(
            transaction_id="TX-6",
            pool_balance=D("50"),
            settlement_amount=D("100"),
            base_limit=D("10000"),
        ))
        self.assertFalse(result.approved)
        self.assertEqual(result.rejection_reason, REJECT_INSUFFICIENT_BALANCE)
        self.assertEqual(result.verified_balance, D("50"))
        self.assertEqual(result.total_pool_allocated, D("0"))
        self.assertEqual(result.total_capital_allocated, D("0"))
        self.assertEqual(result.uncovered_bad_debt, D("0"))
        self.assertTrue(all(a.bad_debt == 0 for a in result.attributions))
        # 恰好一条拒绝审计
        self.assertEqual(len(engine.ledger), 1)
        event = engine.ledger.latest_event()
        self.assertFalse(event.approved)
        self.assertEqual(event.verified_balance, D("50"))
        self.assertEqual(event.event_id, result.audit_event_id)

    def test_reject_when_risk_usage_exceeds_limit(self):
        # 余额充足：100 <= 1000；风险占用 100 + 200*0.5 = 200 > 150。
        result = process_settlement(
            "TX-7", "USD", D("1000"), D("100"), D("200"), D("150"),
            D("0.5"), [Creditor("A", D("100"), "USD")],
        )
        self.assertFalse(result.approved)
        self.assertEqual(result.rejection_reason, REJECT_RISK_LIMIT_EXCEEDED)
        self.assertEqual(result.verified_balance, D("1000"))
        self.assertEqual(result.risk_usage, D("200.0"))

    def test_balance_check_takes_precedence_over_risk_check(self):
        result = process_settlement(
            "TX-8", "USD", D("50"), D("100"), D("500"), D("1"),
            D("1"), [Creditor("A", D("100"), "USD")],
        )
        self.assertFalse(result.approved)
        self.assertEqual(result.rejection_reason, REJECT_INSUFFICIENT_BALANCE)

    def test_zero_base_limit_allows_only_zero_risk_usage(self):
        # 拟清算为 0、敞口 0、系数 0 => 风险占用 0，通过（无可分配资金）。
        creditors = [Creditor("A", D("100"), "USD")]
        ok = process_settlement(
            "TX-9", "USD", D("1000"), D("0"), D("0"), D("0"), D("0"),
            creditors,
        )
        self.assertTrue(ok.approved)
        self.assertEqual(ok.risk_usage, D("0"))
        self.assertEqual(ok.uncovered_bad_debt, D("100"))

        blocked = process_settlement(
            "TX-10", "USD", D("1000"), D("0"), D("100"), D("0"),
            D("0.01"), [Creditor("A", D("0"), "USD")],
        )
        self.assertFalse(blocked.approved)
        self.assertEqual(blocked.rejection_reason, REJECT_RISK_LIMIT_EXCEEDED)

    def test_risk_factor_boundaries_are_valid(self):
        for factor in (D("0"), D("1")):
            result = process_settlement(
                f"TX-B{factor}", "USD", D("1000"), D("0"), D("100"),
                D("100"), factor, [Creditor("A", D("0"), "USD")],
            )
            self.assertTrue(result.approved)

    def test_zero_settlement_with_claims_is_full_bad_debt(self):
        result = process_settlement(
            "TX-11", "USD", D("1000"), D("0"), D("0"), D("1000"), D("0"),
            [Creditor("A", D("30"), "USD"), Creditor("B", D("20"), "USD")],
        )
        self.assertTrue(result.approved)
        self.assertEqual(result.verified_balance, D("1000"))
        self.assertEqual(result.uncovered_bad_debt, D("50"))

    def test_claims_smaller_than_settlement_leaves_residual_pool(self):
        # 拟清算 100，但债权合计只有 60：剩余 40 留在资金池。
        result = process_settlement(
            "TX-12", "USD", D("1000"), D("100"), D("0"), D("1000"), D("0"),
            [Creditor("A", D("60"), "USD")],
        )
        self.assertTrue(result.approved)
        self.assertEqual(result.total_pool_allocated, D("60"))
        self.assertEqual(result.verified_balance, D("940"))


class ValidationTests(unittest.TestCase):
    def setUp(self):
        self.engine = ClearanceEngine()

    def assert_no_audit_side_effects(self):
        self.assertEqual(len(self.engine.ledger), 0)

    def test_negative_amounts_raise_value_error(self):
        with self.assertRaises(ValueError):
            self.engine.submit(request(settlement_amount=D("-1")))
        with self.assertRaises(ValueError):
            self.engine.submit(request(pool_balance=D("-0.01")))
        with self.assertRaises(ValueError):
            self.engine.submit(request(notional_exposure=D("-1")))
        with self.assertRaises(ValueError):
            self.engine.submit(request(base_limit=D("-1")))
        with self.assertRaises(ValueError):
            self.engine.submit(request(supplementary_capital=D("-1")))
        with self.assertRaises(ValueError):
            self.engine.submit(request(
                creditors=[Creditor("A", D("-1"), "USD")]
            ))
        self.assert_no_audit_side_effects()

    def test_duplicate_transaction_id_raises(self):
        self.engine.submit(request(transaction_id="DUP"))
        with self.assertRaises(DuplicateTransactionError):
            self.engine.submit(request(transaction_id="DUP"))
        # 只有第一次的事件，没有重复入账。
        self.assertEqual(len(self.engine.ledger), 1)

    def test_duplicate_id_checked_even_second_request_invalid(self):
        self.engine.submit(request(transaction_id="DUP2",
                                   creditors=[Creditor("A", D("1"), "USD")]))
        with self.assertRaises(EmptyCreditorListError):
            self.engine.submit(request(transaction_id="DUP2", creditors=[]))

    def test_rejected_request_still_registers_id(self):
        self.engine.submit(request(
            transaction_id="DUP3", pool_balance=D("1"),
            settlement_amount=D("2"),
        ))
        with self.assertRaises(DuplicateTransactionError):
            self.engine.submit(request(
                transaction_id="DUP3", pool_balance=D("100"),
                settlement_amount=D("2"),
            ))

    def test_missing_currency_raises_invalid_currency(self):
        with self.assertRaises(InvalidCurrencyError):
            self.engine.submit(request(currency=""))
        with self.assertRaises(InvalidCurrencyError):
            self.engine.submit(request(currency="   "))
        with self.assertRaises(InvalidCurrencyError):
            self.engine.submit(request(
                creditors=[Creditor("A", D("1"), "")]
            ))
        self.assert_no_audit_side_effects()

    def test_empty_creditor_list_raises(self):
        with self.assertRaises(EmptyCreditorListError):
            self.engine.submit(request(creditors=[]))
        self.assert_no_audit_side_effects()

    def test_risk_factor_out_of_range_raises(self):
        with self.assertRaises(InvalidRiskFactorError):
            self.engine.submit(request(risk_factor=D("-0.01")))
        with self.assertRaises(InvalidRiskFactorError):
            self.engine.submit(request(risk_factor=D("1.01")))
        self.assert_no_audit_side_effects()

    def test_mixed_currency_raises(self):
        with self.assertRaises(MixedCurrencyError):
            self.engine.submit(request(
                currency="USD",
                creditors=[Creditor("A", D("1"), "EUR")],
            ))
        self.assert_no_audit_side_effects()

    def test_blank_transaction_id_raises_value_error(self):
        with self.assertRaises(ValueError):
            self.engine.submit(request(transaction_id=" "))
        self.assert_no_audit_side_effects()


class AuditLedgerTests(unittest.TestCase):
    def test_events_are_append_only_and_deterministic(self):
        engine = ClearanceEngine()
        r1 = engine.submit(request(transaction_id="A-1"))
        r2 = engine.submit(request(
            transaction_id="A-2",
            creditors=[Creditor("A", D("1"), "USD")],
        ))
        events = engine.events()
        self.assertEqual([e.sequence for e in events], [1, 2])
        self.assertEqual([e.event_id for e in events], ["EVT-000001", "EVT-000002"])
        self.assertEqual(r1.audit_event_id, "EVT-000001")
        self.assertEqual(r2.audit_event_id, "EVT-000002")
        self.assertIs(engine.get_event("EVT-000001"), events[0])
        self.assertIs(engine.get_by_transaction("A-2"), events[1])
        self.assertIsNone(engine.get_event("NOPE"))

    def test_audit_event_carries_full_record(self):
        engine = ClearanceEngine()
        creditors = [Creditor("senior", D("80"), "USD")]
        result = engine.submit(SettlementRequest(
            transaction_id="A-3", currency="USD", pool_balance=D("500"),
            settlement_amount=D("80"), notional_exposure=D("40"),
            base_limit=D("200"), risk_factor=D("0.5"),
            creditors=creditors, supplementary_capital=D("10"),
        ))
        event = engine.get_by_transaction("A-3")
        self.assertEqual(event.transaction_id, "A-3")
        self.assertEqual(event.input_summary.currency, "USD")
        self.assertEqual(event.input_summary.pool_balance, D("500"))
        self.assertEqual(event.input_summary.creditors, (("senior", D("80")),))
        self.assertTrue(event.approved)
        self.assertEqual(event.risk_usage, D("100.0"))
        self.assertEqual(event.verified_balance, D("420"))
        self.assertEqual(event.allocations[0].capital_allocation, D("0"))
        self.assertEqual(event.uncovered_bad_debt, D("0"))

    def test_readonly_queries_do_not_trigger_clearance(self):
        ledger = AuditLedger()
        engine = ClearanceEngine(ledger)
        self.assertEqual(engine.events(), ())
        self.assertIsNone(engine.ledger.latest_event())
        self.assertIsNone(engine.get_event("X"))
        self.assertIsNone(engine.get_by_transaction("X"))
        self.assertEqual(len(engine.ledger), 0)

    def test_shared_ledger_across_functional_entrypoints(self):
        engine = ClearanceEngine()
        process_settlement(
            "S-1", "USD", D("100"), D("10"), D("0"), D("100"), D("0"),
            [Creditor("A", D("10"), "USD")], engine=engine,
        )
        process_settlement(
            "S-2", "USD", D("100"), D("10"), D("0"), D("100"), D("0"),
            [Creditor("A", D("10"), "USD")], engine=engine,
        )
        self.assertEqual(len(engine.ledger), 2)


class DeterminismTests(unittest.TestCase):
    def test_same_input_yields_same_result(self):
        creditors = [Creditor("C1", D("50"), "USD"), Creditor("C2", D("80"), "USD")]
        kwargs = dict(
            currency="USD", pool_balance=D("1000"), settlement_amount=D("100"),
            notional_exposure=D("200"), base_limit=D("300"),
            risk_factor=D("0.25"), creditors=creditors,
            supplementary_capital=D("20"),
        )
        r1 = process_settlement("R-1", **kwargs)
        r2 = process_settlement("R-2", **kwargs)
        for field_name in (
            "approved", "verified_balance", "risk_usage",
            "total_pool_allocated", "total_capital_allocated",
            "uncovered_bad_debt",
        ):
            self.assertEqual(
                getattr(r1, field_name), getattr(r2, field_name), field_name
            )
        self.assertEqual(
            [(a.pool_allocation, a.capital_allocation, a.bad_debt)
             for a in r1.attributions],
            [(a.pool_allocation, a.capital_allocation, a.bad_debt)
             for a in r2.attributions],
        )


if __name__ == "__main__":
    unittest.main()
