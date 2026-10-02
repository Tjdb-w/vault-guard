"""清算风控引擎：限额校验、清算瀑布、坏账归因与内存审计台账。

公开入口为 :meth:`ClearingEngine.process`（单笔）与
:meth:`ClearingEngine.process_batch`（多笔批次；模块级便捷函数
:func:`process_settlement` / :func:`process_settlement_batch` 内部各自
持有自己的一次性引擎实例）。

处理严格按确定顺序执行：

1. 输入校验（负数 / 币种 / 系数 / 债权清单 / 流水号去重）。
2. 限额校验：风险占用 = 拟清算金额 + 名义敞口 × 风险系数。
3. 通过时按优先债权清单顺序执行资金池清算瀑布。
4. 补充资本按清单顺序补足仍未受偿的债权。
5. 归因并追加恰好一条审计事件。

校验异常不产生任何分配，也不写入台账；重复流水号在任何状态变更之前抛出。
批次入口在任何状态变更之前先完成整批校验：任一请求不合法即整批拒绝，
已通过校验的流水号也不被占用；批次本身不生成审计事件。
"""

from collections.abc import Iterable, Mapping, Sequence
from decimal import Decimal, InvalidOperation
from math import isfinite
from typing import Union

from .errors import (
    DuplicateTransactionError,
    EmptyBatchError,
    EmptyCreditorListError,
    InvalidCurrencyError,
    InvalidRiskFactorError,
    MixedCurrencyError,
)
from .models import (
    AuditEvent,
    BatchSettlementResult,
    Creditor,
    CreditorAttribution,
    SettlementRequest,
    SettlementResult,
)

__all__ = [
    "ClearingEngine",
    "process_settlement",
    "process_settlement_batch",
]

Number = Union[int, float, Decimal]

