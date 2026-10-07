"""清算批次组合限额预占与确定性试算（evaluate_settlement_batch）公开
行为测试。

覆盖：两层（资金方 / 付款方）累计占额与边界取等号、整笔拒绝不部分受理、
拒绝不进瀑布 / 不产生坏账 / 不改占额 / 不影响后续、priority 与
settlement_id Unicode 码点确定性排序、瀑布与补充资本 / 坏账归因、占额
快照口径、限额轨迹四项占额、reason_code 审计事件、批内与跨调用
settlement_id 去重、六类异常与非法请求不部分执行、金额精度语义、不触碰
既有台账与其它入口状态、模块级一次性入口与内联策略。
"""

import unittest
from decimal import Decimal

from vault_guard import (
    ClearingEngine,
    CurrencyMismatchError,
    Creditor,
    DuplicateSettlementIdError,
    DuplicateTransactionError,
    InvalidSettlementAmountError,
    InvalidSettlementBatchError,
    InvalidSettlementPriorityError,
    RiskPolicy,
    RiskPolicyNotFoundError,
    SettlementBatchEvaluation,
    evaluate_settlement_batch,
)

D = Decimal
ZERO = D("0")


def rec(
    sid,
    debtor,
    amount,
    priority,
    treasury=None,
    creditors=(("senior", "60"), ("equity", "40")),
    capital="0",
    currency=None,
):
    item = {
        "settlement_id": sid,
        "debtor_id": debtor,
        "amount": D(amount),
        "priority": priority,
        "creditors": [(name, D(claim)) for name, claim in creditors],
        "supplementary_capital": D(capital),
    }
    if treasury is not None:
        item["treasury_id"] = treasury
    if currency is not None:
        item["currency"] = currency
    return item


def fresh_engine(policy="P1", treasury="100", debtor="100"):
    engine = ClearingEngine()
    engine.register_risk_policy(policy, D(treasury), D(debtor))
    return engine


class EvaluateBatchAcceptanceTests(unittest.TestCase):
    def test_two_layers_must_both_have_room(self):
        engine = fresh_engine(debtor="50", treasury="100")
        # 付款方层快照 40，本笔 20 将达 60 > 50：付款方不足，整笔拒绝。
        result = engine.evaluate_settlement_batch(
            "B", "USD",
            {"debtor:D1": D("40"), "treasury:T1": ZERO},
            "P1",
            [rec("S1", "D1", "20", 0, treasury="T1")],
        )
        (only,) = result.results
        self.assertFalse(only.accepted)
        self.assertEqual(only.reason_code, "LIMIT_EXCEEDED")
        # 拒绝不预占：两层处理后占额都等于处理前值。
        self.assertEqual(only.debtor_reserved_after, D("40"))
        self.assertEqual(only.treasury_reserved_after, ZERO)
        # 拒绝不进瀑布、不产生坏账。
        self.assertEqual(only.waterfalls, ())
        self.assertEqual(only.bad_debt_attributions, ())
        self.assertEqual(only.uncovered_bad_debt, ZERO)

    def test_treasury_layer_shortage_rejects_whole_record(self):
        engine = fresh_engine(treasury="30", debtor="100")
        result = engine.evaluate_settlement_batch(
            "B", "USD",
            {"debtor:D1": ZERO, "treasury:T1": D("20")},
            "P1",
            [rec("S1", "D1", "20", 0, treasury="T1")],
        )
        (only,) = result.results
        self.assertFalse(only.accepted)
        self.assertEqual(only.reason_code, "LIMIT_EXCEEDED")
        self.assertEqual(only.treasury_reserved_after, D("20"))
        self.assertEqual(only.debtor_reserved_after, ZERO)

    def test_boundary_equality_is_accepted(self):
        engine = fresh_engine(treasury="60", debtor="60")
        result = engine.evaluate_settlement_batch(
            "B", "USD",
            {"debtor:D1": D("40"), "treasury:T1": D("10")},
            "P1",
            [rec("S1", "D1", "20", 0, treasury="T1")],
        )
        self.assertTrue(result.results[0].accepted)
        self.assertEqual(result.results[0].reason_code, "ACCEPTED")
        self.assertEqual(result.results[0].debtor_reserved_after, D("60"))
        self.assertEqual(result.results[0].treasury_reserved_after, D("30"))

    def test_snapshot_first_then_accepted_amounts(self):
        engine = fresh_engine()
        result = engine.evaluate_settlement_batch(
            "B", "USD",
            {"debtor:D1": D("10"), "treasury:T1": D("20")},
            "P1",
            [
                rec("S1", "D1", "30", 0, treasury="T1"),  # 40 / 50
                rec("S2", "D1", "30", 1, treasury="T1"),  # 70 / 80
                rec("S3", "D1", "30", 2, treasury="T1"),  # 100 / 110 拒绝
            ],
        )
        statuses = [(r.settlement_id, r.accepted) for r in result.results]
        self.assertEqual(
            statuses, [("S1", True), ("S2", True), ("S3", False)]
        )
        # 拒绝后付款方 / 资金方占额保持第二笔后的值，不影响后续。
        self.assertEqual(result.results[2].debtor_reserved_after, D("70"))
        self.assertEqual(result.results[2].treasury_reserved_after, D("80"))

    def test_record_without_treasury_skips_treasury_layer(self):
        engine = fresh_engine(treasury="1", debtor="100")
        result = engine.evaluate_settlement_batch(
            "B", "USD", {"debtor:D1": ZERO}, "P1",
            [rec("S1", "D1", "50", 0)],
        )
        self.assertTrue(result.results[0].accepted)
        self.assertIsNone(result.results[0].treasury_reserved_after)
        # 无资金方记录不产生资金方限额项。
        self.assertEqual(
            [q.layer for q in result.reservations], ["debtor"]
        )


