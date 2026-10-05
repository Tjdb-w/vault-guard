"""多币种批次结算（process_multicurrency_batch /
process_settlement_multicurrency_batch）公开行为测试。

覆盖：按币种独立滚动余额、额外币种原值保留、结果与事件同序、整体校验
顺序与异常、失败回退、审计序号递增、坏账按币种入台账并可回收、结果
不可变、模块级一次性入口不跨批保留状态。
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
    MulticurrencyBatchResult,
    process_settlement_multicurrency_batch,
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


class MulticurrencyBatchTests(unittest.TestCase):
    def test_independent_rolling_balances_per_currency(self):
        engine = ClearingEngine()
        batch = engine.process_multicurrency_batch(
            {"USD": D("100"), "EUR": D("50")},
            [
                req("M1", "USD", "40"),
                req("M2", "EUR", "20"),
                req("M3", "USD", "30"),
                req("M4", "EUR", "10"),
            ],
        )
        self.assertIsInstance(batch, MulticurrencyBatchResult)
        # USD: 100-40=60, 60-30=30；EUR: 50-20=30, 30-10=20。
        self.assertEqual(
            [r.validated_available_balance for r in batch.results],
            [D("60"), D("30"), D("30"), D("20")],
        )
        self.assertEqual(
            batch.validated_available_balances,
            {"USD": D("30"), "EUR": D("20")},
        )
        self.assertTrue(all(r.approved for r in batch.results))

    def test_unreferenced_currency_keeps_opening_value(self):
        batch = process_settlement_multicurrency_batch(
            {"USD": D("100"), "JPY": D("7")},
            [req("M1", "USD", "40")],
        )
        self.assertEqual(
            batch.validated_available_balances,
            {"USD": D("60"), "JPY": D("7")},
        )

    def test_results_and_event_ids_align_with_requests(self):
        engine = ClearingEngine()
        batch = engine.process_multicurrency_batch(
            {"USD": D("100"), "EUR": D("100")},
            [req("M1", "EUR", "10"), req("M2", "USD", "10"), req("M3", "EUR", "10")],
        )
        self.assertEqual(
            [r.transaction_id for r in batch.results], ["M1", "M2", "M3"]
        )
        self.assertEqual(len(batch.event_ids), 3)
        for result, event_id in zip(batch.results, batch.event_ids, strict=True):
            self.assertEqual(result.event_id, event_id)
            self.assertEqual(
                engine.get_event(result.transaction_id).event_id, event_id
            )
        self.assertEqual(
            [e.currency for e in engine.audit_log], ["EUR", "USD", "EUR"]
        )

    def test_rejection_keeps_own_currency_balance_and_continues(self):
        engine = ClearingEngine()
        batch = engine.process_multicurrency_batch(
            {"USD": D("50"), "EUR": D("50")},
            [
                req("M1", "USD", "40"),               # 通过，USD 余额 10
                req("M2", "USD", "20"),               # 超 USD 余额，拒绝
                req("M3", "EUR", "20"),               # EUR 不受影响
                req("M4", "USD", "10"),               # 通过，USD 余额 0
            ],
        )
        r1, r2, r3, r4 = batch.results
        self.assertTrue(r1.approved)
        self.assertFalse(r2.approved)
        self.assertEqual(
            r2.rejection_reason, "SETTLEMENT_EXCEEDS_AVAILABLE_BALANCE"
        )
        self.assertEqual(r2.validated_available_balance, D("10"))
        self.assertEqual(r2.uncovered_bad_debt, D("0"))
        self.assertTrue(r3.approved)
        self.assertEqual(r3.validated_available_balance, D("30"))
        self.assertTrue(r4.approved)
        self.assertEqual(
            batch.validated_available_balances,
            {"USD": D("0"), "EUR": D("30")},
        )

    def test_risk_occupancy_rejection_in_multicurrency_batch(self):
        batch = process_settlement_multicurrency_batch(
            {"USD": D("100")},
            [req("M1", "USD", "40", exposure="100", limit="90", factor="0.6")],
        )
        (result,) = batch.results
        self.assertFalse(result.approved)
        self.assertEqual(
            result.rejection_reason, "RISK_OCCUPANCY_EXCEEDS_BASE_LIMIT"
        )
        self.assertEqual(batch.validated_available_balances, {"USD": D("100")})

    def test_waterfall_capital_and_bad_debt_per_currency(self):
        engine = ClearingEngine()
        batch = engine.process_multicurrency_batch(
            {"USD": D("100"), "EUR": D("10")},
            [
                req("M1", "USD", "50", capital="40"),
                req("M2", "EUR", "10", capital="5"),
            ],
        )
        r1, r2 = batch.results
        self.assertEqual(r1.pool_allocations, (D("50"), D("0")))
        self.assertEqual(r1.capital_allocations, (D("10"), D("30")))
        self.assertEqual(r1.uncovered_bad_debt, D("10"))
        self.assertEqual(r2.uncovered_bad_debt, D("85"))
        # 坏账按币种进入台账。
        self.assertEqual(
            [d.balance for d in engine.outstanding_bad_debts("USD")], [D("10")]
        )
        self.assertEqual(
            [d.balance for d in engine.outstanding_bad_debts("EUR")],
            [D("45"), D("40")],
        )
        # 回收按币种冲减。
        recovery = engine.process_recovery("R1", "EUR", D("40"))
        self.assertEqual(recovery.total_recovered, D("40"))
        self.assertEqual(
            [d.balance for d in engine.outstanding_bad_debts("EUR")],
            [D("5"), D("40")],
        )
        self.assertEqual(
            [d.balance for d in engine.outstanding_bad_debts("USD")], [D("10")]
        )

    def test_batch_appends_one_event_per_request_in_sequence(self):
        engine = ClearingEngine()
        engine.process(
            "S0", "USD", D("10"), D("10"), D("0"), D("100"), D("0"),
            [("c", D("10"))],
        )
        engine.process_multicurrency_batch(
            {"USD": D("100"), "EUR": D("100")},
            [req("M1", "USD", "10"), req("M2", "EUR", "20")],
        )
        self.assertEqual([e.sequence for e in engine.audit_log], [1, 2, 3])
        self.assertEqual(
            [e.transaction_id for e in engine.audit_log], ["S0", "M1", "M2"]
        )

    def test_result_is_immutable(self):
        batch = process_settlement_multicurrency_batch(
            {"USD": D("10")}, [req("M1", "USD", "5")]
        )
        self.assertIsInstance(batch.results, tuple)
        self.assertIsInstance(batch.event_ids, tuple)
        self.assertIsInstance(batch.validated_available_balances, MappingProxyType)
        with self.assertRaises(FrozenInstanceError):
            batch.validated_available_balances = {}
        with self.assertRaises(TypeError):
            batch.validated_available_balances["USD"] = D("0")


class MulticurrencyBatchValidationTests(unittest.TestCase):
    def test_empty_batch(self):
        for bad in ([], None, ()):
            with self.subTest(bad=bad):
                with self.assertRaises(EmptyBatchError):
                    ClearingEngine().process_multicurrency_batch(
                        {"USD": D("100")}, bad
                    )

    def test_empty_batch_checked_before_opening_balances(self):
        with self.assertRaises(EmptyBatchError):
            ClearingEngine().process_multicurrency_batch("not-a-mapping", [])

    def test_invalid_opening_balances_raise_value_error(self):
        engine = ClearingEngine()
        requests = [req("M1", "USD", "1")]
        with self.assertRaises(ValueError):
            engine.process_multicurrency_batch("not-a-mapping", requests)
        with self.assertRaises(ValueError):
            engine.process_multicurrency_batch(None, requests)
        with self.assertRaises(ValueError):
            engine.process_multicurrency_batch({"": D("1")}, requests)
        with self.assertRaises(ValueError):
            engine.process_multicurrency_batch({"  ": D("1")}, requests)
        with self.assertRaises(ValueError):
            engine.process_multicurrency_batch({1: D("1")}, requests)
        with self.assertRaises(ValueError):
            engine.process_multicurrency_batch({"USD": D("-1")}, requests)
        with self.assertRaises(ValueError):
            engine.process_multicurrency_batch({"USD": "abc"}, requests)
        with self.assertRaises(ValueError):
            engine.process_multicurrency_batch({"USD": float("nan")}, requests)

    def test_invalid_request_values_raise_value_error(self):
        engine = ClearingEngine()
        with self.assertRaises(ValueError):
            engine.process_multicurrency_batch(
                {"USD": D("100")}, [req("M1", "USD", "-1")]
            )
        with self.assertRaises(ValueError):
            engine.process_multicurrency_batch(
                {"USD": D("100")}, [req("M1", "USD", "1", capital="-2")]
            )
        with self.assertRaises(ValueError):
            engine.process_multicurrency_batch(
                {"USD": D("100")}, [{"transaction_id": "M1", "currency": "USD"}]
            )
        with self.assertRaises(ValueError):
            engine.process_multicurrency_batch(
                {"USD": D("100")}, ["not-a-mapping"]
            )

    def test_missing_or_blank_request_currency(self):
        for bad in (None, "", "   "):
            with self.subTest(bad=bad):
                with self.assertRaises(InvalidCurrencyError):
                    ClearingEngine().process_multicurrency_batch(
                        {"USD": D("100")}, [req("M1", bad, "1")]
                    )

    def test_unregistered_request_currency(self):
        with self.assertRaises(InvalidCurrencyError):
            ClearingEngine().process_multicurrency_batch(
                {"USD": D("100")}, [req("M1", "EUR", "1")]
            )

    def test_opening_balances_checked_before_request_currency(self):
        with self.assertRaises(ValueError):
            ClearingEngine().process_multicurrency_batch(
                {"USD": D("-1")}, [req("M1", None, "1")]
            )

    def test_mixed_currency_rejected(self):
        with self.assertRaises(MixedCurrencyError):
            ClearingEngine().process_multicurrency_batch(
                {"USD": D("100"), "EUR": D("100")},
                [req("M1", "USD", "1", creditors=(("foreign", "1", "EUR"),))],
            )

    def test_matching_creditor_currency_accepted(self):
        batch = process_settlement_multicurrency_batch(
            {"USD": D("100"), "EUR": D("100")},
            [
                req("M1", "USD", "1", creditors=(("c", "1", "USD"),)),
                req("M2", "EUR", "1", creditors=(("c", "1", "EUR"),)),
            ],
        )
        self.assertTrue(all(r.approved for r in batch.results))

    def test_mixed_currency_before_duplicate_check(self):
        engine = ClearingEngine()
        engine.process(
            "DUP", "USD", D("10"), D("1"), D("0"), D("100"), D("0"),
            [("c", D("1"))],
        )
        # 第一笔与台账重复、第二笔债权币种不一致：先抛 MixedCurrencyError。
        with self.assertRaises(MixedCurrencyError):
            engine.process_multicurrency_batch(
                {"USD": D("100"), "EUR": D("100")},
                [
                    req("DUP", "USD", "1"),
                    req("M2", "USD", "1", creditors=(("f", "1", "EUR"),)),
                ],
            )

    def test_duplicate_within_batch(self):
        with self.assertRaises(DuplicateTransactionError):
            ClearingEngine().process_multicurrency_batch(
                {"USD": D("100"), "EUR": D("100")},
                [req("M1", "USD", "1"), req("M1", "EUR", "2")],
            )

    def test_duplicate_against_ledger(self):
        engine = ClearingEngine()
        engine.process(
            "M1", "USD", D("10"), D("1"), D("0"), D("100"), D("0"),
            [("c", D("1"))],
        )
        with self.assertRaises(DuplicateTransactionError):
            engine.process_multicurrency_batch(
                {"USD": D("100")}, [req("M1", "USD", "1")]
            )

    def test_duplicate_before_empty_creditor_list(self):
        with self.assertRaises(DuplicateTransactionError):
            ClearingEngine().process_multicurrency_batch(
                {"USD": D("100")},
                [req("M1", "USD", "1"), req("M1", "USD", "1", creditors=())],
            )

    def test_empty_creditor_list(self):
        with self.assertRaises(EmptyCreditorListError):
            ClearingEngine().process_multicurrency_batch(
                {"USD": D("100")}, [req("M1", "USD", "1", creditors=())]
            )

    def test_empty_creditor_list_before_risk_factor(self):
        with self.assertRaises(EmptyCreditorListError):
            ClearingEngine().process_multicurrency_batch(
                {"USD": D("100")},
                [req("M1", "USD", "1", factor="1.01", creditors=())],
            )

    def test_risk_factor_out_of_bounds(self):
        with self.assertRaises(InvalidRiskFactorError):
            ClearingEngine().process_multicurrency_batch(
                {"USD": D("100")}, [req("M1", "USD", "1", factor="1.01")]
            )

    def test_failure_rolls_back_batch_state(self):
        engine = ClearingEngine()
        with self.assertRaises(DuplicateTransactionError):
            engine.process_multicurrency_batch(
                {"USD": D("100"), "EUR": D("100")},
                [req("M1", "USD", "10"), req("M1", "EUR", "20")],
            )
        # 不生成事件、不占流水号、不改余额：修正后可整体重提。
        self.assertEqual(len(engine.audit_log), 0)
        self.assertFalse(engine.has_transaction("M1"))
        batch = engine.process_multicurrency_batch(
            {"USD": D("100"), "EUR": D("100")},
            [req("M1", "USD", "10"), req("M2", "EUR", "20")],
        )
        self.assertEqual(len(engine.audit_log), 2)
        self.assertEqual(
            batch.validated_available_balances,
            {"USD": D("90"), "EUR": D("80")},
        )

    def test_failed_batch_leaves_bad_debt_ledger_untouched(self):
        engine = ClearingEngine()
        engine.process(
            "S0", "USD", D("100"), D("10"), D("0"), D("100"), D("0"),
            [("c", D("150"))],
        )
        before = engine.outstanding_bad_debts("USD")
        with self.assertRaises(ValueError):
            engine.process_multicurrency_batch(
                {"USD": D("100")}, [req("M1", "USD", "10"), req("M2", "USD", "-1")]
            )
        self.assertEqual(engine.outstanding_bad_debts("USD"), before)
        self.assertEqual(len(engine.audit_log), 1)


class ModuleLevelMulticurrencyBatchTests(unittest.TestCase):
    def test_process_settlement_multicurrency_batch_one_shot(self):
        batch = process_settlement_multicurrency_batch(
            {"USD": D("100"), "EUR": D("50")},
            [req("M1", "USD", "40"), req("M2", "EUR", "30")],
        )
        self.assertEqual(
            batch.validated_available_balances,
            {"USD": D("60"), "EUR": D("20")},
        )
        self.assertEqual(len(batch.event_ids), 2)

    def test_no_cross_batch_state(self):
        # 一次性引擎：相同流水号可在不同批次重复使用。
        r1 = process_settlement_multicurrency_batch(
            {"USD": D("100")}, [req("M1", "USD", "10")]
        )
        r2 = process_settlement_multicurrency_batch(
            {"USD": D("100")}, [req("M1", "USD", "10")]
        )
        self.assertTrue(r1.results[0].approved)
        self.assertTrue(r2.results[0].approved)


if __name__ == "__main__":
    unittest.main()
