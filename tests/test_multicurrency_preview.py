"""多币种批次提交前只读预演（preview_multicurrency_batch）公开行为测试。

覆盖：预演与正式提交逐笔一致（除 event_id）、各币种余额独立滚动、拒绝不动
本币种余额、validated_available_balances 覆盖全部期初币种（未使用币种
不变）、坏账归因不写入台账、只读且幂等（不生成事件 / 不占流水号 / 不改
余额、坏账台账与审计核对）、校验顺序与异常类型沿用
process_multicurrency_batch、异常不留状态、结果结构不可变。
"""

import unittest
from dataclasses import FrozenInstanceError
from decimal import Decimal
from types import MappingProxyType

from vault_guard import (
    ClearingEngine,
    DuplicateTransactionError,
    EmptyBatchError,
    EmptyCreditorListError,
    InvalidCurrencyError,
    InvalidRiskFactorError,
    MixedCurrencyError,
    MulticurrencyBatchPreviewResult,
    SettlementPreview,
)

D = Decimal


def req(tid, currency, amount, exposure="0", limit="1000", factor="0",
        creditors=(("senior", "60"), ("equity", "40")), capital="0"):
    normalized = []
    for item in creditors:
        if len(item) == 2:
            name, claim = item
            normalized.append((name, D(claim)))
        else:
            name, claim, ccy = item
            normalized.append((name, D(claim), ccy))
    return {
        "transaction_id": tid,
        "currency": currency,
        "settlement_amount": D(amount),
        "notional_exposure": D(exposure),
        "base_limit": D(limit),
        "risk_factor": D(factor),
        "creditors": normalized,
        "supplementary_capital": D(capital),
    }


def preview_fields(item):
    """去掉 event_id 后的逐字段对比字典（SettlementPreview 无该字段）。"""
    return {
        "transaction_id": item.transaction_id,
        "approved": item.approved,
        "validated_available_balance": item.validated_available_balance,
        "creditors": item.creditors,
        "pool_allocations": item.pool_allocations,
        "capital_allocations": item.capital_allocations,
        "attributions": item.attributions,
        "uncovered_bad_debt": item.uncovered_bad_debt,
        "risk_occupancy": item.risk_occupancy,
        "rejection_reason": item.rejection_reason,
        "group_used_after": item.group_used_after,
    }


