"""Vault Guard 公开行为测试。

覆盖：限额校验、清算瀑布优先级、补充资本补足、坏账归因一致性、
拒绝路径余额不变、六类输入异常、审计台账追加/幂等/只读查询与确定性。
"""

import unittest
from decimal import Decimal

from vault_guard import (
    ClearingEngine,
    Creditor,
    DuplicateTransactionError,
    EmptyCreditorListError,
    InvalidCurrencyError,
    InvalidRiskFactorError,
    MixedCurrencyError,
    process_settlement,
)

D = Decimal


def base_creditors():
    return [
        ("senior", D("60")),
        ("mezzanine", D("30")),
        ("equity", D("10")),
    ]


class SuccessfulSettlementTests(unittest.TestCase):
    def test_full_coverage_by_pool(self):
        engine = ClearingEngine()
        result = engine.process(
            transaction_id="T1",
            currency="USD",
            pool_balance=D("100"),
            settlement_amount=D("100"),
            notional_exposure=D("0"),
            base_limit=D("100"),
            risk_factor=D("0"),
            creditors=base_creditors(),
        )
        self.assertTrue(result.approved)
        self.assertEqual(result.pool_allocations, (D("60"), D("30"), D("10")))
        self.assertEqual(
            result.capital_allocations, (D("0"), D("0"), D("0"))
        )
        self.assertEqual(result.uncovered_bad_debt, D("0"))
        # 池内 100 全部分配，可用余额扣减为 0。
        self.assertEqual(result.validated_available_balance, D("0"))

    def test_waterfall_priority_partial_pool(self):
        engine = ClearingEngine()
        result = engine.process(
            "T2", "USD", D("100"), D("50"), D("0"), D("100"), D("0"),
            base_creditors(),
        )
        self.assertTrue(result.approved)
        # 高优先级先受偿：senior 拿满 60 但资金只有 50。
        self.assertEqual(result.pool_allocations, (D("50"), D("0"), D("0")))
        self.assertEqual(result.validated_available_balance, D("50"))

    def test_supplementary_capital_fills_in_order(self):
        engine = ClearingEngine()
        result = engine.process(
            "T3", "USD", D("100"), D("50"), D("0"), D("200"), D("0"),
            base_creditors(),
            supplementary_capital=D("40"),
        )
        self.assertTrue(result.approved)
        self.assertEqual(result.pool_allocations, (D("50"), D("0"), D("0")))
        # senior 剩余 10 先补，其次 mezzanine 30；资本恰好用完。
        self.assertEqual(result.capital_allocations, (D("10"), D("30"), D("0")))
        # equity 10 未受偿 -> 坏账。
        self.assertEqual(result.uncovered_bad_debt, D("10"))
        attribution = {a.creditor: a for a in result.attributions}
        self.assertEqual(attribution["senior"].bad_debt, D("0"))
        self.assertEqual(attribution["mezzanine"].bad_debt, D("0"))
        self.assertEqual(attribution["equity"].bad_debt, D("10"))
        # 资本不扣减资金池余额。
        self.assertEqual(result.validated_available_balance, D("50"))

    def test_attribution_totals_match_uncovered(self):
        engine = ClearingEngine()
        result = engine.process(
            "T4", "USD", D("100"), D("20"), D("0"), D("200"), D("0"),
            base_creditors(),
            supplementary_capital=D("15"),
        )
        summed = sum((a.bad_debt for a in result.attributions), D("0"))
        self.assertEqual(summed, result.uncovered_bad_debt)
        self.assertEqual(result.uncovered_bad_debt, D("65"))
        for a in result.attributions:
            self.assertEqual(
                a.pool_allocation + a.capital_allocation + a.bad_debt,
                a.claim_amount,
            )

    def test_risk_occupancy_boundary_accepted(self):
        # 风险占用 = 40 + 100 * 0.6 = 100，恰好等于限额，放行。
        engine = ClearingEngine()
        result = engine.process(
            "T5", "USD", D("100"), D("40"), D("100"), D("100"), D("0.6"),
            [("c", D("40"))],
        )
        self.assertTrue(result.approved)
        self.assertEqual(result.risk_occupancy, D("100"))

    def test_zero_risk_factor_and_zero_capital(self):
        engine = ClearingEngine()
        result = engine.process(
            "T6", "USD", D("100"), D("30"), D("500"), D("30"), D("0"),
            [("c", D("30"))],
        )
        self.assertTrue(result.approved)
        self.assertEqual(result.risk_occupancy, D("30"))
        self.assertEqual(result.total_capital_allocated, D("0"))

    def test_complete_uncovered_with_zero_amount(self):
        # 拟清算 0、资本 0：债权全部成为坏账，但请求本身可放行。
        engine = ClearingEngine()
        result = engine.process(
            "T7", "USD", D("0"), D("0"), D("0"), D("0"), D("0"),
            base_creditors(),
        )
        self.assertTrue(result.approved)
        self.assertEqual(result.pool_allocations, (D("0"), D("0"), D("0")))
        self.assertEqual(result.uncovered_bad_debt, D("100"))
        self.assertEqual(result.validated_available_balance, D("0"))

    def test_allocation_never_exceeds_claim_or_funds(self):
        engine = ClearingEngine()
        result = engine.process(
            "T8", "USD", D("1000"), D("1000"), D("0"), D("1000"), D("0"),
            base_creditors(),  # 债权合计仅 100
        )
        self.assertEqual(result.total_pool_allocated, D("100"))
        self.assertEqual(result.validated_available_balance, D("900"))


