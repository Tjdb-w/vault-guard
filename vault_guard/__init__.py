"""Vault Guard —— 金库与清算风控引擎。

公开 API：

- :class:`ClearingEngine` / :func:`process_settlement`：清算入口；
- :class:`~vault_guard.models.SettlementRequest` 等数据模型；
- :mod:`vault_guard.errors` 中定义的各类异常（负数使用内建
  :class:`ValueError`）。
"""

from .engine import ClearingEngine, process_settlement
from .errors import (
    DuplicateTransactionError,
    EmptyCreditorListError,
    InvalidCurrencyError,
    InvalidRiskFactorError,
    MixedCurrencyError,
    VaultGuardError,
)
from .models import (
    AuditEvent,
    Creditor,
    CreditorAttribution,
    SettlementRequest,
    SettlementResult,
)

__all__ = [
    "ClearingEngine",
    "process_settlement",
    "SettlementRequest",
    "SettlementResult",
    "Creditor",
    "CreditorAttribution",
    "AuditEvent",
    "VaultGuardError",
    "DuplicateTransactionError",
    "InvalidCurrencyError",
    "EmptyCreditorListError",
    "InvalidRiskFactorError",
    "MixedCurrencyError",
]

__version__ = "0.1.0"
