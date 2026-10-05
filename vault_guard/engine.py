"""清算风控引擎：限额校验、清算瀑布、坏账归因与内存审计台账。

公开入口为 :meth:`ClearingEngine.process`（模块级便捷函数
:func:`process_settlement` 内部也持有自己的引擎实例）与
:meth:`ClearingEngine.process_batch`（模块级 :func:`process_settlement_batch`
同样使用一次性引擎，不跨批次去重）。

单笔处理严格按确定顺序执行：

1. 输入校验（负数 / 币种 / 系数 / 债权清单 / 流水号去重）。
2. 限额校验：风险占用 = 拟清算金额 + 名义敞口 × 风险系数。
3. 通过时按优先债权清单顺序执行资金池清算瀑布。
4. 补充资本按清单顺序补足仍未受偿的债权。
5. 归因并追加恰好一条审计事件。

批次处理在同一币种下按请求顺序以滚动余额逐笔执行上述规则：批次先做整体
校验（空批次 / 币种 / 债权币种 / 流水号去重），校验失败不生成事件、不占
流水号、不改余额并回退批内状态；校验通过后逐笔限额校验与清算，通过请求
各追加一条现有结构事件，批次本身不建事件。

多币种批次（:meth:`ClearingEngine.process_multicurrency_batch`，模块级
:func:`process_settlement_multicurrency_batch` 使用一次性引擎，不跨批次
保留状态）以期初余额映射替代单一币种：每个请求自带 ``currency``，各币种
独立记账、各自滚动余额，不换汇、不使用汇率；请求币种必须在期初映射中
登记，未登记或空白抛 :class:`InvalidCurrencyError`；单笔内债权混合币种
仍抛 :class:`MixedCurrencyError`。其余限额、瀑布、补充资本、坏账归因、
拒绝原因码与审计事件规则与同币种批次一致。

风险组批次（:meth:`ClearingEngine.process_risk_group_batch`，模块级
:func:`process_settlement_risk_group_batch` 使用一次性引擎，不跨批次保留
风险组状态）在以上规则之上增加风险组累计限额：请求可携带 ``risk_group_id``，
限额表把该标识映射到非负上限；同一风险组跨请求、跨批次按请求顺序累计已
放行请求的风险占用，加入本笔后超过组上限则该笔以
``GROUP_LIMIT_EXCEEDED`` 拒绝（不分配资金、不改资金池、不确认坏账、不增加
已用额度），生成现有结构审计事件并继续处理后续请求。不带风险组的请求仍
只按单笔基础限额处理。

存续坏账回收（:meth:`ClearingEngine.process_recovery`）只冲减已放行结算
留下的未覆盖债权：按审计事件顺序、再按债权清单顺序逐项冲减币种相符且仍
有坏账的债权，前项清零后处理后项，一笔回收可部分覆盖；历史
:class:`SettlementResult` 不回写。回收成功追加一条标识为
``EVT-{recovery_transaction_id}-recovery`` 的审计事件（序号继续递增，
``recovery_allocations`` 保存同额明细），失败不生成事件、不占流水号、
不改状态。

校验异常不产生任何分配，也不写入台账；重复流水号在任何状态变更之前抛出。
"""

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from decimal import Decimal, InvalidOperation
from math import isfinite
from types import MappingProxyType
from typing import Union

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
]

Number = Union[int, float, Decimal]

_ZERO = Decimal(0)
_ONE = Decimal(1)


@dataclass
class _BadDebtEntry:
    """存续坏账台账中的一笔内部记录（引擎私有，可变）。

    按审计事件追加顺序、再按债权清单顺序排列；``remaining`` 随回收冲减。
    """

    source_transaction_id: str
    creditor: str
    currency: str
    remaining: Decimal


def _as_decimal(value: object, field: str) -> Decimal:
    """将数值输入归一化为 Decimal。

    接受 ``int`` / ``Decimal`` / ``float``；``float`` 经 ``str(value)``
    精确转换（如 ``0.6`` -> ``Decimal("0.6")``），不引入二进制尾数。
    NaN 与无穷拒绝；非数值类型拒绝，不做静默字符串解析。
    """
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        raise ValueError(f"{field} 必须是数值类型，收到 {type(value).__name__}")
    if isinstance(value, float):
        if not isfinite(value):
            raise ValueError(f"{field} 不得为 NaN 或无穷")
        return Decimal(str(value))
    try:
        result = Decimal(value)
    except InvalidOperation:
        raise ValueError(f"{field} 不是有效数值") from None
    if not result.is_finite():
        raise ValueError(f"{field} 不得为 NaN 或无穷")
    return result


def _check_non_negative(value: Decimal, field: str) -> None:
    if value < _ZERO:
        raise ValueError(f"{field} 不得为负数，收到 {value}")