class EvaluateBatchRejectionTests(unittest.TestCase):
    def test_rejected_record_does_not_affect_later_record(self):
        engine = fresh_engine(debtor="100")
        result = engine.evaluate_settlement_batch(
            "B", "USD", {"debtor:D1": ZERO}, "P1",
            [
                rec("S1", "D1", "90", 0),
                rec("S2", "D1", "20", 1),   # 若 S1 被拒则 0+20 通过
                rec("S3", "D1", "15", 2),   # S1 受理后 90+15 > 100 拒绝
            ],
        )
        self.assertTrue(result.results[0].accepted)
        self.assertFalse(result.results[1].accepted)
        self.assertEqual(result.results[1].reason_code, "LIMIT_EXCEEDED")
        # 被拒的 S2 不占额：S3 仍以 90 起算。
        self.assertFalse(result.results[2].accepted)
        self.assertEqual(result.results[2].debtor_reserved_after, D("90"))

    def test_rejected_records_have_empty_waterfall_and_bad_debt(self):
        engine = fresh_engine(debtor="10")
        result = engine.evaluate_settlement_batch(
            "B", "USD", {"debtor:D1": ZERO}, "P1",
            [rec("S1", "D1", "20", 0)],
        )
        only = result.results[0]
        self.assertFalse(only.accepted)
        self.assertEqual(only.waterfalls, ())
        self.assertEqual(only.bad_debt_attributions, ())
        self.assertEqual(only.uncovered_bad_debt, ZERO)
        self.assertEqual(only.creditors, ("senior", "equity"))
        # 扁平集合中拒绝记录不贡献任何项。
        self.assertEqual(result.waterfalls, ())
        self.assertEqual(result.bad_debt_attributions, ())


class EvaluateBatchOrderingTests(unittest.TestCase):
    def test_priority_ascending_then_settlement_id_codepoint(self):
        engine = fresh_engine(debtor="100")
        result = engine.evaluate_settlement_batch(
            "B", "USD", {"debtor:D1": ZERO}, "P1",
            [
                rec("b", "D1", "10", 1),
                rec("a", "D1", "10", 1),   # 同 priority，码点 a < b
                rec("z", "D1", "10", 0),   # priority 0 最先
            ],
        )
        self.assertEqual(
            [r.settlement_id for r in result.results], ["z", "a", "b"]
        )
        self.assertEqual(
            [e.sequence for e in result.audit_events], [1, 2, 3]
        )
        self.assertEqual(
            [e.settlement_id for e in result.audit_events], ["z", "a", "b"]
        )

    def test_unicode_codepoint_ordering(self):
        engine = fresh_engine(debtor="1000")
        ids = ["中", "A", "a", "1"]
        records = [rec(sid, "D1", "1", 5) for sid in ids]
        result = engine.evaluate_settlement_batch(
            "B", "USD", {"debtor:D1": ZERO}, "P1", records
        )
        # Unicode 码点：'1'(49) < 'A'(65) < 'a'(97) < '中'(20013)
        self.assertEqual(
            [r.settlement_id for r in result.results],
            ["1", "A", "a", "中"],
        )

    def test_same_input_is_deterministic_across_calls(self):
        records = [rec("b", "D1", "10", 1), rec("a", "D1", "10", 0)]

        def run(engine):
            return engine.evaluate_settlement_batch(
                "B", "USD", {"debtor:D1": ZERO}, "P1",
                [dict(r) for r in records],
            )

        first = run(fresh_engine())
        second = run(fresh_engine())
        self.assertEqual(
            [r.settlement_id for r in first.results],
            [r.settlement_id for r in second.results],
        )
        self.assertEqual(
            [r.accepted for r in first.results],
            [r.accepted for r in second.results],
        )


