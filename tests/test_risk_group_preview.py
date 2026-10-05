"""风险组批次提交前只读预演（preview_risk_group_batch）公开行为测试。

覆盖：预演与正式提交逐笔一致（除 event_id）、从已登记组 used 起步模拟批内
累计、GROUP_LIMIT_EXCEEDED / 余额不足 / 基础限额拒绝不增组额度、无组请求
只走单笔规则、risk_groups 合并快照、只读且幂等（不生成事件 / 不占流水号 /
不登记新组 / 不改已登记额度、坏账台账与审计核对）、校验顺序与异常类型沿用
process_risk_group_batch、异常不留状态、结果结构不可变。
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
    InvalidRiskGroupError,
    MixedCurrencyError,
    RiskGroupBatchPreviewResult,
    RiskGroupUsage,
    SettlementPreview,
)

D = Decimal


def req(tid, amount, exposure="0", limit="1000", factor="0", group=None,
        creditors=(("senior", "60"), ("equity", "40")), capital="0"):
    normalized = []
    for item in creditors:
        if len(item) == 2:
            name, claim = item
            normalized.append((name, D(claim)))
        else:
            name, claim, ccy = item
            normalized.append((name, D(claim), ccy))
    item = {
        "transaction_id": tid,
        "settlement_amount": D(amount),
        "notional_exposure": D(exposure),
        "base_limit": D(limit),
        "risk_factor": D(factor),
        "creditors": normalized,
        "supplementary_capital": D(capital),
    }
    if group is not None:
        item["risk_group_id"] = group
    return item


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


class PreviewRiskGroupBatchTests(unittest.TestCase):
    def test_preview_matches_committed_results_except_event_id(self):
        requests = [
            req("P1", "40", group="G1"),
            req("P2", "30", group="G1"),                       # 70
            req("P3", "40", group="G1"),                       # 70+40>100 拒绝
            req("P4", "30", group="G1"),                       # 100 放行
            req("P5", "20"),                                   # 无组
            req("P6", "900"),                                  # 余额不足
            req("P7", "5", limit="1", group="G1"),             # 基础限额拒绝
        ]
        engine = ClearingEngine()
        limits = {"G1": D("100"), "G2": D("5")}
        preview = engine.preview_risk_group_batch("USD", D("1000"), limits, requests)
        committed = engine.process_risk_group_batch("USD", D("1000"), limits, requests)

        self.assertIsInstance(preview, RiskGroupBatchPreviewResult)
        self.assertEqual(len(preview.results), 7)
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
        self.assertEqual(dict(preview.risk_groups), dict(committed.risk_groups))
        # 正式提交补上了事件标识与台账。
        self.assertEqual(
            [r.event_id for r in committed.results],
            [
                "EVT-P1-approved",
                "EVT-P2-approved",
                "EVT-P3-rejected",
                "EVT-P4-approved",
                "EVT-P5-approved",
                "EVT-P6-rejected",
                "EVT-P7-rejected",
            ],
        )
        self.assertEqual(len(engine.audit_log), 7)

    def test_group_limit_rejected_does_not_consume_quota(self):
        engine = ClearingEngine()
        preview = engine.preview_risk_group_batch(
            "USD", D("1000"), {"G1": D("100")},
            [
                req("B1", "80", group="G1"),
                req("B2", "30", group="G1"),   # 80+30 > 100，拒绝
                req("B3", "20", group="G1"),   # 80+20 = 100，放行
            ],
        )
        r1, r2, r3 = preview.results
        self.assertTrue(r1.approved)
        self.assertFalse(r2.approved)
        self.assertEqual(r2.rejection_reason, "GROUP_LIMIT_EXCEEDED")
        self.assertEqual(r2.total_pool_allocated, D("0"))
        self.assertEqual(r2.uncovered_bad_debt, D("0"))
        self.assertEqual(r2.group_used_after, D("80"))
        self.assertTrue(r3.approved)
        self.assertEqual(r3.group_used_after, D("100"))
        self.assertEqual(
            preview.risk_groups["G1"],
            RiskGroupUsage(used=D("100"), limit=D("100"), remaining=D("0")),
        )
        # 资金池只扣放行两笔。
        self.assertEqual(preview.validated_available_balance, D("900"))

    def test_other_rejections_do_not_increase_group_used(self):
        engine = ClearingEngine()
        preview = engine.preview_risk_group_batch(
            "USD", D("50"), {"G1": D("1000")},
            [
                req("B1", "40", group="G1"),
                req("B2", "20", group="G1"),                # 超余额
                req("B3", "10", limit="5", group="G1"),     # 超单笔限额
                req("B4", "10", group="G1"),
            ],
        )
        r1, r2, r3, r4 = preview.results
        self.assertEqual(r2.rejection_reason, "SETTLEMENT_EXCEEDS_AVAILABLE_BALANCE")
        self.assertEqual(r3.rejection_reason, "RISK_OCCUPANCY_EXCEEDS_BASE_LIMIT")
        self.assertEqual(r2.group_used_after, D("40"))
        self.assertEqual(r3.group_used_after, D("40"))
        self.assertEqual(r4.group_used_after, D("50"))
        self.assertEqual(preview.risk_groups["G1"].used, D("50"))

    def test_request_without_group_uses_base_limit_only(self):
        engine = ClearingEngine()
        preview = engine.preview_risk_group_batch(
            "USD", D("1000"), {"G1": D("10")},
            [
                req("B1", "10", group="G1"),
                req("B2", "500"),
                req("B3", "500", group=None),
            ],
        )
        self.assertTrue(all(r.approved for r in preview.results))
        self.assertIsNone(preview.results[1].group_used_after)
        self.assertIsNone(preview.results[2].group_used_after)
        self.assertEqual(preview.risk_groups["G1"].used, D("10"))

    def test_preview_starts_from_registered_used(self):
        engine = ClearingEngine()
        engine.process_risk_group_batch(
            "USD", D("1000"), {"G1": D("100")}, [req("A1", "60", group="G1")]
        )
        preview = engine.preview_risk_group_batch(
            "USD", D("940"), {"G1": D("100")},
            [
                req("B1", "50", group="G1"),   # 60+50 > 100，拒绝
                req("B2", "40", group="G1"),   # 60+40 = 100，放行
            ],
        )
        self.assertFalse(preview.results[0].approved)
        self.assertEqual(preview.results[0].rejection_reason, "GROUP_LIMIT_EXCEEDED")
        self.assertEqual(preview.results[0].group_used_after, D("60"))
        self.assertTrue(preview.results[1].approved)
        self.assertEqual(preview.results[1].group_used_after, D("100"))

    def test_risk_groups_snapshot_merges_input_and_registered(self):
        engine = ClearingEngine()
        engine.process_risk_group_batch(
            "USD", D("1000"), {"G1": D("100"), "G0": D("9")},
            [req("A1", "60", group="G1"), req("A0", "9", group="G0")],
        )
        preview = engine.preview_risk_group_batch(
            "USD", D("940"), {"G1": D("100"), "G2": D("7")},
            [req("B1", "40", group="G1")],
        )
        self.assertEqual(set(preview.risk_groups), {"G1", "G0", "G2"})
        # 已登记组沿用已登记 used/limit；本次新给上限的组 used 自 0 起。
        self.assertEqual(
            preview.risk_groups["G1"],
            RiskGroupUsage(used=D("100"), limit=D("100"), remaining=D("0")),
        )
        self.assertEqual(
            preview.risk_groups["G0"],
            RiskGroupUsage(used=D("9"), limit=D("9"), remaining=D("0")),
        )
        self.assertEqual(
            preview.risk_groups["G2"],
            RiskGroupUsage(used=D("0"), limit=D("7"), remaining=D("7")),
        )
        self.assertIsInstance(preview.risk_groups, MappingProxyType)

    def test_preview_is_read_only_and_idempotent(self):
        engine = ClearingEngine()
        engine.process_risk_group_batch(
            "USD", D("1000"), {"G1": D("100"), "G0": D("9")},
            [req("A1", "60", group="G1"), req("A0", "9", group="G0")],
        )
        events_before = engine.audit_log
        summary_before = engine.audit_reconciliation("USD")
        debts_before = engine.outstanding_bad_debts("USD")

        requests = [
            req("B1", "50", group="G1"),
            req("B2", "40", group="G1"),
            req("C1", "5", group="G2"),
        ]
        limits = {"G1": D("100"), "G2": D("7")}
        first = engine.preview_risk_group_batch("USD", D("940"), limits, requests)
        second = engine.preview_risk_group_batch("USD", D("940"), limits, requests)

        self.assertEqual(first, second)
        self.assertEqual(engine.audit_log, events_before)
        self.assertEqual(engine.audit_reconciliation("USD"), summary_before)
        self.assertEqual(engine.outstanding_bad_debts("USD"), debts_before)
        # 不占流水号、不写结果索引。
        for tid in ("B1", "B2", "C1"):
            self.assertFalse(engine.has_transaction(tid))
            self.assertIsNone(engine.result_of(tid))
            self.assertIsNone(engine.get_event(tid))
        # 不登记新组、不改已登记组额度。
        self.assertNotIn("G2", engine._risk_group_limits)
        self.assertEqual(engine._risk_group_used["G1"], D("60"))
        # 预演后可正常提交，逐笔一致。
        committed = engine.process_risk_group_batch("USD", D("940"), limits, requests)
        self.assertEqual(
            [preview_fields(r) for r in committed.results],
            [preview_fields(r) for r in first.results],
        )

    def test_repeated_previews_do_not_accumulate(self):
        engine = ClearingEngine()
        requests = [req("B1", "40", group="G1")]
        for _ in range(3):
            preview = engine.preview_risk_group_batch(
                "USD", D("1000"), {"G1": D("100")}, requests
            )
            self.assertEqual(preview.risk_groups["G1"].used, D("40"))
        self.assertEqual(engine._risk_group_used, {})

    def test_bad_debt_attribution_matches_commit(self):
        engine = ClearingEngine()
        preview = engine.preview_risk_group_batch(
            "USD", D("100"), {"G1": D("1000")},
            [req("B1", "60", group="G1",
                 creditors=(("senior", "80"), ("equity", "20")), capital="10")],
        )
        item = preview.results[0]
        self.assertTrue(item.approved)
        self.assertEqual(item.pool_allocations, (D("60"), D("0")))
        self.assertEqual(item.capital_allocations, (D("10"), D("0")))
        self.assertEqual(item.uncovered_bad_debt, D("30"))
        # 预演不把坏账写入存续台账。
        self.assertEqual(engine.outstanding_bad_debts("USD"), ())

    def test_results_are_immutable(self):
        engine = ClearingEngine()
        preview = engine.preview_risk_group_batch(
            "USD", D("100"), {"G1": D("50")}, [req("B1", "20", group="G1")]
        )
        with self.assertRaises(FrozenInstanceError):
            preview.validated_available_balance = D("0")
        with self.assertRaises(FrozenInstanceError):
            preview.results[0].approved = False
        with self.assertRaises(TypeError):
            preview.risk_groups["G1"] = None


class PreviewRiskGroupValidationTests(unittest.TestCase):
    def test_validation_errors_match_process_risk_group_batch(self):
        engine = ClearingEngine()
        with self.assertRaises(EmptyBatchError):
            engine.preview_risk_group_batch("USD", D("100"), {"G1": D("1")}, [])
        with self.assertRaises(InvalidCurrencyError):
            engine.preview_risk_group_batch(
                "  ", D("100"), {"G1": D("1")}, [req("B1", "1")]
            )
        with self.assertRaises(InvalidRiskGroupError):
            engine.preview_risk_group_batch(
                "USD", D("100"), {"G1": D("10")}, [req("B1", "1", group="G2")]
            )
        with self.assertRaises(InvalidRiskGroupError):
            engine.preview_risk_group_batch(
                "USD", D("100"), {"G1": D("-1")}, [req("B1", "1", group="G1")]
            )
        with self.assertRaises(InvalidRiskGroupError):
            engine.preview_risk_group_batch(
                "USD", D("100"), None, [req("B1", "1")]
            )
        with self.assertRaises(InvalidRiskGroupError):
            engine.preview_risk_group_batch(
                "USD", D("100"), {"  ": D("1")}, [req("B1", "1")]
            )
        with self.assertRaises(MixedCurrencyError):
            engine.preview_risk_group_batch(
                "USD", D("100"), {},
                [req("B1", "1", creditors=(("f", "1", "EUR"),))],
            )
        with self.assertRaises(DuplicateTransactionError):
            engine.preview_risk_group_batch(
                "USD", D("100"), {}, [req("B1", "1"), req("B1", "2")]
            )
        with self.assertRaises(EmptyCreditorListError):
            engine.preview_risk_group_batch(
                "USD", D("100"), {}, [req("B1", "1", creditors=())]
            )
        with self.assertRaises(InvalidRiskFactorError):
            engine.preview_risk_group_batch(
                "USD", D("100"), {}, [req("B1", "1", factor="1.5")]
            )
        with self.assertRaises(ValueError):
            engine.preview_risk_group_batch(
                "USD", D("-1"), {}, [req("B1", "1")]
            )
        with self.assertRaises(ValueError):
            bad = req("B1", "1")
            bad["settlement_amount"] = "abc"
            engine.preview_risk_group_batch("USD", D("100"), {}, [bad])
        # 异常不留任何状态。
        self.assertEqual(engine.audit_log, ())
        self.assertFalse(engine.has_transaction("B1"))
        self.assertEqual(engine._risk_group_limits, {})

    def test_duplicate_against_ledger_rejected(self):
        engine = ClearingEngine()
        engine.process("B1", "USD", D("10"), D("1"), D("0"), D("100"), D("0"),
                       [("c", D("1"))])
        with self.assertRaises(DuplicateTransactionError):
            engine.preview_risk_group_batch(
                "USD", D("100"), {}, [req("B1", "1")]
            )

    def test_conflicting_limit_on_same_engine_rejected(self):
        engine = ClearingEngine()
        engine.process_risk_group_batch(
            "USD", D("100"), {"G1": D("100")}, [req("B1", "10", group="G1")]
        )
        with self.assertRaises(InvalidRiskGroupError):
            engine.preview_risk_group_batch(
                "USD", D("100"), {"G1": D("200")}, [req("B2", "10", group="G1")]
            )
        # 相同上限可正常预演，used 从已登记的 10 起累计。
        preview = engine.preview_risk_group_batch(
            "USD", D("100"), {"G1": D("100")}, [req("B2", "10", group="G1")]
        )
        self.assertEqual(preview.risk_groups["G1"].used, D("20"))
        # 预演不写回：再次预演结果不变。
        preview_again = engine.preview_risk_group_batch(
            "USD", D("100"), {"G1": D("100")}, [req("B2", "10", group="G1")]
        )
        self.assertEqual(preview_again, preview)


if __name__ == "__main__":
    unittest.main()
