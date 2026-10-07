"""Vault Guard 公开数据模型。

金额字段统一使用 :class:`decimal.Decimal`，避免浮点误差；输入在引擎入口处
完成归一化，模型内全部为已校验的不可变值。
"""

from dataclasses import dataclass
from decimal import Decimal
from typing import Mapping, NamedTuple

__all__ = [
    "Creditor",
    "SettlementRequest",
    "CreditorAttribution",
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
    "CurrencyAuditSummary",
    "CreditorBadDebtSummary",
    "RiskGroupBadDebtSummary",
    "BadDebtOperation",
    "BadDebtTrail",
    "AuditEvent",
    "BatchRetryConflict",
    "BatchIdentifierInvalid",
    "BATCH_OUTCOME_RETRY_CONFLICT",
    "BATCH_OUTCOME_INVALID_IDENTIFIER",
    "BATCH_INVALID_REASON_MISSING_BATCH_ID",
    "BATCH_INVALID_REASON_MISSING_EXECUTION_ID",
    "BATCH_INVALID_REASON_UNCOMPUTABLE_DIGEST",
    "LimitReservation",
    "SettlementRecordResult",
    "SettlementAuditEvent",
    "SettlementBatchEvaluation",
    "SETTLEMENT_REASON_ACCEPTED",
    "SETTLEMENT_REASON_LIMIT_EXCEEDED",
]


@dataclass(frozen=True)
class Creditor:
    """单笔优先债权。

    - ``name``：债权标识，用于归因对齐。
    - ``amount``：债权金额，必须非负。
    - ``currency``：可选币种；若提供则必须与账户币种一致，否则混合币种。
    """

    name: str
    amount: Decimal
    currency: str | None = None


@dataclass(frozen=True)
class SettlementRequest:
    """归一化后的单笔结算请求（同一币种）。"""

    transaction_id: str
    currency: str
    pool_balance: Decimal
    settlement_amount: Decimal
    notional_exposure: Decimal
    base_limit: Decimal
    risk_factor: Decimal
    creditors: tuple[Creditor, ...]
    supplementary_capital: Decimal
    risk_group_id: str | None = None

    def input_summary(self) -> Mapping[str, object]:
        """用于审计台账的输入摘要（只包含输入事实，不含结果）。"""
        return {
            "transaction_id": self.transaction_id,
            "currency": self.currency,
            "pool_balance": self.pool_balance,
            "settlement_amount": self.settlement_amount,
            "notional_exposure": self.notional_exposure,
            "base_limit": self.base_limit,
            "risk_factor": self.risk_factor,
            "supplementary_capital": self.supplementary_capital,
            "creditors": tuple(
                (c.name, c.amount, c.currency) for c in self.creditors
            ),
        }


@dataclass(frozen=True)
class CreditorAttribution:
    """单笔债权的逐项归因。

    - ``pool_allocation``：资金池（本次拟清算金额）内分配。
    - ``capital_allocation``：补充资本承担。
    - ``bad_debt``：最终未覆盖坏账。
    """

    creditor: str
    claim_amount: Decimal
    pool_allocation: Decimal
    capital_allocation: Decimal
    bad_debt: Decimal


@dataclass(frozen=True)
class SettlementResult:
    """稳定可重复的公开返回结构，成功与拒绝路径同构。"""

    transaction_id: str
    approved: bool
    validated_available_balance: Decimal
    creditors: tuple[str, ...]
    pool_allocations: tuple[Decimal, ...]
    capital_allocations: tuple[Decimal, ...]
    attributions: tuple[CreditorAttribution, ...]
    uncovered_bad_debt: Decimal
    risk_occupancy: Decimal
    event_id: str
    rejection_reason: str | None
    group_used_after: Decimal | None = None

    @property
    def total_pool_allocated(self) -> Decimal:
        return sum(self.pool_allocations, Decimal(0))

    @property
    def total_capital_allocated(self) -> Decimal:
        return sum(self.capital_allocations, Decimal(0))


