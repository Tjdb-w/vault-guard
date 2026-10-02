"""金库与清算风控引擎。

公开入口为 :meth:`ClearanceEngine.submit`，它对同一笔单币种结算请求
依次执行：输入校验 → 限额校验 → 清算瀑布 → 坏账归因 → 追加审计事件。

关键约定：

* 金额使用 ``Decimal`` 精确比较与分配，结果确定可重复；
* 输入校验失败抛出对应领域异常，不产生任何分配或审计事件；
* 业务拒绝（余额不足 / 风险超限）不扣减余额、不产生分配，
  但会追加一条拒绝审计事件；
* 每个被受理的请求恰好产生一个审计事件标识；
* 池内资金按优先债权清单顺序受偿，补充资本在池内瀑布之后
  仍按清单顺序补足剩余债权。
"""

from __future__ import annotations

from decimal import Decimal
from typing import Iterable, List, Optional, Sequence, Tuple

from .audit import AuditEvent, AuditLedger, InputSummary
from .errors import (
    DuplicateTransactionError,
    EmptyCreditorListError,
    InvalidCurrencyError,
    InvalidRiskFactorError,
    MixedCurrencyError,
)
from .models import Creditor, CreditorAttribution, SettlementRequest, SettlementResult

#: 风险系数允许区间（含端点）。
RISK_FACTOR_MIN = Decimal("0")
RISK_FACTOR_MAX = Decimal("1")

#: 拒绝原因的稳定标识。
REJECT_INSUFFICIENT_BALANCE = "INSUFFICIENT_POOL_BALANCE"
REJECT_RISK_LIMIT_EXCEEDED = "RISK_LIMIT_EXCEEDED"

_ZERO = Decimal("0")


def _as_decimal(value: object, field_name: str) -> Decimal:
    """把公开入参规整为 Decimal；不支持的类型直接拒绝，不做静默修正。"""
    if isinstance(value, bool):
        raise TypeError(f"{field_name} 必须是数值类型，收到 bool")
    if isinstance(value, Decimal):
        return value
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, float):
        # 经 str 中转，避免二进制浮点尾数进入精确计算。
        return Decimal(str(value))
    raise TypeError(f"{field_name} 必须是 Decimal/int/float，收到 {type(value)!r}")


