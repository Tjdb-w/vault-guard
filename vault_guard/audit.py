"""内存追加式审计台账。

不产生任何落盘行为：事件只保存在进程内列表中，按提交顺序排列。
只读访问器返回不可变快照，不会触发清算，也不会改变台账状态。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Dict, List, Optional, Tuple

from .models import CreditorAttribution


@dataclass(frozen=True)
class InputSummary:
    """审计记录中的输入摘要。"""

    currency: str
    pool_balance: Decimal
    settlement_amount: Decimal
    notional_exposure: Decimal
    base_limit: Decimal
    risk_factor: Decimal
    supplementary_capital: Decimal
    creditors: Tuple[Tuple[str, Decimal], ...]


@dataclass(frozen=True)
class AuditEvent:
    """一条不可变审计记录。

    每个被引擎受理的请求（放行或业务拒绝）恰好产生一条事件、
    一个事件标识；输入校验异常不产生事件。
    """

    event_id: str
    sequence: int
    transaction_id: str
    input_summary: InputSummary
    approved: bool
    risk_usage: Decimal
    verified_balance: Decimal
    allocations: Tuple[CreditorAttribution, ...]
    total_pool_allocated: Decimal
    total_capital_allocated: Decimal
    uncovered_bad_debt: Decimal
    rejection_reason: Optional[str]


class AuditLedger:
    """追加式内存台账。"""

    def __init__(self) -> None:
        self._events: List[AuditEvent] = []
        self._index_by_transaction: Dict[str, AuditEvent] = {}

    def append(self, event: AuditEvent) -> None:
        """追加一条事件。台账只追加、不修改、不删除。"""
        self._events.append(event)
        self._index_by_transaction.setdefault(event.transaction_id, event)

    def events(self) -> Tuple[AuditEvent, ...]:
        """按追加顺序返回全部事件的只读快照。"""
        return tuple(self._events)

    def latest_event(self) -> Optional[AuditEvent]:
        """返回最近一条事件；台账为空时返回 None。不触发清算。"""
        return self._events[-1] if self._events else None

    def get_event(self, event_id: str) -> Optional[AuditEvent]:
        """按事件标识查询；不存在返回 None。"""
        for event in self._events:
            if event.event_id == event_id:
                return event
        return None

    def get_by_transaction(self, transaction_id: str) -> Optional[AuditEvent]:
        """按业务流水号查询其唯一事件；不存在返回 None。"""
        return self._index_by_transaction.get(transaction_id)

    def __len__(self) -> int:
        return len(self._events)
