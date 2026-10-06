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

提交前预演（:meth:`ClearingEngine.preview_batch`）以相同输入、校验顺序与
清算规则对同币种批次做只读预演：返回逐笔 :class:`SettlementPreview`（除无
``event_id`` 外与 :class:`SettlementResult` 同名字段同义）与最终余额，不
生成审计事件、不占流水号、不改变台账、坏账与风险组状态；预演只读且幂等，
不代替 :meth:`process_batch` 提交。:meth:`ClearingEngine.preview_risk_group_batch`
与 :meth:`ClearingEngine.preview_multicurrency_batch` 分别对风险组批次与
多币种批次做同样的只读预演：风险组预演从已登记组的已用额度起算、只累计会
放行的风险占用，返回同序结果、最终余额与合并输入 / 已登记组的风险组快照；
多币种预演各币种余额独立滚动，返回同序结果与覆盖全部期初币种的余额映射。

风险组批次（:meth:`ClearingEngine.process_risk_group_batch`，模块级
:func:`process_settlement_risk_group_batch` 使用一次性引擎，不跨批次保留
风险组状态）在以上规则之上增加风险组累计限额：请求可携带 ``risk_group_id``，
限额表把该标识映射到非负上限；同一风险组跨请求、跨批次按请求顺序累计已
放行请求的风险占用，加入本笔后超过组上限则该笔以
``GROUP_LIMIT_EXCEEDED`` 拒绝（不分配资金、不改资金池、不确认坏账、不增加
已用额度），生成现有结构审计事件并继续处理后续请求。不带风险组的请求仍
只按单笔基础限额处理。

多币种批次（:meth:`ClearingEngine.process_multicurrency_batch`，模块级
:func:`process_settlement_multicurrency_batch` 使用一次性引擎，不跨批次
保留状态）在普通批次规则之上按币种独立记账：期初余额以币种到非负余额的
映射给出，每个请求自带 ``currency`` 且必须存在于该映射；每笔只动用自身
币种的滚动余额，放行扣款、拒绝不动余额，不换汇、不使用汇率。批次先整体
校验再执行，失败不生成事件、不占流水号、不改余额或坏账台账。

存续坏账回收（:meth:`ClearingEngine.process_recovery`）只冲减已放行结算
留下的未覆盖债权：按审计事件顺序、再按债权清单顺序逐项冲减币种相符且仍
有坏账的债权，前项清零后处理后项，一笔回收可部分覆盖；历史
:class:`SettlementResult` 不回写。回收成功追加一条标识为
``EVT-{recovery_transaction_id}-recovery`` 的审计事件（序号继续递增，
``recovery_allocations`` 保存同额明细），失败不生成事件、不占流水号、
不改状态。

批次重试与断点恢复（:meth:`ClearingEngine.process_batch_retry` /
:meth:`ClearingEngine.process_risk_group_batch_retry` /
:meth:`ClearingEngine.process_multicurrency_batch_retry`）在既有三类批次
入口之上增加稳定批次标识（``batch_id``）与本次执行标识
（``execution_id``）：相同批次标识配相同请求内容时，无论执行标识是否
相同，都返回首次的同一清算结果，不重复扣减、不重复归因、不重复追加审计；
处理中断后从首个未完成步骤继续，已完成步骤不重复产生副作用。相同批次
标识配不同内容在任何资金或审计副作用之前返回 :class:`BatchRetryConflict`；
标识缺失 / 为空或请求摘要无法计算在副作用之前返回
:class:`BatchIdentifierInvalid`。每批次一把锁，并发提交只允许一个请求
推进，其余等待同一最终结果。三类入口仅提供引擎方法，状态只在实例内存中
保留。

