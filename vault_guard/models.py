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
    "RiskGroupUsage",
    "RiskGroupBatchResult",
    "MulticurrencyBatchResult",
    "RecoveryAllocation",
    "RecoveryResult",
    "OutstandingBadDebt",
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
    risk_group_id: str | None = None

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
    group_used_after: Decimal | None = None

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
class RiskGroupUsage:
    """单个风险组在批次结束时的额度快照（不可变）。

    - ``used``：组内已放行请求的累计风险占用。
    - ``limit``：该组登记的非负上限。
    - ``remaining``：``limit - used``。
    """

    used: Decimal
    limit: Decimal
    remaining: Decimal


@dataclass(frozen=True)
class RiskGroupBatchResult:
    """带风险组累计限额的多笔批次结算公开返回结构（不可变）。

    - ``results``：与批次 ``requests`` 同序的单笔 :class:`SettlementResult`；
      带风险组的每笔另含 ``group_used_after``（该笔执行后的组内已用额度，
      未占用的拒绝结果等于执行前值），无风险组的笔为 ``None``。
    - ``event_ids``：与 ``results`` 同序的审计事件标识；批次本身不产生事件。
    - ``validated_available_balance``：批次全部请求执行完毕后的最终可用余额。
    - ``risk_groups``：引擎已登记的全部风险组到 :class:`RiskGroupUsage`
      的只读映射。
    """

    results: tuple[SettlementResult, ...]
    event_ids: tuple[str, ...]
    validated_available_balance: Decimal
    risk_groups: Mapping[str, RiskGroupUsage]


@dataclass(frozen=True)
class MulticurrencyBatchResult:
    """多币种批次结算的公开返回结构（不可变）。

    - ``results``：与批次 ``requests`` 同序的单笔 :class:`SettlementResult`；
      每笔的 ``validated_available_balance`` 为本币种执行后的即时余额。
    - ``event_ids``：与 ``results`` 同序的审计事件标识；批次本身不产生事件。
    - ``validated_available_balances``：全部币种到最终可用余额的只读映射；
      未被任何请求使用的币种保留期初原值。
    """

    results: tuple[SettlementResult, ...]
    event_ids: tuple[str, ...]
    validated_available_balances: Mapping[str, Decimal]


@dataclass(frozen=True)
class RecoveryAllocation:
    """一笔回收对单笔存续坏账的冲减明细（按冲减顺序排列）。

    - ``source_transaction_id``：产生该坏账的来源结算流水号。
    - ``creditor``：被冲减的债权名。
    - ``recovered_amount``：本次回收对该笔坏账的冲减额。
    - ``remaining_bad_debt``：冲减后该笔坏账的剩余余额。
    """

    source_transaction_id: str
    creditor: str
    recovered_amount: Decimal
    remaining_bad_debt: Decimal


@dataclass(frozen=True)
class RecoveryResult:
    """存续坏账回收的公开返回结构（不可变）。

    - ``recovery_amount``：归一化后的回收额。
    - ``allocations``：按冲减顺序排列的 :class:`RecoveryAllocation`。
    - ``total_recovered``：本次回收合计（等于 ``recovery_amount``）。
    - ``outstanding_bad_debt``：回收后该币种的存续坏账总额。
    """

    recovery_transaction_id: str
    currency: str
    recovery_amount: Decimal
    allocations: tuple[RecoveryAllocation, ...]
    total_recovered: Decimal
    outstanding_bad_debt: Decimal
    event_id: str


@dataclass(frozen=True)
class OutstandingBadDebt:
    """查询时点的一笔存续坏账余额。

    - ``source_transaction_id``：产生该坏账的来源结算流水号。
    - ``balance``：该笔坏账的剩余余额。
    """

    source_transaction_id: str
    creditor: str
    currency: str
    balance: Decimal


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
    # 回收事件的冲减明细：
    # (来源结算流水号, 债权名, 本次冲减额, 剩余坏账)；结算事件恒为空元组。
    recovery_allocations: tuple[tuple[str, str, Decimal, Decimal], ...] = ()
