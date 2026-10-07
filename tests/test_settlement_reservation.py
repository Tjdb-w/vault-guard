"""组合限额两阶段预占（reserve / confirm / cancel / reservation_of）测试。

覆盖：reserve 的两层占额与快照口径、活动预占跨调用累计、拒绝不占额度、
confirm 的占额迁移 / 审计追加 / 坏账确认与 recovery / writeoff /
transaction_id 语义、cancel 的释放语义、终态误操作、四类预占异常、
校验失败不留状态、既有入口行为不变。
"""

import unittest
from dataclasses import FrozenInstanceError
from decimal import Decimal

from vault_guard import (
    ClearingEngine,
    CurrencyMismatchError,
    DuplicateSettlementIdError,
    DuplicateSettlementReservationError,
    InvalidSettlementAmountError,
    InvalidSettlementBatchError,
    InvalidSettlementPriorityError,
    InvalidSettlementReservationError,
    RiskPolicyNotFoundError,
    SettlementReservation,
    SettlementReservationNotFoundError,
    SettlementReservationStateError,
    SettlementReservationView,
)

D = Decimal


def rec(sid, debtor, amount, priority, treasury=None, currency=None,
        creditors=(("senior", "60"), ("equity", "40")), capital="0"):
    if isinstance(amount, str):
        try:
            amount = D(amount)
        except ArithmeticError:
            pass
    item = {
        "settlement_id": sid,
        "debtor_id": debtor,
        "amount": amount,
        "priority": priority,
        "creditors": [
            (name, D(claim), *rest) for name, claim, *rest in creditors
        ],
        "supplementary_capital": D(capital),
    }
    if treasury is not None:
        item["treasury_id"] = treasury
    if currency is not None:
        item["currency"] = currency
    return item


def policy(treasury=None, debtor=None):
    return {
        "treasury_limits": {k: D(v) for k, v in (treasury or {}).items()},
        "debtor_limits": {k: D(v) for k, v in (debtor or {}).items()},
    }


def snapshot(treasury=None, debtor=None):
    return {
        "treasury": {k: D(v) for k, v in (treasury or {}).items()},
        "debtor": {k: D(v) for k, v in (debtor or {}).items()},
    }


