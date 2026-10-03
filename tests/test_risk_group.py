"""风险组累计限额批次（process_risk_group_batch /
process_settlement_risk_group_batch）公开行为测试。

覆盖：组限额拒绝与继续处理、已用额度只在放行请求上累计、跨批次累计、
组间互不影响、group_used_after 语义、InvalidRiskGroupError 各触发路径、
异常时不产生事件 / 不占流水号 / 不改资金池与组额度、模块级一次性入口
不保留风险组状态。
"""

import unittest
from dataclasses import FrozenInstanceError
from decimal import Decimal

from vault_guard import (
    ClearingEngine,
    DuplicateTransactionError,
    EmptyBatchError,
    InvalidRiskGroupError,
    RiskGroupBatchResult,
    RiskGroupUsage,
    process_settlement_risk_group_batch,
)

D = Decimal


def req(tid, amount, group="G1", exposure="0", limit="1000", factor="0",
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
        "risk_group_id": group,
    }


LIMITS = {"G1": D("100"), "G2": D("50")}


class RiskGroupBatchTests(unittest.TestCase):
    def test_group_limit_exceeded_rejects_and_continues(self):
        engine = ClearingEngine()
        batch = engine.process_risk_group_batch(
            "USD", D("1000"), {"G1": D("100")},
            [req("T1", "60"), req("T2", "50"), req("T3", "40")],
        )
        r1, r2, r3 = batch.results
        self.assertTrue(r1.approved)
        # 60 + 50 > 100：拒绝，不动资金、不确认坏账、不占额度。
        self.assertFalse(r2.approved)
        self.assertEqual(r2.rejection_reason, "GROUP_LIMIT_EXCEEDED")
        self.assertEqual(r2.total_pool_allocated, D("0"))
        self.assertEqual(r2.uncovered_bad_debt, D("0"))
        self.assertEqual(r2.validated_available_balance, D("940"))
        # 60 + 40 <= 100：放行，批次继续处理。
        self.assertTrue(r3.approved)
        usage = batch.group_usage["G1"]
        self.assertEqual(usage, RiskGroupUsage(used=D("100"), limit=D("100"), remaining=D("0")))
        self.assertEqual(len(batch.event_ids), 3)
        self.assertEqual([e.transaction_id for e in engine.audit_log], ["T1", "T2", "T3"])

    def test_group_used_after_tracks_cumulative_usage(self):
        engine = ClearingEngine()
        batch = engine.process_risk_group_batch(
            "USD", D("1000"), {"G1": D("100")},
            [req("T1", "30"), req("T2", "80"), req("T3", "10")],
        )
        # 放行后累计 30；拒绝笔等于执行前值 30；再放行后 40。
        self.assertEqual(
            [r.group_used_after for r in batch.results],
            [D("30"), D("30"), D("40")],
        )

    def test_other_rejections_do_not_consume_group_usage(self):
        engine = ClearingEngine()
        batch = engine.process_risk_group_batch(
            "USD", D("50"), {"G1": D("100")},
            [
                req("T1", "20"),                      # 放行，已用 20
                req("T2", "40"),                      # 超余额拒绝
                req("T3", "10", limit="5"),           # 超基础限额拒绝
                req("T4", "10", exposure="20", factor="0.5"),  # 占用 20，放行
            ],
        )
        self.assertEqual(
            [r.rejection_reason for r in batch.results],
            [None, "SETTLEMENT_EXCEEDS_AVAILABLE_BALANCE",
             "RISK_OCCUPANCY_EXCEEDS_BASE_LIMIT", None],
        )
        self.assertEqual(batch.group_usage["G1"].used, D("40"))
        self.assertEqual(batch.group_usage["G1"].remaining, D("60"))

    def test_groups_are_independent(self):
        engine = ClearingEngine()
        batch = engine.process_risk_group_batch(
            "USD", D("1000"), LIMITS,
            [req("T1", "80", group="G1"), req("T2", "40", group="G2")],
        )
        self.assertTrue(all(r.approved for r in batch.results))
        self.assertEqual(batch.group_usage["G1"].used, D("80"))
        self.assertEqual(batch.group_usage["G2"].used, D("40"))

    def test_usage_accumulates_across_batches_on_same_engine(self):
        engine = ClearingEngine()
        engine.process_risk_group_batch(
            "USD", D("1000"), {"G1": D("100")}, [req("T1", "60")]
        )
        batch = engine.process_risk_group_batch(
            "USD", D("1000"), {"G1": D("100")},
            [req("T2", "50"), req("T3", "40")],
        )
        r2, r3 = batch.results
        self.assertFalse(r2.approved)
        self.assertEqual(r2.rejection_reason, "GROUP_LIMIT_EXCEEDED")
        self.assertEqual(r2.group_used_after, D("60"))
        self.assertTrue(r3.approved)
        self.assertEqual(batch.group_usage["G1"].used, D("100"))

    def test_result_is_immutable(self):
        batch = process_settlement_risk_group_batch(
            "USD", D("100"), {"G1": D("100")}, [req("T1", "10")]
        )
        self.assertIsInstance(batch, RiskGroupBatchResult)
        self.assertIsInstance(batch.results, tuple)
        self.assertIsInstance(batch.event_ids, tuple)
        with self.assertRaises(FrozenInstanceError):
            batch.validated_available_balance = D("0")
        with self.assertRaises(TypeError):
            batch.group_usage["G1"] = RiskGroupUsage(D("0"), D("0"), D("0"))

    def test_event_structure_matches_existing_shape(self):
        engine = ClearingEngine()
        batch = engine.process_risk_group_batch(
            "USD", D("100"), {"G1": D("50")}, [req("T1", "10"), req("T2", "60")]
        )
        approved, rejected = engine.audit_log
        self.assertEqual(approved.event_id, "EVT-T1-approved")
        self.assertEqual(rejected.event_id, "EVT-T2-rejected")
        self.assertEqual(rejected.validation_result, "REJECTED")
        self.assertEqual(rejected.rejection_reason, "GROUP_LIMIT_EXCEEDED")
        self.assertEqual([e.sequence for e in engine.audit_log], [1, 2])
        self.assertEqual(batch.event_ids, ("EVT-T1-approved", "EVT-T2-rejected"))


