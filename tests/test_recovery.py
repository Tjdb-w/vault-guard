"""存续坏账回收（process_recovery）公开行为测试。

覆盖：冲减顺序（审计事件顺序 + creditors 顺序）、部分覆盖、零额回收、
五类回收异常及其无副作用保证、审计事件序列与 recovery_allocations、
recovery_of / outstanding_bad_debts 只读语义、币种隔离、历史
SettlementResult 不回写，以及批次来源坏账的回收。
"""

import unittest
from decimal import Decimal

from vault_guard import (
    ClearingEngine,
    DuplicateTransactionError,
    InvalidCurrencyError,
    NoOutstandingBadDebtError,
    RecoveryAmountExceedsOutstandingError,
)

D = Decimal

# 全部坏账放行参数：拟清算 0、补充资本 0、占用 0、限额 0、余额 0。
ZERO_KWARGS = dict(
    pool_balance=D("0"),
    settlement_amount=D("0"),
    notional_exposure=D("0"),
    base_limit=D("0"),
    risk_factor=D("0"),
    supplementary_capital=D("0"),
)

CREDITORS_ABC = [
    ("senior", D("60")),
    ("mezzanine", D("30")),
    ("equity", D("10")),
]


def settle_full_bad_debt(engine, tid, currency, creditors):
    return engine.process(
        transaction_id=tid, currency=currency, creditors=creditors,
        **ZERO_KWARGS,
    )


class RecoveryAllocationTests(unittest.TestCase):
    def test_partial_recovery_hits_first_creditor_only(self):
        engine = ClearingEngine()
        settle_full_bad_debt(engine, "TX-A", "USD", CREDITORS_ABC)

        result = engine.process_recovery("RC-1", "USD", D("40"))
        self.assertEqual(result.recovery_transaction_id, "RC-1")
        self.assertEqual(result.currency, "USD")
        self.assertEqual(result.recovery_amount, D("40"))
        self.assertEqual(result.total_recovered, D("40"))
        self.assertEqual(result.outstanding_bad_debt, D("60"))
        self.assertEqual(len(result.allocations), 1)
        allocation = result.allocations[0]
        self.assertEqual(allocation.source_transaction_id, "TX-A")
        self.assertEqual(allocation.creditor, "senior")
        self.assertEqual(allocation.amount, D("40"))
        self.assertEqual(allocation.remaining_bad_debt, D("20"))
        self.assertEqual(result.event_id, "EVT-RC-1-recovery")

    def test_recovery_clears_creditors_in_order(self):
        engine = ClearingEngine()
        settle_full_bad_debt(engine, "TX-A", "USD", CREDITORS_ABC)

        result = engine.process_recovery("RC-1", "USD", D("70"))
        self.assertEqual(result.total_recovered, D("70"))
        self.assertEqual(result.outstanding_bad_debt, D("30"))
        self.assertEqual(
            [(a.creditor, a.amount, a.remaining_bad_debt)
             for a in result.allocations],
            [
                ("senior", D("60"), D("0")),
                ("mezzanine", D("10"), D("20")),
            ],
        )

    def test_recovery_can_clear_everything(self):
        engine = ClearingEngine()
        settle_full_bad_debt(engine, "TX-A", "USD", CREDITORS_ABC)

        result = engine.process_recovery("RC-1", "USD", D("100"))
        self.assertEqual(result.outstanding_bad_debt, D("0"))
        self.assertEqual(
            [(a.creditor, a.amount) for a in result.allocations],
            [("senior", D("60")), ("mezzanine", D("30")), ("equity", D("10"))],
        )
        self.assertTrue(all(a.remaining_bad_debt == D("0")
                            for a in result.allocations))
        self.assertEqual(engine.outstanding_bad_debts("USD"), ())

    def test_allocation_order_follows_audit_events_then_creditors(self):
        engine = ClearingEngine()
        # TX-A 坏账：senior 10 / junior 5；TX-B 坏账：alpha 7。
        settle_full_bad_debt(
            engine, "TX-A", "USD", [("senior", D("10")), ("junior", D("5"))]
        )
        settle_full_bad_debt(engine, "TX-B", "USD", [("alpha", D("7"))])

        first = engine.process_recovery("RC-1", "USD", D("12"))
        self.assertEqual(
            [(a.source_transaction_id, a.creditor, a.amount)
             for a in first.allocations],
            [
                ("TX-A", "senior", D("10")),
                ("TX-A", "junior", D("2")),
            ],
        )

        second = engine.process_recovery("RC-2", "USD", D("8"))
        # TX-A 的 junior 剩 3 先清零，再处理后发生事件 TX-B 的 alpha。
        self.assertEqual(
            [(a.source_transaction_id, a.creditor, a.amount)
             for a in second.allocations],
            [
                ("TX-A", "junior", D("3")),
                ("TX-B", "alpha", D("5")),
            ],
        )
        self.assertEqual(second.outstanding_bad_debt, D("2"))

    def test_subsequent_recovery_continues_from_remainder(self):
        engine = ClearingEngine()
        settle_full_bad_debt(engine, "TX-A", "USD", CREDITORS_ABC)

        engine.process_recovery("RC-1", "USD", D("55"))
        second = engine.process_recovery("RC-2", "USD", D("20"))
        # senior 余 5 先清，mezzanine 再分 15。
        self.assertEqual(
            [(a.creditor, a.amount) for a in second.allocations],
            [("senior", D("5")), ("mezzanine", D("15"))],
        )
        self.assertEqual(second.outstanding_bad_debt, D("25"))

    def test_float_amount_normalized_exactly(self):
        engine = ClearingEngine()
        settle_full_bad_debt(engine, "TX-A", "USD", [("c", D("100"))])
        result = engine.process_recovery("RC-1", "USD", 10.5)
        self.assertEqual(result.recovery_amount, D("10.5"))
        self.assertEqual(result.total_recovered, D("10.5"))