校验异常不产生任何分配，也不写入台账；重复流水号在任何状态变更之前抛出。
"""

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import json
from math import isfinite
from threading import Lock
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
    BatchIdentifierInvalid,
    BatchPreviewResult,
    BatchRetryConflict,
    BatchSettlementResult,
    BATCH_INVALID_REASON_MISSING_BATCH_ID,
    BATCH_INVALID_REASON_MISSING_EXECUTION_ID,
    BATCH_INVALID_REASON_UNCOMPUTABLE_DIGEST,
    Creditor,
    CreditorAttribution,
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
)

__all__ = [
    "ClearingEngine",
    "process_settlement",
    "process_settlement_batch",
    "process_settlement_risk_group_batch",
    "process_settlement_multicurrency_batch",
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


@dataclass
class _BatchRun:
    """一个稳定批次标识的幂等执行记录（引擎私有，可变）。

    - ``kind``：批次类型（``single`` / ``risk_group`` / ``multicurrency``）。
    - ``request_digest``：首次登记的完整请求内容确定性摘要。
    - ``original_execution_id``：首次执行标识。
    - ``normalized``：首次调用归一化后的请求序列（含滚动余额执行所需事实）。
    - ``opening`` / ``balances`` / ``new_limits``：各类型批次执行参数。
    - ``currency``：单币种批次的币种；多币种批次为 ``None``。
    - ``completed_items``：按请求顺序已完成步骤的结果（断点恢复据此跳过）。
    - ``result``：整批完成后的公开结果；未完成时为 ``None``。
    - ``done_event``：本批次推进锁 / 完成信号；并发调用只允许一个推进。
    """

    kind: str
    request_digest: str
    original_execution_id: str
    normalized: list[SettlementRequest]
    opening: Decimal | None = None
    balances: dict[str, Decimal] | None = None
    new_limits: dict[str, Decimal] | None = None
    currency: str | None = None
    completed_items: list[SettlementResult] = field(default_factory=list)
    result: object = None
    done_event: Lock = field(default_factory=Lock)


def _stable_json(value: object) -> str:
    """把批次输入递归归一化为确定性 JSON 文本。

    映射键排序、元组按序列处理；数值按引擎金额归一化口径统一为 Decimal
    的精确规范形式（``int`` / ``float`` / ``Decimal`` 的等价值摘要相同，
    ``float`` 经 ``str(value)`` 转换，不引入二进制尾数）；不可 JSON 化的
    类型（如集合、自定义对象）向上抛出，由调用方转成“请求摘要无法计算”
    的标识无效结果。
    """

    def canonical_number(node: object) -> str:
        decimal_value = (
            Decimal(str(node)) if isinstance(node, float) else Decimal(node)
        )
        # 统一指数写法：40 / 40.0 / 40.00 摘要一致；非有限值保留确定性文本，
        # 由后续既有校验按原口径处理。
        if decimal_value.is_finite():
            decimal_value = (decimal_value + _ZERO).normalize()
            return format(decimal_value, "f")
        return format(decimal_value, "f")

    def normalize(node: object) -> object:
        if node is None or isinstance(node, (bool, str)):
            return node
        if isinstance(node, (int, float, Decimal)):
            return {"__decimal__": canonical_number(node)}
        if isinstance(node, Creditor):
            # 公开支持的债权对象：按其 (name, amount, currency) 事实参与摘要。
            return {
                "name": node.name,
                "amount": {"__decimal__": canonical_number(node.amount)},
                "currency": node.currency,
            }
        if isinstance(node, Mapping):
            return {
                str(key): normalize(node[key])
                for key in sorted(node, key=lambda k: str(k))
            }
        if isinstance(node, (list, tuple)):
            return [normalize(item) for item in node]
        raise TypeError(f"不支持请求摘要的输入类型: {type(node).__name__}")

    return json.dumps(
        normalize(value), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


def _request_digest(kind: str, payload: Mapping[str, object]) -> str:
    """计算批次完整请求内容的 sha256 摘要（十六进制文本）。"""
    canonical = _stable_json({"kind": kind, "payload": payload})
    return sha256(canonical.encode("utf-8")).hexdigest()


def _invalid_batch_identifier(
    batch_id: object, execution_id: object
) -> BatchIdentifierInvalid | None:
    """校验批次标识与执行标识；无效返回唯一结果，有效返回 None。"""
    if not isinstance(batch_id, str) or not batch_id.strip():
        return BatchIdentifierInvalid(
            reason=BATCH_INVALID_REASON_MISSING_BATCH_ID
        )
    if not isinstance(execution_id, str) or not execution_id.strip():
        return BatchIdentifierInvalid(
            reason=BATCH_INVALID_REASON_MISSING_EXECUTION_ID,
            batch_id=batch_id.strip(),
        )
    return None


def _materialize_requests(
    requests: Iterable[Mapping[str, object]],
) -> tuple[list[object], bool]:
    """把请求序列具化为列表；缺失或不可迭代时返回 ``([], True)``。"""
    try:
        return list(requests), False
    except TypeError:
        return [], True


def _digest_or_invalid(
    kind: str, payload: Mapping[str, object], batch_key: str
) -> tuple[str | None, BatchIdentifierInvalid | None]:
    """计算摘要；输入含无法归一化的内容时返回标识无效结果。"""
    try:
        return _request_digest(kind, payload), None
    except (TypeError, ValueError, InvalidOperation):
        return None, BatchIdentifierInvalid(
            reason=BATCH_INVALID_REASON_UNCOMPUTABLE_DIGEST,
            batch_id=batch_key,
        )


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
    :meth:`recovery_of` / :meth:`outstanding_bad_debts` /
    :meth:`audit_reconciliation`）不触发清算。
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
        # 批次重试与断点恢复：稳定批次标识 -> 执行记录；注册表锁保护
        # 登记 / 冲突判定，每批次各自的锁串行并发提交。
        self._batch_runs: dict[str, _BatchRun] = {}
        self._batch_registry_lock = Lock()

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

    def audit_reconciliation(self, currency: str) -> CurrencyAuditSummary:
        """返回该币种的只读审计核对快照 :class:`CurrencyAuditSummary`。

        按审计顺序（台账追加顺序）统计该币种事件：结算事件计入
        ``settlement_count`` 并分别汇总放行 / 拒绝；回收事件计入
        ``recovery_count``。风险占用仅累计放行请求；池内分配、补充资本、
        首次坏账与回收冲减均按事件内明细求和。``outstanding_bad_debt``
        取 :meth:`outstanding_bad_debts` 余额合计，恒满足
        ``initial_bad_debt - recovered_amount == outstanding_bad_debt``。
        ``rejection_counts`` 为按原因码排序的只读映射，省略零次原因；
        ``event_ids`` 保持台账顺序。

        查询只读：重复调用不改变审计序号、事件、结果索引、风险组额度、
        坏账与回收状态。``currency`` 非字符串或去首尾空白后为空抛
        :class:`InvalidCurrencyError`；匹配时去除首尾空白；未出现的币种
        除 ``currency`` 外全部为零、两序列为空。
        """
        if not isinstance(currency, str) or not currency.strip():
            raise InvalidCurrencyError("账户币种缺失或为空")
        currency = currency.strip()

        settlement_count = 0
        approved_count = 0
        rejected_count = 0
        recovery_count = 0
        approved_risk_occupancy = _ZERO
        pool_allocated = _ZERO
        capital_allocated = _ZERO
        initial_bad_debt = _ZERO
        recovered_amount = _ZERO
        rejection_counts: dict[str, int] = {}
        event_ids: list[str] = []

        for event in self._events:
            if event.currency != currency:
                continue
            event_ids.append(event.event_id)
            if event.validation_result == "RECOVERY":
                recovery_count += 1
                recovered_amount += sum(
                    (allocation[2] for allocation in event.recovery_allocations),
                    _ZERO,
                )
                continue

            settlement_count += 1
            if event.approved:
                approved_count += 1
                approved_risk_occupancy += event.risk_occupancy
                pool_allocated += sum(
                    (amount for _name, amount in event.pool_allocations),
                    _ZERO,
                )
                capital_allocated += sum(
                    (amount for _name, amount in event.capital_allocations),
                    _ZERO,
                )
                initial_bad_debt += event.uncovered_bad_debt
            else:
                rejected_count += 1
                reason = event.rejection_reason
                if reason is not None:
                    rejection_counts[reason] = rejection_counts.get(reason, 0) + 1

        outstanding_bad_debt = sum(
            (item.balance for item in self.outstanding_bad_debts(currency)),
            _ZERO,
        )

        return CurrencyAuditSummary(
            currency=currency,
            settlement_count=settlement_count,
            approved_count=approved_count,
            rejected_count=rejected_count,
            recovery_count=recovery_count,
            approved_risk_occupancy=approved_risk_occupancy,
            pool_allocated=pool_allocated,
            capital_allocated=capital_allocated,
            initial_bad_debt=initial_bad_debt,
            recovered_amount=recovered_amount,
            outstanding_bad_debt=outstanding_bad_debt,
            rejection_counts=MappingProxyType(
                dict(sorted(rejection_counts.items()))
            ),
            event_ids=tuple(event_ids),
        )

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

    def preview_batch(
        self,
        currency: str,
        opening_pool_balance: Number,
        requests: Iterable[Mapping[str, object]],
    ) -> BatchPreviewResult:
        """对同币种多笔结算批次做提交前只读预演，返回不可变
        :class:`BatchPreviewResult`。

        输入、债权格式、币种、滚动余额与清算瀑布沿用 :meth:`process_batch`；
        校验顺序与异常类型亦相同（空批次、缺币种、债权币种混用、流水号批内
        或与台账重复、空债权清单、风险系数越界、非法数值），异常不留半批
        状态。

        预演只报告当前台账下的执行结果：不生成审计事件、不占流水号、不改
        余额、结果索引、风险组额度、坏账台账与审计核对，只读且幂等。业务
        拒绝不抛异常，对应项给出 ``approved=False``、``rejection_reason``、
        输入余额与零分配；放行、风险占用与坏账归因同正式提交。

        预演不代替提交；以相同输入随后调用 :meth:`process_batch`，逐笔结果
        与余额除新增 ``event_id`` 外一致。
        """
        _, opening, normalized = self._validate_batch(
            currency, opening_pool_balance, requests
        )

        results: list[SettlementPreview] = []
        balance = opening
        for request in normalized:
            request = replace(request, pool_balance=balance)
            preview = self._adjudicate(request)
            results.append(preview)
            balance = preview.validated_available_balance

        return BatchPreviewResult(
            results=tuple(results),
            validated_available_balance=balance,
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

    def preview_risk_group_batch(
        self,
        currency: str,
        opening_pool_balance: Number,
        risk_group_limits: Mapping[str, Number],
        requests: Iterable[Mapping[str, object]],
    ) -> RiskGroupBatchPreviewResult:
        """对带风险组累计限额的同币种批次做提交前只读预演，返回不可变
        :class:`RiskGroupBatchPreviewResult`。

        输入、校验顺序与异常类型沿用 :meth:`process_risk_group_batch`
        （空批次、缺币种、债权币种混用、流水号批内或与台账重复、空债权
        清单、风险系数越界、非法数值；同组上限冲突、未登记组、空组标识或
        非法上限抛 :class:`InvalidRiskGroupError`），异常不留任何状态。

        预演从已登记组的 ``used`` 起算，按请求顺序只累计会放行的风险占用：
        ``GROUP_LIMIT_EXCEEDED``、余额不足与单笔基础限额拒绝沿用各自原原因
        码，拒绝不增组额度；无风险组请求只走单笔规则。返回的 ``results``
        与 ``requests`` 同序，带组笔含 ``group_used_after``；
        ``risk_groups`` 为合并本次输入风险组与引擎已登记组的快照，每项含
        ``used`` / ``limit`` / ``remaining``。

        预演不生成审计事件或 ``event_id``、不占流水号、不改余额、结果索引、
        风险组额度、坏账台账与审计核对，只读且重复或交叉调用幂等。以相同
        输入随后调用 :meth:`process_risk_group_batch`，逐笔状态、拒绝原因、
        ``group_used_after``、余额与风险组额度与预演一致，正式结果只增
        ``event_id`` 并写台账。
        """
        _, opening, normalized, new_limits = self._validate_risk_group_batch(
            currency, opening_pool_balance, risk_group_limits, requests
        )

        # 预演工作副本：上限与已用额度均从已登记组起算并叠加本次输入表，
        # 仅在副本内滚动，不写回引擎状态。
        working_limits = dict(self._risk_group_limits)
        working_limits.update(new_limits)
        working_used: dict[str, Decimal] = {
            group_id: self._risk_group_used.get(group_id, _ZERO)
            for group_id in working_limits
        }

        results: list[SettlementPreview] = []
        balance = opening
        for request in normalized:
            request = replace(request, pool_balance=balance)
            preview = self._adjudicate(
                request,
                group_used=working_used,
                group_limits=working_limits,
            )
            group_id = request.risk_group_id
            if group_id is not None:
                if preview.approved:
                    # 组额度只在会放行的请求上按其风险占用累计。
                    working_used[group_id] += preview.risk_occupancy
                # 拒绝结果未占用额度：等于执行前值。
                preview = replace(
                    preview, group_used_after=working_used[group_id]
                )
            results.append(preview)
            balance = preview.validated_available_balance

        return RiskGroupBatchPreviewResult(
            results=tuple(results),
            validated_available_balance=balance,
            risk_groups=MappingProxyType(
                {
                    group_id: RiskGroupUsage(
                        used=working_used[group_id],
                        limit=working_limits[group_id],
                        remaining=working_limits[group_id]
                        - working_used[group_id],
                    )
                    for group_id in working_limits
                }
            ),
        )

    def process_multicurrency_batch(
        self,
        opening_pool_balances: Mapping[str, Number],
        requests: Iterable[Mapping[str, object]],
    ) -> MulticurrencyBatchResult:
        """处理多币种结算批次，按币种独立记账，返回不可变
        :class:`MulticurrencyBatchResult`。

        - ``opening_pool_balances``：币种到非负期初资金池余额的映射。
        - ``requests``：请求映射序列，每项字段沿用 :meth:`process_batch`
          的单项字段，并增加 ``currency``（该笔使用的币种，必须存在于
          期初余额映射）；债权形式与金额归一化规则不变。

        每个请求只动用自身币种的滚动余额：放行按池内实际分配扣款，拒绝
        不动余额；不换汇、不使用汇率，各币种互不影响。``results`` 与
        ``event_ids`` 均与 ``requests`` 同序，每笔的
        ``validated_available_balance`` 为本币种执行后的即时余额；
        ``validated_available_balances`` 给出全部币种的最终余额，未被
        请求使用的币种保留期初原值。

        批次先做整体校验再执行，失败不生成事件、不占流水号、不改余额或
        坏账台账。错误依次为：空请求清单抛 :class:`EmptyBatchError`；
        期初映射非法、余额键空白或任何数值非法抛内建 :class:`ValueError`；
        请求币种缺失、空白或不在期初映射中抛 :class:`InvalidCurrencyError`；
        单笔债权混合币种抛 :class:`MixedCurrencyError`；流水号批内或与
        台账重复抛 :class:`DuplicateTransactionError`；空债权清单抛
        :class:`EmptyCreditorListError`；风险系数越界抛
        :class:`InvalidRiskFactorError`。

        校验通过后按请求顺序逐笔执行既有限额校验与清算；放行或拒绝各
        追加一条现有结构事件（序号递增），拒绝后继续处理后续请求，批次
        本身不建事件。未覆盖坏账按各自币种进入存续坏账台账，可由
        :meth:`process_recovery` 冲减。
        """
        balances, normalized = self._validate_multicurrency_batch(
            opening_pool_balances, requests
        )

        # 执行阶段基于已校验数据不会失败；仍防御性回滚，保证异常路径下
        # 事件、序号、流水号、结果索引与坏账台账全部复原。
        events_mark = len(self._events)
        sequence_mark = self._sequence
        ledger_mark = len(self._bad_debt_ledger)
        added_ids: list[str] = []
        results: list[SettlementResult] = []
        working = dict(balances)
        try:
            for request in normalized:
                request = replace(
                    request, pool_balance=working[request.currency]
                )
                self._seen_transactions.add(request.transaction_id)
                added_ids.append(request.transaction_id)
                result = self._execute(request)
                results.append(result)
                working[request.currency] = result.validated_available_balance
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
            validated_available_balances=MappingProxyType(working),
        )

    def preview_multicurrency_batch(
        self,
        opening_pool_balances: Mapping[str, Number],
        requests: Iterable[Mapping[str, object]],
    ) -> MulticurrencyBatchPreviewResult:
        """对多币种结算批次做提交前只读预演，返回不可变
        :class:`MulticurrencyBatchPreviewResult`。

        输入、债权格式、分币种校验顺序与异常类型沿用
        :meth:`process_multicurrency_batch`（空批次、期初映射 / 数值非法抛
        :class:`ValueError`、请求币种缺失或不在期初映射中抛
        :class:`InvalidCurrencyError`、单笔债权混合币种抛
        :class:`MixedCurrencyError`、流水号批内或与台账重复抛
        :class:`DuplicateTransactionError`、空债权清单抛
        :class:`EmptyCreditorListError`、风险系数越界抛
        :class:`InvalidRiskFactorError`），异常不留任何状态。

        各币种余额独立滚动：每个请求只动用自身币种的工作余额，放行按池内
        实际分配扣款、拒绝不动余额；不换汇、不使用汇率。返回的 ``results``
        与 ``requests`` 同序，``validated_available_balances`` 覆盖全部期初
        币种，未使用币种保持期初原值。

        预演不生成审计事件或 ``event_id``、不占流水号、不改余额、结果索引、
        坏账台账与审计核对，只读且重复或交叉调用幂等。以相同输入随后调用
        :meth:`process_multicurrency_batch`，逐笔状态、拒绝原因与各币种余额
        与预演一致，正式结果只增 ``event_id`` 并写台账。
        """
        balances, normalized = self._validate_multicurrency_batch(
            opening_pool_balances, requests
        )

        results: list[SettlementPreview] = []
        working = dict(balances)
        for request in normalized:
            request = replace(
                request, pool_balance=working[request.currency]
            )
            preview = self._adjudicate(request)
            results.append(preview)
            working[request.currency] = preview.validated_available_balance

        return MulticurrencyBatchPreviewResult(
            results=tuple(results),
            validated_available_balances=MappingProxyType(working),
        )

    # ------------------------------------------------------------------ #
    # 批次重试与断点恢复
    #
    # 同一稳定批次标识（batch_id）的首次调用走既有的校验 / 清算 / 坏账 /
    # 审计流程并登记执行记录；后续调用（execution_id 可不同）在任何资金或
    # 审计副作用之前先比对请求内容摘要：内容相同则返回与首次完全相同的结果，
    # 不重复扣减、不重复归因、不重复追加审计；内容不同返回唯一的
    # BatchRetryConflict。处理中断后重提相同请求时，从首个尚未完成的步骤
    # 继续，已完成步骤不再次产生副作用。每批次一把锁，并发提交只允许一个
    # 请求推进，其余等待同一最终结果。
    # ------------------------------------------------------------------ #

    def process_batch_retry(
        self,
        batch_id: str,
        execution_id: str,
        currency: str,
        opening_pool_balance: Number,
        requests: Iterable[Mapping[str, object]],
    ) -> BatchSettlementResult | BatchRetryConflict | BatchIdentifierInvalid:
        """同币种批次的可重试 / 断点恢复入口，参数沿用 :meth:`process_batch`。

        额外参数：

        - ``batch_id``：调用方提供的稳定批次标识；同一批次的首次执行与全部
          重试必须使用相同值。
        - ``execution_id``：本次执行标识；仅用于区分不同执行尝试，重试时可
          不同，不影响清算结果与审计内容。

        返回值三选一：

        - 首次成功或内容一致的重试：原有不可变 :class:`BatchSettlementResult`
          （重试返回首次的同一结果，审计台账不新增事件）；
        - 相同 ``batch_id`` 配不同请求内容：:class:`BatchRetryConflict`，
          在任何资金或审计副作用之前拒绝；
        - 标识缺失 / 为空或请求摘要无法计算：:class:`BatchIdentifierInvalid`，
          同样在任何副作用之前拒绝。

        批次内容本身的既有校验异常（空批次、缺币种、混合币种、重复流水号、
        空债权清单、系数越界、非法数值）仍按原口径抛出，不与上述拒绝结果
        混用。
        """
        identifier_error = _invalid_batch_identifier(batch_id, execution_id)
        if identifier_error is not None:
            return identifier_error
        batch_key = batch_id.strip()

        items, unreadable = _materialize_requests(requests)
        if unreadable:
            # 清单缺失或不可迭代沿用既有口径：直接由原校验顺序抛
            # EmptyBatchError，不登记批次、不产生任何副作用。
            self._validate_batch(currency, opening_pool_balance, requests)
        digest, digest_error = _digest_or_invalid(
            "single",
            {
                "currency": currency,
                "opening_pool_balance": opening_pool_balance,
                "requests": items,
            },
            batch_key,
        )
        if digest_error is not None:
            return digest_error

        with self._batch_registry_lock:
            run = self._batch_runs.get(batch_key)
            if run is None:
                # 首次登记前仍按原顺序做整体校验：异常直接抛出，不登记。
                currency, opening, normalized = self._validate_batch(
                    currency, opening_pool_balance, items
                )
                run = _BatchRun(
                    kind="single",
                    request_digest=digest,
                    original_execution_id=execution_id.strip(),
                    normalized=normalized,
                    opening=opening,
                    currency=currency,
                )
                self._batch_runs[batch_key] = run
            elif run.request_digest != digest:
                # 不同请求内容：在任何资金或审计副作用之前拒绝。
                return BatchRetryConflict(
                    batch_id=batch_key,
                    original_request_digest=run.request_digest,
                    incoming_request_digest=digest,
                    original_execution_id=run.original_execution_id,
                )

        with run.done_event:
            if run.result is None:
                self._complete_single_batch(run)
            return run.result

    def process_risk_group_batch_retry(
        self,
        batch_id: str,
        execution_id: str,
        currency: str,
        opening_pool_balance: Number,
        risk_group_limits: Mapping[str, Number],
        requests: Iterable[Mapping[str, object]],
    ) -> RiskGroupBatchResult | BatchRetryConflict | BatchIdentifierInvalid:
        """风险组批次的可重试 / 断点恢复入口，参数沿用
        :meth:`process_risk_group_batch`，``batch_id`` / ``execution_id``
        语义同 :meth:`process_batch_retry`。
        """
        identifier_error = _invalid_batch_identifier(batch_id, execution_id)
        if identifier_error is not None:
            return identifier_error
        batch_key = batch_id.strip()

        items, unreadable = _materialize_requests(requests)
        if unreadable:
            self._validate_risk_group_batch(
                currency, opening_pool_balance, risk_group_limits, requests
            )
        digest, digest_error = _digest_or_invalid(
            "risk_group",
            {
                "currency": currency,
                "opening_pool_balance": opening_pool_balance,
                "risk_group_limits": risk_group_limits,
                "requests": items,
            },
            batch_key,
        )
        if digest_error is not None:
            return digest_error

        with self._batch_registry_lock:
            run = self._batch_runs.get(batch_key)
            if run is None:
                currency, opening, normalized, new_limits = (
                    self._validate_risk_group_batch(
                        currency,
                        opening_pool_balance,
                        risk_group_limits,
                        items,
                    )
                )
                run = _BatchRun(
                    kind="risk_group",
                    request_digest=digest,
                    original_execution_id=execution_id.strip(),
                    normalized=normalized,
                    opening=opening,
                    new_limits=new_limits,
                    currency=currency,
                )
                self._batch_runs[batch_key] = run
            elif run.request_digest != digest:
                return BatchRetryConflict(
                    batch_id=batch_key,
                    original_request_digest=run.request_digest,
                    incoming_request_digest=digest,
                    original_execution_id=run.original_execution_id,
                )

        with run.done_event:
            if run.result is None:
                self._complete_risk_group_batch(run)
            return run.result

    def process_multicurrency_batch_retry(
        self,
        batch_id: str,
        execution_id: str,
        opening_pool_balances: Mapping[str, Number],
        requests: Iterable[Mapping[str, object]],
    ) -> MulticurrencyBatchResult | BatchRetryConflict | BatchIdentifierInvalid:
        """多币种批次的可重试 / 断点恢复入口，参数沿用
        :meth:`process_multicurrency_batch`，``batch_id`` / ``execution_id``
        语义同 :meth:`process_batch_retry`。
        """
        identifier_error = _invalid_batch_identifier(batch_id, execution_id)
        if identifier_error is not None:
            return identifier_error
        batch_key = batch_id.strip()

        items, unreadable = _materialize_requests(requests)
        if unreadable:
            self._validate_multicurrency_batch(
                opening_pool_balances, requests
            )
        digest, digest_error = _digest_or_invalid(
            "multicurrency",
            {
                "opening_pool_balances": opening_pool_balances,
                "requests": items,
            },
            batch_key,
        )
        if digest_error is not None:
            return digest_error

        with self._batch_registry_lock:
            run = self._batch_runs.get(batch_key)
            if run is None:
                balances, normalized = self._validate_multicurrency_batch(
                    opening_pool_balances, items
                )
                run = _BatchRun(
                    kind="multicurrency",
                    request_digest=digest,
                    original_execution_id=execution_id.strip(),
                    normalized=normalized,
                    balances=balances,
                )
                self._batch_runs[batch_key] = run
            elif run.request_digest != digest:
                return BatchRetryConflict(
                    batch_id=batch_key,
                    original_request_digest=run.request_digest,
                    incoming_request_digest=digest,
                    original_execution_id=run.original_execution_id,
                )

        with run.done_event:
            if run.result is None:
                self._complete_multicurrency_batch(run)
            return run.result

    def _complete_single_batch(self, run: _BatchRun) -> None:
        """在批次锁内推进同币种批次至完成，跳过已完成步骤（断点恢复）。"""
        start = len(run.completed_items)
        balance = (
            run.completed_items[-1].validated_available_balance
            if start
            else run.opening
        )
        for request in run.normalized[start:]:
            request = replace(request, pool_balance=balance)
            result = self._execute_resumable_item(request)
            run.completed_items.append(result)
            balance = result.validated_available_balance

        results = tuple(run.completed_items)
        run.result = BatchSettlementResult(
            results=results,
            event_ids=tuple(result.event_id for result in results),
            validated_available_balance=balance,
        )

    def _complete_risk_group_batch(self, run: _BatchRun) -> None:
        """在批次锁内推进风险组批次至完成，跳过已完成步骤（断点恢复）。"""
        for group_id, limit in run.new_limits.items():
            # 重复登记相同上限是幂等的；断点恢复时这些组通常已在引擎中。
            self._risk_group_limits[group_id] = limit
            self._risk_group_used.setdefault(group_id, _ZERO)

        start = len(run.completed_items)
        balance = (
            run.completed_items[-1].validated_available_balance
            if start
            else run.opening
        )
        for request in run.normalized[start:]:
            request = replace(request, pool_balance=balance)
            result = self._execute_resumable_item(request)
            run.completed_items.append(result)
            balance = result.validated_available_balance

        results = tuple(run.completed_items)
        run.result = RiskGroupBatchResult(
            results=results,
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

    def _complete_multicurrency_batch(self, run: _BatchRun) -> None:
        """在批次锁内推进多币种批次至完成，跳过已完成步骤（断点恢复）。"""
        working = dict(run.balances)
        for index, result in enumerate(run.completed_items):
            working[run.normalized[index].currency] = (
                result.validated_available_balance
            )

        start = len(run.completed_items)
        for index in range(start, len(run.normalized)):
            request = run.normalized[index]
            request = replace(
                request, pool_balance=working[request.currency]
            )
            result = self._execute_resumable_item(request)
            run.completed_items.append(result)
            working[request.currency] = result.validated_available_balance

        results = tuple(run.completed_items)
        run.result = MulticurrencyBatchResult(
            results=results,
            event_ids=tuple(result.event_id for result in results),
            validated_available_balances=MappingProxyType(working),
        )

    def _execute_resumable_item(
        self, request: SettlementRequest
    ) -> SettlementResult:
        """执行断点恢复批次中的单个步骤。

        基于已校验数据执行不会失败；若意外抛错，仅回滚当前步骤的状态
        （事件、序号、流水号、结果索引、该步骤的风险组占用、坏账台账），
        执行记录不计入该步骤，下一轮推进从同一请求重新执行，从而不会出现
        两套金额分配或两条等价审计轨迹。
        """
        events_mark = len(self._events)
        sequence_mark = self._sequence
        ledger_mark = len(self._bad_debt_ledger)
        group_id = request.risk_group_id
        group_used_before = (
            self._risk_group_used.get(group_id, _ZERO)
            if group_id is not None
            else None
        )
        tid = request.transaction_id
        self._seen_transactions.add(tid)
        try:
            return self._execute(request)
        except Exception:
            del self._events[events_mark:]
            self._sequence = sequence_mark
            del self._bad_debt_ledger[ledger_mark:]
            if group_id is not None:
                self._risk_group_used[group_id] = group_used_before
            self._seen_transactions.discard(tid)
            self._results.pop(tid, None)
            raise

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
        preview = self._adjudicate(request)
        group_id = request.risk_group_id
        if group_id is not None:
            if preview.approved:
                # 组额度只在成功放行的请求上按其风险占用增加。
                self._risk_group_used[group_id] += preview.risk_occupancy
            # 拒绝结果未占用额度：等于执行前值。
            preview = replace(
                preview, group_used_after=self._risk_group_used[group_id]
            )
        result = self._to_result(request, preview)
        self._append_audit(request, result)
        return result

    def _adjudicate(
        self,
        request: SettlementRequest,
        *,
        group_used: Mapping[str, Decimal] | None = None,
        group_limits: Mapping[str, Decimal] | None = None,
    ) -> SettlementPreview:
        """纯计算限额校验与清算瀑布，返回预演结果。

        默认只读引擎自身的风险组状态（不增加已用额度），不触碰事件、序号、
        流水号、结果索引与坏账台账；风险组预演传入工作副本映射做只读判定，
        同样不写回引擎。拒绝时余额不变、无任何分配。
        """
        used_by_group = (
            self._risk_group_used if group_used is None else group_used
        )
        limits_by_group = (
            self._risk_group_limits if group_limits is None else group_limits
        )
        risk_occupancy = (
            request.settlement_amount
            + request.notional_exposure * request.risk_factor
        )
        group_id = request.risk_group_id
        if request.settlement_amount > request.pool_balance:
            return self._build_rejected(
                request, risk_occupancy, "SETTLEMENT_EXCEEDS_AVAILABLE_BALANCE"
            )
        if risk_occupancy > request.base_limit:
            return self._build_rejected(
                request, risk_occupancy, "RISK_OCCUPANCY_EXCEEDS_BASE_LIMIT"
            )
        if (
            group_id is not None
            and used_by_group[group_id] + risk_occupancy
            > limits_by_group[group_id]
        ):
            return self._build_rejected(
                request, risk_occupancy, "GROUP_LIMIT_EXCEEDED"
            )
        return self._settle(request, risk_occupancy)

    def _to_result(
        self, request: SettlementRequest, preview: SettlementPreview
    ) -> SettlementResult:
        """为预演结果补上审计事件标识，得到正式提交的公开结果。"""
        return SettlementResult(
            transaction_id=preview.transaction_id,
            approved=preview.approved,
            validated_available_balance=preview.validated_available_balance,
            creditors=preview.creditors,
            pool_allocations=preview.pool_allocations,
            capital_allocations=preview.capital_allocations,
            attributions=preview.attributions,
            uncovered_bad_debt=preview.uncovered_bad_debt,
            risk_occupancy=preview.risk_occupancy,
            event_id=self._build_event_id(
                request.transaction_id, approved=preview.approved
            ),
            rejection_reason=preview.rejection_reason,
            group_used_after=preview.group_used_after,
        )

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
    # 多币种批次校验（整体校验通过后才开始任何状态变更）
    # ------------------------------------------------------------------ #

    def _validate_multicurrency_batch(
        self,
        opening_pool_balances: Mapping[str, Number],
        requests: Iterable[Mapping[str, object]],
    ) -> tuple[dict[str, Decimal], list[SettlementRequest]]:
        """按既定顺序分阶段校验多币种批次，返回期初余额表与归一化请求。

        阶段顺序：空批次 -> 期初映射与数值（ValueError）-> 请求币种
        （InvalidCurrencyError）-> 债权混合币种（MixedCurrencyError）->
        重复流水号（DuplicateTransactionError）-> 空债权清单
        （EmptyCreditorListError）-> 风险系数越界（InvalidRiskFactorError）。
        """
        if requests is None:
            raise EmptyBatchError("批次请求清单为空")
        try:
            items = list(requests)
        except TypeError:
            raise EmptyBatchError("批次请求清单为空") from None
        if not items:
            raise EmptyBatchError("批次请求清单为空")

        # 期初余额映射与逐请求结构 / 数值：一切数值非法在此抛 ValueError。
        balances = self._normalize_opening_balances(opening_pool_balances)
        prenormalized = [
            self._prenormalize_multicurrency_item(item, index)
            for index, item in enumerate(items)
        ]

        # 请求币种：缺失、空白或不在期初映射中均拒绝。
        currencies: list[str] = []
        for index, item in enumerate(items):
            raw_ccy = item.get("currency")  # item 已确认是 Mapping
            if not isinstance(raw_ccy, str) or not raw_ccy.strip():
                raise InvalidCurrencyError(
                    f"requests[{index}] 币种缺失或为空"
                )
            ccy = raw_ccy.strip()
            if ccy not in balances:
                raise InvalidCurrencyError(
                    f"requests[{index}] 币种 {ccy} 不在期初余额映射中"
                )
            currencies.append(ccy)

        # 单笔债权混合币种：债权币种与所属请求币种不一致即拒绝。
        for index, parsed in enumerate(prenormalized):
            currency = currencies[index]
            for name, _claim, ccy in parsed["creditors"]:
                if ccy is None:
                    continue
                if not isinstance(ccy, str) or not ccy.strip():
                    raise InvalidCurrencyError(
                        f"requests[{index}] 债权 {name} 币种为空"
                    )
                ccy = ccy.strip()
                if ccy != currency:
                    raise MixedCurrencyError(
                        f"账户币种 {currency} 与债权 {name} 币种 {ccy} 不一致"
                    )

        # 重复流水号：批内互相重复或与台账重复均拒绝。
        seen_in_batch: set[str] = set()
        for parsed in prenormalized:
            tid = parsed["transaction_id"]
            if tid in seen_in_batch or tid in self._seen_transactions:
                raise DuplicateTransactionError(
                    f"重复的业务流水号: {tid}"
                )
            seen_in_batch.add(tid)

        # 空债权清单。
        for parsed in prenormalized:
            if not parsed["creditors"]:
                raise EmptyCreditorListError("优先债权清单为空")

        # 风险系数越界。
        for parsed in prenormalized:
            factor = parsed["risk_factor"]
            if factor < _ZERO or factor > _ONE:
                raise InvalidRiskFactorError(
                    f"风险系数必须落在 [0, 1]，收到 {factor}"
                )

        # 全部校验通过后组装归一化请求；pool_balance 暂存本币种期初余额，
        # 执行时替换为本币种滚动余额。
        normalized: list[SettlementRequest] = []
        for index, parsed in enumerate(prenormalized):
            currency = currencies[index]
            creditors = tuple(
                Creditor(
                    name=name,
                    amount=claim,
                    currency=None if ccy is None else ccy.strip(),
                )
                for name, claim, ccy in parsed["creditors"]
            )
            normalized.append(
                SettlementRequest(
                    transaction_id=parsed["transaction_id"],
                    currency=currency,
                    pool_balance=balances[currency],
                    settlement_amount=parsed["settlement_amount"],
                    notional_exposure=parsed["notional_exposure"],
                    base_limit=parsed["base_limit"],
                    risk_factor=parsed["risk_factor"],
                    creditors=creditors,
                    supplementary_capital=parsed["supplementary_capital"],
                )
            )
        return balances, normalized

    @staticmethod
    def _normalize_opening_balances(
        opening_pool_balances: Mapping[str, Number],
    ) -> dict[str, Decimal]:
        """归一化期初余额映射；映射非法、键空白或任何数值非法均抛
        内建 :class:`ValueError`。"""
        if not isinstance(opening_pool_balances, Mapping):
            raise ValueError(
                "opening_pool_balances 必须是币种到期初余额的映射"
            )
        balances: dict[str, Decimal] = {}
        for raw_ccy, raw_value in opening_pool_balances.items():
            if not isinstance(raw_ccy, str) or not raw_ccy.strip():
                raise ValueError("期初余额映射的币种键必须是非空字符串")
            ccy = raw_ccy.strip()
            if ccy in balances:
                raise ValueError(
                    f"期初余额映射存在归一化后重复的币种键: {ccy}"
                )
            value = _as_decimal(raw_value, f"opening_pool_balances[{ccy}]")
            _check_non_negative(value, f"opening_pool_balances[{ccy}]")
            balances[ccy] = value
        return balances

    def _prenormalize_multicurrency_item(
        self, item: object, index: int
    ) -> dict[str, object]:
        """多币种批次的单项预归一化：结构、流水号与数值字段（ValueError）。
        币种、债权币种一致性、空债权清单与系数越界在后续阶段统一判定。"""
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
            "settlement_amount": amount,
            "notional_exposure": exposure,
            "base_limit": limit,
            "risk_factor": factor,
            "supplementary_capital": capital,
            "creditors": self._parse_multicurrency_creditors(
                item.get("creditors")
            ),
        }

    @staticmethod
    def _parse_multicurrency_creditors(
        raw: object,
    ) -> list[tuple[str, Decimal, object]]:
        """解析债权清单的结构、名称与金额（非法抛 ValueError），返回
        ``(name, amount, currency)`` 三元组；币种一致性与空清单在后续
        阶段判定，清单缺失或不可迭代时返回空列表。"""
        if raw is None:
            return []
        try:
            iterator = iter(raw)
        except TypeError:
            return []

        parsed: list[tuple[str, Decimal, object]] = []
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

            parsed.append((name.strip(), claim_dec, ccy))
        return parsed

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
    ) -> SettlementPreview:
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

        return SettlementPreview(
            transaction_id=request.transaction_id,
            approved=True,
            validated_available_balance=validated_balance,
            creditors=tuple(c.name for c in creditors),
            pool_allocations=tuple(pool_allocations),
            capital_allocations=tuple(capital_allocations),
            attributions=tuple(attributions),
            uncovered_bad_debt=uncovered_total,
            risk_occupancy=risk_occupancy,
            rejection_reason=None,
        )

    def _build_rejected(
        self,
        request: SettlementRequest,
        risk_occupancy: Decimal,
        reason: str,
    ) -> SettlementPreview:
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
        return SettlementPreview(
            transaction_id=request.transaction_id,
            approved=False,
            validated_available_balance=request.pool_balance,
            creditors=names,
            pool_allocations=zeros,
            capital_allocations=zeros,
            attributions=attributions,
            uncovered_bad_debt=_ZERO,
            risk_occupancy=risk_occupancy,
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
