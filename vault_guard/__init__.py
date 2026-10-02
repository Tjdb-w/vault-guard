"""Vault Guard：金库与清算风控引擎。

公开面：
    ClearanceEngine / process_settlement —— 清算入口
    SettlementRequest / Creditor / SettlementResult / CreditorAttribution
    AuditLedger / AuditEvent / InputSummary —— 内存审计台账
    以及六类领域错误。
"""

from .audit import AuditEvent, AuditLedger, InputSummary
from .engine import (
    REJECT_INSUFFICIENT_BALANCE,
    REJECT_RISK_LIMIT_EXCEEDED,
    RISK_FACTOR_MAX,
    RISK_FACTOR_MIN,
    ClearanceEngine,
    process_settlement,
)
from .errors import (
    DuplicateTransactionError,
    EmptyCreditorListError,
    InvalidCurrencyError,
    InvalidRiskFactorError,
    MixedCurrencyError,
    VaultGuardError,
)
from .models import (
    Creditor,
    CreditorAttribution,
    SettlementRequest,
    SettlementResult,
)

__all__ = [
    "ClearanceEngine",
    "process_settlement",
    "SettlementRequest",
    "Creditor",
    "SettlementResult",
    "CreditorAttribution",
    "AuditLedger",
    "AuditEvent",
    "InputSummary",
    "VaultGuardError",
    "DuplicateTransactionError",
    "InvalidCurrencyError",
    "EmptyCreditorListError",
    "InvalidRiskFactorError",
    "MixedCurrencyError",
    "REJECT_INSUFFICIENT_BALANCE",
    "REJECT_RISK_LIMIT_EXCEEDED",
    "RISK_FACTOR_MIN",
    "RISK_FACTOR_MAX",
]
