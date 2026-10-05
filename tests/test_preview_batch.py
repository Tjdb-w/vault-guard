"""提交前只读预演（preview_batch）公开行为测试。

覆盖：与 process_batch 相同的输入 / 债权格式 / 币种 / 滚动余额 / 清算瀑布；
逐笔结果（除无 event_id）与最终余额同正式提交一致；只读幂等（不生成事件、
不占流水号、不写结果索引与坏账台账、不影响审计核对）；校验顺序与异常沿用
process_batch，异常不留半批状态；返回结构不可变。
"""

import unittest
from dataclasses import FrozenInstanceError
from decimal import Decimal

from vault_guard import (
    BatchPreviewResult,
    ClearingEngine,
    Creditor,
    DuplicateTransactionError,
    EmptyBatchError,
    EmptyCreditorListError,
    InvalidCurrencyError,
    InvalidRiskFactorError,
    MixedCurrencyError,
    SettlementPreview,
)

D = Decimal

# 与 SettlementResult 同名同义的全部字段（SettlementPreview 无 event_id）。
_PREVIEW_FIELDS = (
    "transaction_id",
    "approved",
    "validated_available_balance",
    "creditors",
    "pool_allocations",
    "capital_allocations",
    "attributions",
    "uncovered_bad_debt",
    "risk_occupancy",
    "rejection_reason",
)


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