class ReserveSettlementBatchTest(unittest.TestCase):
    def setUp(self):
        self.engine = ClearingEngine()

    def reserve(self, records, reservation_id="R-1", batch_id="B-1",
                currency="USD", snap=None, pol=None):
        return self.engine.reserve_settlement_batch(
            reservation_id=reservation_id,
            batch_id=batch_id,
            currency=currency,
            reservation_snapshot=snap if snap is not None else snapshot(),
            risk_policy=(
                pol if pol is not None
                else policy(treasury={"T1": "1000"}, debtor={"D1": "1000"})
            ),
            records=records,
        )

    def test_basic_reserve_result_and_snapshot(self):
        result = self.reserve(
            [rec("S1", "D1", "100", 1, treasury="T1")],
            snap=snapshot(treasury={"T1": "50"}, debtor={"D1": "25"}),
        )
        self.assertIsInstance(result, SettlementReservation)
        self.assertEqual(result.reservation_id, "R-1")
        self.assertEqual(result.batch_id, "B-1")
        self.assertEqual(result.currency, "USD")
        self.assertEqual(result.status, "RESERVED")
        self.assertEqual(result.accepted_count, 1)
        self.assertEqual(result.rejected_count, 0)
        self.assertTrue(result.results[0].accepted)
        self.assertEqual(result.results[0].reason_code, "ACCEPTED")

        treasury = result.treasury_limits["T1"]
        self.assertEqual(treasury.initial_reserved, D("50"))
        self.assertEqual(treasury.accepted_reserved, D("100"))
        self.assertEqual(treasury.final_reserved, D("150"))
        self.assertEqual(treasury.limit, D("1000"))
        debtor = result.debtor_limits["D1"]
        self.assertEqual(debtor.initial_reserved, D("25"))
        self.assertEqual(debtor.final_reserved, D("125"))

    def test_reserve_is_immutable(self):
        result = self.reserve([rec("S1", "D1", "10", 1)])
        with self.assertRaises(FrozenInstanceError):
            result.status = "CONFIRMED"
        with self.assertRaises(FrozenInstanceError):
            result.results[0].accepted = False

    def test_reserve_sorting_and_limit_exceeded_match_evaluate(self):
        result = self.reserve(
            [
                rec("S-2", "D1", "60", 1, treasury="T1"),
                rec("S-10", "D1", "50", 1, treasury="T1"),
                rec("S-9", "D1", "10", 2, treasury="T1"),
            ],
            pol=policy(treasury={"T1": "1000"}, debtor={"D1": "100"}),
        )
        # priority 升序、同优先级按 settlement_id 码点升序（"S-10" < "S-2"）。
        self.assertEqual(
            [r.settlement_id for r in result.results],
            ["S-10", "S-2", "S-9"],
        )
        outcomes = {r.settlement_id: r.accepted for r in result.results}
        self.assertEqual(outcomes, {"S-10": True, "S-2": False, "S-9": True})
        rejected = result.results[1]
        self.assertEqual(rejected.reason_code, "LIMIT_EXCEEDED")
        self.assertEqual(rejected.pool_allocations, (D("0"), D("0")))
        self.assertEqual(rejected.uncovered_bad_debt, D("0"))
        # 拒绝不占额度：只累计受理的 50 + 10。
        self.assertEqual(result.debtor_limits["D1"].final_reserved, D("60"))
        self.assertEqual(result.debtor_limits["D1"].rejected_reserved, D("60"))
        self.assertEqual(result.treasury_limits["T1"].final_reserved, D("60"))

    def test_active_reservations_accumulate_across_calls(self):
        first = self.reserve([rec("S1", "D1", "60", 1, treasury="T1")],
                             reservation_id="R-1")
        self.assertEqual(first.debtor_limits["D1"].final_reserved, D("60"))
        # 后续 reserve 计入此前活动预占：60 + 50 > 100 拒绝。
        second = self.reserve(
            [rec("S2", "D1", "50", 1, treasury="T1")],
            reservation_id="R-2", batch_id="B-2",
            pol=policy(treasury={"T1": "1000"}, debtor={"D1": "100"}),
        )
        self.assertFalse(second.results[0].accepted)
        self.assertEqual(second.results[0].reason_code, "LIMIT_EXCEEDED")
        # 起算占额含此前活动预占。
        self.assertEqual(second.debtor_limits["D1"].initial_reserved, D("60"))
        self.assertEqual(second.debtor_limits["D1"].final_reserved, D("60"))
        self.assertEqual(second.treasury_limits["T1"].initial_reserved, D("60"))

    def test_snapshot_and_active_reservation_stack(self):
        self.reserve([rec("S1", "D1", "40", 1)], reservation_id="R-1")
        result = self.reserve(
            [rec("S2", "D1", "50", 1)],
            reservation_id="R-2", batch_id="B-2",
            snap=snapshot(debtor={"D1": "10"}),
        )
        # 起算 = 快照 10 + 活动预占 40。
        self.assertEqual(result.debtor_limits["D1"].initial_reserved, D("50"))
        self.assertEqual(result.debtor_limits["D1"].final_reserved, D("100"))

    def test_reserve_waterfall_and_bad_debt_attribution(self):
        result = self.reserve([
            rec("S1", "D1", "50", 1,
                creditors=(("senior", "60"), ("equity", "40")), capital="25"),
        ])
        record = result.results[0]
        self.assertEqual(record.pool_allocations, (D("50"), D("0")))
        self.assertEqual(record.capital_allocations, (D("10"), D("15")))
        self.assertEqual(record.uncovered_bad_debt, D("25"))

    def test_reserve_does_not_touch_existing_engine_state(self):
        self.engine.process(
            transaction_id="TX-1", currency="USD", pool_balance=D("100"),
            settlement_amount=D("50"), notional_exposure=D("0"),
            base_limit=D("100"), risk_factor=D("0"),
            creditors=[("senior", D("60"))],
        )
        events_before = self.engine.audit_log
        outstanding_before = self.engine.outstanding_bad_debts("USD")
        self.reserve([rec("S1", "D1", "10", 1)])
        # reserve 不追加审计、不占流水号、不确认坏账。
        self.assertEqual(self.engine.audit_log, events_before)
        self.assertFalse(self.engine.has_transaction("S1"))
        self.assertEqual(
            self.engine.outstanding_bad_debts("USD"), outstanding_before
        )
        summary = self.engine.audit_reconciliation("USD")
        self.assertEqual(summary.settlement_count, 1)

    def test_reserve_blocks_evaluate_settlement_id_reuse(self):
        self.reserve([rec("S1", "D1", "10", 1)], reservation_id="R-1")
        # 活动预占的未确认标识在后续 reserve 中重复。
        with self.assertRaises(DuplicateSettlementIdError):
            self.reserve([rec("S1", "D1", "5", 1)],
                         reservation_id="R-2", batch_id="B-2")