class PreviewMulticurrencyTests(unittest.TestCase):
    def test_preview_matches_committed_results_except_event_id(self):
        opening = {"USD": D("100"), "EUR": D("50"), "JPY": D("7"), "GBP": D("3")}
        requests = [
            req("M1", "USD", "40"),
            req("M2", "EUR", "20"),
            req("M3", "USD", "30"),
            req("M4", "EUR", "15"),
            req("M5", "USD", "99"),                       # USD 余额 30，拒绝
            req("M6", "JPY", "7", creditors=(("c", "10"),)),
        ]
        engine = ClearingEngine()
        preview = engine.preview_multicurrency_batch(opening, requests)
        committed = engine.process_multicurrency_batch(opening, requests)

        self.assertIsInstance(preview, MulticurrencyBatchPreviewResult)
        self.assertEqual(len(preview.results), 6)
        for item in preview.results:
            self.assertIsInstance(item, SettlementPreview)
            self.assertFalse(hasattr(item, "event_id"))
        self.assertEqual(
            [preview_fields(item) for item in preview.results],
            [preview_fields(item) for item in committed.results],
        )
        self.assertEqual(
            dict(preview.validated_available_balances),
            dict(committed.validated_available_balances),
        )
        # 正式提交补上事件标识与台账。
        self.assertEqual(len(committed.event_ids), 6)
        self.assertEqual(len(engine.audit_log), 6)

    def test_balances_roll_independently_per_currency(self):
        engine = ClearingEngine()
        preview = engine.preview_multicurrency_batch(
            {"USD": D("100"), "EUR": D("50")},
            [
                req("B1", "USD", "40"),
                req("B2", "EUR", "20"),
                req("B3", "USD", "30"),
                req("B4", "EUR", "10"),
            ],
        )
        self.assertEqual(
            [r.validated_available_balance for r in preview.results],
            [D("60"), D("30"), D("30"), D("20")],
        )
        self.assertEqual(
            dict(preview.validated_available_balances),
            {"USD": D("30"), "EUR": D("20")},
        )
        self.assertTrue(all(r.approved for r in preview.results))

    def test_unused_currency_keeps_opening_balance(self):
        engine = ClearingEngine()
        preview = engine.preview_multicurrency_batch(
            {"USD": D("100"), "EUR": D("50"), "JPY": D("7")},
            [req("B1", "USD", "40")],
        )
        self.assertEqual(
            dict(preview.validated_available_balances),
            {"USD": D("60"), "EUR": D("50"), "JPY": D("7")},
        )

    def test_rejection_keeps_own_currency_balance_and_continues(self):
        engine = ClearingEngine()
        preview = engine.preview_multicurrency_batch(
            {"USD": D("50"), "EUR": D("30")},
            [
                req("B1", "USD", "40"),
                req("B2", "USD", "20"),                # 超 USD 余额，拒绝
                req("B3", "EUR", "25"),
                req("B4", "USD", "10"),
            ],
        )
        r1, r2, r3, r4 = preview.results
        self.assertTrue(r1.approved)
        self.assertFalse(r2.approved)
        self.assertEqual(
            r2.rejection_reason, "SETTLEMENT_EXCEEDS_AVAILABLE_BALANCE"
        )
        self.assertEqual(r2.validated_available_balance, D("10"))
        self.assertEqual(r2.total_pool_allocated, D("0"))
        self.assertEqual(r2.uncovered_bad_debt, D("0"))
        self.assertTrue(r3.approved)
        self.assertTrue(r4.approved)
        self.assertEqual(
            dict(preview.validated_available_balances),
            {"USD": D("0"), "EUR": D("5")},
        )

    def test_base_limit_rejection_keeps_balance(self):
        engine = ClearingEngine()
        preview = engine.preview_multicurrency_batch(
            {"USD": D("100")},
            [req("B1", "USD", "40", exposure="100", limit="90", factor="0.6")],
        )
        (result,) = preview.results
        self.assertFalse(result.approved)
        self.assertEqual(
            result.rejection_reason, "RISK_OCCUPANCY_EXCEEDS_BASE_LIMIT"
        )
        self.assertEqual(preview.validated_available_balances["USD"], D("100"))

    def test_bad_debt_attribution_not_written_to_ledger(self):
        engine = ClearingEngine()
        preview = engine.preview_multicurrency_batch(
            {"USD": D("100"), "EUR": D("10")},
            [
                req("B1", "USD", "50", capital="40"),
                req("B2", "EUR", "10", creditors=(("c", "25"),)),
            ],
        )
        r1, r2 = preview.results
        self.assertEqual(r1.uncovered_bad_debt, D("10"))
        self.assertEqual(r2.uncovered_bad_debt, D("15"))
        # 预演不把坏账写入存续台账。
        self.assertEqual(engine.outstanding_bad_debts("USD"), ())
        self.assertEqual(engine.outstanding_bad_debts("EUR"), ())

    def test_preview_is_read_only_and_idempotent(self):
        engine = ClearingEngine()
        engine.process(  # 既有台账
            "OLD", "USD", D("10"), D("10"), D("0"), D("100"), D("0"),
            [("c", D("10"))],
        )
        events_before = engine.audit_log
        summary_before = engine.audit_reconciliation("USD")
        debts_before = engine.outstanding_bad_debts("USD")

        opening = {"USD": D("10"), "EUR": D("20")}
        requests = [req("Q1", "USD", "5"), req("Q2", "EUR", "9")]
        first = engine.preview_multicurrency_batch(opening, requests)
        second = engine.preview_multicurrency_batch(opening, requests)

        self.assertEqual(first, second)
        self.assertEqual(engine.audit_log, events_before)
        self.assertEqual(engine.audit_reconciliation("USD"), summary_before)
        self.assertEqual(engine.outstanding_bad_debts("USD"), debts_before)
        for tid in ("Q1", "Q2"):
            self.assertFalse(engine.has_transaction(tid))
            self.assertIsNone(engine.result_of(tid))
            self.assertIsNone(engine.get_event(tid))
        self.assertEqual(engine.outstanding_bad_debts("EUR"), ())
        # 预演后可正常提交，逐笔与余额一致。
        committed = engine.process_multicurrency_batch(opening, requests)
        self.assertEqual(
            [preview_fields(r) for r in committed.results],
            [preview_fields(r) for r in first.results],
        )
        self.assertEqual(
            dict(committed.validated_available_balances),
            dict(first.validated_available_balances),
        )

    def test_results_are_immutable(self):
        engine = ClearingEngine()
        preview = engine.preview_multicurrency_batch(
            {"USD": D("10")}, [req("B1", "USD", "5")]
        )
        self.assertIsInstance(preview.results, tuple)
        self.assertIsInstance(preview.validated_available_balances, MappingProxyType)
        with self.assertRaises(FrozenInstanceError):
            preview.results = ()
        with self.assertRaises(FrozenInstanceError):
            preview.results[0].approved = False
        with self.assertRaises(TypeError):
            preview.validated_available_balances["USD"] = D("0")