class ZeroRecoveryTests(unittest.TestCase):
    def test_zero_recovery_with_outstanding_creates_empty_event(self):
        engine = ClearingEngine()
        settle_full_bad_debt(engine, "TX-A", "USD", CREDITORS_ABC)
        events_before = len(engine.audit_log)

        result = engine.process_recovery("RC-0", "USD", D("0"))
        self.assertEqual(result.total_recovered, D("0"))
        self.assertEqual(result.allocations, ())
        self.assertEqual(result.outstanding_bad_debt, D("100"))
        self.assertEqual(len(engine.audit_log), events_before + 1)
        event = engine.get_event("RC-0")
        self.assertEqual(event.event_id, "EVT-RC-0-recovery")
        self.assertEqual(event.recovery_allocations, ())

    def test_zero_recovery_without_outstanding_raises(self):
        engine = ClearingEngine()
        settle_full_bad_debt(engine, "TX-A", "USD", CREDITORS_ABC)
        engine.process_recovery("RC-1", "USD", D("100"))
        with self.assertRaises(NoOutstandingBadDebtError):
            engine.process_recovery("RC-2", "USD", D("0"))


class RecoveryErrorTests(unittest.TestCase):
    def _engine_with_bad_debt(self):
        engine = ClearingEngine()
        settle_full_bad_debt(engine, "TX-A", "USD", CREDITORS_ABC)
        return engine

    def test_no_outstanding_bad_debt_scenarios(self):
        # 完全没有结算。
        with self.assertRaises(NoOutstandingBadDebtError):
            ClearingEngine().process_recovery("RC-1", "USD", D("10"))

        # 只有拒绝结算：不确认坏账。
        engine = ClearingEngine()
        engine.process(
            "RX-1", "USD", pool_balance=D("10"),
            settlement_amount=D("50"), notional_exposure=D("0"),
            base_limit=D("1000"), risk_factor=D("0"),
            creditors=CREDITORS_ABC,
        )
        with self.assertRaises(NoOutstandingBadDebtError):
            engine.process_recovery("RC-1", "USD", D("10"))

        # 该币种坏账已被全部回收。
        engine = self._engine_with_bad_debt()
        engine.process_recovery("RC-1", "USD", D("100"))
        with self.assertRaises(NoOutstandingBadDebtError):
            engine.process_recovery("RC-2", "USD", D("0.01"))

    def test_amount_exceeds_outstanding(self):
        engine = self._engine_with_bad_debt()
        with self.assertRaises(RecoveryAmountExceedsOutstandingError):
            engine.process_recovery("RC-1", "USD", D("100.01"))
        # 边界取等号合法。
        result = engine.process_recovery("RC-1", "USD", D("100"))
        self.assertEqual(result.outstanding_bad_debt, D("0"))

    def test_duplicate_recovery_id(self):
        engine = self._engine_with_bad_debt()
        engine.process_recovery("RC-1", "USD", D("10"))
        with self.assertRaises(DuplicateTransactionError):
            engine.process_recovery("RC-1", "USD", D("10"))

    def test_recovery_id_collides_with_settlement_id(self):
        engine = self._engine_with_bad_debt()
        with self.assertRaises(DuplicateTransactionError):
            engine.process_recovery("TX-A", "USD", D("10"))

    def test_missing_currency(self):
        engine = self._engine_with_bad_debt()
        for bad in (None, "", "   "):
            with self.subTest(bad=bad):
                with self.assertRaises(InvalidCurrencyError):
                    engine.process_recovery("RC-X", bad, D("10"))

    def test_invalid_amounts(self):
        engine = self._engine_with_bad_debt()
        for bad in (D("-0.01"), -1, float("nan"), float("inf"),
                    D("NaN"), D("-Infinity"), "10", None, True):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    engine.process_recovery("RC-X", "USD", bad)

    def test_invalid_recovery_transaction_id(self):
        engine = self._engine_with_bad_debt()
        for bad in (None, "", "   "):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    engine.process_recovery(bad, "USD", D("10"))

    def test_failure_has_no_side_effects(self):
        engine = self._engine_with_bad_debt()
        events_before = tuple(engine.audit_log)
        seq_before = engine.audit_log[-1].sequence

        for call in (
            lambda: engine.process_recovery("RC-1", "USD", D("200")),
            lambda: engine.process_recovery("RC-1", "EUR", D("10")),
            lambda: engine.process_recovery("RC-1", "USD", D("-1")),
        ):
            with self.assertRaises(Exception):
                call()

        # 无新事件、序号不增、流水号未被占用。
        self.assertEqual(tuple(engine.audit_log), events_before)
        self.assertEqual(engine.audit_log[-1].sequence, seq_before)
        self.assertFalse(engine.has_transaction("RC-1"))
        self.assertIsNone(engine.recovery_of("RC-1"))
        # 失败的流水号可在修正后重新提交。
        result = engine.process_recovery("RC-1", "USD", D("10"))
        self.assertEqual(result.total_recovered, D("10"))