@dataclass(frozen=True)
class BatchSettlementResult:
    """多笔批次结算的公开返回结构（不可变）。

    - ``results``：与批次 ``requests`` 同序的单笔 :class:`SettlementResult`；
      其中每笔的 ``validated_available_balance`` 为该笔执行后的即时余额。
    - ``event_ids``：与 ``results`` 同序的审计事件标识；批次本身不产生事件。
    - ``validated_available_balance``：批次全部请求执行完毕后的最终可用余额。
    """

    results: tuple[SettlementResult, ...]
    event_ids: tuple[str, ...]
    validated_available_balance: Decimal


@dataclass(frozen=True)
class SettlementPreview:
    """单笔结算的只读预演结果（不可变）。

    除不含 ``event_id`` 外，各字段与 :class:`SettlementResult` 同名同义；
    预演不生成审计事件，因此没有事件标识。
    """

    transaction_id: str
    approved: bool
    validated_available_balance: Decimal
    creditors: tuple[str, ...]
    pool_allocations: tuple[Decimal, ...]
    capital_allocations: tuple[Decimal, ...]
    attributions: tuple[CreditorAttribution, ...]
    uncovered_bad_debt: Decimal
    risk_occupancy: Decimal
    rejection_reason: str | None
    group_used_after: Decimal | None = None

    @property
    def total_pool_allocated(self) -> Decimal:
        return sum(self.pool_allocations, Decimal(0))

    @property
    def total_capital_allocated(self) -> Decimal:
        return sum(self.capital_allocations, Decimal(0))


@dataclass(frozen=True)
class BatchPreviewResult:
    """同币种多笔结算批次的只读预演结果（不可变）。

    - ``results``：与批次 ``requests`` 同序的单笔 :class:`SettlementPreview`；
      其中每笔的 ``validated_available_balance`` 为该笔预演执行后的即时余额。
    - ``validated_available_balance``：批次全部请求预演完毕后的最终可用余额。

    预演不生成审计事件，因此不含 ``event_ids``。
    """

    results: tuple[SettlementPreview, ...]
    validated_available_balance: Decimal


@dataclass(frozen=True)
class RiskGroupUsage:
    """单个风险组在批次结束时的额度快照（不可变）。

    - ``used``：组内已放行请求的累计风险占用。
    - ``limit``：该组登记的非负上限。
    - ``remaining``：``limit - used``。
    """

    used: Decimal
    limit: Decimal
    remaining: Decimal


@dataclass(frozen=True)
class RiskGroupBatchResult:
    """带风险组累计限额的多笔批次结算公开返回结构（不可变）。

    - ``results``：与批次 ``requests`` 同序的单笔 :class:`SettlementResult`；
      带风险组的每笔另含 ``group_used_after``（该笔执行后的组内已用额度，
      未占用的拒绝结果等于执行前值），无风险组的笔为 ``None``。
    - ``event_ids``：与 ``results`` 同序的审计事件标识；批次本身不产生事件。
    - ``validated_available_balance``：批次全部请求执行完毕后的最终可用余额。
    - ``risk_groups``：引擎已登记的全部风险组到 :class:`RiskGroupUsage`
      的只读映射。
    """

    results: tuple[SettlementResult, ...]
    event_ids: tuple[str, ...]
    validated_available_balance: Decimal
    risk_groups: Mapping[str, RiskGroupUsage]


@dataclass(frozen=True)
class RiskGroupBatchPreviewResult:
    """带风险组累计限额批次的只读预演结果（不可变）。

    - ``results``：与批次 ``requests`` 同序的单笔 :class:`SettlementPreview`；
      带风险组的每笔另含 ``group_used_after``（从已登记组的已用额度起算、
      仅累计本批次会放行的风险占用，该笔预演后的组内已用额度；未占用的
      拒绝结果等于执行前值），无风险组的笔为 ``None``。
    - ``validated_available_balance``：批次全部请求预演完毕后的最终可用余额。
    - ``risk_groups``：合并本次输入限额表与引擎已登记组的只读快照，每项
      含 :class:`RiskGroupUsage`（``used`` / ``limit`` / ``remaining``）；
      未被本批次新增风险占用的组保留已登记已用额度。

    预演不生成审计事件，因此不含 ``event_ids``。
    """

    results: tuple[SettlementPreview, ...]
    validated_available_balance: Decimal
    risk_groups: Mapping[str, RiskGroupUsage]