class PreviewBatchTests(unittest.TestCase):
    def test_rolling_balance_matches_process_batch(self):
        requests = [req("B1", "40"), req("B2", "30"), req("B3", "10")]
        preview = ClearingEngine().preview_batch("USD", D("100"), requests)
        committed = ClearingEngine().process_batch("USD", D("100"), requests)
        self.assertIsInstance(preview, BatchPreviewResult)
        self.assertTrue(
            all(isinstance(r, SettlementPreview) for r in preview.results)
        )
        self.assertEqual(
            [r.validated_available_balance for r in preview.results],
            [D("60"), D("30"), D("20")],
        )
        self.assertEqual(preview.validated_available_balance, D("20"))
        self.assertEqual(
            preview.validated_available_balance,
            committed.validated_available_balance,
        )

    def test_results_follow_request_order_without_event_ids(self):
        preview = ClearingEngine().preview_batch(
            "USD", D("100"), [req("B1", "10"), req("B2", "10"), req("B3", "10")]
        )
        self.assertEqual(
            [r.transaction_id for r in preview.results], ["B1", "B2", "B3"]
        )
        self.assertFalse(hasattr(preview, "event_ids"))
        for result in preview.results:
            self.assertFalse(hasattr(result, "event_id"))

    def test_business_rejection_does_not_raise_and_matches_commit(self):
        requests = [
            req("B1", "40"),                # 通过，余额 10
            req("B2", "20"),                # 超余额，业务拒绝
            req("B3", "10", limit="1000"),  # 通过，余额 0
        ]
        preview = ClearingEngine().preview_batch("USD", D("50"), requests)
        committed = ClearingEngine().process_batch("USD", D("50"), requests)
        p1, p2, p3 = preview.results
        self.assertTrue(p1.approved)
        self.assertFalse(p2.approved)
        self.assertEqual(
            p2.rejection_reason, "SETTLEMENT_EXCEEDS_AVAILABLE_BALANCE"
        )
        # 拒绝：输入余额、零分配、零坏账，随后请求继续滚动。
        self.assertEqual(p2.validated_available_balance, D("10"))
        self.assertEqual(p2.uncovered_bad_debt, D("0"))
        self.assertEqual(p2.total_pool_allocated, D("0"))
        self.assertEqual(p2.total_capital_allocated, D("0"))
        self.assertTrue(p3.approved)
        self.assertEqual(preview.validated_available_balance, D("0"))
        for preview_item, committed_item in zip(
            preview.results, committed.results, strict=True
        ):
            for field in _PREVIEW_FIELDS:
                self.assertEqual(
                    getattr(preview_item, field),
                    getattr(committed_item, field),
                )

    def test_risk_occupancy_rejection_matches_commit(self):
        requests = [req("B1", "40", exposure="100", limit="90", factor="0.6")]
        preview = ClearingEngine().preview_batch("USD", D("100"), requests)
        committed = ClearingEngine().process_batch("USD", D("100"), requests)
        (result,) = preview.results
        self.assertFalse(result.approved)
        self.assertEqual(
            result.rejection_reason, "RISK_OCCUPANCY_EXCEEDS_BASE_LIMIT"
        )
        self.assertEqual(result.risk_occupancy, D("100"))
        self.assertEqual(preview.validated_available_balance, D("100"))
        self.assertEqual(
            result.rejection_reason, committed.results[0].rejection_reason
        )

    def test_waterfall_capital_and_bad_debt_match_commit(self):
        requests = [req("B1", "50", capital="40")]
        preview = ClearingEngine().preview_batch("USD", D("100"), requests)
        committed = ClearingEngine().process_batch("USD", D("100"), requests)
        (result,) = preview.results
        self.assertEqual(result.pool_allocations, (D("50"), D("0")))
        self.assertEqual(result.capital_allocations, (D("10"), D("30")))
        self.assertEqual(result.uncovered_bad_debt, D("10"))
        self.assertEqual(result.validated_available_balance, D("50"))
        for attribution in result.attributions:
            self.assertEqual(
                attribution.pool_allocation
                + attribution.capital_allocation
                + attribution.bad_debt,
                attribution.claim_amount,
            )
        self.assertEqual(
            result.uncovered_bad_debt, committed.results[0].uncovered_bad_debt
        )

    def test_all_creditor_formats_match_process_batch(self):
        requests = [
            {
                "transaction_id": "B1",
                "settlement_amount": D("50"),
                "notional_exposure": D("0"),
                "base_limit": D("100"),
                "risk_factor": D("0"),
                "creditors": [
                    ("senior", D("60")),
                    ("mezz", D("20"), "USD"),
                    {"name": "junior", "amount": D("15")},
                    Creditor(name="equity", amount=D("5")),
                ],
                "supplementary_capital": D("40"),
            }
        ]
        preview = ClearingEngine().preview_batch(" USD ", D("100"), requests)
        committed = ClearingEngine().process_batch(" USD ", D("100"), requests)
        for field in _PREVIEW_FIELDS:
            self.assertEqual(
                getattr(preview.results[0], field),
                getattr(committed.results[0], field),
            )
        self.assertEqual(
            preview.validated_available_balance,
            committed.validated_available_balance,
        )

    def test_preview_then_commit_on_same_engine(self):
        engine = ClearingEngine()
        requests = [req("B1", "40"), req("B2", "20"), req("B3", "10")]
        preview = engine.preview_batch("USD", D("50"), requests)
        # 预演不占流水号：随后相同输入可在同一引擎正式提交。
        committed = engine.process_batch("USD", D("50"), requests)
        for preview_item, committed_item in zip(
            preview.results, committed.results, strict=True
        ):
            for field in _PREVIEW_FIELDS:
                self.assertEqual(
                    getattr(preview_item, field),
                    getattr(committed_item, field),
                )
            self.assertTrue(committed_item.event_id)
        self.assertEqual(
            preview.validated_available_balance,
            committed.validated_available_balance,
        )
        self.assertEqual(
            [event.event_id for event in engine.audit_log],
            list(committed.event_ids),
        )