class RejectionTests(unittest.TestCase):
    def test_reject_when_settlement_exceeds_balance(self):
        engine = ClearingEngine()
        result = engine.process(
            "R1", "USD", D("40"), D("50"), D("0"), D("1000"), D("0"),
            base_creditors(),
        )
        self.assertFalse(result.approved)
        self.assertEqual(
            result.rejection_reason, "SETTLEMENT_EXCEEDS_AVAILABLE_BALANCE"
        )
        self.assertEqual(result.validated_available_balance, D("40"))
        self.assertEqual(result.total_pool_allocated, D("0"))
        self.assertEqual(result.total_capital_allocated, D("0"))
        self.assertEqual(result.uncovered_bad_debt, D("0"))
        self.assertTrue(all(a.bad_debt == D("0") for a in result.attributions))

    def test_reject_when_risk_occupancy_exceeds_limit(self):
        engine = ClearingEngine()
        result = engine.process(
            "R2", "USD", D("100"), D("40"), D("100"), D("90"), D("0.6"),
            [("c", D("40"))],
        )
        self.assertFalse(result.approved)
        self.assertEqual(
            result.rejection_reason, "RISK_OCCUPANCY_EXCEEDS_BASE_LIMIT"
        )
        self.assertEqual(result.risk_occupancy, D("100"))
        self.assertEqual(result.validated_available_balance, D("100"))
        self.assertEqual(result.total_pool_allocated, D("0"))

    def test_zero_limit_only_allows_zero_occupancy(self):
        engine = ClearingEngine()
        rejected = engine.process(
            "R3", "USD", D("100"), D("1"), D("0"), D("0"), D("0"),
            [("c", D("1"))],
        )
        self.assertFalse(rejected.approved)
        self.assertEqual(rejected.validated_available_balance, D("100"))

        approved = engine.process(
            "R4", "USD", D("100"), D("0"), D("0"), D("0"), D("0"),
            [("c", D("5"))],
        )
        self.assertTrue(approved.approved)

    def test_rejection_produces_single_audit_event(self):
        engine = ClearingEngine()
        result = engine.process(
            "R5", "USD", D("10"), D("20"), D("0"), D("1000"), D("0"),
            base_creditors(),
        )
        self.assertFalse(result.approved)
        self.assertEqual(len(engine.audit_log), 1)
        event = engine.audit_log[0]
        self.assertFalse(event.approved)
        self.assertEqual(event.event_id, result.event_id)
        self.assertEqual(event.uncovered_bad_debt, D("0"))