@dataclass(frozen=True)
class MulticurrencyBatchResult:
    """多币种批次结算的公开返回结构（不可变）。

    - ``results``：与批次 ``requests`` 同序的单笔 :class:`SettlementResult`；
      每笔的 ``validated_available_balance`` 为本币种执行后的即时余额。
    - ``event_ids``：与 ``results`` 同序的审计事件标识；批次本身不产生事件。
    - ``validated_available_balances``：全部币种到最终可用余额的只读映射；
      未被任何请求使用的币种保留期初原值。
    """

    results: tuple[SettlementResult, ...]
    event_ids: tuple[str, ...]
    validated_available_balances: Mapping[str, Decimal]


@dataclass(frozen=True)
class MulticurrencyBatchPreviewResult:
    """多币种结算批次的只读预演结果（不可变）。

    - ``results``：与批次 ``requests`` 同序的单笔 :class:`SettlementPreview`；
      每笔的 ``validated_available_balance`` 为本币种预演执行后的即时余额。
    - ``validated_available_balances``：覆盖全部期初币种的最终可用余额只读
      映射；未被任何请求使用的币种保留期初原值。

    预演不生成审计事件，因此不含 ``event_ids``。
    """

    results: tuple[SettlementPreview, ...]
    validated_available_balances: Mapping[str, Decimal]


@dataclass(frozen=True)
class RecoveryAllocation:
    """一笔回收对单笔存续坏账的冲减明细（按冲减顺序排列）。

    - ``source_transaction_id``：产生该坏账的来源结算流水号。
    - ``creditor``：被冲减的债权名。
    - ``recovered_amount``：本次回收对该笔坏账的冲减额。
    - ``remaining_bad_debt``：冲减后该笔坏账的剩余余额。
    """

    source_transaction_id: str
    creditor: str
    recovered_amount: Decimal
    remaining_bad_debt: Decimal


@dataclass(frozen=True)
class RecoveryResult:
    """存续坏账回收的公开返回结构（不可变）。

    - ``recovery_amount``：归一化后的回收额。
    - ``allocations``：按冲减顺序排列的 :class:`RecoveryAllocation`。
    - ``total_recovered``：本次回收合计（等于 ``recovery_amount``）。
    - ``outstanding_bad_debt``：回收后该币种的存续坏账总额。
    """

    recovery_transaction_id: str
    currency: str
    recovery_amount: Decimal
    allocations: tuple[RecoveryAllocation, ...]
    total_recovered: Decimal
    outstanding_bad_debt: Decimal
    event_id: str


@dataclass(frozen=True)
class WriteoffAllocation:
    """一笔核销对单笔存续坏账的核销明细（按核销顺序排列）。

    - ``source_transaction_id``：产生该坏账的来源结算流水号。
    - ``creditor``：被核销的债权名。
    - ``written_off_amount``：本次核销对该笔坏账的核销额。
    - ``remaining_bad_debt``：核销后该笔坏账的剩余余额。
    """

    source_transaction_id: str
    creditor: str
    written_off_amount: Decimal
    remaining_bad_debt: Decimal


@dataclass(frozen=True)
class WriteoffResult:
    """存续坏账核销的公开返回结构（不可变）。

    - ``writeoff_amount``：归一化后的核销额。
    - ``allocations``：按核销顺序排列的 :class:`WriteoffAllocation`。
    - ``total_written_off``：本次核销合计（等于 ``writeoff_amount``）。
    - ``outstanding_bad_debt``：核销后该币种的存续坏账总额。
    """

    writeoff_transaction_id: str
    currency: str
    writeoff_amount: Decimal
    allocations: tuple[WriteoffAllocation, ...]
    total_written_off: Decimal
    outstanding_bad_debt: Decimal
    event_id: str


