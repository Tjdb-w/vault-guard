"""清算批次组合限额试算（evaluate_settlement_batch）公开行为测试。

覆盖：两层累计占额预占、优先级与 settlement_id 确定性排序、整笔拒绝与
LIMIT_EXCEEDED、拒绝不影响后续记录、限额快照四字段、瀑布与坏账归因、
reason_code 审计事件、六类校验异常、异常不留状态、跨批次结算标识去重、
试算不改变既有入口状态。
"""

import unittest
from dataclasses import FrozenInstanceError
from decimal import Decimal

from vault_guard import (
    ClearingEngine,
    CurrencyMismatchError,
    DuplicateSettlementIdError,
    InvalidSettlementAmountError,
    InvalidSettlementBatchError,
    InvalidSettlementPriorityError,
    RiskPolicyNotFoundError,
    SettlementBatchEvaluation,
)

D = Decimal


def rec(sid, debtor, amount, priority, treasury=None, currency=None,
        creditors=(("senior", "60"), ("equity", "40")), capital="0"):
    # 字符串金额转为 Decimal（无法转换时原样传入以验证引擎拒绝口径）；
    # float 等原样传入以验证引擎归一化口径。
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


class EvaluateSettlementBatchTest(unittest.TestCase):
    def setUp(self):
        self.engine = ClearingEngine()

    def evaluate(self, records, batch_id="B-1", currency="USD",
                 snap=None, pol=None):
        return self.engine.evaluate_settlement_batch(
            batch_id=batch_id,
            currency=currency,
            reservation_snapshot=(
                snap if snap is not None else snapshot()
            ),
            risk_policy=(
                pol if pol is not None
                else policy(treasury={"T1": "1000"}, debtor={"D1": "1000"})
            ),
            records=records,
        )

    def test_basic_accept_and_limit_report(self):
        result = self.evaluate(
            [rec("S1", "D1", "100", 1, treasury="T1")],
            snap=snapshot(treasury={"T1": "50"}, debtor={"D1": "25"}),
        )
        self.assertIsInstance(result, SettlementBatchEvaluation)
        self.assertEqual(result.batch_id, "B-1")
        self.assertEqual(result.currency, "USD")
        self.assertEqual(result.accepted_count, 1)
        self.assertEqual(result.rejected_count, 0)

        record = result.results[0]
        self.assertTrue(record.accepted)
        self.assertEqual(record.reason_code, "ACCEPTED")
        self.assertEqual(record.amount, D("100"))

        treasury = result.treasury_limits["T1"]
        self.assertEqual(treasury.initial_reserved, D("50"))
        self.assertEqual(treasury.accepted_reserved, D("100"))
        self.assertEqual(treasury.rejected_reserved, D("0"))
        self.assertEqual(treasury.final_reserved, D("150"))
        self.assertEqual(treasury.limit, D("1000"))

        debtor = result.debtor_limits["D1"]
        self.assertEqual(debtor.initial_reserved, D("25"))
        self.assertEqual(debtor.accepted_reserved, D("100"))
        self.assertEqual(debtor.final_reserved, D("125"))

    def test_priority_and_settlement_id_ordering(self):
        # priority 升序；同优先级按 settlement_id 码点升序（"S-10" < "S-2"）。
        result = self.evaluate([
            rec("S-2", "D1", "10", 1),
            rec("S-9", "D1", "10", 2),
            rec("S-10", "D1", "10", 1),
            rec("S-1", "D1", "10", 0),
        ])
        self.assertEqual(
            [r.settlement_id for r in result.results],
            ["S-1", "S-10", "S-2", "S-9"],
        )
        self.assertEqual(
            [event.settlement_id for event in result.audit_events],
            ["S-1", "S-10", "S-2", "S-9"],
        )
        self.assertEqual(
            [event.sequence for event in result.audit_events], [1, 2, 3, 4]
        )

    def test_debtor_limit_exceeded_rejects_whole_record(self):
        result = self.evaluate(
            [
                rec("S1", "D1", "60", 1, treasury="T1"),
                rec("S2", "D1", "50", 2, treasury="T1"),
                rec("S3", "D1", "10", 3, treasury="T1"),
            ],
            pol=policy(treasury={"T1": "1000"}, debtor={"D1": "100"}),
        )
        outcomes = {r.settlement_id: r.accepted for r in result.results}
        self.assertEqual(
            outcomes, {"S1": True, "S2": False, "S3": True}
        )
        rejected = result.results[1]
        self.assertEqual(rejected.reason_code, "LIMIT_EXCEEDED")
        # 拒绝记录不进瀑布、不产生坏账。
        self.assertEqual(rejected.pool_allocations, (D("0"), D("0")))
        self.assertEqual(rejected.uncovered_bad_debt, D("0"))
        # 拒绝不改占额：debtor 层只累计受理的 60 + 10。
        self.assertEqual(result.debtor_limits["D1"].final_reserved, D("70"))
        self.assertEqual(result.debtor_limits["D1"].rejected_reserved, D("50"))
        self.assertEqual(result.treasury_limits["T1"].final_reserved, D("70"))
        self.assertEqual(result.treasury_limits["T1"].rejected_reserved, D("50"))

    def test_treasury_limit_exceeded_rejects(self):
        result = self.evaluate(
            [rec("S1", "D1", "80", 1, treasury="T1")],
            pol=policy(treasury={"T1": "50"}, debtor={"D1": "1000"}),
        )
        record = result.results[0]
        self.assertFalse(record.accepted)
        self.assertEqual(record.reason_code, "LIMIT_EXCEEDED")
        self.assertEqual(result.treasury_limits["T1"].final_reserved, D("0"))
        self.assertEqual(result.debtor_limits["D1"].final_reserved, D("0"))

    def test_snapshot_counts_toward_limit(self):
        # 先加快照占额：快照 950 + 本笔 100 超过 1000，拒绝。
        result = self.evaluate(
            [rec("S1", "D1", "100", 1)],
            snap=snapshot(debtor={"D1": "950"}),
        )
        self.assertFalse(result.results[0].accepted)
        self.assertEqual(result.debtor_limits["D1"].final_reserved, D("950"))

    def test_boundary_equality_accepted(self):
        result = self.evaluate(
            [rec("S1", "D1", "100", 1, treasury="T1")],
            snap=snapshot(treasury={"T1": "900"}, debtor={"D1": "900"}),
            pol=policy(treasury={"T1": "1000"}, debtor={"D1": "1000"}),
        )
        self.assertTrue(result.results[0].accepted)
        self.assertEqual(result.treasury_limits["T1"].final_reserved, D("1000"))
        self.assertEqual(result.debtor_limits["D1"].final_reserved, D("1000"))

    def test_record_without_treasury_only_uses_debtor_layer(self):
        result = self.evaluate(
            [rec("S1", "D1", "100", 1)],
            pol=policy(treasury={"T1": "0"}, debtor={"D1": "1000"}),
        )
        self.assertTrue(result.results[0].accepted)
        self.assertEqual(result.treasury_limits["T1"].final_reserved, D("0"))
        self.assertEqual(result.debtor_limits["D1"].final_reserved, D("100"))

    def test_waterfall_and_bad_debt_attribution(self):
        result = self.evaluate([
            rec("S1", "D1", "50", 1,
                creditors=(("senior", "60"), ("equity", "40")),
                capital="25"),
        ])
        record = result.results[0]
        self.assertTrue(record.accepted)
        self.assertEqual(record.pool_allocations, (D("50"), D("0")))
        self.assertEqual(record.capital_allocations, (D("10"), D("15")))
        self.assertEqual(record.uncovered_bad_debt, D("25"))
        by_name = {a.creditor: a for a in record.attributions}
        self.assertEqual(by_name["senior"].bad_debt, D("0"))
        self.assertEqual(by_name["equity"].bad_debt, D("25"))
        self.assertEqual(
            by_name["equity"].pool_allocation
            + by_name["equity"].capital_allocation
            + by_name["equity"].bad_debt,
            D("40"),
        )

    def test_audit_events_carry_reason_codes(self):
        result = self.evaluate(
            [
                rec("S1", "D1", "10", 1),
                rec("S2", "D1", "2000", 2),
            ],
            pol=policy(debtor={"D1": "100"}),
        )
        self.assertEqual(len(result.audit_events), 2)
        first, second = result.audit_events
        self.assertEqual(first.event_id, "EVT-S1-accepted")
        self.assertEqual(first.reason_code, "ACCEPTED")
        self.assertTrue(first.accepted)
        self.assertEqual(first.batch_id, "B-1")
        self.assertEqual(second.event_id, "EVT-S2-rejected")
        self.assertEqual(second.reason_code, "LIMIT_EXCEEDED")
        self.assertFalse(second.accepted)

    def test_deterministic_same_input_same_result(self):
        records = [
            rec("S2", "D1", "30", 1, treasury="T1"),
            rec("S1", "D1", "20", 1, treasury="T1"),
        ]
        first = ClearingEngine().evaluate_settlement_batch(
            "B", "USD", snapshot(treasury={"T1": "5"}),
            policy(treasury={"T1": "100"}, debtor={"D1": "100"}), records,
        )
        second = ClearingEngine().evaluate_settlement_batch(
            "B", "USD", snapshot(treasury={"T1": "5"}),
            policy(treasury={"T1": "100"}, debtor={"D1": "100"}), records,
        )
        self.assertEqual(first, second)

    def test_amount_precision_and_float_normalization(self):
        result = self.evaluate([rec("S1", "D1", 0.6, 1)])
        self.assertEqual(result.results[0].amount, D("0.6"))
        self.assertEqual(result.debtor_limits["D1"].final_reserved, D("0.6"))

    def test_result_is_immutable(self):
        result = self.evaluate([rec("S1", "D1", "10", 1)])
        with self.assertRaises(FrozenInstanceError):
            result.batch_id = "X"
        with self.assertRaises(FrozenInstanceError):
            result.results[0].accepted = False

    # -------------------------------------------------------------- #
    # 异常路径
    # -------------------------------------------------------------- #

    def test_missing_batch_id_currency_or_policy(self):
        with self.assertRaises(InvalidSettlementBatchError):
            self.evaluate([rec("S1", "D1", "10", 1)], batch_id="")
        with self.assertRaises(InvalidSettlementBatchError):
            self.evaluate([rec("S1", "D1", "10", 1)], batch_id=None)
        with self.assertRaises(InvalidSettlementBatchError):
            self.evaluate([rec("S1", "D1", "10", 1)], currency=" ")
        with self.assertRaises(InvalidSettlementBatchError):
            self.engine.evaluate_settlement_batch(
                "B", "USD", snapshot(), None, [rec("S1", "D1", "10", 1)]
            )

    def test_duplicate_settlement_id_in_batch(self):
        with self.assertRaises(DuplicateSettlementIdError):
            self.evaluate([rec("S1", "D1", "10", 1), rec("S1", "D1", "5", 2)])

    def test_duplicate_settlement_id_across_batches(self):
        self.evaluate([rec("S1", "D1", "10", 1)], batch_id="B-1")
        with self.assertRaises(DuplicateSettlementIdError):
            self.evaluate([rec("S1", "D1", "10", 1)], batch_id="B-2")

    def test_risk_policy_not_found(self):
        with self.assertRaises(RiskPolicyNotFoundError):
            self.evaluate(
                [rec("S1", "D-unknown", "10", 1)],
                pol=policy(debtor={"D1": "100"}),
            )
        with self.assertRaises(RiskPolicyNotFoundError):
            self.evaluate(
                [rec("S1", "D1", "10", 1, treasury="T-unknown")],
                pol=policy(treasury={"T1": "100"}, debtor={"D1": "100"}),
            )

    def test_currency_mismatch(self):
        with self.assertRaises(CurrencyMismatchError):
            self.evaluate([rec("S1", "D1", "10", 1, currency="EUR")])
        with self.assertRaises(CurrencyMismatchError):
            self.evaluate([
                rec("S1", "D1", "10", 1,
                    creditors=(("senior", "60", "EUR"),)),
            ])
        # 显式给出相同币种合法。
        result = self.evaluate([rec("S1", "D1", "10", 1, currency="USD")])
        self.assertTrue(result.results[0].accepted)

    def test_invalid_amount(self):
        for bad in ("0", "-1", "abc", None, float("nan"), float("inf")):
            with self.subTest(bad=bad):
                with self.assertRaises(InvalidSettlementAmountError):
                    self.evaluate([rec("S1", "D1", bad, 1)])

    def test_invalid_priority(self):
        for bad in ("1", 1.5, None, True):
            with self.subTest(bad=bad):
                with self.assertRaises(InvalidSettlementPriorityError):
                    self.evaluate([rec("S1", "D1", "10", bad)])

    def test_exception_leaves_no_state(self):
        # 异常不产生受理、坏账或审计结果，也不登记结算标识，可修正后重提。
        with self.assertRaises(InvalidSettlementAmountError):
            self.evaluate([rec("S1", "D1", "-5", 1)])
        result = self.evaluate([rec("S1", "D1", "5", 1)])
        self.assertTrue(result.results[0].accepted)

    def test_evaluation_does_not_touch_existing_engine_state(self):
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
        events_before = self.engine.audit_log
        outstanding_before = self.engine.outstanding_bad_debts("USD")
        self.evaluate([rec("S1", "D1", "10", 1)])
        # 试算不追加审计事件、不占流水号、不改坏账台账与核对结果。
        self.assertEqual(self.engine.audit_log, events_before)
        self.assertEqual(len(self.engine.audit_log), 1)
        self.assertFalse(self.engine.has_transaction("S1"))
        self.assertEqual(
            self.engine.outstanding_bad_debts("USD"), outstanding_before
        )
        summary = self.engine.audit_reconciliation("USD")
        self.assertEqual(summary.settlement_count, 1)
        # 既有流水号命名空间与结算标识互不影响。
        self.evaluate([rec("TX-1", "D1", "10", 1)], batch_id="B-2")

    def test_empty_records_allowed(self):
        result = self.evaluate([])
        self.assertEqual(result.results, ())
        self.assertEqual(result.audit_events, ())
        self.assertEqual(result.accepted_count, 0)
        self.assertEqual(result.debtor_limits["D1"].final_reserved, D("0"))

    def test_snapshot_only_id_appears_in_report(self):
        result = self.evaluate(
            [rec("S1", "D1", "10", 1)],
            snap=snapshot(debtor={"D1": "5", "D-extra": "7"}),
        )
        extra = result.debtor_limits["D-extra"]
        self.assertIsNone(extra.limit)
        self.assertEqual(extra.initial_reserved, D("7"))
        self.assertEqual(extra.final_reserved, D("7"))


if __name__ == "__main__":
    unittest.main()
