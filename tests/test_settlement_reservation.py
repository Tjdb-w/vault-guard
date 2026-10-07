"""组合限额两阶段占用（reserve / confirm / cancel + 只读查询）公开行为测试。

覆盖：两层活动预占与已确认占额累计、后续预占计入快照与未结预占、确认
转已确认占额并重验余量、确认写既有结构审计与坏账并衔接 recovery /
writeoff、取消释放活动预占与未确认 settlement_id、状态机终态误操作、
只读查询视图、四类预占标识异常、reserve 输入异常沿用 evaluate 口径且
不留任何状态、既有入口行为保持不变。
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
    SettlementReservationView,
    SettlementReservationNotFoundError,
    SettlementReservationStateError,
    RESERVATION_STATE_CANCELLED,
    RESERVATION_STATE_CONFIRMED,
    RESERVATION_STATE_RESERVED,
)

D = Decimal


def rec(sid, debtor, amount, priority, treasury=None, currency=None,
        creditors=(("senior", "60"), ("equity", "40")), capital="0"):
    item = {
        "settlement_id": sid,
        "debtor_id": debtor,
        "amount": D(str(amount)),
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


POLICY = policy(treasury={"T1": "1000"}, debtor={"D1": "1000"})
SNAP = snapshot(treasury={"T1": "50"}, debtor={"D1": "25"})


class ReserveSettlementBatchTest(unittest.TestCase):
    def setUp(self):
        self.engine = ClearingEngine()

    def reserve(self, reservation_id, records, batch_id="B-1", currency="USD",
                snap=None, pol=None):
        return self.engine.reserve_settlement_batch(
            reservation_id,
            batch_id,
            currency,
            SNAP if snap is None else snap,
            POLICY if pol is None else pol,
            records,
        )

    def test_reserve_returns_immutable_reserved_result(self):
        result = self.reserve("R-1", [rec("S1", "D1", "100", 1, treasury="T1")])
        self.assertIsInstance(result, SettlementReservation)
        self.assertEqual(result.reservation_id, "R-1")
        self.assertEqual(result.state, RESERVATION_STATE_RESERVED)
        self.assertEqual(result.batch_id, "B-1")
        self.assertEqual(result.currency, "USD")
        self.assertEqual(result.event_ids, ())
        self.assertEqual(result.accepted_count, 1)
        self.assertEqual(result.rejected_count, 0)
        with self.assertRaises(FrozenInstanceError):
            result.state = RESERVATION_STATE_CONFIRMED
        with self.assertRaises(FrozenInstanceError):
            result.results[0].accepted = False

    def test_reserve_two_layer_snapshot_includes_external_snapshot(self):
        result = self.reserve("R-1", [rec("S1", "D1", "100", 1, treasury="T1")])
        treasury = result.treasury_limits["T1"]
        self.assertEqual(treasury.initial_reserved, D("50"))
        self.assertEqual(treasury.accepted_reserved, D("100"))
        self.assertEqual(treasury.rejected_reserved, D("0"))
        self.assertEqual(treasury.final_reserved, D("150"))
        self.assertEqual(treasury.limit, D("1000"))
        debtor = result.debtor_limits["D1"]
        self.assertEqual(debtor.initial_reserved, D("25"))
        self.assertEqual(debtor.final_reserved, D("125"))

    def test_ordering_limit_exceeded_waterfall_and_bad_debt_match_evaluate(self):
        evaluation = self.engine.evaluate_settlement_batch(
            "B-E", "USD", SNAP, POLICY,
            [
                rec("S-2", "D1", "10", 1),
                rec("S-9", "D1", "10", 2),
                rec("S-10", "D1", "10", 1),
                rec("S-1", "D1", "10", 0),
            ],
        )
        fresh = ClearingEngine()
        reserved = fresh.reserve_settlement_batch(
            "R-1", "B-1", "USD", SNAP, POLICY,
            [
                rec("S-2", "D1", "10", 1),
                rec("S-9", "D1", "10", 2),
                rec("S-10", "D1", "10", 1),
                rec("S-1", "D1", "10", 0),
            ],
        )
        self.assertEqual(
            [r.settlement_id for r in reserved.results],
            [r.settlement_id for r in evaluation.results],
        )
        self.assertEqual(
            [e.reason_code for e in reserved.audit_events],
            [e.reason_code for e in evaluation.audit_events],
        )
        self.assertEqual(reserved.treasury_limits, evaluation.treasury_limits)
        self.assertEqual(reserved.debtor_limits, evaluation.debtor_limits)

    def test_rejected_records_do_not_reserve_capacity(self):
        result = self.reserve(
            "R-1",
            [
                rec("S1", "D1", "60", 1, treasury="T1"),
                rec("S2", "D1", "2000", 2, treasury="T1"),
                rec("S3", "D1", "40", 3, treasury="T1"),
            ],
            snap=snapshot(),
            pol=policy(treasury={"T1": "100"}, debtor={"D1": "1000"}),
        )
        self.assertEqual(
            [r.accepted for r in result.results], [True, False, True]
        )
        # 拒绝不占额度：受理 60 + 40 恰好打满 100；拒绝金额只进统计。
        self.assertEqual(result.treasury_limits["T1"].final_reserved, D("100"))
        self.assertEqual(result.treasury_limits["T1"].rejected_reserved, D("2000"))
        rejected = result.results[1]
        self.assertEqual(rejected.reason_code, "LIMIT_EXCEEDED")
        self.assertEqual(rejected.pool_allocations, (D("0"), D("0")))
        self.assertEqual(rejected.uncovered_bad_debt, D("0"))

    def test_boundary_equality_accepted(self):
        result = self.reserve(
            "R-1",
            [rec("S1", "D1", "100", 1, treasury="T1")],
            snap=snapshot(treasury={"T1": "900"}, debtor={"D1": "900"}),
        )
        self.assertTrue(result.results[0].accepted)
        self.assertEqual(result.treasury_limits["T1"].final_reserved, D("1000"))
        self.assertEqual(result.debtor_limits["D1"].final_reserved, D("1000"))

    def test_reserve_without_audit_bad_debt_or_transaction_registration(self):
        self.reserve(
            "R-1",
            [rec("S1", "D1", "50", 1, treasury="T1", capital="25")],
        )
        # 预占不写审计台账、不确认坏账、不占业务流水号。
        self.assertEqual(self.engine.audit_log, ())
        self.assertEqual(self.engine.outstanding_bad_debts("USD"), ())
        self.assertFalse(self.engine.has_transaction("S1"))
        # 但 settlement_id 已登记用于跨预占 / 试算去重。
        with self.assertRaises(DuplicateSettlementIdError):
            self.engine.evaluate_settlement_batch(
                "B-2", "USD", SNAP, POLICY,
                [rec("S1", "D1", "10", 1, treasury="T1")],
            )

    def test_empty_records_reserve_is_valid(self):
        result = self.reserve("R-1", [])
        self.assertEqual(result.results, ())
        self.assertEqual(result.audit_events, ())
        self.assertEqual(result.accepted_count, 0)
        self.assertEqual(result.rejected_count, 0)


class ActiveReservationAccumulationTest(unittest.TestCase):
    def setUp(self):
        self.engine = ClearingEngine()
        self.pol = policy(treasury={"T1": "100"}, debtor={"D1": "1000"})

    def test_later_reserve_counts_prior_active_and_confirmed_usage(self):
        # 首个预占：外部快照 0，受理 60（活动）。
        first = self.engine.reserve_settlement_batch(
            "R-1", "B-1", "USD", snapshot(), self.pol,
            [rec("S1", "D1", "60", 1, treasury="T1")],
        )
        self.assertEqual(first.treasury_limits["T1"].initial_reserved, D("0"))
        # 第二个预占：外部快照仍给 40，叠加首个活动预占 60 => 起始 100，
        # 再占 1 即超额拒绝。
        second = self.engine.reserve_settlement_batch(
            "R-2", "B-2", "USD", snapshot(treasury={"T1": "40"}), self.pol,
            [rec("S2", "D1", "1", 1, treasury="T1")],
        )
        self.assertEqual(second.treasury_limits["T1"].initial_reserved, D("100"))
        self.assertFalse(second.results[0].accepted)

        # 确认首个预占后，已确认占额继续阻止超额。
        self.engine.confirm_settlement_batch("R-1")
        third = self.engine.reserve_settlement_batch(
            "R-3", "B-3", "USD", snapshot(treasury={"T1": "40"}), self.pol,
            [rec("S3", "D1", "1", 1, treasury="T1")],
        )
        self.assertFalse(third.results[0].accepted)

    def test_cancelled_reservation_releases_capacity(self):
        self.engine.reserve_settlement_batch(
            "R-1", "B-1", "USD", snapshot(), self.pol,
            [rec("S1", "D1", "60", 1, treasury="T1")],
        )
        self.engine.cancel_settlement_batch("R-1")
        # 活动预占释放：起始只剩外部快照 40，60 受理（40+60=100 边界）。
        after = self.engine.reserve_settlement_batch(
            "R-2", "B-2", "USD", snapshot(treasury={"T1": "40"}), self.pol,
            [rec("S2", "D1", "60", 1, treasury="T1")],
        )
        self.assertTrue(after.results[0].accepted)
        self.assertEqual(after.treasury_limits["T1"].initial_reserved, D("40"))

    def test_debtor_layer_accumulates_independently(self):
        # 无 treasury 的记录只占 debtor 层；多预占按 debtor 累计。
        pol = policy(treasury={"T1": "1000"}, debtor={"D1": "100"})
        self.engine.reserve_settlement_batch(
            "R-1", "B-1", "USD", snapshot(), pol,
            [rec("S1", "D1", "60", 1)],
        )
        second = self.engine.reserve_settlement_batch(
            "R-2", "B-2", "USD", snapshot(), pol,
            [rec("S2", "D1", "40", 1), rec("S3", "D1", "1", 2)],
        )
        self.assertTrue(second.results[0].accepted)
        self.assertFalse(second.results[1].accepted)
        self.assertEqual(second.debtor_limits["D1"].initial_reserved, D("60"))
        self.assertEqual(second.debtor_limits["D1"].final_reserved, D("100"))


class ConfirmSettlementBatchTest(unittest.TestCase):
    def setUp(self):
        self.engine = ClearingEngine()

    def test_confirm_appends_audit_events_and_keeps_reason_codes(self):
        records = [
            rec("S1", "D1", "50", 1, treasury="T1", capital="25"),
            rec("S2", "D1", "2000", 2, treasury="T1",
                creditors=(("bond", "10"),)),
        ]
        reserved = self.engine.reserve_settlement_batch(
            "R-1", "B-1", "USD", SNAP, POLICY, records
        )
        confirmed = self.engine.confirm_settlement_batch("R-1")

        self.assertEqual(confirmed.state, RESERVATION_STATE_CONFIRMED)
        self.assertEqual(
            confirmed.event_ids, ("EVT-S1-approved", "EVT-S2-rejected")
        )
        self.assertEqual(confirmed.results, reserved.results)
        self.assertEqual(confirmed.audit_events, reserved.audit_events)

        events = self.engine.audit_log
        self.assertEqual(len(events), 2)
        self.assertEqual([event.sequence for event in events], [1, 2])
        approved, rejected = events
        self.assertEqual(approved.transaction_id, "S1")
        self.assertEqual(approved.validation_result, "APPROVED")
        self.assertTrue(approved.approved)
        self.assertIsNone(approved.rejection_reason)
        self.assertEqual(approved.pool_allocations, (("senior", D("50")), ("equity", D("0"))))
        self.assertEqual(approved.capital_allocations, (("senior", D("10")), ("equity", D("15"))))
        self.assertEqual(approved.uncovered_bad_debt, D("25"))
        self.assertEqual(rejected.transaction_id, "S2")
        self.assertEqual(rejected.validation_result, "REJECTED")
        self.assertFalse(rejected.approved)
        self.assertEqual(rejected.rejection_reason, "LIMIT_EXCEEDED")
        self.assertEqual(rejected.pool_allocations, (("bond", D("0")),))
        self.assertEqual(rejected.uncovered_bad_debt, D("0"))

    def test_confirm_registers_bad_debt_and_transaction_semantics(self):
        self.engine.reserve_settlement_batch(
            "R-1", "B-1", "USD", SNAP, POLICY,
            [rec("S1", "D1", "50", 1, treasury="T1", capital="25")],
        )
        self.engine.confirm_settlement_batch("R-1")

        # settlement_id 成为业务流水号：结果索引、坏账轨迹与报告可见。
        self.assertTrue(self.engine.has_transaction("S1"))
        settled = self.engine.result_of("S1")
        self.assertTrue(settled.approved)
        self.assertEqual(settled.event_id, "EVT-S1-approved")
        self.assertEqual(settled.uncovered_bad_debt, D("25"))

        outstanding = self.engine.outstanding_bad_debts("USD")
        self.assertEqual(
            [(entry.source_transaction_id, entry.creditor, entry.balance)
             for entry in outstanding],
            [("S1", "equity", D("25"))],
        )
        trail = self.engine.bad_debt_trail("S1")
        self.assertEqual(trail.initial_bad_debt, D("25"))
        self.assertEqual(trail.outstanding_bad_debt, D("25"))

        report = self.engine.creditor_bad_debt_report("USD")
        self.assertEqual(len(report), 1)
        self.assertEqual(report[0].creditor, "equity")
        self.assertEqual(report[0].source_transaction_ids, ("S1",))
        self.assertEqual(report[0].outstanding_bad_debt, D("25"))

        summary = self.engine.audit_reconciliation("USD")
        self.assertEqual(summary.settlement_count, 1)
        self.assertEqual(summary.approved_count, 1)
        self.assertEqual(summary.rejected_count, 0)
        self.assertEqual(summary.initial_bad_debt, D("25"))

    def test_confirmed_bad_debt_supports_recovery_and_writeoff(self):
        self.engine.reserve_settlement_batch(
            "R-1", "B-1", "USD", SNAP, POLICY,
            [rec("S1", "D1", "50", 1, treasury="T1", capital="25")],
        )
        self.engine.confirm_settlement_batch("R-1")

        recovery = self.engine.process_recovery("RC-1", "USD", D("10"))
        self.assertEqual(recovery.total_recovered, D("10"))
        self.assertEqual(
            recovery.allocations[0].source_transaction_id, "S1"
        )
        self.assertEqual(recovery.outstanding_bad_debt, D("15"))

        writeoff = self.engine.process_writeoff("WO-1", "USD", D("15"))
        self.assertEqual(writeoff.total_written_off, D("15"))
        self.assertEqual(self.engine.outstanding_bad_debts("USD"), ())

        trail = self.engine.bad_debt_trail("S1")
        self.assertEqual(trail.recovered_amount, D("10"))
        self.assertEqual(trail.written_off_amount, D("15"))
        self.assertEqual(trail.outstanding_bad_debt, D("0"))
        self.assertEqual(
            [operation.operation_id for operation in trail.recoveries],
            ["RC-1"],
        )
        self.assertEqual(
            [operation.operation_id for operation in trail.writeoffs],
            ["WO-1"],
        )

    def test_confirmed_bad_debt_supports_targeted_operations(self):
        self.engine.reserve_settlement_batch(
            "R-1", "B-1", "USD", SNAP, POLICY,
            [rec("S1", "D1", "50", 1, treasury="T1", capital="25")],
        )
        self.engine.confirm_settlement_batch("R-1")
        targeted = self.engine.process_targeted_recovery(
            "RC-9", "USD", "S1", "equity", D("25")
        )
        self.assertEqual(
            [(a.source_transaction_id, a.creditor, a.recovered_amount)
             for a in targeted.allocations],
            [("S1", "equity", D("25"))],
        )

    def test_confirm_moves_usage_to_confirmed_bucket(self):
        pol = policy(treasury={"T1": "100"}, debtor={"D1": "1000"})
        self.engine.reserve_settlement_batch(
            "R-1", "B-1", "USD", snapshot(), pol,
            [rec("S1", "D1", "60", 1, treasury="T1")],
        )
        self.engine.confirm_settlement_batch("R-1")
        # 已确认占额计入后续预占：外部 0 + 已确认 60，50 受理（60+50>100
        # 才拒绝，40 受理）。
        accepted = self.engine.reserve_settlement_batch(
            "R-2", "B-2", "USD", snapshot(), pol,
            [rec("S2", "D1", "40", 1, treasury="T1")],
        )
        self.assertTrue(accepted.results[0].accepted)
        rejected = self.engine.reserve_settlement_batch(
            "R-3", "B-3", "USD", snapshot(), pol,
            [rec("S3", "D1", "1", 1, treasury="T1")],
        )
        self.assertFalse(rejected.results[0].accepted)

    def test_confirm_rechecks_limits_and_rolls_back_atomically(self):
        pol = policy(treasury={"T1": "100"}, debtor={"D1": "1000"})
        # A 确认 50。
        self.engine.reserve_settlement_batch(
            "A", "BA", "USD", snapshot(), pol,
            [rec("SA", "D1", "50", 1, treasury="T1")],
        )
        self.engine.confirm_settlement_batch("A")
        # B 用外部快照 40：40 + 已确认 50 = 90，再占 10 受理，保持活动。
        self.engine.reserve_settlement_batch(
            "B", "BB", "USD", snapshot(treasury={"T1": "40"}), pol,
            [rec("SB", "D1", "10", 1, treasury="T1")],
        )
        # D 占 40 并先确认：已确认 50 -> 90。
        self.engine.reserve_settlement_batch(
            "D", "BD", "USD", snapshot(), pol,
            [rec("SD", "D1", "40", 1, treasury="T1")],
        )
        self.engine.confirm_settlement_batch("D")

        events_before = self.engine.audit_log
        # B 确认时按其快照 40 + 已确认 90 = 130，+10 > 100：拒绝且原子回滚。
        with self.assertRaises(InvalidSettlementBatchError):
            self.engine.confirm_settlement_batch("B")
        self.assertEqual(self.engine.audit_log, events_before)
        view = self.engine.get_settlement_reservation("B")
        self.assertEqual(view.state, RESERVATION_STATE_RESERVED)
        # 预占仍可取消；取消后 SB 标识释放、活动占额回落。
        self.engine.cancel_settlement_batch("B")
        reused = self.engine.evaluate_settlement_batch(
            "BX", "USD", snapshot(), pol,
            [rec("SB", "D1", "5", 1, treasury="T1")],
        )
        self.assertTrue(reused.results[0].accepted)

    def test_confirm_empty_reservation_adds_no_events(self):
        self.reserve_empty = self.engine.reserve_settlement_batch(
            "R-0", "B-0", "USD", SNAP, POLICY, []
        )
        confirmed = self.engine.confirm_settlement_batch("R-0")
        self.assertEqual(confirmed.state, RESERVATION_STATE_CONFIRMED)
        self.assertEqual(confirmed.event_ids, ())
        self.assertEqual(self.engine.audit_log, ())

    def test_terminal_state_operations_rejected(self):
        self.engine.reserve_settlement_batch(
            "R-1", "B-1", "USD", SNAP, POLICY,
            [rec("S1", "D1", "10", 1, treasury="T1")],
        )
        self.engine.confirm_settlement_batch("R-1")
        with self.assertRaises(SettlementReservationStateError):
            self.engine.cancel_settlement_batch("R-1")
        with self.assertRaises(SettlementReservationStateError):
            self.engine.confirm_settlement_batch("R-1")


class CancelSettlementBatchTest(unittest.TestCase):
    def setUp(self):
        self.engine = ClearingEngine()

    def reserve(self, reservation_id, records, snap=None, pol=None):
        return self.engine.reserve_settlement_batch(
            reservation_id, "B-" + reservation_id, "USD",
            SNAP if snap is None else snap,
            POLICY if pol is None else pol,
            records,
        )

    def test_cancel_releases_active_usage_and_unconfirmed_ids(self):
        self.reserve("R-1", [rec("S1", "D1", "100", 1, treasury="T1")])
        cancelled = self.engine.cancel_settlement_batch("R-1")
        self.assertEqual(cancelled.state, RESERVATION_STATE_CANCELLED)
        self.assertEqual(cancelled.event_ids, ())
        self.assertEqual(cancelled.results[0].settlement_id, "S1")

        # 不生成审计、不确认坏账、不登记业务流水号。
        self.assertEqual(self.engine.audit_log, ())
        self.assertEqual(self.engine.outstanding_bad_debts("USD"), ())
        self.assertFalse(self.engine.has_transaction("S1"))

        # settlement_id 释放：后续 evaluate / reserve 可复用。
        evaluation = self.engine.evaluate_settlement_batch(
            "B-2", "USD", SNAP, POLICY,
            [rec("S1", "D1", "100", 1, treasury="T1")],
        )
        self.assertTrue(evaluation.results[0].accepted)

    def test_cancel_releases_rejected_and_accepted_ids(self):
        # 取消同时释放受理与拒绝记录的 settlement_id。
        self.reserve(
            "R-1",
            [
                rec("S1", "D1", "10", 1, treasury="T1"),
                rec("S2", "D1", "2000", 2, treasury="T1"),
            ],
            snap=snapshot(),
            pol=policy(treasury={"T1": "100"}, debtor={"D1": "1000"}),
        )
        self.engine.cancel_settlement_batch("R-1")
        for sid in ("S1", "S2"):
            result = self.engine.evaluate_settlement_batch(
                f"B-{sid}", "USD", snapshot(),
                policy(treasury={"T1": "100"}, debtor={"D1": "1000"}),
                [rec(sid, "D1", "10", 1, treasury="T1")],
            )
            self.assertTrue(result.results[0].accepted)

    def test_cancel_does_not_change_pool_or_other_reservations(self):
        self.reserve("R-1", [rec("S1", "D1", "60", 1, treasury="T1")],
                     snap=snapshot(),
                     pol=policy(treasury={"T1": "1000"}, debtor={"D1": "1000"}))
        other = self.reserve("R-2", [rec("S2", "D1", "30", 1, treasury="T1")],
                             snap=snapshot(),
                             pol=policy(treasury={"T1": "1000"}, debtor={"D1": "1000"}))
        self.assertEqual(other.treasury_limits["T1"].initial_reserved, D("60"))
        self.engine.cancel_settlement_batch("R-1")
        # R-2 仍然活动且占额不变；其占额快照保留建立时刻的值。
        view = self.engine.get_settlement_reservation("R-2")
        self.assertEqual(view.state, RESERVATION_STATE_RESERVED)
        self.assertEqual(view.treasury_limits["T1"].initial_reserved, D("60"))
        confirmed = self.engine.confirm_settlement_batch("R-2")
        self.assertEqual(confirmed.event_ids, ("EVT-S2-approved",))

    def test_cancelled_reservation_is_terminal(self):
        self.reserve("R-1", [rec("S1", "D1", "10", 1, treasury="T1")])
        self.engine.cancel_settlement_batch("R-1")
        with self.assertRaises(SettlementReservationStateError):
            self.engine.confirm_settlement_batch("R-1")
        with self.assertRaises(SettlementReservationStateError):
            self.engine.cancel_settlement_batch("R-1")
        # 终态预占标识仍占位：重复 reserve 同一 reservation_id 判重。
        with self.assertRaises(DuplicateSettlementReservationError):
            self.reserve("R-1", [rec("S9", "D1", "10", 1, treasury="T1")])


class GetSettlementReservationTest(unittest.TestCase):
    def setUp(self):
        self.engine = ClearingEngine()

    def test_view_exposes_only_state_batch_currency_reasons_and_usage(self):
        self.engine.reserve_settlement_batch(
            "R-1", "B-1", "USD", SNAP, POLICY,
            [
                rec("S1", "D1", "100", 1, treasury="T1"),
                rec("S2", "D1", "2000", 2, treasury="T1"),
            ],
        )
        view = self.engine.get_settlement_reservation("R-1")
        self.assertIsInstance(view, SettlementReservationView)
        self.assertEqual(view.reservation_id, "R-1")
        self.assertEqual(view.state, RESERVATION_STATE_RESERVED)
        self.assertEqual(view.batch_id, "B-1")
        self.assertEqual(view.currency, "USD")
        self.assertEqual(view.reason_codes, ("ACCEPTED", "LIMIT_EXCEEDED"))
        self.assertEqual(view.treasury_limits["T1"].final_reserved, D("150"))
        self.assertEqual(view.debtor_limits["D1"].final_reserved, D("125"))
        # 只读视图不含逐记录结果 / 审计事件 / event_ids。
        self.assertFalse(hasattr(view, "results"))
        self.assertFalse(hasattr(view, "audit_events"))
        self.assertFalse(hasattr(view, "event_ids"))
        with self.assertRaises(FrozenInstanceError):
            view.state = RESERVATION_STATE_CONFIRMED

    def test_view_tracks_terminal_states(self):
        self.engine.reserve_settlement_batch(
            "R-1", "B-1", "USD", SNAP, POLICY,
            [rec("S1", "D1", "10", 1, treasury="T1")],
        )
        self.engine.confirm_settlement_batch("R-1")
        self.assertEqual(
            self.engine.get_settlement_reservation("R-1").state,
            RESERVATION_STATE_CONFIRMED,
        )

        self.engine.reserve_settlement_batch(
            "R-2", "B-2", "USD", SNAP, POLICY,
            [rec("S2", "D1", "10", 1, treasury="T1")],
        )
        self.engine.cancel_settlement_batch("R-2")
        self.assertEqual(
            self.engine.get_settlement_reservation("R-2").state,
            RESERVATION_STATE_CANCELLED,
        )

    def test_view_identifier_errors(self):
        with self.assertRaises(InvalidSettlementReservationError):
            self.engine.get_settlement_reservation("")
        with self.assertRaises(InvalidSettlementReservationError):
            self.engine.get_settlement_reservation("   ")
        with self.assertRaises(InvalidSettlementReservationError):
            self.engine.get_settlement_reservation(None)
        with self.assertRaises(SettlementReservationNotFoundError):
            self.engine.get_settlement_reservation("missing")

    def test_view_is_read_only(self):
        events_before = self.engine.audit_log
        self.engine.reserve_settlement_batch(
            "R-1", "B-1", "USD", SNAP, POLICY,
            [rec("S1", "D1", "10", 1, treasury="T1")],
        )
        for _ in range(2):
            view = self.engine.get_settlement_reservation("R-1")
            self.assertEqual(view.state, RESERVATION_STATE_RESERVED)
        self.assertEqual(self.engine.audit_log, events_before)


class ReservationIdentifierErrorTest(unittest.TestCase):
    def setUp(self):
        self.engine = ClearingEngine()

    def test_missing_or_blank_reservation_id(self):
        for bad in (None, "", "   ", 123, b"R-1"):
            with self.subTest(bad=bad):
                with self.assertRaises(InvalidSettlementReservationError):
                    self.engine.reserve_settlement_batch(
                        bad, "B", "USD", SNAP, POLICY,
                        [rec("S1", "D1", "10", 1, treasury="T1")],
                    )
                with self.assertRaises(InvalidSettlementReservationError):
                    self.engine.confirm_settlement_batch(bad)
                with self.assertRaises(InvalidSettlementReservationError):
                    self.engine.cancel_settlement_batch(bad)

    def test_duplicate_reservation_id_including_terminal(self):
        records = [rec("S1", "D1", "10", 1, treasury="T1")]
        self.engine.reserve_settlement_batch("R-1", "B-1", "USD", SNAP, POLICY, records)
        with self.assertRaises(DuplicateSettlementReservationError):
            self.engine.reserve_settlement_batch(
                "R-1", "B-2", "USD", SNAP, POLICY,
                [rec("S2", "D1", "10", 1, treasury="T1")],
            )
        self.engine.confirm_settlement_batch("R-1")
        with self.assertRaises(DuplicateSettlementReservationError):
            self.engine.reserve_settlement_batch(
                "R-1", "B-3", "USD", SNAP, POLICY,
                [rec("S3", "D1", "10", 1, treasury="T1")],
            )

    def test_unknown_identifier_on_confirm_and_cancel(self):
        with self.assertRaises(SettlementReservationNotFoundError):
            self.engine.confirm_settlement_batch("missing")
        with self.assertRaises(SettlementReservationNotFoundError):
            self.engine.cancel_settlement_batch("missing")


class ReserveInputValidationTest(unittest.TestCase):
    """reserve 输入异常完全沿用 evaluate_settlement_batch，且不留状态。"""

    def setUp(self):
        self.engine = ClearingEngine()

    def reserve(self, reservation_id, *args, **kwargs):
        return self.engine.reserve_settlement_batch(
            reservation_id, *args, **kwargs
        )

    def test_invalid_batch_inputs(self):
        with self.assertRaises(InvalidSettlementBatchError):
            self.reserve("R-1", "", "USD", SNAP, POLICY, [])
        with self.assertRaises(InvalidSettlementBatchError):
            self.reserve("R-1", "B", " ", SNAP, POLICY, [])
        with self.assertRaises(InvalidSettlementBatchError):
            self.reserve("R-1", "B", "USD", SNAP, None, [])

    def test_duplicate_settlement_id(self):
        with self.assertRaises(DuplicateSettlementIdError):
            self.reserve(
                "R-1", "B", "USD", SNAP, POLICY,
                [rec("S1", "D1", "10", 1), rec("S1", "D1", "5", 2)],
            )
        # 批内成功后，跨预占重复也拒绝。
        self.reserve(
            "R-2", "B-2", "USD", SNAP, POLICY,
            [rec("S2", "D1", "10", 1)],
        )
        with self.assertRaises(DuplicateSettlementIdError):
            self.reserve(
                "R-3", "B-3", "USD", SNAP, POLICY,
                [rec("S2", "D1", "10", 1)],
            )

    def test_policy_not_found_currency_amount_priority(self):
        with self.assertRaises(RiskPolicyNotFoundError):
            self.reserve(
                "R-1", "B", "USD", SNAP,
                policy(debtor={"D1": "100"}),
                [rec("S1", "D-unknown", "10", 1)],
            )
        with self.assertRaises(CurrencyMismatchError):
            self.reserve(
                "R-2", "B", "USD", SNAP, POLICY,
                [rec("S1", "D1", "10", 1, currency="EUR")],
            )
        with self.assertRaises(InvalidSettlementAmountError):
            self.reserve(
                "R-3", "B", "USD", SNAP, POLICY,
                [rec("S1", "D1", "0", 1)],
            )
        with self.assertRaises(InvalidSettlementPriorityError):
            self.reserve(
                "R-4", "B", "USD", SNAP, POLICY,
                [{"settlement_id": "S1", "debtor_id": "D1", "amount": D("10"),
                  "priority": 1.5, "creditors": [("c", D("1"))]}],
            )

    def test_failed_validation_leaves_no_state_at_all(self):
        with self.assertRaises(InvalidSettlementAmountError):
            self.reserve(
                "R-bad", "B", "USD", SNAP, POLICY,
                [rec("S1", "D1", "-5", 1)],
            )
        # 预占标识未登记。
        with self.assertRaises(SettlementReservationNotFoundError):
            self.engine.get_settlement_reservation("R-bad")
        # settlement_id 未登记：修正后可整体重提。
        result = self.reserve(
            "R-bad", "B", "USD", SNAP, POLICY,
            [rec("S1", "D1", "5", 1)],
        )
        self.assertTrue(result.results[0].accepted)
        # 失败不产生占额、审计、坏账。
        self.assertEqual(self.engine.audit_log, ())
        self.assertEqual(self.engine.outstanding_bad_debts("USD"), ())

    def test_duplicate_reservation_id_checked_before_input_validation(self):
        self.engine.reserve_settlement_batch(
            "R-1", "B-1", "USD", SNAP, POLICY,
            [rec("S1", "D1", "10", 1)],
        )
        # 即使本次输入本身也非法，重复标识优先报 Duplicate 预占异常。
        with self.assertRaises(DuplicateSettlementReservationError):
            self.engine.reserve_settlement_batch(
                "R-1", "B-2", "USD", SNAP, POLICY,
                [rec("S1", "D1", "-5", 1)],
            )


class ReservationInteropTest(unittest.TestCase):
    def setUp(self):
        self.engine = ClearingEngine()

    def test_evaluate_keeps_its_own_snapshot_semantics(self):
        # evaluate 的起始占额只读外部快照，不读引擎内部活动 / 已确认桶；
        # 既有入口行为保持不变。
        self.engine.reserve_settlement_batch(
            "R-1", "B-1", "USD", snapshot(),
            policy(treasury={"T1": "1000"}, debtor={"D1": "1000"}),
            [rec("S1", "D1", "100", 1, treasury="T1")],
        )
        evaluation = self.engine.evaluate_settlement_batch(
            "B-E", "USD", snapshot(),
            policy(treasury={"T1": "1000"}, debtor={"D1": "1000"}),
            [rec("S2", "D1", "100", 1, treasury="T1")],
        )
        self.assertEqual(
            evaluation.treasury_limits["T1"].initial_reserved, D("0")
        )
        self.assertTrue(evaluation.results[0].accepted)

    def test_reservations_coexist_with_settlement_audit_entries(self):
        # 既有 process 入口与预占确认共享审计序号与坏账台账，互不破坏。
        self.engine.process(
            transaction_id="TX-1",
            currency="USD",
            pool_balance=D("100"),
            settlement_amount=D("50"),
            notional_exposure=D("0"),
            base_limit=D("100"),
            risk_factor=D("0"),
            creditors=[("senior", D("60"))],
        )
        self.engine.reserve_settlement_batch(
            "R-1", "B-1", "USD", SNAP, POLICY,
            [rec("S1", "D1", "50", 1, treasury="T1", capital="25")],
        )
        self.engine.confirm_settlement_batch("R-1")
        events = self.engine.audit_log
        self.assertEqual([event.sequence for event in events], [1, 2])
        self.assertEqual(events[0].transaction_id, "TX-1")
        self.assertEqual(events[1].transaction_id, "S1")
        # 无风险组的确认记录不进入风险组坏账报告（risk group 语义保留）。
        self.assertEqual(self.engine.risk_group_bad_debt_report("USD"), ())


if __name__ == "__main__":
    unittest.main()