@dataclass(frozen=True)
class OutstandingBadDebt:
    """查询时点的一笔存续坏账余额。

    - ``source_transaction_id``：产生该坏账的来源结算流水号。
    - ``balance``：该笔坏账的剩余余额。
    """

    source_transaction_id: str
    creditor: str
    currency: str
    balance: Decimal


@dataclass(frozen=True)
class CurrencyAuditSummary:
    """单币种审计核对快照（不可变，只读查询结果）。

    按审计顺序汇总该币种事件：

    - ``settlement_count``：结算事件总数（放行与拒绝）。
    - ``approved_count`` / ``rejected_count``：放行 / 拒绝结算事件数。
    - ``recovery_count``：回收事件数。
    - ``writeoff_count``：核销事件数。
    - ``approved_risk_occupancy``：仅放行请求的风险占用合计。
    - ``pool_allocated``：放行请求池内分配按明细求和。
    - ``capital_allocated``：放行请求补充资本按明细求和。
    - ``initial_bad_debt``：放行时确认的首次坏账（未覆盖坏账）合计。
    - ``recovered_amount``：回收冲减额合计（只计回收事件）。
    - ``written_off_amount``：核销冲减额合计（只计核销事件）。
    - ``outstanding_bad_debt``：该币种存续坏账余额合计，恒等于
      ``initial_bad_debt - recovered_amount - written_off_amount``。
    - ``rejection_counts``：按原因码排序的不可变原因次数（省略零次原因）。
    - ``event_ids``：该币种全部审计事件标识，保持台账顺序。
    """

    currency: str
    settlement_count: int
    approved_count: int
    rejected_count: int
    recovery_count: int
    writeoff_count: int
    approved_risk_occupancy: Decimal
    pool_allocated: Decimal
    capital_allocated: Decimal
    initial_bad_debt: Decimal
    recovered_amount: Decimal
    written_off_amount: Decimal
    outstanding_bad_debt: Decimal
    rejection_counts: Mapping[str, int]
    event_ids: tuple[str, ...]


class CreditorBadDebtSummary(NamedTuple):
    """单个债权人维度的坏账汇总（不可变，只读查询结果）。

    合并同一债权人名下的多来源坏账记录，金额均为 :class:`Decimal`：

    - ``creditor``：债权人名（与台账归因中的名称完全一致）。
    - ``source_transaction_ids``：放行归因坏账大于零的来源结算流水号，按
      审计事件顺序去重（同一流水号在多笔债权归因中只出现一次）。
    - ``recovery_transaction_ids``：实际冲减到该债权人的回收流水号，按
      审计事件顺序去重；空明细零额回收不归属任何债权人。
    - ``writeoff_transaction_ids``：实际核销到该债权人的核销流水号，按
      审计事件顺序去重；空明细零额核销不归属任何债权人。
    - ``initial_bad_debt``：放行归因中该债权人名下大于零坏账的合计。
    - ``recovered_amount``：现有回收分配中归属该债权人的冲减额合计。
    - ``written_off_amount``：现有核销分配中归属该债权人的核销额合计。
    - ``outstanding_bad_debt``：
      ``initial_bad_debt - recovered_amount - written_off_amount``，
      恒等于 :meth:`ClearingEngine.outstanding_bad_debts` 中该债权人的
      来源余额合计；全额结清的债权人保留该行（该项为 0）。
    """

    creditor: str
    source_transaction_ids: tuple[str, ...]
    recovery_transaction_ids: tuple[str, ...]
    writeoff_transaction_ids: tuple[str, ...]
    initial_bad_debt: Decimal
    recovered_amount: Decimal
    written_off_amount: Decimal
    outstanding_bad_debt: Decimal


