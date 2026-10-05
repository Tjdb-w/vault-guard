"""多币种批次结算（process_multicurrency_batch /
process_settlement_multicurrency_batch）公开行为测试。

覆盖：按币种独立滚动记账、拒绝不动本币种余额、最终余额映射（额外币种
原值保留）、逐笔瀑布 / 补充资本 / 坏账归因、坏账按币种入台账并可回收、
整体校验顺序与异常、失败回退（不生成事件 / 不占流水号 / 不改余额与坏账
台账）、审计序号递增、结果结构不可变、模块级一次性入口不跨批次去重。
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


class MulticurrencyExecutionTests(unittest.TestCase):
    def test_balances_roll_independently_per_currency(self):
        engine = ClearingEngine()
        batch = engine.process_multicurrency_batch(
            {"USD": D("100"), "EUR": D("50")},
            [
                req("B1", "USD", "40"),
                req("B2", "EUR", "20"),
                req("B3", "USD", "30"),
                req("B4", "EUR", "10"),
            ],
        )
        self.assertIsInstance(batch, MulticurrencyBatchResult)
        # 各笔即时余额只反映本币种：USD 100-40=60、60-30=30；EUR 50-20=30、30-10=20。
        self.assertEqual(
            [r.validated_available_balance for r in batch.results],
            [D("60"), D("30"), D("30"), D("20")],
        )
        self.assertEqual(
            dict(batch.validated_available_balances),
            {"USD": D("30"), "EUR": D("20")},
        )
        self.assertTrue(all(r.approved for r in batch.results))

    def test_results_and_event_ids_align_with_requests(self):
        engine = ClearingEngine()
        batch = engine.process_multicurrency_batch(
            {"USD": D("100"), "EUR": D("100")},
            [req("B1", "EUR", "10"), req("B2", "USD", "10"), req("B3", "EUR", "5")],
        )
        self.assertEqual(
            [r.transaction_id for r in batch.results], ["B1", "B2", "B3"]
        )
        self.assertEqual(len(batch.event_ids), 3)
        for result, event_id in zip(batch.results, batch.event_ids, strict=True):
            self.assertEqual(result.event_id, event_id)
            self.assertEqual(
                engine.get_event(result.transaction_id).event_id, event_id
            )
        # 审计事件按请求顺序追加，币种与请求一致。
        self.assertEqual(
            [e.currency for e in engine.audit_log], ["EUR", "USD", "EUR"]
        )

    def test_unused_currency_keeps_opening_balance(self):
        batch = process_settlement_multicurrency_batch(
            {"USD": D("100"), "EUR": D("50"), "JPY": D("7")},
            [req("B1", "USD", "40")],
        )
        self.assertEqual(
            dict(batch.validated_available_balances),
            {"USD": D("60"), "EUR": D("50"), "JPY": D("7")},
        )

    def test_rejection_keeps_own_currency_balance_and_continues(self):
        engine = ClearingEngine()
        batch = engine.process_multicurrency_batch(
            {"USD": D("50"), "EUR": D("30")},
            [
                req("B1", "USD", "40"),                # 通过，USD 余额 10
                req("B2", "USD", "20"),                # 超 USD 余额，拒绝
                req("B3", "EUR", "25"),                # 通过，EUR 余额 5
                req("B4", "USD", "10"),                # 通过，USD 余额 0
            ],
        )
        r1, r2, r3, r4 = batch.results
        self.assertTrue(r1.approved)
        self.assertFalse(r2.approved)
        self.assertEqual(
            r2.rejection_reason, "SETTLEMENT_EXCEEDS_AVAILABLE_BALANCE"
        )
        # 拒绝不动本币种资金、不确认坏账。
        self.assertEqual(r2.validated_available_balance, D("10"))
        self.assertEqual(r2.uncovered_bad_debt, D("0"))
        self.assertEqual(r2.total_pool_allocated, D("0"))
        self.assertTrue(r3.approved)
        self.assertTrue(r4.approved)
        self.assertEqual(
            dict(batch.validated_available_balances),
            {"USD": D("0"), "EUR": D("5")},
        )

    def test_risk_occupancy_rejection_in_multicurrency_batch(self):
        batch = process_settlement_multicurrency_batch(
            {"USD": D("100")},
            [req("B1", "USD", "40", exposure="100", limit="90", factor="0.6")],
        )
        (result,) = batch.results
        self.assertFalse(result.approved)
        self.assertEqual(
            result.rejection_reason, "RISK_OCCUPANCY_EXCEEDS_BASE_LIMIT"
        )
        self.assertEqual(batch.validated_available_balances["USD"], D("100"))

    def test_waterfall_capital_and_bad_debt_per_request(self):
        engine = ClearingEngine()
        batch = engine.process_multicurrency_batch(
            {"USD": D("100"), "EUR": D("10")},
            [
                req("B1", "USD", "50", capital="40"),
                req("B2", "EUR", "10", creditors=(("c", "25"),)),
            ],
        )
        r1, r2 = batch.results
        self.assertEqual(r1.pool_allocations, (D("50"), D("0")))
        self.assertEqual(r1.capital_allocations, (D("10"), D("30")))
        self.assertEqual(r1.uncovered_bad_debt, D("10"))
        self.assertEqual(r2.uncovered_bad_debt, D("15"))
        # 坏账按各自币种进入台账。
        self.assertEqual(
            [d.balance for d in engine.outstanding_bad_debts("USD")], [D("10")]
        )
        self.assertEqual(
            [d.balance for d in engine.outstanding_bad_debts("EUR")], [D("15")]
        )
        # 回收只冲减对应币种。
        recovery = engine.process_recovery("RC1", "EUR", D("15"))
        self.assertEqual(recovery.outstanding_bad_debt, D("0"))
        self.assertEqual(
            [d.balance for d in engine.outstanding_bad_debts("USD")], [D("10")]
        )

    def test_batch_appends_one_event_per_request_in_sequence(self):
        engine = ClearingEngine()
        engine.process(  # 既有单笔事件，序号为 1
            "S0", "USD", D("10"), D("10"), D("0"), D("100"), D("0"),
            [("c", D("10"))],
        )
        engine.process_multicurrency_batch(
            {"USD": D("100"), "EUR": D("100")},
            [req("B1", "USD", "10"), req("B2", "EUR", "20")],
        )
        self.assertEqual([e.sequence for e in engine.audit_log], [1, 2, 3])
        self.assertEqual(
            [e.transaction_id for e in engine.audit_log], ["S0", "B1", "B2"]
        )

    def test_result_is_immutable(self):
        batch = process_settlement_multicurrency_batch(
            {"USD": D("10")}, [req("B1", "USD", "5")]
        )
        self.assertIsInstance(batch.results, tuple)
        self.assertIsInstance(batch.event_ids, tuple)
        self.assertIsInstance(batch.validated_available_balances, MappingProxyType)
        with self.assertRaises(FrozenInstanceError):
            batch.results = ()
        with self.assertRaises(TypeError):
            batch.validated_available_balances["USD"] = D("0")


class MulticurrencyValidationTests(unittest.TestCase):
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
        requests = [req("B1", "USD", "1")]
        with self.assertRaises(ValueError):
            engine.process_multicurrency_batch("not-a-mapping", requests)
        with self.assertRaises(ValueError):
            engine.process_multicurrency_batch(None, requests)
        for bad_key in ("", "   ", 7):
            with self.subTest(bad_key=bad_key):
                with self.assertRaises(ValueError):
                    engine.process_multicurrency_batch(
                        {bad_key: D("100")}, requests
                    )
        for bad_value in (D("-1"), "100", float("nan"), None):
            with self.subTest(bad_value=bad_value):
                with self.assertRaises(ValueError):
                    engine.process_multicurrency_batch(
                        {"USD": bad_value}, requests
                    )

    def test_opening_balances_checked_before_request_currency(self):
        with self.assertRaises(ValueError):
            ClearingEngine().process_multicurrency_batch(
                {"USD": D("-1")}, [req("B1", None, "1")]
            )

    def test_missing_or_blank_request_currency(self):
        for bad in (None, "", "   ", 7):
            with self.subTest(bad=bad):
                with self.assertRaises(InvalidCurrencyError):
                    ClearingEngine().process_multicurrency_batch(
                        {"USD": D("100")}, [req("B1", bad, "1")]
                    )

    def test_request_currency_must_exist_in_opening_balances(self):
        with self.assertRaises(InvalidCurrencyError):
            ClearingEngine().process_multicurrency_batch(
                {"USD": D("100")}, [req("B1", "EUR", "1")]
            )

    def test_request_currency_checked_before_mixed_creditor(self):
        # 第一笔请求币种缺失、第二笔债权币种不一致：先抛 InvalidCurrencyError。
        with self.assertRaises(InvalidCurrencyError):
            ClearingEngine().process_multicurrency_batch(
                {"USD": D("100")},
                [
                    req("B1", None, "1"),
                    req("B2", "USD", "1", creditors=(("f", "1", "EUR"),)),
                ],
            )

    def test_mixed_currency_rejected(self):
        with self.assertRaises(MixedCurrencyError):
            ClearingEngine().process_multicurrency_batch(
                {"USD": D("100"), "EUR": D("100")},
                [req("B1", "USD", "1", creditors=(("foreign", "1", "EUR"),))],
            )

    def test_creditor_currency_matching_own_request_passes(self):
        batch = process_settlement_multicurrency_batch(
            {"USD": D("100"), "EUR": D("100")},
            [
                req("B1", "USD", "1", creditors=(("c", "1", "USD"),)),
                req("B2", "EUR", "1", creditors=(("c", "1", "EUR"),)),
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
                {"USD": D("100")},
                [req("DUP", "USD", "1"),
                 req("B2", "USD", "1", creditors=(("f", "1", "EUR"),))],
            )

    def test_duplicate_within_batch(self):
        with self.assertRaises(DuplicateTransactionError):
            ClearingEngine().process_multicurrency_batch(
                {"USD": D("100"), "EUR": D("100")},
                [req("B1", "USD", "1"), req("B1", "EUR", "2")],
            )

    def test_duplicate_against_ledger(self):
        engine = ClearingEngine()
        engine.process(
            "B1", "USD", D("10"), D("1"), D("0"), D("100"), D("0"),
            [("c", D("1"))],
        )
        with self.assertRaises(DuplicateTransactionError):
            engine.process_multicurrency_batch(
                {"USD": D("100")}, [req("B1", "USD", "1")]
            )

    def test_duplicate_before_empty_creditor_list(self):
        with self.assertRaises(DuplicateTransactionError):
            ClearingEngine().process_multicurrency_batch(
                {"USD": D("100")},
                [req("B1", "USD", "1", creditors=()), req("B1", "USD", "2")],
            )

    def test_empty_creditor_list(self):
        with self.assertRaises(EmptyCreditorListError):
            ClearingEngine().process_multicurrency_batch(
                {"USD": D("100")}, [req("B1", "USD", "1", creditors=())]
            )

    def test_empty_creditor_list_before_risk_factor(self):
        with self.assertRaises(EmptyCreditorListError):
            ClearingEngine().process_multicurrency_batch(
                {"USD": D("100")},
                [req("B1", "USD", "1", factor="1.01", creditors=())],
            )

    def test_risk_factor_out_of_bounds(self):
        with self.assertRaises(InvalidRiskFactorError):
            ClearingEngine().process_multicurrency_batch(
                {"USD": D("100")}, [req("B1", "USD", "1", factor="1.01")]
            )

    def test_invalid_request_values_raise_value_error(self):
        engine = ClearingEngine()
        with self.assertRaises(ValueError):
            engine.process_multicurrency_batch(
                {"USD": D("100")}, [req("B1", "USD", "-1")]
            )
        with self.assertRaises(ValueError):
            engine.process_multicurrency_batch(
                {"USD": D("100")}, [req("B1", "USD", "1", capital="-2")]
            )
        with self.assertRaises(ValueError):
            engine.process_multicurrency_batch(
                {"USD": D("100")}, [{"transaction_id": "B1", "currency": "USD"}]
            )
        with self.assertRaises(ValueError):
            engine.process_multicurrency_batch(
                {"USD": D("100")}, ["not-a-mapping"]
            )

    def test_failure_rolls_back_batch_state(self):
        engine = ClearingEngine()
        with self.assertRaises(DuplicateTransactionError):
            engine.process_multicurrency_batch(
                {"USD": D("100")},
                [req("B1", "USD", "10"), req("B1", "USD", "20")],
            )
        # 不生成事件、不占流水号、不改余额：修正后可整体重提。
        self.assertEqual(len(engine.audit_log), 0)
        self.assertFalse(engine.has_transaction("B1"))
        batch = engine.process_multicurrency_batch(
            {"USD": D("100"), "EUR": D("50")},
            [req("B1", "USD", "10"), req("B2", "EUR", "20")],
        )
        self.assertEqual(len(engine.audit_log), 2)
        self.assertEqual(
            dict(batch.validated_available_balances),
            {"USD": D("90"), "EUR": D("30")},
        )

    def test_failed_batch_leaves_bad_debt_ledger_untouched(self):
        engine = ClearingEngine()
        engine.process(  # 既有放行，留下 USD 坏账 20
            "T1", "USD", D("100"), D("40"), D("0"), D("100"), D("0"),
            [("a", D("30")), ("b", D("30"))],
        )
        with self.assertRaises(InvalidRiskFactorError):
            engine.process_multicurrency_batch(
                {"USD": D("100")},
                [req("B1", "USD", "10", creditors=(("c", "50"),)),
                 req("B2", "USD", "1", factor="2")],
            )
        self.assertEqual(len(engine.audit_log), 1)
        self.assertIsNone(engine.result_of("B1"))
        # 台账只剩 T1 的坏账，批次未追加。
        debts = engine.outstanding_bad_debts("USD")
        self.assertEqual(len(debts), 1)
        self.assertEqual(debts[0].source_transaction_id, "T1")


class ModuleLevelMulticurrencyTests(unittest.TestCase):
    def test_process_settlement_multicurrency_batch_one_shot(self):
        batch = process_settlement_multicurrency_batch(
            {"USD": D("100"), "EUR": D("50")},
            [req("M1", "USD", "40"), req("M2", "EUR", "30")],
        )
        self.assertEqual(
            dict(batch.validated_available_balances),
            {"USD": D("60"), "EUR": D("20")},
        )
        self.assertEqual(len(batch.event_ids), 2)

    def test_no_cross_batch_dedup(self):
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