_ZERO = Decimal(0)
_ONE = Decimal(1)


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
    （:meth:`get_event` / :meth:`events` / :meth:`result_of`）不触发清算。
    """

    def __init__(self) -> None:
        self._seen_transactions: set[str] = set()
        self._events: list[AuditEvent] = []
        self._results: dict[str, SettlementResult] = {}
        self._sequence = 0

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

        # 限额校验。拒绝时余额不变、无任何分配。
        risk_occupancy = (
            request.settlement_amount
            + request.notional_exposure * request.risk_factor
        )
        if request.settlement_amount > request.pool_balance:
            result = self._build_rejected(
                request, risk_occupancy, "SETTLEMENT_EXCEEDS_AVAILABLE_BALANCE"
            )
        elif risk_occupancy > request.base_limit:
            result = self._build_rejected(
                request, risk_occupancy, "RISK_OCCUPANCY_EXCEEDS_BASE_LIMIT"
            )
        else:
            result = self._settle(request, risk_occupancy)

        self._append_audit(request, result)
        return result

    # ------------------------------------------------------------------ #
    # 批量入口（整批先校验、滚动余额执行；批次本身不建事件）
    # ------------------------------------------------------------------ #

    def process_batch(
        self,
        currency: str,
        opening_pool_balance: Number,
        requests: Sequence[Mapping[str, object]],
    ) -> BatchSettlementResult:
        """按序处理一批同币种结算请求，返回不可变批量结果。

        批次字段：

        - ``currency``：批次币种，批内所有请求共用；逐笔请求无需携带
          币种，若显式携带则必须与批次一致，否则按
          :class:`MixedCurrencyError` 处理；债权币种同样必须与批次一致。
        - ``opening_pool_balance``：批次期初资金池余额；各笔看到的可用
          余额为自此滚动的即时余额（被放行请求的池内分配逐笔扣减）。
        - ``requests``：单笔请求映射序列，字段沿用 :meth:`process`
          （``transaction_id`` / ``settlement_amount`` /
          ``notional_exposure`` / ``base_limit`` / ``risk_factor`` /
          ``creditors`` / 可选 ``supplementary_capital``；不含
          ``currency`` 与 ``pool_balance``）。

        任何一笔请求校验不通过，整批在任何状态变更之前被拒绝：不生成
        事件、不占用流水号、余额与台账均保持批次开始前状态。通过校验的
        请求随后按序以滚动余额执行限额与清算；被限额拒绝的请求不动资金、
        不确认坏账，但仍产生一条拒绝事件，其后的请求继续处理。
        """
        # 第一层：批次级校验先于一切。
        if requests is None:
            raise EmptyBatchError("批量结算请求为空")
        try:
            iterator = iter(requests)
        except TypeError:
            raise EmptyBatchError("批量结算请求为空") from None
        raw_requests = list(iterator)
        if not raw_requests:
            raise EmptyBatchError("批量结算请求为空")

        if not isinstance(currency, str) or not currency.strip():
            raise InvalidCurrencyError("账户币种缺失或为空")
        currency = currency.strip()

        opening = _as_decimal(opening_pool_balance, "opening_pool_balance")
        _check_non_negative(opening, "opening_pool_balance")

        # 第二层：逐笔字段校验，整批完成前不触碰引擎状态。
        # 批内暂存集合从台账已有流水号复制：批内重复与台账重复同源判定。
        validated: list[SettlementRequest] = []
        pending_seen: set[str] = set(self._seen_transactions)
        for index, raw in enumerate(raw_requests):
            if not isinstance(raw, Mapping):
                raise ValueError(
                    f"requests[{index}] 必须是字段映射，收到 "
                    f"{type(raw).__name__}"
                )
            if "currency" in raw and raw["currency"] is not None:
                request_currency = raw["currency"]
                if (
                    not isinstance(request_currency, str)
                    or not request_currency.strip()
                ):
                    raise InvalidCurrencyError(
                        f"requests[{index}] 币种为空"
                    )
                if request_currency.strip() != currency:
                    raise MixedCurrencyError(
                        f"批次币种 {currency} 与 requests[{index}] 币种 "
                        f"{request_currency.strip()} 不一致"
                    )
            if "pool_balance" in raw:
                raise ValueError(
                    f"requests[{index}] 不得携带 pool_balance："
                    f"批量结算使用批次滚动余额"
                )
            try:
                request = self._validate(
                    transaction_id=raw["transaction_id"],
                    currency=currency,
                    pool_balance=opening,
                    settlement_amount=raw["settlement_amount"],
                    notional_exposure=raw["notional_exposure"],
                    base_limit=raw["base_limit"],
                    risk_factor=raw["risk_factor"],
                    creditors=raw["creditors"],
                    supplementary_capital=raw.get(
                        "supplementary_capital", _ZERO
                    ),
                    seen=pending_seen,
                )
            except KeyError as exc:
                raise ValueError(
                    f"requests[{index}] 缺少必填字段: {exc.args[0]}"
                ) from None
            validated.append(request)

        # 整批校验通过：一次性提交全部流水号（异常路径不会执行到这里，
        # 故引擎状态在任何校验失败时保持原样）。
        self._seen_transactions.update(
            request.transaction_id for request in validated
        )

        # 第三层：按序以滚动余额执行限额与清算，逐笔追加审计事件。
        results: list[SettlementResult] = []
        rolling_balance = opening
        for request in validated:
            # 每笔看到的 pool_balance 为当前滚动余额；结果与事件中的
            # validated_available_balance 即该笔处理完后的即时余额。
            current = self._with_pool_balance(request, rolling_balance)
            risk_occupancy = (
                current.settlement_amount
                + current.notional_exposure * current.risk_factor
            )
            if current.settlement_amount > current.pool_balance:
                result = self._build_rejected(
                    current,
                    risk_occupancy,
                    "SETTLEMENT_EXCEEDS_AVAILABLE_BALANCE",
                )
            elif risk_occupancy > current.base_limit:
                result = self._build_rejected(
                    current,
                    risk_occupancy,
                    "RISK_OCCUPANCY_EXCEEDS_BASE_LIMIT",
                )
            else:
                result = self._settle(current, risk_occupancy)

            self._append_audit(current, result)
            results.append(result)
            rolling_balance = result.validated_available_balance

        event_ids = tuple(result.event_id for result in results)
        return BatchSettlementResult(
            results=tuple(results),
            event_ids=event_ids,
            validated_available_balance=rolling_balance,
        )

    @staticmethod
    def _with_pool_balance(
        request: SettlementRequest, pool_balance: Decimal
    ) -> SettlementRequest:
        """返回把池余额替换为滚动余额的请求副本，其余字段不变。"""
        if request.pool_balance == pool_balance:
            return request
        return SettlementRequest(
            transaction_id=request.transaction_id,
            currency=request.currency,
            pool_balance=pool_balance,
            settlement_amount=request.settlement_amount,
            notional_exposure=request.notional_exposure,
            base_limit=request.base_limit,
            risk_factor=request.risk_factor,
            creditors=request.creditors,
            supplementary_capital=request.supplementary_capital,
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
        seen: set[str] | None = None,
    ) -> SettlementRequest:
        """校验并归一化单笔请求。

        ``seen`` 为 ``None`` 时是单笔入口语义：重复判定基于台账，校验通过
        后立即提交流水号（历史行为不变）。批量入口传入"台账 ∪ 批内暂存"
        的集合做重复判定，但不触碰引擎状态；批内流水号由
        :meth:`process_batch` 在整批校验通过后统一提交，保证失败批整体
        回退、不占用任何流水号。
        """
        if not isinstance(transaction_id, str) or not transaction_id.strip():
            raise ValueError("transaction_id 必须是非空字符串")

        # 重复流水号：在任何归一化/状态变更之前判定。
        committed = self._seen_transactions
        dedupe = committed if seen is None else seen
        if transaction_id in dedupe:
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

        # 单笔入口（seen is None）沿用即时登记；批量入口不在这里提交，
        # 由 process_batch 在整批校验通过后一次性登记，失败批整体回退。
        if seen is None:
            self._seen_transactions.add(transaction_id)
        else:
            seen.add(transaction_id)
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
    requests: Sequence[Mapping[str, object]],
) -> BatchSettlementResult:
    """模块级便捷入口：用一次性引擎实例处理一整批请求并返回结果。

    参数与返回结构同 :meth:`ClearingEngine.process_batch`；批次之间不保留
    台账与去重状态（即不跨批次去重）。需要复用审计台账与流水号去重时，
    请直接使用 :class:`ClearingEngine`。
    """
    return ClearingEngine().process_batch(
        currency=currency,
        opening_pool_balance=opening_pool_balance,
        requests=requests,
    )
