"""Vault Guard 异常类型。

每一类输入错误只对应本模块中的一个异常类型，引擎不会静默修正输入。

- 负数金额 / 负数余额 -> 内建 :class:`ValueError`
- 批次为空 -> :class:`EmptyBatchError`
- 重复流水号 -> :class:`DuplicateTransactionError`
- 缺币种 -> :class:`InvalidCurrencyError`
- 空债权清单 -> :class:`EmptyCreditorListError`
- 风险系数越界 -> :class:`InvalidRiskFactorError`
- 混合币种 -> :class:`MixedCurrencyError`
"""

__all__ = [
    "VaultGuardError",
    "EmptyBatchError",
    "DuplicateTransactionError",
    "InvalidCurrencyError",
    "EmptyCreditorListError",
    "InvalidRiskFactorError",
    "MixedCurrencyError",
]


class VaultGuardError(Exception):
    """所有 Vault Guard 自定义异常的基类。"""


class EmptyBatchError(VaultGuardError):
    """批量结算请求未包含任何单笔请求。"""


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
