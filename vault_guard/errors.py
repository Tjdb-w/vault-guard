"""Vault Guard 的公开错误类型。

每一类校验失败只使用对应的一个异常类型，失败请求不会被静默修正。
"""


class VaultGuardError(Exception):
    """Vault Guard 全部领域错误的基类。"""


class DuplicateTransactionError(VaultGuardError):
    """同一业务流水号在同一引擎实例中被重复提交。"""


class InvalidCurrencyError(VaultGuardError):
    """账户币种缺失或不合法。"""


class EmptyCreditorListError(VaultGuardError):
    """优先债权清单为空。"""


class InvalidRiskFactorError(VaultGuardError):
    """风险系数超出允许区间 [0, 1]。"""


class MixedCurrencyError(VaultGuardError):
    """同一笔结算请求中混入了不同币种。"""
