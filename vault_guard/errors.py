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