class EvaluateBatchWaterfallTests(unittest.TestCase):
    def test_pool_then_capital_then_bad_debt(self):
        engine = fresh_engine(debtor="1000")
        result = engine.evaluate_settlement_batch(
            "B", "USD", {"debtor:D1": ZERO}, "P1",
            [
                rec(
                    "S1", "D1", "50", 0,
                    creditors=(("senior", "60"), ("mezz", "60"), ("eq", "30")),
                    capital="40",
                )
            ],
        )
        only = result.results[0]
        self.assertTrue(only.accepted)
        # 池内 50：senior 50；资本 40：senior 余 10、mezz 30。
        self.assertEqual(
            [(w.creditor, w.pool_allocation, w.capital_allocation, w.bad_debt)
             for w in only.waterfalls],
            [
                ("senior", D("50"), D("10"), ZERO),
                ("mezz", ZERO, D("30"), D("30")),
                ("eq", ZERO, ZERO, D("30")),
            ],
        )
        self.assertEqual(only.uncovered_bad_debt, D("60"))
        # 逐项恒等式：池内 + 资本 + 坏账 = 债权金额。
        for w in only.waterfalls:
            self.assertEqual(
                w.pool_allocation + w.capital_allocation + w.bad_debt,
                w.claim_amount,
            )
        # 坏账归因与瀑布逐项同序对齐。
        self.assertEqual(
            [b.creditor for b in only.bad_debt_attributions],
            [w.creditor for w in only.waterfalls],
        )

    def test_accepted_amount_is_fund_source_not_claim_total(self):
        engine = fresh_engine(debtor="1000")
        result = engine.evaluate_settlement_batch(
            "B", "USD", {"debtor:D1": ZERO}, "P1",
            [rec("S1", "D1", "10", 0, creditors=(("a", "100"),))],
        )
        (w,) = result.results[0].waterfalls
        self.assertEqual(w.pool_allocation, D("10"))
        self.assertEqual(w.bad_debt, D("90"))

    def test_flat_waterfalls_follow_processing_order(self):
        engine = fresh_engine(debtor="1000")
        result = engine.evaluate_settlement_batch(
            "B", "USD", {"debtor:D1": ZERO}, "P1",
            [
                rec("hi", "D1", "10", 1, creditors=(("x", "10"),)),
                rec("lo", "D1", "10", 0, creditors=(("y", "10"),)),
            ],
        )
        self.assertEqual(
            [(w.creditor) for w in result.waterfalls], ["y", "x"]
        )


