"""Vault Guard —— 金库与清算风控引擎。

公开 API：

- :class:`ClearingEngine` / :func:`process_settlement`：单笔清算入口；
- :meth:`ClearingEngine.process_batch` / :func:`process_settlement_batch`：
  同一币种的多笔批次结算入口；
- :meth:`ClearingEngine.process_multicurrency_batch` /
  :func:`process_settlement_multicurrency_batch`：多币种批次结算入口
  （按币种独立记账，不换汇、不使用汇率）；
- :meth:`ClearingEngine.process_risk_group_batch` /
  :func:`process_settlement_risk_group_batch`：带风险组累计限额的批次入口；
- :meth:`ClearingEngine.process_recovery`：存续坏账回收入口（回收依赖台账
  状态，仅提供引擎方法，无模块级一次性入口）；
- :class:`~vault_guard.models.SettlementRequest` 等数据模型；
- :mod:`vault_guard.errors` 中定义的各类异常（负数使用内建
  :class:`ValueError`）。
"""

from .engine import (
    ClearingEngine,
    process_settlement,
    process_settlement_batch,
    process_settlement_multicurrency_batch,
    process_settlement_risk_group_batch,
)
from .errors import (
    DuplicateTransactionError,
    EmptyBatchError,
    EmptyCreditorListError,
    InvalidCurrencyError,
    InvalidRiskFactorError,
    InvalidRiskGroupError,
    MixedCurrencyError,
    NoOutstandingBadDebtError,
    RecoveryAmountExceedsOutstandingError,
    VaultGuardError,
)
from .models import (
    AuditEvent,
    BatchSettlementResult,
    Creditor,
    CreditorAttribution,
    MulticurrencyBatchResult,
    OutstandingBadDebt,
    RecoveryAllocation,
    RecoveryResult,
    RiskGroupBatchResult,
    RiskGroupUsage,
    SettlementRequest,
    SettlementResult,
)

__all__ = [
    "ClearingEngine",
    "process_settlement",
    "process_settlement_batch",
    "process_settlement_multicurrency_batch",
    "process_settlement_risk_group_batch",
    "SettlementRequest",
    "SettlementResult",
    "BatchSettlementResult",
    "MulticurrencyBatchResult",
    "RiskGroupUsage",
    "RiskGroupBatchResult",
    "RecoveryAllocation",
    "RecoveryResult",
    "OutstandingBadDebt",
    "Creditor",
    "CreditorAttribution",
    "AuditEvent",
    "VaultGuardError",
    "DuplicateTransactionError",
    "InvalidCurrencyError",
    "EmptyCreditorListError",
    "InvalidRiskFactorError",
    "MixedCurrencyError",
    "EmptyBatchError",
    "InvalidRiskGroupError",
    "NoOutstandingBadDebtError",
    "RecoveryAmountExceedsOutstandingError",
]

__version__ = "0.1.0"