class RiskGroupBadDebtSummary(NamedTuple):
    """单个风险组维度的坏账责任汇总（不可变，只读查询结果）。

    汇总该币种下携带同一 ``risk_group_id`` 的结算及其坏账处理记录；
    金额为 :class:`Decimal`，计数为 ``int``：

    - ``risk_group_id``：风险组标识（与登记时一致）。
    - ``settlement_count`` / ``approved_count`` / ``rejected_count``：
      组内结算事件总数与放行 / 拒绝数。
    - ``risk_occupancy``：仅放行请求的风险占用合计；拒绝仅计数。
    - ``initial_bad_debt``：组内放行归因坏账（大于零者）合计。
    - ``recovered_amount`` / ``written_off_amount``：按来源结算回溯
      本组的回收 / 核销（含定向操作）冲减额合计；空明细零额操作不计。
    - ``outstanding_bad_debt``：
      ``initial_bad_debt - recovered_amount - written_off_amount``，
      与组内各来源在 :meth:`ClearingEngine.outstanding_bad_debts` 中的
      余额合计一致。
    - ``source_transaction_ids`` / ``recovery_transaction_ids`` /
      ``writeoff_transaction_ids``：三类流水号按首次出现顺序去重；
      来源仅含放行归因坏账大于零的结算。
    - ``creditor_summaries``：组内按债权人名称字典序排列的
      :class:`CreditorBadDebtSummary` 元组，口径同债权人报告；组内
      各债权人存续坏账之和等于本组 ``outstanding_bad_debt``。
    """

    risk_group_id: str
    settlement_count: int
    approved_count: int
    rejected_count: int
    risk_occupancy: Decimal
    initial_bad_debt: Decimal
    recovered_amount: Decimal
    written_off_amount: Decimal
    outstanding_bad_debt: Decimal
    source_transaction_ids: tuple[str, ...]
    recovery_transaction_ids: tuple[str, ...]
    writeoff_transaction_ids: tuple[str, ...]
    creditor_summaries: tuple[CreditorBadDebtSummary, ...]


class BadDebtOperation(NamedTuple):
    """坏账轨迹上的一条回收或核销操作（不可变，只读查询结果）。

    仅收录明细中来源结算流水号命中的记录；同一债权多次部分处理不合并、
    不覆盖，每条单独保留：

    - ``operation_id``：回收或核销自身的业务流水号。
    - ``creditor``：被冲减 / 核销的债权名。
    - ``amount``：本次操作对该债权的回收或核销金额。
    - ``remaining``：该债权在本次操作处理后的坏账余额。
    """

    operation_id: str
    creditor: str
    amount: Decimal
    remaining: Decimal


class BadDebtTrail(NamedTuple):
    """按来源结算流水号串联的坏账处理只读轨迹（不可变）。

    - ``transaction_id``：命中的已登记来源结算流水号（精确匹配）。
    - ``initial_bad_debt``：该来源结算放行时确认的首次坏账，即原结算的
      ``uncovered_bad_debt``；拒绝或未形成坏账的放行结算为 0。
    - ``recovered_amount``：命中该来源的全部回收明细金额之和。
    - ``written_off_amount``：命中该来源的全部核销明细金额之和。
    - ``outstanding_bad_debt``：
      ``initial_bad_debt - recovered_amount - written_off_amount``，
      与 :meth:`ClearingEngine.outstanding_bad_debts` 中该来源的同来源
      余额合计一致。
    - ``recoveries`` / ``writeoffs``：按审计顺序、再按事件内明细顺序
      排列的 :class:`BadDebtOperation` 元组，只保留来源命中的记录；
      拒绝结算或未形成坏账时均为空元组。
    """

    transaction_id: str
    initial_bad_debt: Decimal
    recovered_amount: Decimal
    written_off_amount: Decimal
    outstanding_bad_debt: Decimal
    recoveries: tuple[BadDebtOperation, ...]
    writeoffs: tuple[BadDebtOperation, ...]