class EvaluateBatchReservationTests(unittest.TestCase):
    def test_reservation_totals(self):
        engine = fresh_engine(treasury="200", debtor="200")
        result = engine.evaluate_settlement_batch(
            "B", "USD",
            {"debtor:D1": D("15"), "treasury:T1": D("25")},
            "P1",
            [
                rec("S1", "D1", "10", 0, treasury="T1"),
                rec("S2", "D1", "1000", 1, treasury="T1"),  # 拒绝
                rec("S3", "D2", "20", 2, treasury="T1"),
            ],
        )
        by_key = {q.limit_key: q for q in result.reservations}
        d1 = by_key["debtor:D1"]
        self.assertEqual(d1.initial_reserved, D("15"))
        self.assertEqual(d1.accepted_reserved, D("10"))   # 仅 S1
        self.assertEqual(d1.rejected_reserved, ZERO)
        self.assertEqual(d1.final_reserved, D("25"))
        self.assertEqual(d1.layer, "debtor")
        self.assertEqual(d1.party_id, "D1")
        self.assertEqual(d1.limit, D("200"))
        self.assertEqual(d1.remaining, D("175"))

        t1 = by_key["treasury:T1"]
        self.assertEqual(t1.initial_reserved, D("25"))
        self.assertEqual(t1.accepted_reserved, D("30"))   # S1 + S3
        self.assertEqual(t1.final_reserved, D("55"))
        self.assertEqual(t1.layer, "treasury")

        d2 = by_key["debtor:D2"]
        self.assertEqual(d2.initial_reserved, ZERO)
        self.assertEqual(d2.accepted_reserved, D("20"))

    def test_snapshot_only_limit_item_is_reported(self):
        engine = fresh_engine()
        result = engine.evaluate_settlement_batch(
            "B", "USD",
            {"debtor:UNUSED": D("5"), "treasury:IDLE": D("7")},
            "P1",
            [rec("S1", "D1", "10", 0, treasury="T1")],
        )
        keys = {q.limit_key for q in result.reservations}
        self.assertIn("debtor:UNUSED", keys)
        self.assertIn("treasury:IDLE", keys)
        idle = next(
            q for q in result.reservations if q.limit_key == "debtor:UNUSED"
        )
        self.assertEqual(idle.initial_reserved, D("5"))
        self.assertEqual(idle.accepted_reserved, ZERO)
        self.assertEqual(idle.final_reserved, D("5"))


class EvaluateBatchAuditTests(unittest.TestCase):
    def test_one_audit_event_per_record_with_reason_code(self):
        engine = fresh_engine(debtor="100")
        result = engine.evaluate_settlement_batch(
            "B", "USD", {"debtor:D1": ZERO}, "P1",
            [
                rec("S1", "D1", "60", 0),
                rec("S2", "D1", "60", 1),   # 拒绝
            ],
        )
        self.assertEqual(len(result.audit_events), 2)
        e1, e2 = result.audit_events
        self.assertEqual(e1.reason_code, "ACCEPTED")
        self.assertTrue(e1.accepted)
        self.assertEqual(e1.amount, D("60"))
        self.assertEqual(e2.reason_code, "LIMIT_EXCEEDED")
        self.assertFalse(e2.accepted)
        self.assertEqual(e1.treasury_id, None)
        # 记录结果内嵌同一审计事件。
        self.assertIs(result.results[0].audit_event, e1)

    def test_evaluation_does_not_touch_engine_audit_log(self):
        engine = fresh_engine()
        before = engine.audit_log
        engine.evaluate_settlement_batch(
            "B", "USD", {"debtor:D1": ZERO}, "P1",
            [rec("S1", "D1", "10", 0)],
        )
        self.assertEqual(engine.audit_log, before)
        self.assertEqual(len(engine.events()), 0)
        self.assertIsNone(engine.get_event("S1"))
        self.assertIsNone(engine.result_of("S1"))


