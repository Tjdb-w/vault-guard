"""风险组批次提交前只读预演（preview_risk_group_batch）公开行为测试。

覆盖：预演与正式提交逐笔一致（除 event_id）、从已登记组 used 起算、
组限额 / 余额不足 / 基础限额拒绝均不增组额度、无风险组请求只走单笔规则、
risk_groups 快照合并输入组与已登记组、只读且幂等（含交叉调用）、校验顺序
与异常类型沿用 process_risk_group_batch、异常不留状态、结果结构不可变。
"""

import unittest
from dataclasses import FrozenInstanceError
from decimal import Decimal

from vault_guard import (
    ClearingEngine,
    DuplicateTransactionError,
    EmptyBatchError,
    InvalidCurrencyError,
    InvalidRiskGroupError,
    RiskGroupBatchPreviewResult,
    RiskGroupUsage,
    SettlementPreview,
)

D = Decimal


def req(tid, amount, exposure="0", limit="1000", factor="0", group=None,
        creditors=(("senior", "60"), ("equity", "40")), capital="0"):
    normalized = [(name, D(claim)) for name, claim in creditors]
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
            req("P2", "30", group="G1"),   # 累计 70
            req("P3", "50", group="G1"),   # 70+50 > 100，组限额拒绝
            req("P4", "10", group="G1"),   # 70+10 = 80，放行
            req("P5", "5"),                # 无风险组，只走单笔规则
        ]
        engine = ClearingEngine()
        preview = engine.preview_risk_group_batch(
            "USD", D("1000"), {"G1": D("100")}, requests
        )
        committed = engine.process_risk_group_batch(
            "USD", D("1000"), {"G1": D("100")}, requests
        )

        self.assertIsInstance(preview, RiskGroupBatchPreviewResult)
        self.assertEqual(len(preview.results), 5)
        for item in preview.results:
            self.assertIsInstance(item, SettlementPreview)
            self.assertFalse(hasattr(item, "event_id"))
        self.assertFalse(hasattr(preview, "event_ids"))
        self.assertEqual(
            [preview_fields(item) for item in preview.results],
            [preview_fields(item) for item in committed.results],
        )
        self.assertEqual(
            preview.validated_available_balance,
            committed.validated_available_balance,
        )
        self.assertEqual(dict(preview.risk_groups), dict(committed.risk_groups))
        self.assertEqual(
            preview.risk_groups["G1"],
            RiskGroupUsage(used=D("80"), limit=D("100"), remaining=D("20")),
        )
        # 正式提交补上了事件标识。
        self.assertEqual(
            [r.event_id for r in committed.results],
            [
                "EVT-P1-approved",
                "EVT-P2-approved",
                "EVT-P3-rejected",
                "EVT-P4-approved",
                "EVT-P5-approved",
            ],
        )

    def test_preview_starts_from_registered_group_used(self):
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
        r1, r2 = preview.results
        self.assertFalse(r1.approved)
        self.assertEqual(r1.rejection_reason, "GROUP_LIMIT_EXCEEDED")
        # 拒绝结果的 group_used_after 等于执行前的已登记已用额度。
        self.assertEqual(r1.group_used_after, D("60"))
        self.assertTrue(r2.approved)
        self.assertEqual(r2.group_used_after, D("100"))
        # 预演不改变已登记已用额度。
        self.assertEqual(engine._risk_group_used["G1"], D("60"))
        self.assertEqual(
            preview.risk_groups["G1"],
            RiskGroupUsage(used=D("100"), limit=D("100"), remaining=D("0")),
        )

    def test_group_limit_rejection_does_not_increase_usage(self):
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
        # 拒绝不扣资金池，最终余额只反映两笔放行。
        self.assertEqual(preview.validated_available_balance, D("900"))

    def test_balance_and_base_limit_rejections_do_not_increase_usage(self):
        engine = ClearingEngine()
        preview = engine.preview_risk_group_batch(
            "USD", D("50"), {"G1": D("1000")},
            [
                req("B1", "40", group="G1"),
                req("B2", "20", group="G1"),                    # 超余额
                req("B3", "10", limit="5", group="G1"),         # 超单笔限额
                req("B4", "10", group="G1"),
            ],
        )
        r1, r2, r3, r4 = preview.results
        self.assertEqual(
            r2.rejection_reason, "SETTLEMENT_EXCEEDS_AVAILABLE_BALANCE"
        )
        self.assertEqual(
            r3.rejection_reason, "RISK_OCCUPANCY_EXCEEDS_BASE_LIMIT"
        )
        # 两种拒绝均不占用组额度。
        self.assertEqual(r2.group_used_after, D("40"))
        self.assertEqual(r3.group_used_after, D("40"))
        self.assertEqual(r4.group_used_after, D("50"))
        self.assertEqual(preview.risk_groups["G1"].used, D("50"))

    def test_request_without_group_uses_base_limit_only(self):
        engine = ClearingEngine()
        preview = engine.preview_risk_group_batch(
            "USD", D("1000"), {"G1": D("10")},
            [
                req("B1", "10", group="G1"),       # 占满 G1
                req("B2", "500"),                  # 无风险组
                req("B3", "500", group=None),      # 显式 None 同缺省
            ],
        )
        self.assertTrue(all(r.approved for r in preview.results))
        self.assertIsNone(preview.results[1].group_used_after)
        self.assertIsNone(preview.results[2].group_used_after)
        self.assertEqual(preview.risk_groups["G1"].used, D("10"))

    def test_risk_groups_snapshot_merges_input_and_registered_groups(self):
        engine = ClearingEngine()
        engine.process_risk_group_batch(
            "USD", D("1000"),
            {"G1": D("100"), "G2": D("50")},
            [
                req("A1", "60", group="G1"),
                req("A2", "20", group="G2"),
            ],
        )
        # 本次只声明 G1；G3 为本次新增；G2 已登记但本次未声明。
        preview = engine.preview_risk_group_batch(
            "USD", D("920"),
            {"G1": D("100"), "G3": D("10")},
            [
                req("B1", "10", group="G1"),
                req("B2", "10", group="G3"),
            ],
        )
        self.assertEqual(
            dict(preview.risk_groups),
            {
                # 已登记 used 60 + 本批放行 10。
                "G1": RiskGroupUsage(used=D("70"), limit=D("100"),
                                     remaining=D("30")),
                # 未在本批使用的已登记组保留原 used / limit / remaining。
                "G2": RiskGroupUsage(used=D("20"), limit=D("50"),
                                     remaining=D("30")),
                # 本次新登记组从零起算。
                "G3": RiskGroupUsage(used=D("10"), limit=D("10"),
                                     remaining=D("0")),
            },
        )

    def test_preview_is_read_only_and_idempotent(self):
        engine = ClearingEngine()
        engine.process_risk_group_batch(
            "USD", D("1000"), {"G1": D("100")}, [req("A1", "40", group="G1")]
        )
        events_before = engine.audit_log
        bad_debt_before = engine.outstanding_bad_debts("USD")
        summary_before = engine.audit_reconciliation("USD")
        group_state = dict(engine._risk_group_limits), dict(
            engine._risk_group_used
        )

        requests = [
            req("B1", "20", group="G1"),
            req("B2", "5", group="G1"),
        ]
        first = engine.preview_risk_group_batch(
            "USD", D("1000"), {"G1": D("100")}, requests
        )
        second = engine.preview_risk_group_batch(
            "USD", D("1000"), {"G1": D("100")}, requests
        )

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
        # 不改风险组额度与坏账台账。
        self.assertEqual(
            (dict(engine._risk_group_limits), dict(engine._risk_group_used)),
            group_state,
        )
        self.assertEqual(engine.outstanding_bad_debts("USD"), bad_debt_before)
        # 预演后以相同输入正式提交：逐笔与余额、组额度一致，只多 event_id。
        committed = engine.process_risk_group_batch(
            "USD", D("1000"), {"G1": D("100")}, requests
        )
        self.assertEqual(
            [preview_fields(r) for r in committed.results],
            [preview_fields(r) for r in first.results],
        )
        self.assertEqual(
            committed.validated_available_balance,
            first.validated_available_balance,
        )

    def test_preview_is_idempotent_across_cross_calls(self):
        engine = ClearingEngine()
        requests = [req("B1", "20", group="G1")]
        first = engine.preview_risk_group_batch(
            "USD", D("1000"), {"G1": D("100")}, requests
        )
        # 交叉调用另一预演入口与只读查询，不影响本预演结果。
        engine.preview_multicurrency_batch(
            {"EUR": D("50")},
            [
                {
                    "transaction_id": "X1",
                    "currency": "EUR",
                    "settlement_amount": D("10"),
                    "notional_exposure": D("0"),
                    "base_limit": D("100"),
                    "risk_factor": D("0"),
                    "creditors": [("c", D("10"))],
                }
            ],
        )
        engine.audit_reconciliation("USD")
        second = engine.preview_risk_group_batch(
            "USD", D("1000"), {"G1": D("100")}, requests
        )
        self.assertEqual(first, second)
        self.assertEqual(engine.audit_log, ())

    def test_same_inputs_commit_after_preview_match_group_usage_snapshot(self):
        engine = ClearingEngine()
        engine.process_risk_group_batch(
            "USD", D("1000"), {"G1": D("100")}, [req("A1", "60", group="G1")]
        )
        requests = [
            req("B1", "50", group="G1"),
            req("B2", "40", group="G1"),
            req("B3", "5"),
        ]
        preview = engine.preview_risk_group_batch(
            "USD", D("940"), {"G1": D("100"), "G2": D("10")}, requests
        )
        committed = engine.process_risk_group_batch(
            "USD", D("940"), {"G1": D("100"), "G2": D("10")}, requests
        )
        self.assertEqual(
            [preview_fields(r) for r in preview.results],
            [preview_fields(r) for r in committed.results],
        )
        self.assertEqual(
            preview.validated_available_balance,
            committed.validated_available_balance,
        )
        self.assertEqual(dict(preview.risk_groups), dict(committed.risk_groups))
        # 正式结果只增 event_id 并写台账。
        self.assertEqual(len(committed.event_ids), 3)
        self.assertEqual(len(engine.audit_log), 4)


