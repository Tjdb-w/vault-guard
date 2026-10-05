"""同币种批次提交前只读预演（preview_batch）公开行为测试。

覆盖：预演与正式提交逐笔一致（除 event_id）、只读且幂等（不生成事件 /
不占流水号 / 不改余额、坏账台账与审计核对）、业务拒绝结构、校验顺序与
异常类型沿用 process_batch、异常不留半批状态、结果结构不可变。
"""

import unittest
from dataclasses import FrozenInstanceError
from decimal import Decimal

from vault_guard import (
    BatchPreviewResult,
    ClearingEngine,
    DuplicateTransactionError,
    EmptyBatchError,
    EmptyCreditorListError,
    InvalidCurrencyError,
    InvalidRiskFactorError,
    MixedCurrencyError,
    SettlementPreview,
)

D = Decimal


def req(tid, amount, exposure="0", limit="1000", factor="0",
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


class PreviewBatchTests(unittest.TestCase):
    def test_preview_matches_committed_results_except_event_id(self):
        requests = [
            req("P1", "40"),
            req("P2", "30", exposure="100", factor="0.5", limit="1000"),
            req("P3", "50"),  # 余额不足：40+30 后仅剩 30
            req("P4", "10", capital="100"),
        ]
        engine = ClearingEngine()
        preview = engine.preview_batch("USD", D("100"), requests)
        committed = engine.process_batch("USD", D("100"), requests)

        self.assertIsInstance(preview, BatchPreviewResult)
        self.assertEqual(len(preview.results), 4)
        for item in preview.results:
            self.assertIsInstance(item, SettlementPreview)
            self.assertFalse(hasattr(item, "event_id"))
        self.assertEqual(
            [preview_fields(item) for item in preview.results],
            [preview_fields(item) for item in committed.results],
        )
        self.assertEqual(
            preview.validated_available_balance,
            committed.validated_available_balance,
        )
        # 正式提交补上了事件标识。
        self.assertEqual(
            [r.event_id for r in committed.results],
            [
                "EVT-P1-approved",
                "EVT-P2-approved",
                "EVT-P3-rejected",
                "EVT-P4-approved",
            ],
        )

    def test_rejected_item_reports_reason_input_balance_and_zero_allocations(self):
        engine = ClearingEngine()
        preview = engine.preview_batch(
            "USD",
            D("100"),
            [
                req("R1", "80"),
                req("R2", "50"),  # SETTLEMENT_EXCEEDS_AVAILABLE_BALANCE
                req("R3", "10", exposure="100", factor="1", limit="50"),
            ],
        )
        over_balance, over_limit = preview.results[1], preview.results[2]

        self.assertFalse(over_balance.approved)
        self.assertEqual(
            over_balance.rejection_reason,
            "SETTLEMENT_EXCEEDS_AVAILABLE_BALANCE",
        )
        # 拒绝项余额为输入（滚动）余额，分配与坏账均为零。
        self.assertEqual(over_balance.validated_available_balance, D("20"))
        self.assertEqual(over_balance.pool_allocations, (D(0), D(0)))
        self.assertEqual(over_balance.capital_allocations, (D(0), D(0)))
        self.assertEqual(over_balance.uncovered_bad_debt, D(0))
        self.assertTrue(all(a.bad_debt == D(0) for a in over_balance.attributions))

        self.assertFalse(over_limit.approved)
        self.assertEqual(
            over_limit.rejection_reason, "RISK_OCCUPANCY_EXCEEDS_BASE_LIMIT"
        )
        # 风险占用与正式提交同义：拟清算金额 + 名义敞口 × 风险系数。
        self.assertEqual(over_limit.risk_occupancy, D("110"))
        # 拒绝不改滚动余额，后续请求继续执行。
        self.assertEqual(preview.validated_available_balance, D("20"))

    def test_preview_is_read_only_and_idempotent(self):
        engine = ClearingEngine()
        engine.process_batch("USD", D("100"), [req("A1", "40", capital="0")])
        events_before = engine.audit_log
        bad_debt_before = engine.outstanding_bad_debts("USD")
        summary_before = engine.audit_reconciliation("USD")

        requests = [req("B1", "20"), req("B2", "5")]
        first = engine.preview_batch("USD", D("100"), requests)
        second = engine.preview_batch("USD", D("100"), requests)

        # 幂等：重复预演结果相等。
        self.assertEqual(first, second)
        # 不生成事件、不改审计序号与核对快照。
        self.assertEqual(engine.audit_log, events_before)
        self.assertEqual(engine.audit_reconciliation("USD"), summary_before)
        # 不占流水号、不写结果索引。
        self.assertFalse(engine.has_transaction("B1"))
        self.assertFalse(engine.has_transaction("B2"))
        self.assertIsNone(engine.result_of("B1"))
        self.assertIsNone(engine.get_event("B1"))
        # 不改坏账台账。
        self.assertEqual(engine.outstanding_bad_debts("USD"), bad_debt_before)
        # 预演后可正常提交相同流水号。
        committed = engine.process_batch("USD", D("100"), requests)
        self.assertEqual(
            [preview_fields(r) for r in committed.results],
            [preview_fields(r) for r in first.results],
        )

    def test_preview_uses_current_ledger_state(self):
        engine = ClearingEngine()
        engine.process_batch("USD", D("100"), [req("A1", "70")])
        # 与台账重复的流水号在预演中同样拒绝。
        with self.assertRaises(DuplicateTransactionError):
            engine.preview_batch("USD", D("100"), [req("A1", "1")])
        # 预演基于期初入参滚动，不读取也不写回引擎余额。
        preview = engine.preview_batch("USD", D("30"), [req("B1", "30")])
        self.assertEqual(preview.validated_available_balance, D("0"))

    def test_bad_debt_attribution_matches_commit(self):
        engine = ClearingEngine()
        preview = engine.preview_batch(
            "USD",
            D("100"),
            [req("B1", "60", creditors=(("senior", "80"), ("equity", "20")),
                 capital="10")],
        )
        item = preview.results[0]
        self.assertTrue(item.approved)
        self.assertEqual(item.pool_allocations, (D("60"), D("0")))
        self.assertEqual(item.capital_allocations, (D("10"), D("0")))
        self.assertEqual(item.uncovered_bad_debt, D("30"))
        self.assertEqual(item.total_pool_allocated, D("60"))
        self.assertEqual(item.total_capital_allocated, D("10"))
        # 预演不把坏账写入存续台账。
        self.assertEqual(engine.outstanding_bad_debts("USD"), ())

    def test_validation_errors_match_process_batch(self):
        engine = ClearingEngine()
        with self.assertRaises(EmptyBatchError):
            engine.preview_batch("USD", D("100"), [])
        with self.assertRaises(InvalidCurrencyError):
            engine.preview_batch("  ", D("100"), [req("B1", "1")])
        with self.assertRaises(MixedCurrencyError):
            engine.preview_batch(
                "USD",
                D("100"),
                [req("B1", "1", creditors=(("senior", "1", "EUR"),))],
            )
        with self.assertRaises(DuplicateTransactionError):
            engine.preview_batch(
                "USD", D("100"), [req("B1", "1"), req("B1", "2")]
            )
        with self.assertRaises(EmptyCreditorListError):
            engine.preview_batch(
                "USD", D("100"), [req("B1", "1", creditors=())]
            )
        with self.assertRaises(InvalidRiskFactorError):
            engine.preview_batch(
                "USD", D("100"), [req("B1", "1", factor="1.5")]
            )
        with self.assertRaises(ValueError):
            engine.preview_batch("USD", D("-1"), [req("B1", "1")])
        with self.assertRaises(ValueError):
            bad = req("B1", "1")
            bad["settlement_amount"] = "abc"
            engine.preview_batch("USD", D("100"), [bad])
        # 异常不留半批状态。
        self.assertEqual(engine.audit_log, ())
        self.assertFalse(engine.has_transaction("B1"))

    def test_results_are_immutable(self):
        engine = ClearingEngine()
        preview = engine.preview_batch("USD", D("100"), [req("B1", "10")])
        with self.assertRaises(FrozenInstanceError):
            preview.validated_available_balance = D("0")
        with self.assertRaises(FrozenInstanceError):
            preview.results[0].approved = False


if __name__ == "__main__":
    unittest.main()