class ClearanceEngine:
    """单笔单币种结算的风控引擎，持有内存审计台账。"""

    def __init__(self, ledger: Optional[AuditLedger] = None) -> None:
        self._ledger = ledger if ledger is not None else AuditLedger()

    @property
    def ledger(self) -> AuditLedger:
        """暴露台账以供只读查询；查询本身不会触发清算。"""
        return self._ledger

    # ------------------------------------------------------------------
    # 公开入口
    # ------------------------------------------------------------------
    def submit(self, request: SettlementRequest) -> SettlementResult:
        """受理一笔结算请求，返回稳定结构的处理结果。

        同一 ``transaction_id`` 在同一引擎实例内只能成功受理一次。
        """
        normalized = self._validate(request)

        # 重复流水号：在校验全部通过后判定，直接拒绝，不留任何痕迹。
        if self._ledger.get_by_transaction(normalized.transaction_id) is not None:
            raise DuplicateTransactionError(
                f"业务流水号重复: {normalized.transaction_id!r}"
            )

        risk_usage = (
            normalized.settlement_amount
            + normalized.notional_exposure * normalized.risk_factor
        )

        rejection_reason: Optional[str] = None
        if normalized.settlement_amount > normalized.pool_balance:
            rejection_reason = REJECT_INSUFFICIENT_BALANCE
        elif risk_usage > normalized.base_limit:
            rejection_reason = REJECT_RISK_LIMIT_EXCEEDED

        input_summary = self._build_input_summary(normalized)

        if rejection_reason is not None:
            # 拒绝：可用余额不变，无任何分配与坏账。
            attributions: List[CreditorAttribution] = [
                CreditorAttribution(
                    name=c.name,
                    currency=c.currency,
                    claim_amount=c.amount,
                    pool_allocation=_ZERO,
                    capital_allocation=_ZERO,
                    bad_debt=_ZERO,
                )
                for c in normalized.creditors
            ]
            result = SettlementResult(
                transaction_id=normalized.transaction_id,
                currency=normalized.currency,
                approved=False,
                verified_balance=normalized.pool_balance,
                risk_usage=risk_usage,
                pool_allocations=attributions,
                total_pool_allocated=_ZERO,
                total_capital_allocated=_ZERO,
                uncovered_bad_debt=_ZERO,
                rejection_reason=rejection_reason,
            )
        else:
            attributions, total_pool, total_capital, uncovered = self._waterfall(
                normalized.creditors,
                normalized.settlement_amount,
                normalized.supplementary_capital,
            )
            result = SettlementResult(
                transaction_id=normalized.transaction_id,
                currency=normalized.currency,
                approved=True,
                verified_balance=normalized.pool_balance - total_pool,
                risk_usage=risk_usage,
                pool_allocations=attributions,
                total_pool_allocated=total_pool,
                total_capital_allocated=total_capital,
                uncovered_bad_debt=uncovered,
                rejection_reason=None,
            )

        # 结果与归因构建完成后一次性落账，杜绝半成品事件。
        event = self._build_event(input_summary, result)
        self._ledger.append(event)
        return SettlementResult(
            transaction_id=result.transaction_id,
            currency=result.currency,
            approved=result.approved,
            verified_balance=result.verified_balance,
            risk_usage=result.risk_usage,
            pool_allocations=list(result.pool_allocations),
            total_pool_allocated=result.total_pool_allocated,
            total_capital_allocated=result.total_capital_allocated,
            uncovered_bad_debt=result.uncovered_bad_debt,
            rejection_reason=result.rejection_reason,
            audit_event_id=event.event_id,
        )

    # ------------------------------------------------------------------
    # 校验
    # ------------------------------------------------------------------
    def _validate(self, request: SettlementRequest) -> SettlementRequest:
        if not isinstance(request, SettlementRequest):
            raise TypeError("request 必须是 SettlementRequest 实例")

        # 1) 币种缺失
        currency = request.currency
        if not isinstance(currency, str) or not currency.strip():
            raise InvalidCurrencyError("账户币种缺失或为空")
        currency = currency.strip()

        # 2) 数值规整（不支持的类型抛 TypeError，不做静默修正）
        pool_balance = _as_decimal(request.pool_balance, "pool_balance")
        settlement_amount = _as_decimal(request.settlement_amount, "settlement_amount")
        notional_exposure = _as_decimal(request.notional_exposure, "notional_exposure")
        base_limit = _as_decimal(request.base_limit, "base_limit")
        risk_factor = _as_decimal(request.risk_factor, "risk_factor")
        supplementary_capital = _as_decimal(
            request.supplementary_capital, "supplementary_capital"
        )

        # 3) 负数
        negative_fields = []
        if pool_balance < 0:
            negative_fields.append("pool_balance")
        if settlement_amount < 0:
            negative_fields.append("settlement_amount")
        if notional_exposure < 0:
            negative_fields.append("notional_exposure")
        if base_limit < 0:
            negative_fields.append("base_limit")
        if supplementary_capital < 0:
            negative_fields.append("supplementary_capital")
        if negative_fields:
            raise ValueError(f"以下字段不允许为负数: {', '.join(negative_fields)}")

        # 4) 风险系数越界
        if risk_factor < RISK_FACTOR_MIN or risk_factor > RISK_FACTOR_MAX:
            raise InvalidRiskFactorError(
                f"风险系数 {risk_factor} 超出允许区间 "
                f"[{RISK_FACTOR_MIN}, {RISK_FACTOR_MAX}]"
            )

        # 5) 债权清单：类型 → 空清单 → 逐笔币种与金额
        creditors_raw = request.creditors
        if not isinstance(creditors_raw, (list, tuple)):
            raise TypeError("creditors 必须是列表或元组")
        if len(creditors_raw) == 0:
            raise EmptyCreditorListError("优先债权清单不能为空")

        creditors: List[Creditor] = []
        for index, creditor in enumerate(creditors_raw):
            if not isinstance(creditor, Creditor):
                raise TypeError(f"第 {index} 项债权必须是 Creditor 实例")
            creditor_currency = creditor.currency
            if not isinstance(creditor_currency, str) or not creditor_currency.strip():
                raise InvalidCurrencyError(f"第 {index} 项债权缺少币种")
            creditor_currency = creditor_currency.strip()
            if creditor_currency != currency:
                raise MixedCurrencyError(
                    f"检测到混合币种: 账户币种 {currency!r}，"
                    f"债权 {creditor.name!r} 币种 {creditor_currency!r}"
                )
            amount = _as_decimal(creditor.amount, f"creditors[{index}].amount")
            if amount < 0:
                raise ValueError(f"债权 {creditor.name!r} 金额不允许为负数")
            creditors.append(
                Creditor(
                    name=creditor.name,
                    amount=amount,
                    currency=creditor_currency,
                )
            )

        transaction_id = request.transaction_id
        if not isinstance(transaction_id, str) or not transaction_id.strip():
            raise ValueError("业务流水号缺失或为空")

        return SettlementRequest(
            transaction_id=transaction_id,
            currency=currency,
            pool_balance=pool_balance,
            settlement_amount=settlement_amount,
            notional_exposure=notional_exposure,
            base_limit=base_limit,
            risk_factor=risk_factor,
            creditors=creditors,
            supplementary_capital=supplementary_capital,
        )

    # ------------------------------------------------------------------
    # 清算瀑布
    # ------------------------------------------------------------------
    def _waterfall(
        self,
        creditors: Sequence[Creditor],
        settlement_amount: Decimal,
        supplementary_capital: Decimal,
    ) -> Tuple[List[CreditorAttribution], Decimal, Decimal, Decimal]:
        """按清单顺序执行两级瀑布。

        对每笔债权依次使用：池内可用资金 → 补充资本，剩余即为坏账。
        由于两级都按同一清单顺序贪心受偿，逐笔两级补足与
        “先全量池内瀑布、再全量资本瀑布”结果完全一致，且高优先级
        债权总是先得到全额受偿。
        """
        remaining_pool = settlement_amount
        remaining_capital = supplementary_capital
        attributions: List[CreditorAttribution] = []
        total_pool = _ZERO
        total_capital = _ZERO
        total_bad_debt = _ZERO

        for creditor in creditors:
            pool_alloc = min(creditor.amount, remaining_pool)
            remaining_pool -= pool_alloc

            unpaid_after_pool = creditor.amount - pool_alloc
            capital_alloc = min(unpaid_after_pool, remaining_capital)
            remaining_capital -= capital_alloc

            bad_debt = unpaid_after_pool - capital_alloc

            attributions.append(
                CreditorAttribution(
                    name=creditor.name,
                    currency=creditor.currency,
                    claim_amount=creditor.amount,
                    pool_allocation=pool_alloc,
                    capital_allocation=capital_alloc,
                    bad_debt=bad_debt,
                )
            )
            total_pool += pool_alloc
            total_capital += capital_alloc
            total_bad_debt += bad_debt

        return attributions, total_pool, total_capital, total_bad_debt

    # ------------------------------------------------------------------
    # 审计
    # ------------------------------------------------------------------
    def _build_input_summary(self, request: SettlementRequest) -> InputSummary:
        return InputSummary(
            currency=request.currency,
            pool_balance=request.pool_balance,
            settlement_amount=request.settlement_amount,
            notional_exposure=request.notional_exposure,
            base_limit=request.base_limit,
            risk_factor=request.risk_factor,
            supplementary_capital=request.supplementary_capital,
            creditors=tuple((c.name, c.amount) for c in request.creditors),
        )

    def _build_event(
        self, summary: InputSummary, result: SettlementResult
    ) -> AuditEvent:
        sequence = len(self._ledger) + 1
        event_id = f"EVT-{sequence:06d}"
        return AuditEvent(
            event_id=event_id,
            sequence=sequence,
            transaction_id=result.transaction_id,
            input_summary=summary,
            approved=result.approved,
            risk_usage=result.risk_usage,
            verified_balance=result.verified_balance,
            allocations=tuple(result.pool_allocations),
            total_pool_allocated=result.total_pool_allocated,
            total_capital_allocated=result.total_capital_allocated,
            uncovered_bad_debt=result.uncovered_bad_debt,
            rejection_reason=result.rejection_reason,
        )

    # ------------------------------------------------------------------
    # 只读查询（不触发清算）
    # ------------------------------------------------------------------
    def events(self) -> Tuple[AuditEvent, ...]:
        """按顺序返回全部审计事件的只读快照。"""
        return self._ledger.events()

    def get_event(self, event_id: str) -> Optional[AuditEvent]:
        """按事件标识读取已有事件。"""
        return self._ledger.get_event(event_id)

    def get_by_transaction(self, transaction_id: str) -> Optional[AuditEvent]:
        """按业务流水号读取已有事件。"""
        return self._ledger.get_by_transaction(transaction_id)


def process_settlement(
    transaction_id: str,
    currency: str,
    pool_balance: Decimal,
    settlement_amount: Decimal,
    notional_exposure: Decimal,
    base_limit: Decimal,
    risk_factor: Decimal,
    creditors: Iterable[Creditor],
    supplementary_capital: Decimal = _ZERO,
    engine: Optional[ClearanceEngine] = None,
) -> SettlementResult:
    """函数式公开入口：一次性构建请求并提交给引擎。

    传入已有 ``engine`` 可复用同一审计台账；省略时使用临时引擎。
    """
    active_engine = engine if engine is not None else ClearanceEngine()
    request = SettlementRequest(
        transaction_id=transaction_id,
        currency=currency,
        pool_balance=pool_balance,
        settlement_amount=settlement_amount,
        notional_exposure=notional_exposure,
        base_limit=base_limit,
        risk_factor=risk_factor,
        creditors=list(creditors),
        supplementary_capital=supplementary_capital,
    )
    return active_engine.submit(request)