class RiskGroupValidationTests(unittest.TestCase):
    def test_empty_group_id(self):
        for bad in (None, "", "   ", 7):
            with self.subTest(bad=bad):
                with self.assertRaises(InvalidRiskGroupError):
                    ClearingEngine().process_risk_group_batch(
                        "USD", D("100"), {"G1": D("100")},
                        [req("T1", "10", group=bad)],
                    )

    def test_unregistered_group_reference(self):
        with self.assertRaises(InvalidRiskGroupError):
            ClearingEngine().process_risk_group_batch(
                "USD", D("100"), {"G1": D("100")}, [req("T1", "10", group="GX")]
            )

    def test_invalid_limit_values(self):
        for bad in (D("-1"), "abc", None, float("nan")):
            with self.subTest(bad=bad):
                with self.assertRaises(InvalidRiskGroupError):
                    ClearingEngine().process_risk_group_batch(
                        "USD", D("100"), {"G1": bad}, [req("T1", "10")]
                    )

    def test_empty_group_id_in_limit_table(self):
        for bad_key in ("", "   ", None):
            with self.subTest(bad_key=bad_key):
                with self.assertRaises(InvalidRiskGroupError):
                    ClearingEngine().process_risk_group_batch(
                        "USD", D("100"), {bad_key: D("10")}, [req("T1", "10")]
                    )

    def test_limit_table_must_be_mapping(self):
        with self.assertRaises(InvalidRiskGroupError):
            ClearingEngine().process_risk_group_batch(
                "USD", D("100"), [("G1", D("10"))], [req("T1", "10")]
            )

    def test_conflicting_limit_on_same_engine(self):
        engine = ClearingEngine()
        engine.process_risk_group_batch(
            "USD", D("100"), {"G1": D("100")}, [req("T1", "10")]
        )
        with self.assertRaises(InvalidRiskGroupError):
            engine.process_risk_group_batch(
                "USD", D("100"), {"G1": D("200")}, [req("T2", "10")]
            )
        # 相同上限（含等值不同精度）不冲突。
        batch = engine.process_risk_group_batch(
            "USD", D("100"), {"G1": D("100.0")}, [req("T2", "10")]
        )
        self.assertTrue(batch.results[0].approved)

    def test_existing_batch_errors_reuse_existing_types(self):
        engine = ClearingEngine()
        with self.assertRaises(EmptyBatchError):
            engine.process_risk_group_batch("USD", D("100"), {"G1": D("1")}, [])
        with self.assertRaises(DuplicateTransactionError):
            engine.process_risk_group_batch(
                "USD", D("100"), {"G1": D("100")},
                [req("T1", "10"), req("T1", "20")],
            )
        with self.assertRaises(ValueError):
            engine.process_risk_group_batch(
                "USD", D("100"), {"G1": D("100")}, [req("T1", "-1")]
            )

    def test_failed_batch_leaves_no_trace(self):
        engine = ClearingEngine()
        engine.process_risk_group_batch(
            "USD", D("1000"), {"G1": D("100")}, [req("T0", "40")]
        )
        with self.assertRaises(InvalidRiskGroupError):
            engine.process_risk_group_batch(
                "USD", D("1000"), {"G1": D("100"), "G2": D("-5")},
                [req("T1", "10", group="G2")],
            )
        # 不生成事件、不占流水号、不登记新组、不改已用额度。
        self.assertEqual(len(engine.audit_log), 1)
        self.assertFalse(engine.has_transaction("T1"))
        batch = engine.process_risk_group_batch(
            "USD", D("1000"), {"G1": D("100"), "G2": D("50")},
            [req("T1", "10", group="G2")],
        )
        self.assertTrue(batch.results[0].approved)
        self.assertEqual(batch.group_usage["G1"].used, D("40"))
        self.assertEqual(batch.group_usage["G2"].used, D("10"))


class ModuleLevelRiskGroupTests(unittest.TestCase):
    def test_one_shot_entry(self):
        batch = process_settlement_risk_group_batch(
            "USD", D("100"), {"G1": D("50")},
            [req("M1", "30"), req("M2", "30")],
        )
        self.assertTrue(batch.results[0].approved)
        self.assertFalse(batch.results[1].approved)
        self.assertEqual(batch.results[1].rejection_reason, "GROUP_LIMIT_EXCEEDED")

    def test_no_cross_batch_group_state(self):
        # 一次性引擎：相同流水号与组额度在各批次独立。
        for _ in range(2):
            batch = process_settlement_risk_group_batch(
                "USD", D("100"), {"G1": D("50")}, [req("M1", "40")]
            )
            self.assertTrue(batch.results[0].approved)
            self.assertEqual(batch.group_usage["G1"].used, D("40"))


if __name__ == "__main__":
    unittest.main()