@dataclass(frozen=True)
class AuditEvent:
    """内存追加式审计台账中的一条事件。

    每个请求（放行或拒绝）恰好产生一条事件；输入校验异常不产生事件。
    """

    event_id: str
    sequence: int
    transaction_id: str
    approved: bool
    currency: str
    input_summary: Mapping[str, object]
    validation_result: str
    risk_occupancy: Decimal
    pool_allocations: tuple[tuple[str, Decimal], ...]
    capital_allocations: tuple[tuple[str, Decimal], ...]
    uncovered_bad_debt: Decimal
    validated_available_balance: Decimal
    rejection_reason: str | None
    # 回收事件的冲减明细：
    # (来源结算流水号, 债权名, 本次冲减额, 剩余坏账)；结算与核销事件恒为空元组。
    recovery_allocations: tuple[tuple[str, str, Decimal, Decimal], ...] = ()
    # 核销事件的核销明细：
    # (来源结算流水号, 债权名, 本次核销额, 剩余坏账)；结算与回收事件恒为空元组。
    writeoff_allocations: tuple[tuple[str, str, Decimal, Decimal], ...] = ()


# --------------------------------------------------------------------------- #
# 批次重试与断点恢复
# --------------------------------------------------------------------------- #

# 相同批次标识配不同请求内容：在任何资金或审计副作用之前拒绝。
BATCH_OUTCOME_RETRY_CONFLICT = "BATCH_RETRY_CONFLICT"
# 批次标识 / 执行标识缺失、为空或请求摘要无法计算：在任何副作用之前拒绝。
BATCH_OUTCOME_INVALID_IDENTIFIER = "BATCH_IDENTIFIER_INVALID"

# BatchIdentifierInvalid.reason 的机器可读原因码。
BATCH_INVALID_REASON_MISSING_BATCH_ID = "MISSING_BATCH_ID"
BATCH_INVALID_REASON_MISSING_EXECUTION_ID = "MISSING_EXECUTION_ID"
BATCH_INVALID_REASON_UNCOMPUTABLE_DIGEST = "UNCOMPUTABLE_REQUEST_DIGEST"


@dataclass(frozen=True)
class BatchRetryConflict:
    """批次重试冲突的唯一拒绝结果（不可变）。

    同一稳定批次标识此前已登记，但本次请求内容的确定性摘要与首次不同：
    系统在产生任何资金或审计副作用之前拒绝本次执行。

    - ``outcome``：固定为 :data:`BATCH_OUTCOME_RETRY_CONFLICT`。
    - ``batch_id``：冲突批次的稳定标识。
    - ``original_request_digest``：首次登记请求内容的摘要。
    - ``incoming_request_digest``：本次请求内容的摘要。
    - ``original_execution_id``：首次执行标识。
    """

    batch_id: str
    original_request_digest: str
    incoming_request_digest: str
    original_execution_id: str
    outcome: str = BATCH_OUTCOME_RETRY_CONFLICT


@dataclass(frozen=True)
class BatchIdentifierInvalid:
    """批次标识无效的唯一拒绝结果（不可变）。

    批次标识或执行标识缺失、为空（去首尾空白后），或请求摘要无法计算时，
    系统在产生任何资金或审计副作用之前拒绝本次执行。

    - ``outcome``：固定为 :data:`BATCH_IDENTIFIER_INVALID`。
    - ``reason``：机器可读的无效原因码：``MISSING_BATCH_ID`` /
      ``MISSING_EXECUTION_ID`` / ``UNCOMPUTABLE_REQUEST_DIGEST``。
    - ``batch_id``：可观察到的批次标识；标识本身缺失或为空时为 ``None``。
    """

    reason: str
    batch_id: str | None = None
    outcome: str = BATCH_OUTCOME_INVALID_IDENTIFIER


# --------------------------------------------------------------------------- #
# 清算批次组合限额试算（evaluate_settlement_batch）
# --------------------------------------------------------------------------- #

# 受理记录的机器可读原因码。
SETTLEMENT_REASON_ACCEPTED = "ACCEPTED"
# 单层限额不足导致整笔拒绝的机器可读原因码（可预期拒绝，不抛异常）。
SETTLEMENT_REASON_LIMIT_EXCEEDED = "LIMIT_EXCEEDED"


