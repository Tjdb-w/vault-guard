"""公开数据模型：请求输入、逐项归因、处理结果。

金额一律使用 ``decimal.Decimal``，避免二进制浮点误差带来的不确定性。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import List, Optional


@dataclass(frozen=True)
class Creditor:
    """一笔优先债权。

    :param name: 债权标识（按清单先后顺序受偿）。
    :param amount: 债权金额，必须为非负数。
    :param currency: 债权币种，必须与账户币种一致。
    """

    name: str
    amount: Decimal
    currency: str


@dataclass(frozen=True)
class SettlementRequest:
    """单笔单币种结算请求的完整输入。"""

    transaction_id: str
    currency: str
    pool_balance: Decimal
    settlement_amount: Decimal
    notional_exposure: Decimal
    base_limit: Decimal
    risk_factor: Decimal
    creditors: List[Creditor]
    supplementary_capital: Decimal = Decimal(0)


@dataclass(frozen=True)
class CreditorAttribution:
    """单笔债权的逐项归因。

    ``pool_allocation + capital_allocation + bad_debt == claim_amount``
    恒成立；池内分配与资本承担均不得超过对应债权金额。
    """

    name: str
    currency: str
    claim_amount: Decimal
    pool_allocation: Decimal
    capital_allocation: Decimal
    bad_debt: Decimal


@dataclass(frozen=True)
class SettlementResult:
    """一次结算请求的稳定、可重复的公开返回结构。

    拒绝路径下不发生任何分配：``verified_balance`` 与输入池余额一致，
    归因列表各项分配与坏账均为零。
    """

    transaction_id: str
    currency: str
    approved: bool
    verified_balance: Decimal
    risk_usage: Decimal
    pool_allocations: List[CreditorAttribution] = field(default_factory=list)
    total_pool_allocated: Decimal = Decimal(0)
    total_capital_allocated: Decimal = Decimal(0)
    uncovered_bad_debt: Decimal = Decimal(0)
    rejection_reason: Optional[str] = None
    audit_event_id: str = ""

    @property
    def attributions(self) -> List[CreditorAttribution]:
        """逐项归因（pool_allocations 的语义别名，同一序列）。"""
        return self.pool_allocations
