"""Vault Guard —— 金库与清算风控引擎。

公开 API：

- :class:`ClearingEngine` / :func:`process_settlement`：单笔清算入口；
- :meth:`ClearingEngine.process_batch` / :func:`process_settlement_batch`：
  同一币种的多笔批次结算入口；
- :meth:`ClearingEngine.preview_batch`：同币种批次的提交前只读预演入口
  （不生成审计事件、不占流水号、不改台账状态）；
- :meth:`ClearingEngine.preview_risk_group_batch` /
  :meth:`ClearingEngine.preview_multicurrency_batch`：分别为风险组批次与
  多币种批次的提交前只读预演入口（同样不生成事件、不占流水号、不改台账
  与风险组额度）；
- :meth:`ClearingEngine.process_risk_group_batch` /
  :func:`process_settlement_risk_group_batch`：带风险组累计限额的批次入口；
- :meth:`ClearingEngine.process_multicurrency_batch` /
  :func:`process_settlement_multicurrency_batch`：按币种独立记账的多币种
  批次入口（不换汇、不使用汇率）；
- :meth:`ClearingEngine.process_recovery`：存续坏账回收入口（回收依赖台账
  状态，仅提供引擎方法，无模块级一次性入口）；
- :meth:`ClearingEngine.process_writeoff`：存续坏账核销入口（核销依赖台账
  状态，仅提供引擎方法，无模块级一次性入口）；
- :meth:`ClearingEngine.process_targeted_recovery` /
  :meth:`ClearingEngine.process_targeted_writeoff`：定向坏账回收 / 核销
  入口（按币种、来源结算流水号与债权人定位唯一存续明细，仅提供引擎
  方法，无模块级一次性入口）；
- :meth:`ClearingEngine.creditor_bad_debt_report`：债权人维度只读坏账
  报告入口（按债权人合并多来源坏账，返回不可变
  :class:`~vault_guard.models.CreditorBadDebtSummary` 元组，不新增事件、
  不改任何状态）；
- :meth:`ClearingEngine.bad_debt_trail`：按来源结算流水号串联坏账处理
  记录的只读轨迹入口（精确匹配已登记结算流水号，返回不可变
  :class:`~vault_guard.models.BadDebtTrail`，不新增事件、不改任何状态）；
- :meth:`ClearingEngine.process_batch_retry` /
  :meth:`ClearingEngine.process_risk_group_batch_retry` /
  :meth:`ClearingEngine.process_multicurrency_batch_retry`：已进入清算处理
  的批次的可重试与断点恢复入口（稳定批次标识 + 本次执行标识；重试不重复
  扣减 / 归因 / 审计，内容冲突与标识无效分别返回唯一拒绝结果）；
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
    WriteoffAmountExceedsOutstandingError,
)
from .models import (
    AuditEvent,
    BadDebtOperation,
    BadDebtTrail,
    BatchIdentifierInvalid,
    BatchPreviewResult,
    BatchRetryConflict,
    BatchSettlementResult,
    BATCH_INVALID_REASON_MISSING_BATCH_ID,
    BATCH_INVALID_REASON_MISSING_EXECUTION_ID,
    BATCH_INVALID_REASON_UNCOMPUTABLE_DIGEST,
    BATCH_OUTCOME_INVALID_IDENTIFIER,
    BATCH_OUTCOME_RETRY_CONFLICT,
    Creditor,
    CreditorAttribution,
    CreditorBadDebtSummary,
    CurrencyAuditSummary,
    MulticurrencyBatchResult,
    MulticurrencyBatchPreviewResult,
    OutstandingBadDebt,
    RecoveryAllocation,
    RecoveryResult,
    RiskGroupBatchResult,
    RiskGroupBatchPreviewResult,
    RiskGroupUsage,
    SettlementPreview,
    SettlementRequest,
    SettlementResult,
    WriteoffAllocation,
    WriteoffResult,
)

__all__ = [
    "ClearingEngine",
    "process_settlement",
    "process_settlement_batch",
    "process_settlement_risk_group_batch",
    "process_settlement_multicurrency_batch",
    "SettlementRequest",
    "SettlementResult",
    "BatchSettlementResult",
    "SettlementPreview",
    "BatchPreviewResult",
    "RiskGroupUsage",
    "RiskGroupBatchResult",
    "RiskGroupBatchPreviewResult",
    "MulticurrencyBatchResult",
    "MulticurrencyBatchPreviewResult",
    "RecoveryAllocation",
    "RecoveryResult",
    "WriteoffAllocation",
    "WriteoffResult",
    "OutstandingBadDebt",
    "Creditor",
    "CreditorAttribution",
    "CreditorBadDebtSummary",
    "BadDebtOperation",
    "BadDebtTrail",
    "CurrencyAuditSummary",
    "AuditEvent",
    "BatchRetryConflict",
    "BatchIdentifierInvalid",
    "BATCH_OUTCOME_RETRY_CONFLICT",
    "BATCH_OUTCOME_INVALID_IDENTIFIER",
    "BATCH_INVALID_REASON_MISSING_BATCH_ID",
    "BATCH_INVALID_REASON_MISSING_EXECUTION_ID",
    "BATCH_INVALID_REASON_UNCOMPUTABLE_DIGEST",
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
    "WriteoffAmountExceedsOutstandingError",
]

__version__ = "0.1.0"