@dataclass(frozen=True)
class LimitReservation:
    """单层累计占额限额在批次试算后的快照（不可变）。

    - ``limit``：风险策略为该标识登记的非负上限；标识仅出现在占额快照而
      策略未登记时为 ``None``。
    - ``initial_reserved``：占额快照给出的期初已占额度（缺省为 0）。
    - ``accepted_reserved``：本批次受理记录累计预占的额度。
    - ``rejected_reserved``：本批次引用该限额但被整笔拒绝的记录金额合计
      （拒绝不占用额度，仅作审计统计）。
    - ``final_reserved``：``initial_reserved + accepted_reserved``，
      即批次试算后的累计占额。
    """

    limit: Decimal | None
    initial_reserved: Decimal
    accepted_reserved: Decimal
    rejected_reserved: Decimal
    final_reserved: Decimal


@dataclass(frozen=True)
class SettlementRecordResult:
    """清算批次中单条记录的试算结果（不可变），受理与拒绝同构。

    - ``settlement_id`` / ``debtor_id`` / ``treasury_id`` / ``priority`` /
      ``amount``：归一化后的记录事实（``treasury_id`` 可缺省为 ``None``）。
    - ``accepted``：两层限额均有余量时整笔受理。
    - ``reason_code``：``ACCEPTED`` 或 ``LIMIT_EXCEEDED``。
    - ``creditors`` / ``pool_allocations`` / ``capital_allocations`` /
      ``attributions`` / ``uncovered_bad_debt``：清算瀑布集合与坏账归因，
      口径同 :class:`SettlementResult`；拒绝记录不进瀑布，各层分配全为 0、
      不确认坏账。
    """

    settlement_id: str
    debtor_id: str
    treasury_id: str | None
    priority: int
    amount: Decimal
    accepted: bool
    reason_code: str
    creditors: tuple[str, ...]
    pool_allocations: tuple[Decimal, ...]
    capital_allocations: tuple[Decimal, ...]
    attributions: tuple[CreditorAttribution, ...]
    uncovered_bad_debt: Decimal

    @property
    def total_pool_allocated(self) -> Decimal:
        return sum(self.pool_allocations, Decimal(0))

    @property
    def total_capital_allocated(self) -> Decimal:
        return sum(self.capital_allocations, Decimal(0))


@dataclass(frozen=True)
class SettlementAuditEvent:
    """清算批次试算的一条 reason_code 审计事件（不可变）。

    每条记录（受理或拒绝）恰好产生一条事件；``sequence`` 为批内按处理
    顺序（priority 升序、同优先级按 settlement_id 码点升序）从 1 递增的
    序号。事件只随试算结果返回，不写入引擎的既有审计台账。
    """

    event_id: str
    sequence: int
    batch_id: str
    settlement_id: str
    accepted: bool
    reason_code: str
    amount: Decimal
    debtor_id: str
    treasury_id: str | None
    priority: int


@dataclass(frozen=True)
class SettlementBatchEvaluation:
    """清算批次组合限额试算的公开返回结构（不可变）。

    - ``batch_id`` / ``currency``：归一化后的批次标识与币种。
    - ``results``：按处理顺序（priority 升序、同优先级按 settlement_id
      码点升序）排列的逐记录 :class:`SettlementRecordResult`。
    - ``treasury_limits`` / ``debtor_limits``：两层各标识到
      :class:`LimitReservation` 的只读映射，覆盖风险策略与占额快照中
      出现的全部标识，按标识码点升序排列。
    - ``audit_events``：与 ``results`` 同序的 reason_code 审计事件。
    - ``accepted_count`` / ``rejected_count``：受理 / 拒绝记录数。
    """

    batch_id: str
    currency: str
    results: tuple[SettlementRecordResult, ...]
    treasury_limits: Mapping[str, LimitReservation]
    debtor_limits: Mapping[str, LimitReservation]
    audit_events: tuple[SettlementAuditEvent, ...]
    accepted_count: int
    rejected_count: int