class ConfirmSettlementBatchTest(unittest.TestCase):
    def setUp(self):
        self.engine = ClearingEngine()

    def reserve(self, records, reservation_id="R-1", batch_id="B-1",
                currency="USD", snap=None, pol=None):
        return self.engine.reserve_settlement_batch(
            reservation_id=reservation_id,
            batch_id=batch_id,
            currency=currency,
            reservation_snapshot=snap if snap is not None else snapshot(),
            risk_policy=(
                pol if pol is not None
                else policy(treasury={"T1": "1000"}, debtor={"D1": "1000"})
            ),
            records=records,
        )

    def test_confirm_returns_confirmed_result(self):
        self.reserve([rec("S1", "D1", "10", 1)])
        confirmed = self.engine.confirm_settlement_batch("R-1")
        self.assertEqual(confirmed.status, "CONFIRMED")
        self.assertEqual(confirmed.reservation_id, "R-1")
        self.assertEqual(confirmed.accepted_count, 1)
        self.assertEqual(confirmed.results[0].settlement_id, "S1")

    def test_confirm_appends_audit_and_confirms_bad_debt(self):
        self.reserve([
            rec("S1", "D1", "50", 1,
                creditors=(("senior", "60"), ("equity", "40")), capital="25"),
            rec("S2", "D1", "2000", 2),
        ])
        confirmed = self.engine.confirm_settlement_batch("R-1")
        self.assertEqual(confirmed.rejected_count, 1)

        events = self.engine.audit_log
        self.assertEqual(len(events), 2)
        accepted_event, rejected_event = events
        self.assertEqual(accepted_event.event_id, "EVT-S1-approved")
        self.assertEqual(accepted_event.validation_result, "APPROVED")
        self.assertEqual(accepted_event.transaction_id, "S1")
        self.assertEqual(accepted_event.uncovered_bad_debt, D("25"))
        self.assertEqual(
            accepted_event.pool_allocations,
            (("senior", D("50")), ("equity", D("0"))),
        )
        self.assertEqual(
            accepted_event.capital_allocations,
            (("senior", D("10")), ("equity", D("15"))),
        )
        self.assertEqual(rejected_event.event_id, "EVT-S2-rejected")
        self.assertEqual(rejected_event.validation_result, "REJECTED")
        self.assertEqual(rejected_event.rejection_reason, "LIMIT_EXCEEDED")
        self.assertEqual(rejected_event.uncovered_bad_debt, D("0"))
        self.assertEqual([e.sequence for e in events], [1, 2])

        # 受理记录的坏账进入存续台账；拒绝记录不确认坏账。
        outstanding = self.engine.outstanding_bad_debts("USD")
        self.assertEqual(len(outstanding), 1)
        self.assertEqual(outstanding[0].source_transaction_id, "S1")
        self.assertEqual(outstanding[0].creditor, "equity")
        self.assertEqual(outstanding[0].balance, D("25"))

        # transaction_id 语义：结果可查、流水号已登记。
        self.assertTrue(self.engine.has_transaction("S1"))
        result = self.engine.result_of("S1")
        self.assertIsNotNone(result)
        self.assertTrue(result.approved)
        self.assertEqual(result.uncovered_bad_debt, D("25"))
        self.assertEqual(result.event_id, "EVT-S1-approved")

    def test_confirmed_bad_debt_supports_recovery_and_writeoff(self):
        self.reserve([
            rec("S1", "D1", "50", 1,
                creditors=(("senior", "60"), ("equity", "40"))),
        ])
        self.engine.confirm_settlement_batch("R-1")
        # 坏账合计 50（senior 10 + equity 40）。
        recovery = self.engine.process_recovery("REC-1", "USD", D("20"))
        self.assertEqual(recovery.total_recovered, D("20"))
        self.assertEqual(recovery.allocations[0].source_transaction_id, "S1")
        writeoff = self.engine.process_writeoff("WO-1", "USD", D("30"))
        self.assertEqual(writeoff.total_written_off, D("30"))
        self.assertEqual(self.engine.outstanding_bad_debts("USD"), ())
        # 审计核对恒等式保持。
        summary = self.engine.audit_reconciliation("USD")
        self.assertEqual(summary.initial_bad_debt, D("50"))
        self.assertEqual(
            summary.initial_bad_debt
            - summary.recovered_amount
            - summary.written_off_amount,
            summary.outstanding_bad_debt,
        )

    def test_confirmed_occupancy_keeps_blocking(self):
        self.reserve([rec("S1", "D1", "60", 1)], reservation_id="R-1")
        self.engine.confirm_settlement_batch("R-1")
        # 已确认占额继续阻止超额：60 + 50 > 100。
        result = self.engine.reserve_settlement_batch(
            reservation_id="R-2", batch_id="B-2", currency="USD",
            reservation_snapshot=snapshot(),
            risk_policy=policy(debtor={"D1": "100"}),
            records=[rec("S2", "D1", "50", 1)],
        )
        self.assertFalse(result.results[0].accepted)
        self.assertEqual(result.debtor_limits["D1"].initial_reserved, D("60"))

    def test_confirm_registers_settlement_ids(self):
        self.reserve([rec("S1", "D1", "10", 1)])
        self.engine.confirm_settlement_batch("R-1")
        # 确认后标识转为已登记：试算与预占均不得复用。
        with self.assertRaises(DuplicateSettlementIdError):
            self.engine.evaluate_settlement_batch(
                "B-9", "USD", snapshot(),
                policy(debtor={"D1": "100"}), [rec("S1", "D1", "5", 1)],
            )
        with self.assertRaises(DuplicateSettlementIdError):
            self.reserve([rec("S1", "D1", "5", 1)],
                         reservation_id="R-2", batch_id="B-2")

    def test_confirm_terminal_state_errors(self):
        self.reserve([rec("S1", "D1", "10", 1)])
        self.engine.confirm_settlement_batch("R-1")
        with self.assertRaises(SettlementReservationStateError):
            self.engine.confirm_settlement_batch("R-1")
        with self.assertRaises(SettlementReservationStateError):
            self.engine.cancel_settlement_batch("R-1")