class PreviewReadOnlyTests(unittest.TestCase):
    def test_preview_leaves_no_trace(self):
        engine = ClearingEngine()
        requests = [req("B1", "40"), req("B2", "20", capital="5")]
        engine.preview_batch("USD", D("50"), requests)
        self.assertEqual(engine.audit_log, ())
        self.assertEqual(engine.events(), ())
        self.assertEqual(len(engine.audit_log), 0)
        for tid in ("B1", "B2"):
            self.assertFalse(engine.has_transaction(tid))
            self.assertIsNone(engine.result_of(tid))
            self.assertIsNone(engine.get_event(tid))
        self.assertEqual(engine.outstanding_bad_debts("USD"), ())
        summary = engine.audit_reconciliation("USD")
        self.assertEqual(summary.settlement_count, 0)
        self.assertEqual(summary.event_ids, ())
        self.assertEqual(summary.initial_bad_debt, D("0"))

    def test_preview_is_idempotent(self):
        engine = ClearingEngine()
        requests = [req("B1", "40"), req("B2", "20")]
        first = engine.preview_batch("USD", D("50"), requests)
        second = engine.preview_batch("USD", D("50"), requests)
        self.assertEqual(first, second)
        self.assertEqual(engine.audit_log, ())

    def test_preview_does_not_disturb_existing_ledger(self):
        engine = ClearingEngine()
        engine.process(
            "S0", "USD", D("100"), D("10"), D("0"), D("100"), D("0"),
            [("c", D("10"))],
        )
        events_before = engine.audit_log
        preview = engine.preview_batch(
            "USD", D("90"), [req("B1", "30"), req("B2", "20")]
        )
        # 台账事件、序号、结果索引、坏账均保持预演前状态。
        self.assertEqual(engine.audit_log, events_before)
        self.assertEqual([e.sequence for e in engine.audit_log], [1])
        self.assertIsNone(engine.result_of("B1"))
        # 预演自身仍报告当前台账下的滚动结果：90-30=60，60-20=40。
        self.assertEqual(
            [r.validated_available_balance for r in preview.results],
            [D("60"), D("40")],
        )
        self.assertEqual(preview.validated_available_balance, D("40"))
        # 预演不占流水号：B1/B2 仍可正式提交。
        committed = engine.process_batch(
            "USD", D("90"), [req("B1", "30"), req("B2", "20")]
        )
        self.assertEqual([e.sequence for e in engine.audit_log], [1, 2, 3])
        self.assertEqual(committed.validated_available_balance, D("40"))

    def test_preview_with_bad_debt_does_not_create_recoverable_ledger(self):
        engine = ClearingEngine()
        preview = engine.preview_batch(
            "USD", D("100"), [req("B1", "50", capital="10")]
        )
        # 债权 60/40：池内 50、资本 10 -> 坏账 40，但只预演不落台账。
        self.assertEqual(preview.results[0].uncovered_bad_debt, D("40"))
        self.assertEqual(engine.outstanding_bad_debts("USD"), ())