class ClearingEngine:
    """金库清算风控引擎。

    审计台账为实例内内存中的追加式序列（``self.audit_log``），只读查询
    （:meth:`get_event` / :meth:`events` / :meth:`result_of` /
    :meth:`recovery_of` / :meth:`outstanding_bad_debts`）不触发清算。
    """

    def __init__(self) -> None:
        self._seen_transactions: set[str] = set()
        self._events: list[AuditEvent] = []
        self._results: dict[str, SettlementResult] = {}
        self._sequence = 0
        # 风险组登记上限与累计已用额度，跨批次保留。
        self._risk_group_limits: dict[str, Decimal] = {}
        self._risk_group_used: dict[str, Decimal] = {}
        # 存续坏账台账（按事件顺序追加）与已登记回收结果。
        self._bad_debt_ledger: list[_BadDebtEntry] = []
        self._recoveries: dict[str, RecoveryResult] = {}

    # ------------------------------------------------------------------ #
    # 只读查询（不触发清算，不改变任何状态）
    # ------------------------------------------------------------------ #

    @property
    def audit_log(self) -> tuple[AuditEvent, ...]:
        """按追加顺序返回全部审计事件的只读快照。"""
        return tuple(self._events)

    def events(self) -> tuple[AuditEvent, ...]:
        """按追加顺序返回全部审计事件。"""
        return tuple(self._events)

    def get_event(self, transaction_id: str) -> AuditEvent | None:
        """按流水号读取已有审计事件；不存在返回 None，不触发清算。"""
        for event in self._events:
            if event.transaction_id == transaction_id:
                return event
        return None

    def result_of(self, transaction_id: str) -> SettlementResult | None:
        """按流水号读取已有处理结果；不存在返回 None。"""
        return self._results.get(transaction_id)

    def recovery_of(self, recovery_transaction_id: str) -> RecoveryResult | None:
        """按回收流水号读取已有回收结果；不存在返回 None，不触发清算。"""
        return self._recoveries.get(recovery_transaction_id)

    def outstanding_bad_debts(self, currency: str) -> tuple[OutstandingBadDebt, ...]:
        """按台账顺序返回该币种仍有余额的存续坏账明细；不触发清算。

        逐项含来源结算流水号、债权名、币种与剩余余额；该币种无存续坏账
        时返回空元组，其他币种不受影响。
        """
        if not isinstance(currency, str):
            return ()
        currency = currency.strip()
        return tuple(
            OutstandingBadDebt(
                source_transaction_id=entry.source_transaction_id,
                creditor=entry.creditor,
                currency=entry.currency,
                balance=entry.remaining,
            )
            for entry in self._bad_debt_ledger
            if entry.currency == currency and entry.remaining > _ZERO
        )

    def has_transaction(self, transaction_id: str) -> bool:
        return transaction_id in self._seen_transactions

    # ------------------------------------------------------------------ #
    # 公开入口
    # ------------------------------------------------------------------ #

    def process(
        self,
        transaction_id: str,
        currency: str,
        pool_balance: Number,
        settlement_amount: Number,
        notional_exposure: Number,
        base_limit: Number,
        risk_factor: Number,
        creditors: Sequence[Mapping[str, object] | Creditor | tuple[str, Number]],
        supplementary_capital: Number = _ZERO,
    ) -> SettlementResult:
        """处理单笔同币种结算请求，返回稳定可重复的公开结构。

        ``creditors`` 每项可为：

        - :class:`~vault_guard.models.Creditor`；
        - ``(name, amount)`` 或 ``(name, amount, currency)`` 元组；
        - 映射 ``{"name": ..., "amount": ..., "currency": ...（可选）}``。
        """
        request = self._validate(
            transaction_id=transaction_id,
            currency=currency,
            pool_balance=pool_balance,
            settlement_amount=settlement_amount,
            notional_exposure=notional_exposure,
            base_limit=base_limit,
            risk_factor=risk_factor,
            creditors=creditors,
            supplementary_capital=supplementary_capital,
        )
        return self._execute(request)

    def process_batch(
        self,
        currency: str,
        opening_pool_balance: Number,
        requests: Iterable[Mapping[str, object]],
    ) -> BatchSettlementResult:
        """处理同一币种的多笔结算批次，返回不可变 :class:`BatchSettlementResult`。

        - ``currency``：批次币种，适用于批次内全部请求。
        - ``opening_pool_balance``：批次期初资金池余额；各请求的
          ``pool_balance`` 取滚动余额（上一笔执行后的可用余额）。
        - ``requests``：请求映射序列，每项字段沿用 :meth:`process`
          （``transaction_id`` / ``settlement_amount`` / ``notional_exposure``
          / ``base_limit`` / ``risk_factor`` / ``creditors``，以及可选的
          ``supplementary_capital``，默认 0）；``currency`` 与
          ``pool_balance`` 不在单项内指定。

        批次先做整体校验：空批次、缺币种、债权币种不一致、流水号批内或与
        台账重复依次抛出 :class:`EmptyBatchError`、
        :class:`InvalidCurrencyError`、:class:`MixedCurrencyError`、
        :class:`DuplicateTransactionError`；空债权清单与风险系数越界抛出
        :class:`EmptyCreditorListError` 与 :class:`InvalidRiskFactorError`；
        错误数值抛内建 :class:`ValueError`。校验失败不生成事件、不占流水号、
        不改余额，并回退批内已产生的全部状态。

        校验通过后按请求顺序以滚动余额逐笔执行限额校验与清算；通过请求各
        追加一条现有结构事件（序号递增），批次本身不建事件。
        """
        currency, opening, normalized = self._validate_batch(
            currency, opening_pool_balance, requests
        )

        # 执行阶段基于已校验数据不会失败；仍防御性回滚，保证异常路径下
        # 事件、序号、流水号、结果索引与坏账台账全部复原。
        events_mark = len(self._events)
        sequence_mark = self._sequence
        ledger_mark = len(self._bad_debt_ledger)
        added_ids: list[str] = []
        results: list[SettlementResult] = []
        balance = opening
        try:
            for request in normalized:
                request = replace(request, pool_balance=balance)
                self._seen_transactions.add(request.transaction_id)
                added_ids.append(request.transaction_id)
                result = self._execute(request)
                results.append(result)
                balance = result.validated_available_balance
        except Exception:
            del self._events[events_mark:]
            self._sequence = sequence_mark
            del self._bad_debt_ledger[ledger_mark:]
            for tid in added_ids:
                self._seen_transactions.discard(tid)
                self._results.pop(tid, None)
            raise

        return BatchSettlementResult(
            results=tuple(results),
            event_ids=tuple(result.event_id for result in results),
            validated_available_balance=balance,
        )

    def process_multicurrency_batch(
        self,
        opening_pool_balances: Mapping[str, Number],
        requests: Iterable[Mapping[str, object]],
    ) -> MulticurrencyBatchResult:
        """处理多币种批次结算，返回不可变 :class:`MulticurrencyBatchResult`。

        - ``opening_pool_balances``：币种到非负期初资金池余额的映射；批次内
          出现的每个请求币种都必须在此登记。
        - ``requests``：请求映射序列，每项字段沿用 :meth:`process_batch`
          单项字段（``transaction_id`` / ``settlement_amount`` /
          ``notional_exposure`` / ``base_limit`` / ``risk_factor`` /
          ``creditors``，以及可选的 ``supplementary_capital``，默认 0），
          并增加必填的 ``currency``；``pool_balance`` 不在单项内指定，取
          该币种的滚动余额。

        各币种独立记账：每笔只动用自身币种的余额，放行扣减本币种余额，拒绝
        不动余额；不做换汇，不使用汇率。未被任何请求引用的币种在结果中保留
        期初原值。

        批次先做整体校验，失败不生成事件、不占流水号、不改余额与坏账台账。
        错误依次为：空批次 :class:`EmptyBatchError`；期初映射非法、空白余额
        键或任何数值非法 :class:`ValueError`；请求币种缺失、空白或未在期初
        映射中登记 :class:`InvalidCurrencyError`；单笔债权混合币种
        :class:`MixedCurrencyError`；重复流水号
        :class:`DuplicateTransactionError`；空债权清单
        :class:`EmptyCreditorListError`；风险系数越界
        :class:`InvalidRiskFactorError`。

        校验通过后按请求顺序逐笔执行既有单笔规则（限额、瀑布、补充资本、
        坏账归因与两种拒绝原因码）；放行与拒绝各追加一条现有结构事件
        （序号递增），拒绝后继续处理后续请求，批次本身不建事件。未覆盖坏账
        按各自币种进入存续坏账台账，可由 :meth:`process_recovery` 冲减。
        """
        opening, normalized = self._validate_multicurrency_batch(
            opening_pool_balances, requests
        )

        # 执行阶段基于已校验数据不会失败；仍防御性回滚，保证异常路径下
        # 事件、序号、流水号、结果索引与坏账台账全部复原。
        events_mark = len(self._events)
        sequence_mark = self._sequence
        ledger_mark = len(self._bad_debt_ledger)
        added_ids: list[str] = []
        results: list[SettlementResult] = []
        balances = dict(opening)
        try:
            for request in normalized:
                request = replace(
                    request, pool_balance=balances[request.currency]
                )
                self._seen_transactions.add(request.transaction_id)
                added_ids.append(request.transaction_id)
                result = self._execute(request)
                results.append(result)
                balances[request.currency] = result.validated_available_balance
        except Exception:
            del self._events[events_mark:]
            self._sequence = sequence_mark
            del self._bad_debt_ledger[ledger_mark:]
            for tid in added_ids:
                self._seen_transactions.discard(tid)
                self._results.pop(tid, None)
            raise

        return MulticurrencyBatchResult(
            results=tuple(results),
            event_ids=tuple(result.event_id for result in results),
            validated_available_balances=MappingProxyType(balances),
        )

    def process_risk_group_batch(
        self,
        currency: str,
        opening_pool_balance: Number,
        risk_group_limits: Mapping[str, Number],
        requests: Iterable[Mapping[str, object]],
    ) -> RiskGroupBatchResult:
        """处理带风险组累计限额的同币种多笔结算批次。

        - ``currency`` / ``opening_pool_balance`` / ``requests``：语义同
          :meth:`process_batch`；``requests`` 每项在既有字段外可携带
          ``risk_group_id``（缺省或 ``None`` 表示不参与风险组控制，仍只按
          单笔基础限额处理）。
        - ``risk_group_limits``：风险组标识到非负上限的映射。标识为空、
          限额非法（非数值 / NaN / 无穷 / 负数）、请求引用未登记组，或同一
          引擎对同一组给出与已登记不同的上限，均抛出
          :class:`InvalidRiskGroupError`；其余输入异常沿用既有类型。

        风险占用仍按 ``拟清算金额 + 名义敞口 × 风险系数`` 计算，按请求顺序
        在组内累计；加入本笔后超过组上限的笔以 ``GROUP_LIMIT_EXCEEDED``
        拒绝：不分配资金、不改资金池、不确认坏账、不增加已用额度，但生成
        现有结构审计事件并继续处理后续请求。组额度只在成功放行的请求上
        增加；余额不足、单笔基础限额或组限额拒绝均不占用。同一引擎后续
        批次继续累计，不同风险组互不影响。

        校验失败不生成事件、不占流水号、不改余额与风险组额度，并回退批内
        已产生的全部状态。
        """
        currency, opening, normalized, new_limits = (
            self._validate_risk_group_batch(
                currency, opening_pool_balance, risk_group_limits, requests
            )
        )

        # 执行阶段基于已校验数据不会失败；仍防御性回滚，保证异常路径下
        # 事件、序号、流水号、结果索引、风险组状态与坏账台账全部复原。
        events_mark = len(self._events)
        sequence_mark = self._sequence
        limits_mark = dict(self._risk_group_limits)
        used_mark = dict(self._risk_group_used)
        ledger_mark = len(self._bad_debt_ledger)
        added_ids: list[str] = []
        results: list[SettlementResult] = []
        balance = opening
        try:
            for group_id, limit in new_limits.items():
                self._risk_group_limits[group_id] = limit
                self._risk_group_used.setdefault(group_id, _ZERO)
            for request in normalized:
                request = replace(request, pool_balance=balance)
                self._seen_transactions.add(request.transaction_id)
                added_ids.append(request.transaction_id)
                result = self._execute(request)
                results.append(result)
                balance = result.validated_available_balance
        except Exception:
            del self._events[events_mark:]
            self._sequence = sequence_mark
            self._risk_group_limits = limits_mark
            self._risk_group_used = used_mark
            del self._bad_debt_ledger[ledger_mark:]
            for tid in added_ids:
                self._seen_transactions.discard(tid)
                self._results.pop(tid, None)
            raise

        return RiskGroupBatchResult(
            results=tuple(results),
            event_ids=tuple(result.event_id for result in results),
            validated_available_balance=balance,
            risk_groups=MappingProxyType(
                {
                    group_id: RiskGroupUsage(
                        used=self._risk_group_used[group_id],
                        limit=limit,
                        remaining=limit - self._risk_group_used[group_id],
                    )
                    for group_id, limit in self._risk_group_limits.items()
                }
            ),
        )

    def process_recovery(
        self,
        recovery_transaction_id: str,
        currency: str,
        recovery_amount: Number,
    ) -> RecoveryResult:
        """提交一笔存续坏账回收，返回不可变 :class:`RecoveryResult`。

        只冲减已放行结算留下的未覆盖债权：按审计事件顺序、再按债权清单
        顺序逐项冲减币种相符且仍有坏账的债权，前项清零后处理后项，一笔
        回收可部分覆盖单项坏账。历史 :class:`SettlementResult` 不回写。

        - 重复回收流水号抛 :class:`DuplicateTransactionError`；
        - 缺币种抛 :class:`InvalidCurrencyError`；
        - 负数、NaN、无穷或非数值回收额抛内建 :class:`ValueError`；
        - 该币种无存续坏账（含此时零额回收）抛
          :class:`NoOutstandingBadDebtError`；
        - 回收额超过该币种存续坏账总额抛
          :class:`RecoveryAmountExceedsOutstandingError`。

        失败不生成事件、不占流水号、不改状态。有坏账时零额回收生成一条
        空明细事件，标识为 ``EVT-{recovery_transaction_id}-recovery``，
        审计序号继续递增。
        """
        if not isinstance(recovery_transaction_id, str) or not recovery_transaction_id.strip():
            raise ValueError("recovery_transaction_id 必须是非空字符串")

        # 重复流水号：在任何归一化/状态变更之前判定。
        if recovery_transaction_id in self._seen_transactions:
            raise DuplicateTransactionError(
                f"重复的业务流水号: {recovery_transaction_id}"
            )

        if not isinstance(currency, str) or not currency.strip():
            raise InvalidCurrencyError("账户币种缺失或为空")
        currency = currency.strip()

        amount = _as_decimal(recovery_amount, "recovery_amount")
        _check_non_negative(amount, "recovery_amount")

        outstanding_total = sum(
            (
                entry.remaining
                for entry in self._bad_debt_ledger
                if entry.currency == currency
            ),
            _ZERO,
        )
        if outstanding_total <= _ZERO:
            raise NoOutstandingBadDebtError(
                f"币种 {currency} 无存续坏账可回收"
            )
        if amount > outstanding_total:
            raise RecoveryAmountExceedsOutstandingError(
                f"回收额 {amount} 超过币种 {currency} 存续坏账 {outstanding_total}"
            )

        # 校验全部通过后才登记流水号并执行冲减。
        self._seen_transactions.add(recovery_transaction_id)

        remaining = amount
        allocations: list[RecoveryAllocation] = []
        for entry in self._bad_debt_ledger:
            if remaining <= _ZERO:
                break
            if entry.currency != currency or entry.remaining <= _ZERO:
                continue
            write_down = min(entry.remaining, remaining)
            entry.remaining -= write_down
            remaining -= write_down
            allocations.append(
                RecoveryAllocation(
                    source_transaction_id=entry.source_transaction_id,
                    creditor=entry.creditor,
                    recovered_amount=write_down,
                    remaining_bad_debt=entry.remaining,
                )
            )

        outstanding_after = outstanding_total - amount
        event_id = f"EVT-{recovery_transaction_id}-recovery"
        result = RecoveryResult(
            recovery_transaction_id=recovery_transaction_id,
            currency=currency,
            recovery_amount=amount,
            allocations=tuple(allocations),
            total_recovered=amount - remaining,
            outstanding_bad_debt=outstanding_after,
            event_id=event_id,
        )
        self._append_recovery_audit(result)
        self._recoveries[recovery_transaction_id] = result
        return result

    def _execute(self, request: SettlementRequest) -> SettlementResult:
        """对已校验请求执行限额校验、清算与审计追加。"""
        # 限额校验。拒绝时余额不变、无任何分配。
        risk_occupancy = (
            request.settlement_amount
            + request.notional_exposure * request.risk_factor
        )
        group_id = request.risk_group_id
        if request.settlement_amount > request.pool_balance:
            result = self._build_rejected(
                request, risk_occupancy, "SETTLEMENT_EXCEEDS_AVAILABLE_BALANCE"
            )
        elif risk_occupancy > request.base_limit:
            result = self._build_rejected(
                request, risk_occupancy, "RISK_OCCUPANCY_EXCEEDS_BASE_LIMIT"
            )
        elif (
            group_id is not None
            and self._risk_group_used[group_id] + risk_occupancy
            > self._risk_group_limits[group_id]
        ):
            result = self._build_rejected(
                request, risk_occupancy, "GROUP_LIMIT_EXCEEDED"
            )
        else:
            result = self._settle(request, risk_occupancy)
            if group_id is not None:
                # 组额度只在成功放行的请求上按其风险占用增加。
                self._risk_group_used[group_id] += risk_occupancy

        if group_id is not None:
            # 拒绝结果未占用额度：等于执行前值。
            result = replace(
                result, group_used_after=self._risk_group_used[group_id]
            )

        self._append_audit(request, result)
        return result

    # ------------------------------------------------------------------ #
    # 输入校验（不静默修正；异常路径不产生任何状态变更）
    # ------------------------------------------------------------------ #

    def _validate(
        self,
        *,
        transaction_id: str,
        currency: str,
        pool_balance: Number,
        settlement_amount: Number,
        notional_exposure: Number,
        base_limit: Number,
        risk_factor: Number,
        creditors: Iterable[Mapping[str, object] | Creditor | tuple[str, Number]],
        supplementary_capital: Number,
    ) -> SettlementRequest:
        if not isinstance(transaction_id, str) or not transaction_id.strip():
            raise ValueError("transaction_id 必须是非空字符串")

        # 重复流水号：在任何归一化/状态变更之前判定。
        if transaction_id in self._seen_transactions:
            raise DuplicateTransactionError(
                f"重复的业务流水号: {transaction_id}"
            )

        if not isinstance(currency, str) or not currency.strip():
            raise InvalidCurrencyError("账户币种缺失或为空")
        currency = currency.strip()

        pool = _as_decimal(pool_balance, "pool_balance")
        amount = _as_decimal(settlement_amount, "settlement_amount")
        exposure = _as_decimal(notional_exposure, "notional_exposure")
        limit = _as_decimal(base_limit, "base_limit")
        factor = _as_decimal(risk_factor, "risk_factor")
        capital = _as_decimal(supplementary_capital, "supplementary_capital")

        _check_non_negative(pool, "pool_balance")
        _check_non_negative(amount, "settlement_amount")
        _check_non_negative(exposure, "notional_exposure")
        _check_non_negative(limit, "base_limit")
        _check_non_negative(capital, "supplementary_capital")

        if factor < _ZERO or factor > _ONE:
            raise InvalidRiskFactorError(
                f"风险系数必须落在 [0, 1]，收到 {factor}"
            )

        creditor_list = self._normalize_creditors(creditors, currency)

        request = SettlementRequest(
            transaction_id=transaction_id,
            currency=currency,
            pool_balance=pool,
            settlement_amount=amount,
            notional_exposure=exposure,
            base_limit=limit,
            risk_factor=factor,
            creditors=tuple(creditor_list),
            supplementary_capital=capital,
        )

        # 校验全部通过后才登记流水号：保证异常路径不产生半成品状态。
        self._seen_transactions.add(transaction_id)
        return request

    @staticmethod
    def _normalize_creditors(
        raw: Iterable[Mapping[str, object] | Creditor | tuple[str, Number]],
        account_currency: str,
    ) -> list[Creditor]:
        if raw is None:
            raise EmptyCreditorListError("优先债权清单为空")
        try:
            iterator = iter(raw)
        except TypeError:
            raise EmptyCreditorListError("优先债权清单为空") from None

        creditors: list[Creditor] = []
        for index, item in enumerate(iterator):
            if isinstance(item, Creditor):
                name, claim, ccy = item.name, item.amount, item.currency
            elif isinstance(item, Mapping):
                name = item.get("name")
                claim = item.get("amount")
                ccy = item.get("currency")
            elif isinstance(item, tuple):
                if len(item) == 2:
                    name, claim = item
                    ccy = None
                elif len(item) == 3:
                    name, claim, ccy = item
                else:
                    raise ValueError(
                        f"第 {index} 项债权元组必须是 (name, amount) 或 "
                        f"(name, amount, currency)"
                    )
            else:
                raise ValueError(
                    f"第 {index} 项债权格式不被支持: {type(item).__name__}"
                )

            if not isinstance(name, str) or not name.strip():
                raise ValueError(f"第 {index} 项债权缺少有效名称")
            claim_dec = _as_decimal(claim, f"creditors[{index}].amount")
            _check_non_negative(claim_dec, f"creditors[{index}].amount")

            if ccy is not None:
                if not isinstance(ccy, str) or not ccy.strip():
                    raise InvalidCurrencyError(
                        f"creditors[{index}] 币种为空"
                    )
                ccy = ccy.strip()
                if ccy != account_currency:
                    raise MixedCurrencyError(
                        f"账户币种 {account_currency} 与债权 {name} 币种 {ccy} 不一致"
                    )

            creditors.append(
                Creditor(name=name.strip(), amount=claim_dec, currency=ccy)
            )

        if not creditors:
            raise EmptyCreditorListError("优先债权清单为空")
        return creditors

    # ------------------------------------------------------------------ #
    # 批次校验（整体校验通过后才开始任何状态变更）
    # ------------------------------------------------------------------ #

    def _validate_batch(
        self,
        currency: str,
        opening_pool_balance: Number,
        requests: Iterable[Mapping[str, object]],
    ) -> tuple[str, Decimal, list[SettlementRequest]]:
        if requests is None:
            raise EmptyBatchError("批次请求清单为空")
        try:
            items = list(requests)
        except TypeError:
            raise EmptyBatchError("批次请求清单为空") from None
        if not items:
            raise EmptyBatchError("批次请求清单为空")

        if not isinstance(currency, str) or not currency.strip():
            raise InvalidCurrencyError("账户币种缺失或为空")
        currency = currency.strip()

        opening = _as_decimal(opening_pool_balance, "opening_pool_balance")
        _check_non_negative(opening, "opening_pool_balance")

        # 逐请求归一化（不含流水号去重）：数值 / 系数 / 债权币种错误在此抛出。
        normalized = [
            self._normalize_batch_item(item, index, currency, opening)
            for index, item in enumerate(items)
        ]

        # 重复流水号最后统一判定：批内互相重复或与台账重复均拒绝。
        seen_in_batch: set[str] = set()
        for request in normalized:
            tid = request.transaction_id
            if tid in seen_in_batch or tid in self._seen_transactions:
                raise DuplicateTransactionError(
                    f"重复的业务流水号: {tid}"
                )
            seen_in_batch.add(tid)

        return currency, opening, normalized

    # ------------------------------------------------------------------ #
    # 多币种批次校验（整体校验通过后才开始任何状态变更）
    # ------------------------------------------------------------------ #

    def _validate_multicurrency_batch(
        self,
        opening_pool_balances: Mapping[str, Number],
        requests: Iterable[Mapping[str, object]],
    ) -> tuple[dict[str, Decimal], list[SettlementRequest]]:
        """按既定顺序分阶段校验多币种批次，返回期初余额表与归一化请求。

        阶段顺序即公开文档的错误顺序：空批次 -> 期初映射 / 数值 ->
        请求币种 -> 债权币种 -> 重复流水号 -> 空债权清单 -> 风险系数区间。
        """
        if requests is None:
            raise EmptyBatchError("批次请求清单为空")
        try:
            items = list(requests)
        except TypeError:
            raise EmptyBatchError("批次请求清单为空") from None
        if not items:
            raise EmptyBatchError("批次请求清单为空")

        opening = self._normalize_opening_balances(opening_pool_balances)

        # 逐请求解析结构与数值（不含币种归属、去重、空清单与系数区间）。
        parsed = [
            self._parse_multicurrency_item(item, index)
            for index, item in enumerate(items)
        ]

        # 请求币种：缺失、空白或未在期初映射中登记均拒绝。
        for record in parsed:
            currency = record["currency"]
            if not isinstance(currency, str) or not currency.strip():
                raise InvalidCurrencyError("账户币种缺失或为空")
            currency = currency.strip()
            if currency not in opening:
                raise InvalidCurrencyError(
                    f"请求币种 {currency} 未在期初余额映射中登记"
                )
            record["currency"] = currency

        # 债权币种：显式给出的债权币种必须与本笔请求币种一致。
        for record in parsed:
            for cindex, creditor in enumerate(record["creditors"] or ()):
                ccy = creditor.currency
                if ccy is None:
                    continue
                if not isinstance(ccy, str) or not ccy.strip():
                    raise InvalidCurrencyError(
                        f"creditors[{cindex}] 币种为空"
                    )
                ccy = ccy.strip()
                if ccy != record["currency"]:
                    raise MixedCurrencyError(
                        f"账户币种 {record['currency']} 与债权 "
                        f"{creditor.name} 币种 {ccy} 不一致"
                    )

        # 重复流水号：批内互相重复或与台账重复均拒绝。
        seen_in_batch: set[str] = set()
        for record in parsed:
            tid = record["transaction_id"]
            if tid in seen_in_batch or tid in self._seen_transactions:
                raise DuplicateTransactionError(
                    f"重复的业务流水号: {tid}"
                )
            seen_in_batch.add(tid)

        # 空债权清单。
        for record in parsed:
            if not record["creditors"]:
                raise EmptyCreditorListError("优先债权清单为空")

        # 风险系数区间。
        for record in parsed:
            factor = record["risk_factor"]
            if factor < _ZERO or factor > _ONE:
                raise InvalidRiskFactorError(
                    f"风险系数必须落在 [0, 1]，收到 {factor}"
                )

        normalized = [
            SettlementRequest(
                transaction_id=record["transaction_id"],
                currency=record["currency"],
                # 暂存本币种期初余额，执行时替换为该币种滚动余额。
                pool_balance=opening[record["currency"]],
                settlement_amount=record["settlement_amount"],
                notional_exposure=record["notional_exposure"],
                base_limit=record["base_limit"],
                risk_factor=record["risk_factor"],
                creditors=tuple(record["creditors"]),
                supplementary_capital=record["supplementary_capital"],
            )
            for record in parsed
        ]
        return opening, normalized

    @staticmethod
    def _normalize_opening_balances(
        opening_pool_balances: Mapping[str, Number],
    ) -> dict[str, Decimal]:
        """归一化币种到期初余额的映射；映射、键或数值非法抛 :class:`ValueError`。"""
        if not isinstance(opening_pool_balances, Mapping):
            raise ValueError(
                "opening_pool_balances 必须是币种到期初余额的映射，收到 "
                f"{type(opening_pool_balances).__name__}"
            )
        opening: dict[str, Decimal] = {}
        for raw_key, raw_value in opening_pool_balances.items():
            if not isinstance(raw_key, str) or not raw_key.strip():
                raise ValueError("期初余额映射的币种键必须是非空字符串")
            key = raw_key.strip()
            value = _as_decimal(raw_value, f"opening_pool_balances[{key}]")
            _check_non_negative(value, f"opening_pool_balances[{key}]")
            opening[key] = value
        return opening

    def _parse_multicurrency_item(
        self, item: object, index: int
    ) -> dict[str, object]:
        """解析多币种批次中的单个请求映射：结构、流水号与全部数值在此
        校验（抛 :class:`ValueError`）；币种归属、债权币种一致性、空债权
        清单与风险系数区间由后续阶段统一判定。债权清单缺失或不可迭代时
        暂存 ``None``，留待空清单阶段抛出。"""
        if not isinstance(item, Mapping):
            raise ValueError(
                f"requests[{index}] 必须是字段映射，收到 "
                f"{type(item).__name__}"
            )

        transaction_id = item.get("transaction_id")
        if not isinstance(transaction_id, str) or not transaction_id.strip():
            raise ValueError("transaction_id 必须是非空字符串")

        amount = _as_decimal(
            item.get("settlement_amount"), f"requests[{index}].settlement_amount"
        )
        exposure = _as_decimal(
            item.get("notional_exposure"),
            f"requests[{index}].notional_exposure",
        )
        limit = _as_decimal(
            item.get("base_limit"), f"requests[{index}].base_limit"
        )
        factor = _as_decimal(
            item.get("risk_factor"), f"requests[{index}].risk_factor"
        )
        capital = _as_decimal(
            item.get("supplementary_capital", _ZERO),
            f"requests[{index}].supplementary_capital",
        )

        _check_non_negative(amount, f"requests[{index}].settlement_amount")
        _check_non_negative(exposure, f"requests[{index}].notional_exposure")
        _check_non_negative(limit, f"requests[{index}].base_limit")
        _check_non_negative(capital, f"requests[{index}].supplementary_capital")

        return {
            "transaction_id": transaction_id,
            "currency": item.get("currency"),
            "settlement_amount": amount,
            "notional_exposure": exposure,
            "base_limit": limit,
            "risk_factor": factor,
            "supplementary_capital": capital,
            "creditors": self._parse_multicurrency_creditors(
                item.get("creditors"), index
            ),
        }

    @staticmethod
    def _parse_multicurrency_creditors(
        raw: Iterable[Mapping[str, object] | Creditor | tuple[str, Number]],
        index: int,
    ) -> list[Creditor] | None:
        """解析债权清单的各项结构、名称与金额；币种一致性与空清单不在此
        判定（币种原样保留，缺失或不可迭代返回 ``None``）。"""
        if raw is None:
            return None
        try:
            iterator = iter(raw)
        except TypeError:
            return None

        creditors: list[Creditor] = []
        for cindex, entry in enumerate(iterator):
            if isinstance(entry, Creditor):
                name, claim, ccy = entry.name, entry.amount, entry.currency
            elif isinstance(entry, Mapping):
                name = entry.get("name")
                claim = entry.get("amount")
                ccy = entry.get("currency")
            elif isinstance(entry, tuple):
                if len(entry) == 2:
                    name, claim = entry
                    ccy = None
                elif len(entry) == 3:
                    name, claim, ccy = entry
                else:
                    raise ValueError(
                        f"第 {cindex} 项债权元组必须是 (name, amount) 或 "
                        f"(name, amount, currency)"
                    )
            else:
                raise ValueError(
                    f"第 {cindex} 项债权格式不被支持: {type(entry).__name__}"
                )

            if not isinstance(name, str) or not name.strip():
                raise ValueError(f"第 {cindex} 项债权缺少有效名称")
            claim_dec = _as_decimal(claim, f"creditors[{cindex}].amount")
            _check_non_negative(claim_dec, f"creditors[{cindex}].amount")

            creditors.append(
                Creditor(
                    name=name.strip(),
                    amount=claim_dec,
                    currency=ccy.strip() if isinstance(ccy, str) else ccy,
                )
            )
        return creditors

    # ------------------------------------------------------------------ #
    # 风险组批次校验（整体校验通过后才开始任何状态变更）
    # ------------------------------------------------------------------ #

    def _validate_risk_group_batch(
        self,
        currency: str,
        opening_pool_balance: Number,
        risk_group_limits: Mapping[str, Number],
        requests: Iterable[Mapping[str, object]],
    ) -> tuple[str, Decimal, list[SettlementRequest], dict[str, Decimal]]:
        if requests is None:
            raise EmptyBatchError("批次请求清单为空")
        try:
            items = list(requests)
        except TypeError:
            raise EmptyBatchError("批次请求清单为空") from None
        if not items:
            raise EmptyBatchError("批次请求清单为空")

        if not isinstance(currency, str) or not currency.strip():
            raise InvalidCurrencyError("账户币种缺失或为空")
        currency = currency.strip()

        opening = _as_decimal(opening_pool_balance, "opening_pool_balance")
        _check_non_negative(opening, "opening_pool_balance")

        new_limits = self._normalize_risk_group_limits(risk_group_limits)
        registered = set(self._risk_group_limits) | set(new_limits)

        # 逐请求归一化（不含流水号去重）：数值 / 系数 / 债权币种 / 风险组
        # 标识错误在此抛出。
        normalized = [
            self._normalize_batch_item(
                item, index, currency, opening, registered_groups=registered
            )
            for index, item in enumerate(items)
        ]

        # 重复流水号最后统一判定：批内互相重复或与台账重复均拒绝。
        seen_in_batch: set[str] = set()
        for request in normalized:
            tid = request.transaction_id
            if tid in seen_in_batch or tid in self._seen_transactions:
                raise DuplicateTransactionError(
                    f"重复的业务流水号: {tid}"
                )
            seen_in_batch.add(tid)

        return currency, opening, normalized, new_limits

    def _normalize_risk_group_limits(
        self, risk_group_limits: Mapping[str, Number]
    ) -> dict[str, Decimal]:
        """归一化风险组限额表；所有风险组字段错误只抛
        :class:`InvalidRiskGroupError`。同一引擎内同一组上限必须一致。"""
        if not isinstance(risk_group_limits, Mapping):
            raise InvalidRiskGroupError("风险组限额表必须是标识到上限的映射")

        new_limits: dict[str, Decimal] = {}
        for raw_id, raw_limit in risk_group_limits.items():
            if not isinstance(raw_id, str) or not raw_id.strip():
                raise InvalidRiskGroupError("风险组标识必须是非空字符串")
            group_id = raw_id.strip()
            try:
                limit = _as_decimal(raw_limit, f"risk_group_limits[{group_id}]")
            except ValueError as exc:
                raise InvalidRiskGroupError(str(exc)) from None
            if limit < _ZERO:
                raise InvalidRiskGroupError(
                    f"风险组 {group_id} 限额不得为负数，收到 {limit}"
                )
            existing = self._risk_group_limits.get(group_id)
            if existing is not None and existing != limit:
                raise InvalidRiskGroupError(
                    f"风险组 {group_id} 已登记上限 {existing}，"
                    f"与本次给出的 {limit} 不一致"
                )
            new_limits[group_id] = limit
        return new_limits

    def _normalize_batch_item(
        self,
        item: object,
        index: int,
        currency: str,
        opening_pool_balance: Decimal,
        registered_groups: set[str] | None = None,
    ) -> SettlementRequest:
        """归一化批次中的单个请求映射；``pool_balance`` 暂存期初余额，
        执行时替换为滚动余额。``registered_groups`` 为 ``None`` 时忽略
        风险组字段（普通批次），否则校验 ``risk_group_id``。"""
        if not isinstance(item, Mapping):
            raise ValueError(
                f"requests[{index}] 必须是字段映射，收到 "
                f"{type(item).__name__}"
            )

        transaction_id = item.get("transaction_id")
        if not isinstance(transaction_id, str) or not transaction_id.strip():
            raise ValueError("transaction_id 必须是非空字符串")

        risk_group_id = None
        if registered_groups is not None:
            raw_group_id = item.get("risk_group_id")
            if raw_group_id is not None:
                if not isinstance(raw_group_id, str) or not raw_group_id.strip():
                    raise InvalidRiskGroupError(
                        f"requests[{index}] 的风险组标识必须是非空字符串"
                    )
                risk_group_id = raw_group_id.strip()
                if risk_group_id not in registered_groups:
                    raise InvalidRiskGroupError(
                        f"requests[{index}] 引用了未登记的风险组: "
                        f"{risk_group_id}"
                    )

        amount = _as_decimal(
            item.get("settlement_amount"), f"requests[{index}].settlement_amount"
        )
        exposure = _as_decimal(
            item.get("notional_exposure"),
            f"requests[{index}].notional_exposure",
        )
        limit = _as_decimal(
            item.get("base_limit"), f"requests[{index}].base_limit"
        )
        factor = _as_decimal(
            item.get("risk_factor"), f"requests[{index}].risk_factor"
        )
        capital = _as_decimal(
            item.get("supplementary_capital", _ZERO),
            f"requests[{index}].supplementary_capital",
        )

        _check_non_negative(amount, f"requests[{index}].settlement_amount")
        _check_non_negative(exposure, f"requests[{index}].notional_exposure")
        _check_non_negative(limit, f"requests[{index}].base_limit")
        _check_non_negative(capital, f"requests[{index}].supplementary_capital")

        if factor < _ZERO or factor > _ONE:
            raise InvalidRiskFactorError(
                f"风险系数必须落在 [0, 1]，收到 {factor}"
            )

        creditor_list = self._normalize_creditors(
            item.get("creditors"), currency
        )

        return SettlementRequest(
            transaction_id=transaction_id,
            currency=currency,
            pool_balance=opening_pool_balance,
            settlement_amount=amount,
            notional_exposure=exposure,
            base_limit=limit,
            risk_factor=factor,
            creditors=tuple(creditor_list),
            supplementary_capital=capital,
            risk_group_id=risk_group_id,
        )

    # ------------------------------------------------------------------ #
    # 清算瀑布与归因
    # ------------------------------------------------------------------ #

    @staticmethod
    def _waterfall(
        creditors: Sequence[Creditor], funds: Decimal
    ) -> list[Decimal]:
        """按清单先后顺序分配资金，高优先级先受偿，返回各层实际分配。"""
        allocations: list[Decimal] = []
        remaining = funds
        for creditor in creditors:
            share = min(creditor.amount, remaining)
            if share < _ZERO:
                share = _ZERO
            allocations.append(share)
            remaining -= share
        return allocations

    def _settle(
        self, request: SettlementRequest, risk_occupancy: Decimal
    ) -> SettlementResult:
        creditors = request.creditors

        # 第一层：资金池（本次拟清算金额）按优先顺序分配。
        pool_allocations = self._waterfall(creditors, request.settlement_amount)

        # 第二层：补充资本按清单顺序补足仍未受偿的债权。
        residual_claims = [
            creditor.amount - pool_allocations[i]
            for i, creditor in enumerate(creditors)
        ]
        remaining_capital = request.supplementary_capital
        capital_allocations: list[Decimal] = []
        for residual in residual_claims:
            share = min(residual, remaining_capital)
            capital_allocations.append(share)
            remaining_capital -= share

        attributions: list[CreditorAttribution] = []
        uncovered_total = _ZERO
        for i, creditor in enumerate(creditors):
            pool_share = pool_allocations[i]
            capital_share = capital_allocations[i]
            bad_debt = creditor.amount - pool_share - capital_share
            uncovered_total += bad_debt
            attributions.append(
                CreditorAttribution(
                    creditor=creditor.name,
                    claim_amount=creditor.amount,
                    pool_allocation=pool_share,
                    capital_allocation=capital_share,
                    bad_debt=bad_debt,
                )
            )

        # 校验后可用余额：池内实际分配从资金池扣减；拒绝路径余额不变。
        total_pool = sum(pool_allocations, _ZERO)
        validated_balance = request.pool_balance - total_pool

        event_id = self._build_event_id(request.transaction_id, approved=True)
        return SettlementResult(
            transaction_id=request.transaction_id,
            approved=True,
            validated_available_balance=validated_balance,
            creditors=tuple(c.name for c in creditors),
            pool_allocations=tuple(pool_allocations),
            capital_allocations=tuple(capital_allocations),
            attributions=tuple(attributions),
            uncovered_bad_debt=uncovered_total,
            risk_occupancy=risk_occupancy,
            event_id=event_id,
            rejection_reason=None,
        )

    def _build_rejected(
        self,
        request: SettlementRequest,
        risk_occupancy: Decimal,
        reason: str,
    ) -> SettlementResult:
        names = tuple(c.name for c in request.creditors)
        zeros = tuple(_ZERO for _ in request.creditors)
        attributions = tuple(
            CreditorAttribution(
                creditor=c.name,
                claim_amount=c.amount,
                pool_allocation=_ZERO,
                capital_allocation=_ZERO,
                # 拒绝时两层资金均未部署：债权未被处理，不确认坏账。
                bad_debt=_ZERO,
            )
            for c in request.creditors
        )
        event_id = self._build_event_id(request.transaction_id, approved=False)
        return SettlementResult(
            transaction_id=request.transaction_id,
            approved=False,
            validated_available_balance=request.pool_balance,
            creditors=names,
            pool_allocations=zeros,
            capital_allocations=zeros,
            attributions=attributions,
            uncovered_bad_debt=_ZERO,
            risk_occupancy=risk_occupancy,
            event_id=event_id,
            rejection_reason=reason,
        )

    # ------------------------------------------------------------------ #
    # 审计台账（追加式；每个请求恰好一个事件标识）
    # ------------------------------------------------------------------ #

    @staticmethod
    def _build_event_id(transaction_id: str, *, approved: bool) -> str:
        status = "approved" if approved else "rejected"
        return f"EVT-{transaction_id}-{status}"

    def _append_audit(
        self, request: SettlementRequest, result: SettlementResult
    ) -> None:
        self._sequence += 1
        event = AuditEvent(
            event_id=result.event_id,
            sequence=self._sequence,
            transaction_id=request.transaction_id,
            approved=result.approved,
            currency=request.currency,
            input_summary=request.input_summary(),
            validation_result="APPROVED" if result.approved else "REJECTED",
            risk_occupancy=result.risk_occupancy,
            pool_allocations=tuple(
                zip(result.creditors, result.pool_allocations, strict=True)
            ),
            capital_allocations=tuple(
                zip(result.creditors, result.capital_allocations, strict=True)
            ),
            uncovered_bad_debt=result.uncovered_bad_debt,
            validated_available_balance=result.validated_available_balance,
            rejection_reason=result.rejection_reason,
        )
        self._events.append(event)
        self._results[request.transaction_id] = result
        if result.approved:
            # 已放行结算的未覆盖债权进入存续坏账台账，供后续回收冲减；
            # 台账顺序即审计事件顺序 + 债权清单顺序。
            for attribution in result.attributions:
                if attribution.bad_debt > _ZERO:
                    self._bad_debt_ledger.append(
                        _BadDebtEntry(
                            source_transaction_id=request.transaction_id,
                            creditor=attribution.creditor,
                            currency=request.currency,
                            remaining=attribution.bad_debt,
                        )
                    )

    def _append_recovery_audit(self, result: RecoveryResult) -> None:
        """为回收结果追加一条审计事件；结算专属字段按回收语义置空。"""
        self._sequence += 1
        event = AuditEvent(
            event_id=result.event_id,
            sequence=self._sequence,
            transaction_id=result.recovery_transaction_id,
            approved=True,
            currency=result.currency,
            input_summary={
                "recovery_transaction_id": result.recovery_transaction_id,
                "currency": result.currency,
                "recovery_amount": result.recovery_amount,
            },
            validation_result="RECOVERY",
            risk_occupancy=_ZERO,
            pool_allocations=(),
            capital_allocations=(),
            uncovered_bad_debt=result.outstanding_bad_debt,
            validated_available_balance=_ZERO,
            rejection_reason=None,
            recovery_allocations=tuple(
                (
                    allocation.source_transaction_id,
                    allocation.creditor,
                    allocation.recovered_amount,
                    allocation.remaining_bad_debt,
                )
                for allocation in result.allocations
            ),
        )
        self._events.append(event)