class CancelSettlementBatchTest(unittest.TestCase):
    def setUp(self):
        self.engine = ClearingEngine()

    def reserve(self, records, reservation_id="R-1", batch_id="B-1",
                pol=None):
        return self.engine.reserve_settlement_batch(
            reservation_id=reservation_id,
            batch_id=batch_id,
            currency="USD",
            reservation_snapshot=snapshot(),
            risk_policy=(
                pol if pol is not None
                else policy(treasury={"T1": "1000"}, debtor={"D1": "1000"})
            ),
            records=records,
        )

    def test_cancel_releases_occupancy_and_ids(self):
        self.reserve([rec("S1", "D1", "60", 1)], reservation_id="R-1")
        cancelled = self.engine.cancel_settlement_batch("R-1")
        self.assertEqual(cancelled.status, "CANCELLED")
        self.assertEqual(cancelled.reservation_id, "R-1")

        # 释放活动预占：同样的记录可以重新预占成功。
        result = self.reserve([rec("S1", "D1", "60", 1)], reservation_id="R-2")
        self.assertTrue(result.results[0].accepted)
        self.assertEqual(result.debtor_limits["D1"].initial_reserved, D("0"))

    def test_cancel_produces_no_audit_or_bad_debt(self):
        self.reserve([
            rec("S1", "D1", "50", 1,
                creditors=(("senior", "60"), ("equity", "40"))),
        ])
        self.engine.cancel_settlement_batch("R-1")
        self.assertEqual(self.engine.audit_log, ())
        self.assertEqual(self.engine.outstanding_bad_debts("USD"), ())
        self.assertFalse(self.engine.has_transaction("S1"))
        self.assertIsNone(self.engine.result_of("S1"))

    def test_cancel_terminal_state_errors(self):
        self.reserve([rec("S1", "D1", "10", 1)])
        self.engine.cancel_settlement_batch("R-1")
        with self.assertRaises(SettlementReservationStateError):
            self.engine.confirm_settlement_batch("R-1")
        with self.assertRaises(SettlementReservationStateError):
            self.engine.cancel_settlement_batch("R-1")