class PreviewValidationTests(unittest.TestCase):
    def test_empty_batch(self):
        for bad in ([], None, ()):
            with self.subTest(bad=bad):
                with self.assertRaises(EmptyBatchError):
                    ClearingEngine().preview_batch("USD", D("100"), bad)

    def test_missing_currency(self):
        for bad in (None, "", "   "):
            with self.subTest(bad=bad):
                with self.assertRaises(InvalidCurrencyError):
                    ClearingEngine().preview_batch(
                        bad, D("100"), [req("B1", "1")]
                    )

    def test_empty_batch_checked_before_currency(self):
        with self.assertRaises(EmptyBatchError):
            ClearingEngine().preview_batch(None, D("100"), [])

    def test_mixed_currency_before_duplicate_check(self):
        engine = ClearingEngine()
        engine.process(
            "DUP", "USD", D("10"), D("1"), D("0"), D("100"), D("0"),
            [("c", D("1"))],
        )
        with self.assertRaises(MixedCurrencyError):
            engine.preview_batch(
                "USD", D("100"),
                [
                    req("DUP", "1"),
                    req("B2", "1", creditors=(("f", "1", "EUR"),)),
                ],
            )
        # 异常不留状态：既有台账仍只有一条事件。
        self.assertEqual(len(engine.audit_log), 1)

    def test_mixed_currency_rejected(self):
        with self.assertRaises(MixedCurrencyError):
            ClearingEngine().preview_batch(
                "USD", D("100"),
                [req("B1", "1", creditors=(("foreign", "1", "EUR"),))],
            )

    def test_duplicate_within_batch(self):
        with self.assertRaises(DuplicateTransactionError):
            ClearingEngine().preview_batch(
                "USD", D("100"), [req("B1", "1"), req("B1", "2")]
            )

    def test_duplicate_against_ledger(self):
        engine = ClearingEngine()
        engine.process(
            "B1", "USD", D("10"), D("1"), D("0"), D("100"), D("0"),
            [("c", D("1"))],
        )
        with self.assertRaises(DuplicateTransactionError):
            engine.preview_batch("USD", D("100"), [req("B1", "1")])
        # 预演去重只读：失败后流水号状态与事件数不变。
        self.assertTrue(engine.has_transaction("B1"))
        self.assertEqual(len(engine.audit_log), 1)

    def test_empty_creditor_list(self):
        with self.assertRaises(EmptyCreditorListError):
            ClearingEngine().preview_batch(
                "USD", D("100"), [req("B1", "1", creditors=())]
            )

    def test_risk_factor_out_of_bounds(self):
        with self.assertRaises(InvalidRiskFactorError):
            ClearingEngine().preview_batch(
                "USD", D("100"), [req("B1", "1", factor="1.01")]
            )

    def test_invalid_values_raise_value_error(self):
        engine = ClearingEngine()
        with self.assertRaises(ValueError):
            engine.preview_batch("USD", D("-1"), [req("B1", "1")])
        with self.assertRaises(ValueError):
            engine.preview_batch("USD", D("100"), [req("B1", "-1")])
        with self.assertRaises(ValueError):
            engine.preview_batch(
                "USD", D("100"), [req("B1", "1", capital="-2")]
            )
        with self.assertRaises(ValueError):
            engine.preview_batch("USD", D("100"), [{"transaction_id": "B1"}])
        with self.assertRaises(ValueError):
            engine.preview_batch("USD", D("100"), ["not-a-mapping"])

    def test_failed_validation_leaves_no_trace_and_is_replayable(self):
        engine = ClearingEngine()
        with self.assertRaises(DuplicateTransactionError):
            engine.preview_batch(
                "USD", D("100"), [req("B1", "10"), req("B1", "20")]
            )
        self.assertEqual(engine.audit_log, ())
        self.assertFalse(engine.has_transaction("B1"))
        # 修正后既可再预演，也可直接正式提交。
        preview = engine.preview_batch(
            "USD", D("100"), [req("B1", "10"), req("B2", "20")]
        )
        self.assertEqual(preview.validated_available_balance, D("70"))
        committed = engine.process_batch(
            "USD", D("100"), [req("B1", "10"), req("B2", "20")]
        )
        self.assertEqual(committed.validated_available_balance, D("70"))
        self.assertEqual(len(engine.audit_log), 2)


class PreviewStructureTests(unittest.TestCase):
    def test_result_is_immutable(self):
        preview = ClearingEngine().preview_batch(
            "USD", D("10"), [req("B1", "5")]
        )
        self.assertIsInstance(preview.results, tuple)
        with self.assertRaises(FrozenInstanceError):
            preview.validated_available_balance = D("0")
        with self.assertRaises(FrozenInstanceError):
            preview.results[0].approved = False

    def test_accepts_generator_requests(self):
        engine = ClearingEngine()

        def make():
            yield req("G1", "5")
            yield req("G2", "5")

        preview = engine.preview_batch("USD", D("20"), make())
        self.assertEqual(
            [r.transaction_id for r in preview.results], ["G1", "G2"]
        )
        self.assertEqual(preview.validated_available_balance, D("10"))
        # 预演不占流水号：新生成器可在同一引擎提交。
        committed = engine.process_batch("USD", D("20"), make())
        self.assertEqual(
            committed.validated_available_balance, preview.validated_available_balance
        )


if __name__ == "__main__":
    unittest.main()