class RecoveryAuditTests(unittest.TestCase):
    def test_recovery_event_continues_sequence_and_keeps_settlement_events(self):
        engine = ClearingEngine()
        settle_full_bad_debt(engine, "TX-A", "USD", CREDITORS_ABC)
        settle_full_bad_debt(engine, "TX-B", "USD", [("c", D("5"))])
        self.assertEqual([e.sequence for e in engine.audit_log], [1, 2])

        result = engine.process_recovery("RC-1", "USD", D("70"))
        events = engine.audit_log
        self.assertEqual(len(events), 3)
        event = events[-1]
        self.assertEqual(event.sequence, 3)
        self.assertEqual(event.event_id, result.event_id)
        self.assertEqual(event.transaction_id, "RC-1")
        self.assertEqual(event.currency, "USD")
        self.assertTrue(event.approved)
        self.assertEqual(event.validation_result, "RECOVERY")
        self.assertEqual(event.uncovered_bad_debt, D("35"))
        self.assertEqual(event.pool_allocations, ())
        self.assertEqual(event.capital_allocations, ())
        self.assertEqual(event.rejection_reason, None)

        # 既有结算事件值不变：recovery_allocations 恒为空元组。
        for settlement_event in events[:2]:
            self.assertEqual(settlement_event.recovery_allocations, ())
            self.assertIn(
                settlement_event.validation_result, ("APPROVED", "REJECTED")
            )

    def test_recovery_allocations_in_event_match_result(self):
        engine = ClearingEngine()
        settle_full_bad_debt(engine, "TX-A", "USD", CREDITORS_ABC)
        result = engine.process_recovery("RC-1", "USD", D("70"))
        event = engine.get_event("RC-1")
        self.assertEqual(
            event.recovery_allocations,
            tuple(
                (
                    a.source_transaction_id,
                    a.creditor,
                    a.amount,
                    a.remaining_bad_debt,
                )
                for a in result.allocations
            ),
        )
        self.assertEqual(
            sum((row[2] for row in event.recovery_allocations), D("0")),
            result.total_recovered,
        )

    def test_failed_recovery_does_not_consume_sequence(self):
        engine = ClearingEngine()
        settle_full_bad_debt(engine, "TX-A", "USD", CREDITORS_ABC)
        with self.assertRaises(RecoveryAmountExceedsOutstandingError):
            engine.process_recovery("RC-BAD", "USD", D("999"))
        result = engine.process_recovery("RC-1", "USD", D("10"))
        self.assertEqual(engine.get_event("RC-1").sequence, 2)
        self.assertEqual(result.event_id, "EVT-RC-1-recovery")


