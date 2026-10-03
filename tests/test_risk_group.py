"""风险组累计限额批次（process_risk_group_batch /
process_settlement_risk_group_batch）公开行为测试。

覆盖：组内按序累计与组限额拒绝、拒绝不占用额度且继续处理、跨批次累计、
组间独立、group_used_after 语义、InvalidRiskGroupError 各触发路径、
异常不产生事件 / 不占流水号 / 不改资金池与组额度、模块级一次性入口。
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


class RiskGroupBatchTests(unittest.TestCase):
    def test_group_usage_accumulates_in_order(self):
        engine = ClearingEngine()
        batch = engine.process_risk_group_batch(
            "USD", D("1000"), {"G1": D("100")},
            [
                req("B1", "40", group="G1"),
                req("B2", "30", group="G1"),
                req("B3", "30", group="G1"),   # 累计 100，恰好等于上限，放行
            ],
        )
        self.assertIsInstance(batch, RiskGroupBatchResult)
        self.assertTrue(all(r.approved for r in batch.results))
        self.assertEqual(
            [r.group_used_after for r in batch.results],
            [D("40"), D("70"), D("100")],
        )
        usage = batch.risk_groups["G1"]
        self.assertEqual(usage, RiskGroupUsage(used=D("100"), limit=D("100"), remaining=D("0")))
        self.assertEqual(usage.used, D("100"))
        self.assertEqual(usage.remaining, D("0"))

    def test_group_limit_exceeded_rejects_and_continues(self):
        engine = ClearingEngine()
        batch = engine.process_risk_group_batch(
            "USD", D("1000"), {"G1": D("100")},
            [
                req("B1", "80", group="G1"),
                req("B2", "30", group="G1"),   # 80+30 > 100，拒绝
                req("B3", "20", group="G1"),   # 80+20 = 100，放行
            ],
        )
        r1, r2, r3 = batch.results
        self.assertTrue(r1.approved)
        self.assertFalse(r2.approved)
        self.assertEqual(r2.rejection_reason, "GROUP_LIMIT_EXCEEDED")
        # 拒绝不分配资金、不改资金池、不确认坏账、不增加已用额度。
        self.assertEqual(r2.total_pool_allocated, D("0"))
        self.assertEqual(r2.uncovered_bad_debt, D("0"))
        self.assertEqual(r2.group_used_after, D("80"))
        self.assertTrue(r3.approved)
        self.assertEqual(r3.group_used_after, D("100"))
        # 拒绝仍生成现有结构审计事件。
        self.assertEqual(len(batch.event_ids), 3)
        event = engine.get_event("B2")
        self.assertIsNotNone(event)
        self.assertEqual(event.rejection_reason, "GROUP_LIMIT_EXCEEDED")
        self.assertEqual(event.validation_result, "REJECTED")
        # 资金池只扣放行两笔。
        self.assertEqual(batch.validated_available_balance, D("900"))

    def test_group_usage_unaffected_by_other_rejections(self):
        engine = ClearingEngine()
        batch = engine.process_risk_group_batch(
            "USD", D("50"), {"G1": D("1000")},
            [
                req("B1", "40", group="G1"),
                req("B2", "20", group="G1"),                    # 超余额
                req("B3", "10", limit="5", group="G1"),         # 超单笔限额
                req("B4", "10", group="G1"),
            ],
        )
        r1, r2, r3, r4 = batch.results
        self.assertEqual(r2.rejection_reason, "SETTLEMENT_EXCEEDS_AVAILABLE_BALANCE")
        self.assertEqual(r3.rejection_reason, "RISK_OCCUPANCY_EXCEEDS_BASE_LIMIT")
        # 两种拒绝均不占用组额度。
        self.assertEqual(r2.group_used_after, D("40"))
        self.assertEqual(r3.group_used_after, D("40"))
        self.assertEqual(r4.group_used_after, D("50"))
        self.assertEqual(batch.risk_groups["G1"].used, D("50"))

    def test_groups_are_independent(self):
        engine = ClearingEngine()
        batch = engine.process_risk_group_batch(
            "USD", D("1000"),
            {"G1": D("50"), "G2": D("50")},
            [
                req("B1", "50", group="G1"),
                req("B2", "50", group="G2"),   # G2 独立，放行
                req("B3", "1", group="G1"),    # G1 已满，拒绝
            ],
        )
        self.assertTrue(batch.results[1].approved)
        self.assertFalse(batch.results[2].approved)
        self.assertEqual(batch.risk_groups["G1"].used, D("50"))
        self.assertEqual(batch.risk_groups["G2"].used, D("50"))

    def test_usage_accumulates_across_batches_on_same_engine(self):
        engine = ClearingEngine()
        engine.process_risk_group_batch(
            "USD", D("1000"), {"G1": D("100")}, [req("B1", "60", group="G1")]
        )
        batch = engine.process_risk_group_batch(
            "USD", D("940"), {"G1": D("100")},
            [
                req("B2", "50", group="G1"),   # 60+50 > 100，拒绝
                req("B3", "40", group="G1"),   # 60+40 = 100，放行
            ],
        )
        self.assertFalse(batch.results[0].approved)
        self.assertEqual(batch.results[0].group_used_after, D("60"))
        self.assertTrue(batch.results[1].approved)
        self.assertEqual(batch.risk_groups["G1"].used, D("100"))

    def test_risk_occupancy_includes_exposure_times_factor(self):
        engine = ClearingEngine()
        batch = engine.process_risk_group_batch(
            "USD", D("1000"), {"G1": D("100")},
            [req("B1", "40", exposure="100", factor="0.6", group="G1")],
        )
        (result,) = batch.results
        self.assertTrue(result.approved)
        self.assertEqual(result.risk_occupancy, D("100"))
        self.assertEqual(result.group_used_after, D("100"))

    def test_request_without_group_uses_base_limit_only(self):
        engine = ClearingEngine()
        batch = engine.process_risk_group_batch(
            "USD", D("1000"), {"G1": D("10")},
            [
                req("B1", "10", group="G1"),       # 占满 G1
                req("B2", "500"),                  # 无风险组，不受组限额影响
                req("B3", "500", group=None),      # 显式 None 同缺省
            ],
        )
        self.assertTrue(all(r.approved for r in batch.results))
        self.assertIsNone(batch.results[1].group_used_after)
        self.assertIsNone(batch.results[2].group_used_after)
        self.assertEqual(batch.risk_groups["G1"].used, D("10"))

    def test_result_structure_and_immutability(self):
        batch = process_settlement_risk_group_batch(
            "USD", D("100"), {"G1": D("50")}, [req("B1", "20", group="G1")]
        )
        self.assertIsInstance(batch.results, tuple)
        self.assertIsInstance(batch.event_ids, tuple)
        self.assertEqual(batch.validated_available_balance, D("80"))
        self.assertEqual(dict(batch.risk_groups), {
            "G1": RiskGroupUsage(used=D("20"), limit=D("50"), remaining=D("30"))
        })
        with self.assertRaises(FrozenInstanceError):
            batch.validated_available_balance = D("0")
        with self.assertRaises(TypeError):
            batch.risk_groups["G1"] = None

    def test_event_sequence_continues_from_ledger(self):
        engine = ClearingEngine()
        engine.process(
            "S0", "USD", D("10"), D("10"), D("0"), D("100"), D("0"),
            [("c", D("10"))],
        )
        engine.process_risk_group_batch(
            "USD", D("100"), {"G1": D("100")},
            [req("B1", "10", group="G1"), req("B2", "10", group="G1")],
        )
        self.assertEqual([e.sequence for e in engine.audit_log], [1, 2, 3])


class RiskGroupValidationTests(unittest.TestCase):
    def test_empty_group_id_rejected(self):
        engine = ClearingEngine()
        for bad in ("", "   ", 123):
            with self.subTest(bad=bad):
                with self.assertRaises(InvalidRiskGroupError):
                    engine.process_risk_group_batch(
                        "USD", D("100"), {"G1": D("10")},
                        [req("B1", "1", group=bad)],
                    )

    def test_unregistered_group_rejected(self):
        engine = ClearingEngine()
        with self.assertRaises(InvalidRiskGroupError):
            engine.process_risk_group_batch(
                "USD", D("100"), {"G1": D("10")},
                [req("B1", "1", group="G2")],
            )

    def test_invalid_limits_rejected(self):
        engine = ClearingEngine()
        for bad in (D("-1"), -1, "abc", float("nan"), float("inf"), None, True):
            with self.subTest(bad=bad):
                with self.assertRaises(InvalidRiskGroupError):
                    engine.process_risk_group_batch(
                        "USD", D("100"), {"G1": bad},
                        [req("B1", "1", group="G1")],
                    )

    def test_empty_group_id_in_limit_table_rejected(self):
        engine = ClearingEngine()
        with self.assertRaises(InvalidRiskGroupError):
            engine.process_risk_group_batch(
                "USD", D("100"), {"": D("10")}, [req("B1", "1")]
            )

    def test_limit_table_must_be_mapping(self):
        engine = ClearingEngine()
        for bad in (None, [("G1", D("1"))], "G1"):
            with self.subTest(bad=bad):
                with self.assertRaises(InvalidRiskGroupError):
                    engine.process_risk_group_batch(
                        "USD", D("100"), bad, [req("B1", "1")]
                    )

    def test_conflicting_limit_on_same_engine_rejected(self):
        engine = ClearingEngine()
        engine.process_risk_group_batch(
            "USD", D("100"), {"G1": D("100")}, [req("B1", "10", group="G1")]
        )
        with self.assertRaises(InvalidRiskGroupError):
            engine.process_risk_group_batch(
                "USD", D("100"), {"G1": D("200")}, [req("B2", "10", group="G1")]
            )
        # 相同上限不冲突。
        batch = engine.process_risk_group_batch(
            "USD", D("100"), {"G1": D("100")}, [req("B2", "10", group="G1")]
        )
        self.assertEqual(batch.risk_groups["G1"].used, D("20"))

    def test_previously_registered_group_need_not_be_redeclared(self):
        engine = ClearingEngine()
        engine.process_risk_group_batch(
            "USD", D("100"), {"G1": D("100")}, [req("B1", "10", group="G1")]
        )
        batch = engine.process_risk_group_batch(
            "USD", D("100"), {}, [req("B2", "10", group="G1")]
        )
        self.assertEqual(batch.risk_groups["G1"].used, D("20"))

    def test_batch_input_errors_reuse_existing_types(self):
        engine = ClearingEngine()
        with self.assertRaises(EmptyBatchError):
            engine.process_risk_group_batch("USD", D("100"), {"G1": D("1")}, [])
        with self.assertRaises(ValueError):
            engine.process_risk_group_batch(
                "USD", D("100"), {"G1": D("1")}, [req("B1", "-1", group="G1")]
            )
        engine.process("B1", "USD", D("10"), D("1"), D("0"), D("100"), D("0"),
                       [("c", D("1"))])
        with self.assertRaises(DuplicateTransactionError):
            engine.process_risk_group_batch(
                "USD", D("100"), {"G1": D("1")}, [req("B1", "1", group="G1")]
            )

    def test_failed_batch_leaves_no_trace(self):
        engine = ClearingEngine()
        with self.assertRaises(InvalidRiskGroupError):
            engine.process_risk_group_batch(
                "USD", D("100"), {"G1": D("100"), "G2": D("-1")},
                [req("B1", "10", group="G1")],
            )
        # 不产生事件、不占用流水号、不登记风险组。
        self.assertEqual(len(engine.audit_log), 0)
        self.assertFalse(engine.has_transaction("B1"))
        batch = engine.process_risk_group_batch(
            "USD", D("100"), {"G1": D("100"), "G2": D("50")},
            [req("B1", "10", group="G1")],
        )
        self.assertEqual(batch.risk_groups["G1"].used, D("10"))
        self.assertEqual(batch.risk_groups["G2"].used, D("0"))

    def test_failed_second_batch_rolls_back_group_state(self):
        engine = ClearingEngine()
        engine.process_risk_group_batch(
            "USD", D("100"), {"G1": D("100")}, [req("B1", "10", group="G1")]
        )
        with self.assertRaises(DuplicateTransactionError):
            engine.process_risk_group_batch(
                "USD", D("100"), {"G1": D("100"), "G2": D("50")},
                [req("B2", "10", group="G2"), req("B2", "20", group="G2")],
            )
        # G2 未登记、G1 已用额度不变、流水号未占用。
        batch = engine.process_risk_group_batch(
            "USD", D("100"), {"G1": D("100"), "G2": D("50")},
            [req("B2", "10", group="G2")],
        )
        self.assertEqual(batch.risk_groups["G1"].used, D("10"))
        self.assertEqual(batch.risk_groups["G2"].used, D("10"))


class ModuleLevelRiskGroupBatchTests(unittest.TestCase):
    def test_one_shot_engine_keeps_no_group_state(self):
        r1 = process_settlement_risk_group_batch(
            "USD", D("100"), {"G1": D("50")}, [req("M1", "50", group="G1")]
        )
        self.assertEqual(r1.risk_groups["G1"].used, D("50"))
        # 一次性引擎：相同流水号与组上限可在下一批次重新使用，不累计。
        r2 = process_settlement_risk_group_batch(
            "USD", D("100"), {"G1": D("50")}, [req("M1", "50", group="G1")]
        )
        self.assertTrue(r2.results[0].approved)
        self.assertEqual(r2.risk_groups["G1"].used, D("50"))


if __name__ == "__main__":
    unittest.main()