def process_settlement(
    transaction_id: str,
    currency: str,
    pool_balance: Number,
    settlement_amount: Number,
    notional_exposure: Number,
    base_limit: Number,
    risk_factor: Number,
    creditors: Sequence[Mapping[str, object] | Creditor | tuple[str, Number]],
    supplementary_capital: Number = _ZERO,
) -> SettlementResult:
    """模块级便捷入口：用一次性引擎实例处理单笔请求并返回结果。

    需要复用审计台账与流水号去重时，请直接使用 :class:`ClearingEngine`。
    """
    return ClearingEngine().process(
        transaction_id=transaction_id,
        currency=currency,
        pool_balance=pool_balance,
        settlement_amount=settlement_amount,
        notional_exposure=notional_exposure,
        base_limit=base_limit,
        risk_factor=risk_factor,
        creditors=creditors,
        supplementary_capital=supplementary_capital,
    )


def process_settlement_batch(
    currency: str,
    opening_pool_balance: Number,
    requests: Iterable[Mapping[str, object]],
) -> BatchSettlementResult:
    """模块级便捷入口：用一次性引擎实例处理整个批次并返回结果。

    不保留跨批次的台账与流水号去重状态；需要复用时请直接使用
    :class:`ClearingEngine` 的 :meth:`~ClearingEngine.process_batch`。
    """
    return ClearingEngine().process_batch(
        currency=currency,
        opening_pool_balance=opening_pool_balance,
        requests=requests,
    )