class EvaluateBatchValidationTests(unittest.TestCase):
    def setUp(self):
        self.engine = fresh_engine()

    def _call(self, **overrides):
        kw = dict(
            batch_id="B",
            currency="USD",
            occupancy_snapshot={},
            risk_policy="P1",
            records=[rec("S1", "D1", "1", 0)],
        )
        kw.update(overrides)
        return self.engine.evaluate_settlement_batch(**kw)

    def test_missing_batch_id(self):
        for bad in (None, "", "   "):
            with self.subTest(bad=bad):
                with self.assertRaises(InvalidSettlementBatchError):
                    self._call(batch_id=bad)

    def test_missing_currency(self):
        for bad in (None, "", "  "):
            with self.subTest(bad=bad):
                with self.assertRaises(InvalidSettlementBatchError):
                    self._call(currency=bad)

    def test_missing_risk_policy(self):
        for bad in (None, "", "  "):
            with self.subTest(bad=bad):
                with self.assertRaises(InvalidSettlementBatchError):
                    self._call(risk_policy=bad)

    def test_unknown_policy(self):
        with self.assertRaises(RiskPolicyNotFoundError):
            self._call(risk_policy="MISSING")

    def test_duplicate_within_batch(self):
        records = [rec("S1", "D1", "1", 0), rec("S1", "D2", "1", 1)]
        with self.assertRaises(DuplicateSettlementIdError):
            self._call(records=records)

    def test_duplicate_across_calls(self):
        self._call()
        with self.assertRaises(DuplicateSettlementIdError):
            self._call(batch_id="B2")

    def test_currency_mismatch(self):
        with self.assertRaises(CurrencyMismatchError):
            self._call(records=[rec("S1", "D1", "1", 0, currency="EUR")])

    def test_creditor_currency_mismatch(self):
        bad = {
            "settlement_id": "S1", "debtor_id": "D1", "amount": D("1"),
            "priority": 0, "creditors": [Creditor("c", D("1"), "EUR")],
        }
        with self.assertRaises(CurrencyMismatchError):
            self._call(records=[bad])

    def test_amount_must_be_finite_positive(self):
        for bad in (0, D("0"), D("-1"), -5, float("nan"), float("inf"),
                    D("NaN"), "10", None, True):
            with self.subTest(bad=bad):
                raw = {
                    "settlement_id": "S1", "debtor_id": "D1",
                    "amount": bad, "priority": 0,
                    "creditors": [("c", D("1"))],
                }
                with self.assertRaises(InvalidSettlementAmountError):
                    self._call(records=[raw])

    def test_priority_must_be_integer(self):
        for bad in (1.0, D("1"), "1", None, True, False):
            with self.subTest(bad=bad):
                with self.assertRaises(InvalidSettlementPriorityError):
                    self._call(
                        records=[
                            {
                                "settlement_id": "S1", "debtor_id": "D1",
                                "amount": D("1"), "priority": bad,
                                "creditors": [("c", D("1"))],
                            }
                        ]
                    )

    def test_invalid_snapshot_raises_value_error(self):
        with self.assertRaises(ValueError):
            self._call(occupancy_snapshot={"unknown:D": D("1")})
        with self.assertRaises(ValueError):
            self._call(occupancy_snapshot={"debtor:D": D("-1")})
        with self.assertRaises(ValueError):
            self._call(occupancy_snapshot="not-a-mapping")

    def test_invalid_records_container(self):
        with self.assertRaises(InvalidSettlementBatchError):
            self._call(records=None)

    def test_missing_record_fields(self):
        base = {
            "settlement_id": "S1", "debtor_id": "D1", "amount": D("1"),
            "priority": 0, "creditors": [("c", D("1"))],
        }
        # 缺少专用异常的标识 / 债权字段归入批次级异常。
        for omitted in ("settlement_id", "debtor_id", "creditors"):
            bad = dict(base)
            del bad[omitted]
            with self.subTest(omitted=omitted):
                with self.assertRaises(InvalidSettlementBatchError):
                    self._call(records=[bad])
        # 缺 amount / priority 分别按各自专用异常处理。
        no_amount = dict(base)
        del no_amount["amount"]
        with self.assertRaises(InvalidSettlementAmountError):
            self._call(records=[no_amount])
        no_priority = dict(base)
        del no_priority["priority"]
        with self.assertRaises(InvalidSettlementPriorityError):
            self._call(records=[no_priority])

    def test_invalid_request_is_atomic(self):
        # 整批失败不登记任何 settlement_id，修正后可用同一标识重新提交。
        records = [
            rec("A1", "D1", "1", 0),
            {
                "settlement_id": "A2", "debtor_id": "D1",
                "amount": D("0"), "priority": 0,
                "creditors": [("c", D("1"))],   # amount 非正数
            },
        ]
        with self.assertRaises(InvalidSettlementAmountError):
            self._call(records=records)
        follow_up = self._call(batch_id="B2", records=[rec("A1", "D1", "1", 0)])
        self.assertTrue(follow_up.results[0].accepted)

    def test_failed_batch_leaves_no_reservations_or_audit(self):
        with self.assertRaises(DuplicateSettlementIdError):
            self.engine.evaluate_settlement_batch(
                "B", "USD", {"debtor:D1": D("5")}, "P1",
                [rec("A1", "D1", "60", 0), rec("A1", "D2", "60", 1)],
            )
        self.assertEqual(len(self.engine.events()), 0)