class ReservationQueryTest(unittest.TestCase):
    def setUp(self):
        self.engine = ClearingEngine()

    def reserve(self, records, reservation_id="R-1", batch_id="B-1"):
        return self.engine.reserve_settlement_batch(
            reservation_id=reservation_id,
            batch_id=batch_id,
            currency="USD",
            reservation_snapshot=snapshot(),
            risk_policy=policy(treasury={"T1": "100"}, debtor={"D1": "100"}),
            records=records,
        )

    def test_query_reserved_view(self):
        self.reserve([
            rec("S1", "D1", "60", 1, treasury="T1"),
            rec("S2", "D1", "50", 2, treasury="T1"),
        ])
        view = self.engine.reservation_of("R-1")
        self.assertIsInstance(view, SettlementReservationView)
        self.assertEqual(view.status, "RESERVED")
        self.assertEqual(view.batch_id, "B-1")
        self.assertEqual(view.currency, "USD")
        self.assertEqual(
            dict(view.reason_codes),
            {"S1": "ACCEPTED", "S2": "LIMIT_EXCEEDED"},
        )
        # 占额只含受理记录。
        self.assertEqual(dict(view.treasury_reserved), {"T1": D("60")})
        self.assertEqual(dict(view.debtor_reserved), {"D1": D("60")})

    def test_query_confirmed_view(self):
        self.reserve([rec("S1", "D1", "60", 1)])
        self.engine.confirm_settlement_batch("R-1")
        view = self.engine.reservation_of("R-1")
        self.assertEqual(view.status, "CONFIRMED")
        self.assertEqual(dict(view.debtor_reserved), {"D1": D("60")})

    def test_query_cancelled_view_released(self):
        self.reserve([rec("S1", "D1", "60", 1)])
        self.engine.cancel_settlement_batch("R-1")
        view = self.engine.reservation_of("R-1")
        self.assertEqual(view.status, "CANCELLED")
        self.assertEqual(dict(view.treasury_reserved), {})
        self.assertEqual(dict(view.debtor_reserved), {})
        self.assertEqual(dict(view.reason_codes), {"S1": "ACCEPTED"})

    def test_query_unknown_and_invalid_id(self):
        with self.assertRaises(SettlementReservationNotFoundError):
            self.engine.reservation_of("R-unknown")
        with self.assertRaises(InvalidSettlementReservationError):
            self.engine.reservation_of("")
        with self.assertRaises(InvalidSettlementReservationError):
            self.engine.reservation_of(None)