class PreviewRiskGroupValidationTests(unittest.TestCase):
    def test_validation_errors_match_process_risk_group_batch(self):
        engine = ClearingEngine()
        with self.assertRaises(EmptyBatchError):
            engine.preview_risk_group_batch(
                "USD", D("100"), {"G1": D("1")}, []
            )
        with self.assertRaises(InvalidCurrencyError):
            engine.preview_risk_group_batch(
                "  ", D("100"), {"G1": D("1")}, [req("B1", "1", group="G1")]
            )
        with self.assertRaises(InvalidRiskGroupError):
            engine.preview_risk_group_batch(
                "USD", D("100"), {"G1": D("10")}, [req("B1", "1", group="G2")]
            )
        with self.assertRaises(InvalidRiskGroupError):
            engine.preview_risk_group_batch(
                "USD", D("100"), {"G1": D("-1")},
                [req("B1", "1", group="G1")],
            )
        with self.assertRaises(InvalidRiskGroupError):
            engine.preview_risk_group_batch(
                "USD", D("100"), {"": D("10")}, [req("B1", "1")]
            )
        with self.assertRaises(InvalidRiskGroupError):
            engine.preview_risk_group_batch(
                "USD", D("100"), {"G1": D("10")}, [req("B1", "1", group=" ")]
            )
        with self.assertRaises(DuplicateTransactionError):
            engine.preview_risk_group_batch(
                "USD", D("100"), {"G1": D("10")},
                [req("B1", "1", group="G1"), req("B1", "2", group="G1")],
            )
        with self.assertRaises(ValueError):
            engine.preview_risk_group_batch(
                "USD", D("100"), {"G1": D("1")},
                [req("B1", "-1", group="G1")],
            )

    def test_conflicting_limit_rejected_in_preview_without_registration(self):
        engine = ClearingEngine()
        engine.process_risk_group_batch(
            "USD", D("100"), {"G1": D("100")}, [req("A1", "10", group="G1")]
        )
        with self.assertRaises(InvalidRiskGroupError):
            engine.preview_risk_group_batch(
                "USD", D("100"), {"G1": D("200")},
                [req("B1", "10", group="G1")],
            )
        # 相同上限可正常预演。
        preview = engine.preview_risk_group_batch(
            "USD", D("100"), {"G1": D("100")}, [req("B1", "10", group="G1")]
        )
        self.assertEqual(preview.risk_groups["G1"].used, D("20"))

    def test_duplicate_against_ledger_rejected(self):
        engine = ClearingEngine()
        engine.process(
            "B1", "USD", D("10"), D("1"), D("0"), D("100"), D("0"),
            [("c", D("1"))],
        )
        with self.assertRaises(DuplicateTransactionError):
            engine.preview_risk_group_batch(
                "USD", D("100"), {"G1": D("1")}, [req("B1", "1", group="G1")]
            )

    def test_failed_preview_leaves_no_trace(self):
        engine = ClearingEngine()
        with self.assertRaises(InvalidRiskGroupError):
            engine.preview_risk_group_batch(
                "USD", D("100"), {"G1": D("100"), "G2": D("-1")},
                [req("B1", "10", group="G1")],
            )
        self.assertEqual(engine.audit_log, ())
        self.assertFalse(engine.has_transaction("B1"))
        # 未登记任何风险组：随后正式提交的快照中 G2 从零起算。
        batch = engine.process_risk_group_batch(
            "USD", D("100"), {"G1": D("100"), "G2": D("50")},
            [req("B1", "10", group="G1")],
        )
        self.assertEqual(batch.risk_groups["G1"].used, D("10"))
        self.assertEqual(batch.risk_groups["G2"].used, D("0"))

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


if __name__ == "__main__":
    unittest.main()