class PreviewMulticurrencyValidationTests(unittest.TestCase):
    def test_empty_batch_checked_first(self):
        for bad in ([], None, ()):
            with self.subTest(bad=bad):
                with self.assertRaises(EmptyBatchError):
                    ClearingEngine().preview_multicurrency_batch(
                        {"USD": D("100")}, bad
                    )
        with self.assertRaises(EmptyBatchError):
            ClearingEngine().preview_multicurrency_batch("not-a-mapping", [])

    def test_invalid_opening_balances_raise_value_error(self):
        engine = ClearingEngine()
        requests = [req("B1", "USD", "1")]
        for bad in ("not-a-mapping", None):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    engine.preview_multicurrency_batch(bad, requests)
        for bad_key in ("", "   ", 7):
            with self.subTest(bad_key=bad_key):
                with self.assertRaises(ValueError):
                    engine.preview_multicurrency_batch(
                        {bad_key: D("100")}, requests
                    )
        for bad_value in (D("-1"), "100", float("nan"), None):
            with self.subTest(bad_value=bad_value):
                with self.assertRaises(ValueError):
                    engine.preview_multicurrency_batch(
                        {"USD": bad_value}, requests
                    )

    def test_missing_or_unknown_request_currency(self):
        for bad in (None, "", "   ", 7):
            with self.subTest(bad=bad):
                with self.assertRaises(InvalidCurrencyError):
                    ClearingEngine().preview_multicurrency_batch(
                        {"USD": D("100")}, [req("B1", bad, "1")]
                    )
        with self.assertRaises(InvalidCurrencyError):
            ClearingEngine().preview_multicurrency_batch(
                {"USD": D("100")}, [req("B1", "EUR", "1")]
            )

    def test_mixed_currency_rejected(self):
        with self.assertRaises(MixedCurrencyError):
            ClearingEngine().preview_multicurrency_batch(
                {"USD": D("100"), "EUR": D("100")},
                [req("B1", "USD", "1", creditors=(("foreign", "1", "EUR"),))],
            )

    def test_duplicate_transaction_rejected(self):
        with self.assertRaises(DuplicateTransactionError):
            ClearingEngine().preview_multicurrency_batch(
                {"USD": D("100"), "EUR": D("100")},
                [req("B1", "USD", "1"), req("B1", "EUR", "2")],
            )
        engine = ClearingEngine()
        engine.process(
            "B1", "USD", D("10"), D("1"), D("0"), D("100"), D("0"),
            [("c", D("1"))],
        )
        with self.assertRaises(DuplicateTransactionError):
            engine.preview_multicurrency_batch(
                {"USD": D("100")}, [req("B1", "USD", "1")]
            )

    def test_empty_creditor_list_and_bad_factor(self):
        with self.assertRaises(EmptyCreditorListError):
            ClearingEngine().preview_multicurrency_batch(
                {"USD": D("100")}, [req("B1", "USD", "1", creditors=())]
            )
        with self.assertRaises(InvalidRiskFactorError):
            ClearingEngine().preview_multicurrency_batch(
                {"USD": D("100")}, [req("B1", "USD", "1", factor="1.01")]
            )

    def test_invalid_request_values_raise_value_error(self):
        with self.assertRaises(ValueError):
            ClearingEngine().preview_multicurrency_batch(
                {"USD": D("100")}, [req("B1", "USD", "-1")]
            )
        with self.assertRaises(ValueError):
            ClearingEngine().preview_multicurrency_batch(
                {"USD": D("100")}, ["not-a-mapping"]
            )

    def test_failed_validation_leaves_no_state(self):
        engine = ClearingEngine()
        with self.assertRaises(DuplicateTransactionError):
            engine.preview_multicurrency_batch(
                {"USD": D("100")},
                [req("B1", "USD", "10"), req("B1", "USD", "20")],
            )
        self.assertEqual(engine.audit_log, ())
        self.assertFalse(engine.has_transaction("B1"))
        # 修正后可整体重提。
        preview = engine.preview_multicurrency_batch(
            {"USD": D("100"), "EUR": D("50")},
            [req("B1", "USD", "10"), req("B2", "EUR", "20")],
        )
        self.assertEqual(
            dict(preview.validated_available_balances),
            {"USD": D("90"), "EUR": D("30")},
        )
        self.assertEqual(engine.audit_log, ())


if __name__ == "__main__":
    unittest.main()