class ValidationErrorTests(unittest.TestCase):
    def _kwargs(self, **overrides):
        kwargs = dict(
            transaction_id="V1",
            currency="USD",
            pool_balance=D("100"),
            settlement_amount=D("50"),
            notional_exposure=D("0"),
            base_limit=D("100"),
            risk_factor=D("0"),
            creditors=base_creditors(),
        )
        kwargs.update(overrides)
        return kwargs

    def test_negative_amounts_raise_value_error(self):
        for field, value in [
            ("pool_balance", D("-1")),
            ("settlement_amount", D("-0.01")),
            ("notional_exposure", D("-5")),
            ("base_limit", D("-1")),
            ("supplementary_capital", D("-1")),
        ]:
            with self.subTest(field=field):
                with self.assertRaises(ValueError):
                    ClearingEngine().process(**self._kwargs(**{field: value}))

    def test_negative_creditor_claim_raises_value_error(self):
        with self.assertRaises(ValueError):
            ClearingEngine().process(
                **self._kwargs(creditors=[("bad", D("-1"))])
            )

    def test_float_input_normalized_exactly(self):
        # float 经字符串精确转换：0.6 不携带二进制尾数误差。
        engine = ClearingEngine()
        result = engine.process(
            **self._kwargs(
                transaction_id="VF1",
                pool_balance=100.0,
                settlement_amount=40,
                notional_exposure=100,
                base_limit=100,
                risk_factor=0.6,
            )
        )
        self.assertTrue(result.approved)
        self.assertEqual(result.risk_occupancy, D("100"))

    def test_non_numeric_input_rejected(self):
        with self.assertRaises(ValueError):
            ClearingEngine().process(**self._kwargs(pool_balance="100"))

    def test_nan_and_infinity_rejected(self):
        for bad in (
            float("nan"),
            float("inf"),
            D("NaN"),
            D("Infinity"),
        ):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    ClearingEngine().process(
                        **self._kwargs(pool_balance=bad)
                    )

    def test_duplicate_transaction_id(self):
        engine = ClearingEngine()
        engine.process(**self._kwargs())
        with self.assertRaises(DuplicateTransactionError):
            engine.process(**self._kwargs())
        # 重复请求不产生第二条事件，也不重复入账。
        self.assertEqual(len(engine.audit_log), 1)
        self.assertEqual(engine.audit_log[0].sequence, 1)

    def test_missing_currency(self):
        for bad in (None, "", "   "):
            with self.subTest(bad=bad):
                with self.assertRaises(InvalidCurrencyError):
                    ClearingEngine().process(**self._kwargs(currency=bad))

    def test_empty_creditor_list(self):
        for bad in ([], None):
            with self.subTest(bad=bad):
                with self.assertRaises(EmptyCreditorListError):
                    ClearingEngine().process(**self._kwargs(creditors=bad))

    def test_risk_factor_bounds(self):
        for bad in (D("-0.01"), D("1.01"), D("2")):
            with self.subTest(bad=bad):
                with self.assertRaises(InvalidRiskFactorError):
                    ClearingEngine().process(**self._kwargs(risk_factor=bad))
        # 边界值 0 与 1 合法。
        for boundary in (D("0"), D("1")):
            engine = ClearingEngine()
            result = engine.process(
                **self._kwargs(
                    transaction_id=f"B{boundary}",
                    risk_factor=boundary,
                    base_limit=D("50"),
                    notional_exposure=D("0"),
                )
            )
            self.assertTrue(result.approved)

    def test_mixed_currency_rejected(self):
        with self.assertRaises(MixedCurrencyError):
            ClearingEngine().process(
                **self._kwargs(
                    creditors=[("foreign", D("10"), "EUR")]
                )
            )

    def test_matching_creditor_currency_accepted(self):
        engine = ClearingEngine()
        result = engine.process(
            **self._kwargs(creditors=[("local", D("10"), "USD")])
        )
        self.assertTrue(result.approved)

    def test_invalid_input_leaves_no_half_state(self):
        engine = ClearingEngine()
        with self.assertRaises(ValueError):
            engine.process(**self._kwargs(pool_balance=D("-1")))
        # 同一流水号未被占用，可重新提交并成功。
        result = engine.process(**self._kwargs())
        self.assertTrue(result.approved)
        self.assertEqual(len(engine.audit_log), 1)
        self.assertIsNone(engine.get_event("does-not-exist"))