class RecoveryQueryTests(unittest.TestCase):
    def test_recovery_of_returns_result_or_none(self):
        engine = ClearingEngine()
        settle_full_bad_debt(engine, "TX-A", "USD", CREDITORS_ABC)
        self.assertIsNone(engine.recovery_of("RC-1"))
        submitted = engine.process_recovery("RC-1", "USD", D("10"))
        self.assertIs(engine.recovery_of("RC-1"), submitted)

    def test_outstanding_bad_debts_fields_and_order(self):
        engine = ClearingEngine()
        settle_full_bad_debt(
            engine, "TX-A", "USD", [("senior", D("60")), ("equity", D("10"))]
        )
        settle_full_bad_debt(engine, "TX-B", "USD", [("alpha", D("7"))])
        engine.process_recovery("RC-1", "USD", D("60"))  # senior 清零

        outstanding = engine.outstanding_bad_debts("USD")
        self.assertEqual(
            [(o.source_transaction_id, o.creditor, o.currency, o.balance)
             for o in outstanding],
            [
                ("TX-A", "equity", "USD", D("10")),
                ("TX-B", "alpha", "USD", D("7")),
            ],
        )
        self.assertEqual(
            sum((o.balance for o in outstanding), D("0")), D("17")
        )

    def test_queries_are_read_only(self):
        engine = ClearingEngine()
        settle_full_bad_debt(engine, "TX-A", "USD", CREDITORS_ABC)
        snapshot = tuple(engine.audit_log)
        engine.recovery_of("nope")
        engine.outstanding_bad_debts("USD")
        engine.outstanding_bad_debts("EUR")
        engine.outstanding_bad_debts("")
        self.assertEqual(tuple(engine.audit_log), snapshot)

    def test_currency_isolation(self):
        engine = ClearingEngine()
        settle_full_bad_debt(engine, "TX-USD", "USD", [("a", D("100"))])
        settle_full_bad_debt(engine, "TX-EUR", "EUR", [("b", D("50"))])

        result = engine.process_recovery("RC-1", "USD", D("40"))
        self.assertEqual(result.outstanding_bad_debt, D("60"))

        eur = engine.outstanding_bad_debts("EUR")
        self.assertEqual(len(eur), 1)
        self.assertEqual(eur[0].balance, D("50"))
        self.assertEqual(
            engine.outstanding_bad_debts("USD")[0].balance, D("60")
        )

        # 其他币种的回收不能冲减 USD 坏账，反之亦然。
        with self.assertRaises(NoOutstandingBadDebtError):
            engine.process_recovery("RC-2", "JPY", D("1"))
        eur_recovery = engine.process_recovery("RC-2", "EUR", D("50"))
        self.assertEqual(eur_recovery.outstanding_bad_debt, D("0"))
        self.assertEqual(
            engine.outstanding_bad_debts("USD")[0].balance, D("60")
        )

    def test_historical_settlement_result_not_rewritten(self):
        engine = ClearingEngine()
        settled = settle_full_bad_debt(
            engine, "TX-A", "USD", CREDITORS_ABC
        )
        engine.process_recovery("RC-1", "USD", D("100"))
        stored = engine.result_of("TX-A")
        self.assertIs(stored, settled)
        self.assertEqual(stored.uncovered_bad_debt, D("100"))
        self.assertEqual(
            tuple(a.bad_debt for a in stored.attributions),
            (D("60"), D("30"), D("10")),
        )

    def test_results_are_immutable(self):
        engine = ClearingEngine()
        settle_full_bad_debt(engine, "TX-A", "USD", CREDITORS_ABC)
        result = engine.process_recovery("RC-1", "USD", D("10"))
        with self.assertRaises(Exception):
            result.total_recovered = D("999")
        with self.assertRaises(Exception):
            engine.outstanding_bad_debts("USD")[0].balance = D("1")


class BatchSourcedBadDebtTests(unittest.TestCase):
    def test_bad_debt_from_batch_is_recoverable(self):
        engine = ClearingEngine()
        batch = engine.process_batch(
            currency="USD",
            opening_pool_balance=D("0"),
            requests=[
                {
                    "transaction_id": "B-1",
                    "settlement_amount": D("0"),
                    "notional_exposure": D("0"),
                    "base_limit": D("0"),
                    "risk_factor": D("0"),
                    "creditors": [("senior", D("40")), ("equity", D("20"))],
                },
                {
                    "transaction_id": "B-2",
                    "settlement_amount": D("0"),
                    "notional_exposure": D("0"),
                    "base_limit": D("0"),
                    "risk_factor": D("0"),
                    "creditors": [("alpha", D("30"))],
                },
            ],
        )
        self.assertTrue(all(r.approved for r in batch.results))

        result = engine.process_recovery("RC-1", "USD", D("50"))
        self.assertEqual(
            [(a.source_transaction_id, a.creditor, a.amount)
             for a in result.allocations],
            [
                ("B-1", "senior", D("40")),
                ("B-1", "equity", D("10")),
            ],
        )
        self.assertEqual(result.outstanding_bad_debt, D("40"))


if __name__ == "__main__":
    unittest.main()