def process_settlement_multicurrency_batch(
    opening_pool_balances: Mapping[str, Number],
    requests: Iterable[Mapping[str, object]],
) -> MulticurrencyBatchResult:
    """模块级便捷入口：用一次性引擎实例处理多币种批次并返回结果。

    不保留跨批次的台账与流水号去重状态；需要复用时请直接使用
    :class:`ClearingEngine` 的
    :meth:`~ClearingEngine.process_multicurrency_batch`。
    """
    return ClearingEngine().process_multicurrency_batch(
        opening_pool_balances=opening_pool_balances,
        requests=requests,
    )


def process_settlement_risk_group_batch(
    currency: str,
    opening_pool_balance: Number,
    risk_group_limits: Mapping[str, Number],
    requests: Iterable[Mapping[str, object]],
) -> RiskGroupBatchResult:
    """模块级便捷入口：用一次性引擎实例处理带风险组限额的批次并返回结果。

    不保留跨批次的台账、流水号去重与风险组状态；需要跨批次累计风险组
    额度时，请直接使用 :class:`ClearingEngine` 的
    :meth:`~ClearingEngine.process_risk_group_batch`。
    """
    return ClearingEngine().process_risk_group_batch(
        currency=currency,
        opening_pool_balance=opening_pool_balance,
        risk_group_limits=risk_group_limits,
        requests=requests,
    )