class EvaluateBatchPrecisionTests(unittest.TestCase):
    def test_float_amount_normalized_by_string_form(self):
        engine = fresh_engine(debtor="100")
        result = engine.evaluate_settlement_batch(
            "B", "USD", {"debtor:D1": ZERO}, "P1",
            [{
                "settlement_id": "S1", "debtor_id": "D1", "amount": 0.1,
                "priority": 0, "creditors": [("c", 1.0)],
            }],
        )
        w = result.results[0].waterfalls[0]
        self.assertEqual(w.pool_allocation, D("0.1"))
        self.assertEqual(w.bad_debt, D("0.9"))

    def test_equivalent_amounts_reserve_identically(self):
        engine = fresh_engine(debtor="10")
        records = [{
            "settlement_id": "S1", "debtor_id": "D1", "amount": amount,
            "priority": 0, "creditors": [("c", D("10"))],
        } for amount in (10, 10.0, D("10.00"))]
        for record in records:
            engine = fresh_engine(debtor="10")
            result = engine.evaluate_settlement_batch(
                "B", "USD", {"debtor:D1": ZERO}, "P1", [dict(record)]
            )
            self.assertTrue(result.results[0].accepted)
            (q,) = result.reservations
            self.assertEqual(q.final_reserved, D("10"))


class EvaluateBatchIsolationTests(unittest.TestCase):
    def test_inputs_not_retained_across_calls(self):
        engine = fresh_engine()
        # 占额快照只服务本次调用：第二次调用不带快照时从 0 起算。
        first = engine.evaluate_settlement_batch(
            "B1", "USD", {"debtor:D1": D("80")}, "P1",
            [rec("S1", "D1", "10", 0)],
        )
        self.assertEqual(first.reservations[0].final_reserved, D("90"))
        second = engine.evaluate_settlement_batch(
            "B2", "USD", {}, "P1", [rec("S2", "D1", "10", 0)]
        )
        # 新批次快照为 0；此前批内受理额不带入。
        self.assertEqual(second.reservations[0].initial_reserved, ZERO)
        self.assertEqual(second.reservations[0].final_reserved, D("10"))

    def test_does_not_change_existing_entries(self):
        engine = fresh_engine()
        # 既有单笔流程仍写既有台账。
        settled = engine.process(
            transaction_id="TX-1", currency="USD", pool_balance=D("100"),
            settlement_amount=D("40"), notional_exposure=D("0"),
            base_limit=D("100"), risk_factor=D("0"),
            creditors=[("senior", D("40"))],
        )
        ledger_before = len(engine.audit_log)
        self.assertTrue(settled.approved)
        engine.evaluate_settlement_batch(
            "EB", "USD", {}, "P1", [rec("E1", "D1", "10", 0)]
        )
        self.assertEqual(len(engine.audit_log), ledger_before)
        # settlement_id 与既有 transaction_id 命名空间互不影响。
        self.assertTrue(engine.has_transaction("TX-1"))
        self.assertFalse(engine.has_transaction("E1"))
        with self.assertRaises(DuplicateTransactionError):
            engine.process(
                transaction_id="TX-1", currency="USD", pool_balance=D("100"),
                settlement_amount=D("1"), notional_exposure=D("0"),
                base_limit=D("100"), risk_factor=D("0"),
                creditors=[("senior", D("1"))],
            )


class InlinePolicyAndModuleFuncTests(unittest.TestCase):
    def test_inline_risks_policy_object(self):
        engine = ClearingEngine()
        result = engine.evaluate_settlement_batch(
            "B", "USD", {}, RiskPolicy("inline", D("5"), D("5")),
            [rec("S1", "D1", "5", 0)],
        )
        self.assertTrue(result.results[0].accepted)
        self.assertEqual(result.reservations[0].limit, D("5"))

    def test_inline_policy_mapping(self):
        result = evaluate_settlement_batch(
            "B", "USD", {"debtor:D1": D("4")},
            {"treasury_limit": D("100"), "debtor_limit": D("10")},
            [rec("S1", "D1", "6", 0)],
        )
        self.assertIsInstance(result, SettlementBatchEvaluation)
        self.assertTrue(result.results[0].accepted)

    def test_module_func_one_shot_has_no_registered_policy(self):
        with self.assertRaises(RiskPolicyNotFoundError):
            evaluate_settlement_batch(
                "B", "USD", {}, "P1", [rec("S1", "D1", "1", 0)]
            )

    def test_registered_policy_lookup_and_readback(self):
        engine = ClearingEngine()
        policy = engine.register_risk_policy("RP", D("11"), D("22"))
        self.assertEqual(policy.treasury_limit, D("11"))
        self.assertEqual(engine.risk_policy("RP").debtor_limit, D("22"))
        result = engine.evaluate_settlement_batch(
            "B", "USD", {}, "RP", [rec("S1", "D1", "5", 0)]
        )
        self.assertTrue(result.results[0].accepted)


if __name__ == "__main__":
    unittest.main()
