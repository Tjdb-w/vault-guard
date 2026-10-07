"""Vault Guard 异常类型。

每一类输入错误只对应本模块中的一个异常类型，引擎不会静默修正输入。

- 负数金额 / 负数余额 -> 内建 :class:`ValueError`
- 重复流水号 -> :class:`DuplicateTransactionError`
- 缺币种 -> :class:`InvalidCurrencyError`
- 空债权清单 -> :class:`EmptyCreditorListError`
- 风险系数越界 -> :class:`InvalidRiskFactorError`
- 混合币种 -> :class:`MixedCurrencyError`
- 空批次 -> :class:`EmptyBatchError`
- 风险组标识 / 限额错误 -> :class:`InvalidRiskGroupError`
- 回收币种无存续坏账 -> :class:`NoOutstandingBadDebtError`
- 回收额超过存续坏账 -> :class:`RecoveryAmountExceedsOutstandingError`
- 核销额超过存续坏账 -> :class:`WriteoffAmountExceedsOutstandingError`
- 清算批次标识 / 币种 / 风险策略缺失或结构非法 -> :class:`InvalidSettlementBatchError`
- 结算标识批内或已登记重复 -> :class:`DuplicateSettlementIdError`
- 记录引用的限额未在风险策略中登记 -> :class:`RiskPolicyNotFoundError`
- 记录币种与批次币种不一致 -> :class:`CurrencyMismatchError`
- 结算金额非有限正数 -> :class:`InvalidSettlementAmountError`
- 结算优先级非整数 -> :class:`InvalidSettlementPriorityError`
- 预占标识缺失或为空 -> :class:`InvalidSettlementReservationError`
- 预占标识重复 -> :class:`DuplicateSettlementReservationError`
- 未知预占标识 -> :class:`SettlementReservationNotFoundError`
- 终态预占误操作（确认后取消 / 取消后确认 / 终态再确认或取消） ->
  :class:`SettlementReservationStateError`
"""

__all__ = [
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
    "InvalidSettlementBatchError",
    "DuplicateSettlementIdError",
    "RiskPolicyNotFoundError",
    "CurrencyMismatchError",
    "InvalidSettlementAmountError",
    "InvalidSettlementPriorityError",
    "InvalidSettlementReservationError",
    "DuplicateSettlementReservationError",
    "SettlementReservationNotFoundError",
    "SettlementReservationStateError",
]


class VaultGuardError(Exception):
    """所有 Vault Guard 自定义异常的基类。"""


class DuplicateTransactionError(VaultGuardError):
    """同一清算引擎实例内出现重复业务流水号。"""


class InvalidCurrencyError(VaultGuardError):
    """账户币种缺失或为空。"""


class EmptyCreditorListError(VaultGuardError):
    """优先债权清单为空。"""


class InvalidRiskFactorError(VaultGuardError):
    """风险系数越界（小于 0 或大于 1）。"""


class MixedCurrencyError(VaultGuardError):
    """单笔结算请求内出现混合币种。"""


class EmptyBatchError(VaultGuardError):
    """批次请求清单为空。"""


class InvalidRiskGroupError(VaultGuardError):
    """风险组标识为空、引用未登记组、限额非法或同一引擎内上限不一致。"""


class NoOutstandingBadDebtError(VaultGuardError):
    """回收币种下不存在存续坏账（含零额回收）。"""


class RecoveryAmountExceedsOutstandingError(VaultGuardError):
    """回收金额超过该币种存续坏账总额。"""


class WriteoffAmountExceedsOutstandingError(VaultGuardError):
    """核销金额超过该币种存续坏账总额。"""


class InvalidSettlementBatchError(VaultGuardError):
    """清算批次标识、币种或风险策略缺失，或请求结构非法。"""


class DuplicateSettlementIdError(VaultGuardError):
    """结算标识在批内重复或与已登记的结算标识重复。"""


class RiskPolicyNotFoundError(VaultGuardError):
    """记录引用的 treasury_id / debtor_id 未在风险策略中登记限额。"""


class CurrencyMismatchError(VaultGuardError):
    """记录币种与批次币种不一致。"""


class InvalidSettlementAmountError(VaultGuardError):
    """结算金额不是有限正数。"""


class InvalidSettlementPriorityError(VaultGuardError):
    """结算优先级不是整数。"""


class InvalidSettlementReservationError(VaultGuardError):
    """预占标识（reservation_id）缺失或为空。"""


class DuplicateSettlementReservationError(VaultGuardError):
    """同一清算引擎实例内出现重复的预占标识。"""


class SettlementReservationNotFoundError(VaultGuardError):
    """confirm / cancel / 查询引用了引擎中不存在的预占标识。"""


class SettlementReservationStateError(VaultGuardError):
    """对已确认或已取消（终态）的预占执行了不允许的操作。"""