class ReservationErrorTest(unittest.TestCase):
    def setUp(self):
        self.engine = ClearingEngine()

    def reserve(self, records, reservation_id="R-1", batch_id="B-1",
                currency="USD", snap=None, pol=None):
        return self.engine.reserve_settlement_batch(
            reservation_id=reservation_id,
            batch_id=batch_id,
            currency=currency,
            reservation_snapshot=snap if snap is not None else snapshot(),
            risk_policy=(
                pol if pol is not None
                else policy(treasury={"T1": "1000"}, debtor={"D1": "1000"})
            ),
            records=records,
        )

    def test_missing_or_empty_reservation_id(self):
        for bad in ("", "  ", None, 123):
            with self.subTest(bad=bad):
                with self.assertRaises(InvalidSettlementReservationError):
                    self.reserve([rec("S1", "D1", "10", 1)], reservation_id=bad)
        for method in (
            self.engine.confirm_settlement_batch,
            self.engine.cancel_settlement_batch,
            self.engine.reservation_of,
        ):
            with self.assertRaises(InvalidSettlementReservationError):
                method(" ")

    def test_duplicate_reservation_id(self):
        self.reserve([rec("S1", "D1", "10", 1)], reservation_id="R-1")
        with self.assertRaises(DuplicateSettlementReservationError):
            self.reserve([rec("S2", "D1", "10", 1)], reservation_id="R-1")
        # 终态（确认 / 取消）后标识仍保留，不得复用。
        self.engine.confirm_settlement_batch("R-1")
        with self.assertRaises(DuplicateSettlementReservationError):
            self.reserve([rec("S2", "D1", "10", 1)], reservation_id="R-1")
        self.reserve([rec("S3", "D1", "10", 1)], reservation_id="R-2",
                     batch_id="B-2")
        self.engine.cancel_settlement_batch("R-2")
        with self.assertRaises(DuplicateSettlementReservationError):
            self.reserve([rec("S4", "D1", "10", 1)], reservation_id="R-2")

    def test_unknown_reservation_id(self):
        with self.assertRaises(SettlementReservationNotFoundError):
            self.engine.confirm_settlement_batch("R-unknown")
        with self.assertRaises(SettlementReservationNotFoundError):
            self.engine.cancel_settlement_batch("R-unknown")

    def test_input_validation_mirrors_evaluate(self):
        with self.assertRaises(InvalidSettlementBatchError):
            self.reserve([rec("S1", "D1", "10", 1)], batch_id="")
        with self.assertRaises(InvalidSettlementBatchError):
            self.reserve([rec("S1", "D1", "10", 1)], currency=" ")
        with self.assertRaises(InvalidSettlementBatchError):
            self.engine.reserve_settlement_batch(
                "R-1", "B-1", "USD", snapshot(), None,
                [rec("S1", "D1", "10", 1)],
            )
        with self.assertRaises(DuplicateSettlementIdError):
            self.reserve([rec("S1", "D1", "10", 1), rec("S1", "D1", "5", 2)])
        with self.assertRaises(RiskPolicyNotFoundError):
            self.reserve([rec("S1", "D-x", "10", 1)])
        with self.assertRaises(CurrencyMismatchError):
            self.reserve([rec("S1", "D1", "10", 1, currency="EUR")])
        with self.assertRaises(InvalidSettlementAmountError):
            self.reserve([rec("S1", "D1", "-5", 1)])
        with self.assertRaises(InvalidSettlementPriorityError):
            self.reserve([rec("S1", "D1", "10", 1.5)])

    def test_failed_validation_leaves_no_state(self):
        # 校验失败不产生占额、审计、坏账或标识登记，可修正后重提。
        with self.assertRaises(InvalidSettlementAmountError):
            self.reserve([rec("S1", "D1", "-5", 1)], reservation_id="R-1")
        with self.assertRaises(SettlementReservationNotFoundError):
            self.engine.reservation_of("R-1")
        result = self.reserve([rec("S1", "D1", "5", 1)], reservation_id="R-1")
        self.assertTrue(result.results[0].accepted)
        self.assertEqual(self.engine.audit_log, ())
        self.assertEqual(self.engine.outstanding_bad_debts("USD"), ())

    def test_existing_entries_unchanged(self):
        # 既有入口行为不变：试算、单笔与批次处理不受预占功能影响。
        evaluation = self.engine.evaluate_settlement_batch(
            "B-0", "USD", snapshot(), policy(debtor={"D1": "100"}),
            [rec("E1", "D1", "10", 1)],
        )
        self.assertTrue(evaluation.results[0].accepted)
        result = self.engine.process(
            transaction_id="TX-1", currency="USD", pool_balance=D("100"),
            settlement_amount=D("50"), notional_exposure=D("0"),
            base_limit=D("100"), risk_factor=D("0"),
            creditors=[("senior", D("60"))],
        )
        self.assertTrue(result.approved)
        # 确认后的预占与既有台账共同对账。
        self.reserve([rec("S1", "D1", "10", 1,
                          creditors=(("senior", "20"),))],
                     reservation_id="R-1", batch_id="B-1")
        self.engine.confirm_settlement_batch("R-1")
        summary = self.engine.audit_reconciliation("USD")
        self.assertEqual(summary.settlement_count, 2)
        self.assertEqual(summary.approved_count, 2)


if __name__ == "__main__":
    unittest.main()
