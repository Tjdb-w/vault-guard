"""Vault Guard 公开数据模型。

金额字段统一使用 :class:`decimal.Decimal`，避免浮点误差；输入在引擎入口处
完成归一化，模型内全部为已校验的不可变值。
"""

from dataclasses import dataclass
from decimal import Decimal
from typing import Mapping

__all__ = [
    "Creditor",
    "SettlementRequest",
    "CreditorAttribution",
    "SettlementResult",
    "BatchSettlementResult",
    "AuditEvent",
]


@dataclass(frozen=True)
class Creditor:
    """单笔优先债权。

    - ``name``：债权标识，用于归因对齐。
    - ``amount``：债权金额，必须非负。
    - ``currency``：可选币种；若提供则必须与账户币种一致，否则混合币种。
    """

    name: str
    amount: Decimal
    currency: str | None = None


@dataclass(frozen=True)
class SettlementRequest:
    """归一化后的单笔结算请求（同一币种）。"""

    transaction_id: str
    currency: str
    pool_balance: Decimal
    settlement_amount: Decimal
    notional_exposure: Decimal
    base_limit: Decimal
    risk_factor: Decimal
    creditors: tuple[Creditor, ...]
    supplementary_capital: Decimal

    def input_summary(self) -> Mapping[str, object]:
        """用于审计台账的输入摘要（只包含输入事实，不含结果）。"""
        return {
            "transaction_id": self.transaction_id,
            "currency": self.currency,
            "pool_balance": self.pool_balance,
            "settlement_amount": self.settlement_amount,
            "notional_exposure": self.notional_exposure,
            "base_limit": self.base_limit,
            "risk_factor": self.risk_factor,
            "supplementary_capital": self.supplementary_capital,
            "creditors": tuple(
                (c.name, c.amount, c.currency) for c in self.creditors
            ),
        }


@dataclass(frozen=True)
class CreditorAttribution:
    """单笔债权的逐项归因。

    - ``pool_allocation``：资金池（本次拟清算金额）内分配。
    - ``capital_allocation``：补充资本承担。
    - ``bad_debt``：最终未覆盖坏账。
    """

    creditor: str
    claim_amount: Decimal
    pool_allocation: Decimal
    capital_allocation: Decimal
    bad_debt: Decimal


@dataclass(frozen=True)
class SettlementResult:
    """稳定可重复的公开返回结构，成功与拒绝路径同构。"""

    transaction_id: str
    approved: bool
    validated_available_balance: Decimal
    creditors: tuple[str, ...]
    pool_allocations: tuple[Decimal, ...]
    capital_allocations: tuple[Decimal, ...]
    attributions: tuple[CreditorAttribution, ...]
    uncovered_bad_debt: Decimal
    risk_occupancy: Decimal
    event_id: str
    rejection_reason: str | None

    @property
    def total_pool_allocated(self) -> Decimal:
        return sum(self.pool_allocations, Decimal(0))

    @property
    def total_capital_allocated(self) -> Decimal:
        return sum(self.capital_allocations, Decimal(0))


@dataclass(frozen=True)
class BatchSettlementResult:
    """多笔批次结算的公开返回结构（不可变）。

    - ``results``：与批次 ``requests`` 同序的单笔 :class:`SettlementResult`；
      其中每笔的 ``validated_available_balance`` 为该笔执行后的即时余额。
    - ``event_ids``：与 ``results`` 同序的审计事件标识；批次本身不产生事件。
    - ``validated_available_balance``：批次全部请求执行完毕后的最终可用余额。
    """

    results: tuple[SettlementResult, ...]
    event_ids: tuple[str, ...]
    validated_available_balance: Decimal


@dataclass(frozen=True)
class AuditEvent:
    """内存追加式审计台账中的一条事件。

    每个请求（放行或拒绝）恰好产生一条事件；输入校验异常不产生事件。
    """

    event_id: str
    sequence: int
    transaction_id: str
    approved: bool
    currency: str
    input_summary: Mapping[str, object]
    validation_result: str
    risk_occupancy: Decimal
    pool_allocations: tuple[tuple[str, Decimal], ...]
    capital_allocations: tuple[tuple[str, Decimal], ...]
    uncovered_bad_debt: Decimal
    validated_available_balance: Decimal
    rejection_reason: str | None