class AuditLogTests(unittest.TestCase):
    def test_event_id_unique_per_request_and_stable(self):
        engine = ClearingEngine()
        r1 = engine.process(
            "A1", "USD", D("100"), D("10"), D("0"), D("100"), D("0"),
            base_creditors(),
        )
        r2 = engine.process(
            "A2", "USD", D("100"), D("10"), D("0"), D("100"), D("0"),
            base_creditors(),
        )
        self.assertNotEqual(r1.event_id, r2.event_id)
        self.assertEqual(r1.event_id, engine.get_event("A1").event_id)
        event_ids = {e.event_id for e in engine.audit_log}
        self.assertEqual(len(event_ids), 2)

    def test_audit_event_contains_required_fields(self):
        engine = ClearingEngine()
        result = engine.process(
            "A3", "USD", D("100"), D("50"), D("0"), D("100"), D("0"),
            base_creditors(),
            supplementary_capital=D("20"),
        )
        event = engine.get_event("A3")
        self.assertEqual(event.sequence, 1)
        self.assertEqual(event.transaction_id, "A3")
        self.assertEqual(event.currency, "USD")
        self.assertEqual(event.validation_result, "APPROVED")
        self.assertEqual(event.input_summary["transaction_id"], "A3")
        self.assertEqual(event.risk_occupancy, result.risk_occupancy)
        self.assertEqual(
            tuple(event.pool_allocations),
            tuple(zip(result.creditors, result.pool_allocations, strict=True)),
        )
        self.assertEqual(event.uncovered_bad_debt, result.uncovered_bad_debt)
        self.assertEqual(
            event.validated_available_balance,
            result.validated_available_balance,
        )

    def test_sequence_monotonic_append_only(self):
        engine = ClearingEngine()
        for i in range(3):
            engine.process(
                f"S{i}", "USD", D("100"), D("10"), D("0"), D("100"), D("0"),
                base_creditors(),
            )
        self.assertEqual([e.sequence for e in engine.audit_log], [1, 2, 3])

    def test_read_only_queries_do_not_settle(self):
        engine = ClearingEngine()
        self.assertIsNone(engine.get_event("X"))
        self.assertIsNone(engine.result_of("X"))
        self.assertEqual(engine.events(), ())
        self.assertFalse(engine.has_transaction("X"))
        self.assertEqual(len(engine.audit_log), 0)


class CreditorFormatTests(unittest.TestCase):
    def test_dict_and_object_formats(self):
        expected = [("a", D("20")), ("b", D("30"))]
        r1 = process_settlement(
            "F1", "USD", D("100"), D("50"), D("0"), D("100"), D("0"), expected
        )
        r2 = process_settlement(
            "F2", "USD", D("100"), D("50"), D("0"), D("100"), D("0"),
            [{"name": "a", "amount": D("20")},
             Creditor(name="b", amount=D("30"))],
        )
        self.assertEqual(r1.pool_allocations, r2.pool_allocations)
        self.assertEqual(r1.creditors, r2.creditors)


class DeterminismTests(unittest.TestCase):
    def test_same_input_same_output_structure(self):
        def run(tid):
            return ClearingEngine().process(
                tid, "USD", D("100"), D("50"), D("20"), D("100"), D("0.5"),
                base_creditors(),
                supplementary_capital=D("30"),
            )

        r1, r2 = run("D1"), run("D2")
        for field in (
            "approved",
            "validated_available_balance",
            "pool_allocations",
            "capital_allocations",
            "uncovered_bad_debt",
            "risk_occupancy",
            "rejection_reason",
        ):
            self.assertEqual(
                getattr(r1, field), getattr(r2, field), field
            )
        self.assertEqual(
            [a.bad_debt for a in r1.attributions],
            [a.bad_debt for a in r2.attributions],
        )

    def test_module_level_entry_point(self):
        result = process_settlement(
            "M1", "USD", D("100"), D("60"), D("0"), D("100"), D("0"),
            base_creditors(),
        )
        self.assertTrue(result.approved)
        self.assertEqual(result.pool_allocations[0], D("60"))


if __name__ == "__main__":
    unittest.main()
