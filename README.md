# Vault Guard

金库与清算风控引擎：限额校验、清算瀑布、坏账归因与审计台账。

## 范围

本仓库从零开始实现上述方向的可用工具，不依赖外部同类实现，仅使用 Python
标准库（要求 Python 3.10+）。

结算请求默认在**同一币种**内处理：单笔走 `process`，多笔批次走
`process_batch`（批次内共享币种与滚动余额）；多币种批次走
`process_multicurrency_batch`（每个请求自带币种，按币种独立记账，不换汇、
不使用汇率）。外部定价与货币换算不在范围内，单笔请求内出现混合币种直接
抛出 `MixedCurrencyError`。无任何落盘行为，审计台账以内存中的追加式序列
表示。

## 公开入口

```python
from decimal import Decimal
from vault_guard import ClearingEngine

engine = ClearingEngine()
result = engine.process(
    transaction_id="TX-001",          # 唯一业务流水号
    currency="USD",                   # 账户币种
    pool_balance=Decimal("100"),      # 资金池可用余额
    settlement_amount=Decimal("50"),  # 本次拟清算金额
    notional_exposure=Decimal("100"), # 账户名义敞口
    base_limit=Decimal("100"),        # 账户基础限额
    risk_factor=Decimal("0.5"),       # 风险系数，取值 [0, 1]
    creditors=[                       # 优先债权清单，按先后顺序受偿
        ("senior", Decimal("60")),
        ("mezzanine", Decimal("30")),
        ("equity", Decimal("10")),
    ],
    supplementary_capital=Decimal("25"),  # 补充资本，默认 0
)
```

`creditors` 每项支持三种等价形式：`(name, amount)`、
`(name, amount, currency)`、`{"name": ..., "amount": ..., "currency": ...}`
或 `vault_guard.Creditor` 对象。金额接受 `int` / `float` / `Decimal`，
内部统一精确归一化为 `Decimal`（`float` 按其字符串形式转换，如 `0.6` →
`Decimal("0.6")`）；NaN、无穷及非数值类型抛出 `ValueError`。

需要一次性处理时可使用模块级便捷函数
`vault_guard.process_settlement(...)`（参数与返回结构相同，但不保留跨请求
台账与去重状态）。

## 多笔批次结算

`ClearingEngine.process_batch(currency, opening_pool_balance, requests)` 在
同一币种下按请求顺序处理多笔结算：

```python
batch = engine.process_batch(
    currency="USD",                    # 批次币种，适用于全部请求
    opening_pool_balance=Decimal("100"),  # 批次期初资金池余额
    requests=[
        {   # 字段沿用 process（不含 currency / pool_balance）
            "transaction_id": "TX-101",
            "settlement_amount": Decimal("40"),
            "notional_exposure": Decimal("0"),
            "base_limit": Decimal("100"),
            "risk_factor": Decimal("0"),
            "creditors": [("senior", Decimal("60"))],
            "supplementary_capital": Decimal("0"),  # 可选，默认 0
        },
        # ... 后续请求
    ],
)
```

- 各请求的 `pool_balance` 取**滚动余额**：从 `opening_pool_balance` 开始，
  每笔执行后的可用余额即下一笔的输入余额；拒绝的请求不改余额。
- 批次先做整体校验，随后按序逐笔执行限额校验与清算；每个过校验请求
  （放行或拒绝）追加一条现有结构审计事件，序号递增，批次本身不建事件。
- 校验失败（异常）不生成事件、不占流水号、不改余额，并回退批内已产生
  的全部状态，修正后可整体重提。
- 异常顺序：空批次 → `EmptyBatchError`；缺币种 → `InvalidCurrencyError`；
  债权币种不一致 → `MixedCurrencyError`；流水号批内或与台账重复 →
  `DuplicateTransactionError`；空债权清单 / 风险系数越界 →
  `EmptyCreditorListError` / `InvalidRiskFactorError`；错误数值 →
  `ValueError`。

返回不可变的 `BatchSettlementResult`：

| 字段 | 含义 |
| --- | --- |
| `results` | 与 `requests` 同序的 `SettlementResult` 元组；每笔的 `validated_available_balance` 为该笔执行后的即时余额 |
| `event_ids` | 与 `results` 同序的审计事件标识 |
| `validated_available_balance` | 批次全部执行后的最终可用余额 |

模块级 `vault_guard.process_settlement_batch(currency,
opening_pool_balance, requests)` 使用一次性引擎返回结果，不跨批次去重。

### 提交前只读预演

三个预演入口分别与对应的正式批次入口使用相同的输入、校验顺序、异常类型、
滚动余额与清算瀑布做只读预演，只报告当前台账下的执行结果，不代替提交：

```python
# 同币种批次，对应 process_batch
preview = engine.preview_batch("USD", Decimal("100"), requests)

# 风险组批次，对应 process_risk_group_batch
preview = engine.preview_risk_group_batch(
    "USD", Decimal("100"), {"G1": Decimal("100")}, requests
)

# 多币种批次，对应 process_multicurrency_batch
preview = engine.preview_multicurrency_batch(
    {"USD": Decimal("100"), "EUR": Decimal("50")}, requests
)
```

- 逐笔返回 `SettlementPreview`（除无 `event_id` 外与 `SettlementResult`
  同名字段同义），与 `requests` 同序；预演不生成审计事件，因此返回结构都
  没有 `event_ids`：
  - `preview_batch` → 不可变 `BatchPreviewResult`，另含预演最终余额
    `validated_available_balance`；
  - `preview_risk_group_batch` → 不可变 `RiskGroupBatchPreviewResult`，
    另含最终余额与 `risk_groups` 快照；
  - `preview_multicurrency_batch` → 不可变
    `MulticurrencyBatchPreviewResult`，另含覆盖全部期初币种的
    `validated_available_balances`。
- 风险组预演从已登记组的 `used` 起算，按请求顺序只累计会放行的风险占用：
  `GROUP_LIMIT_EXCEEDED`、余额不足与基础限额拒绝沿用各自原原因码，拒绝不
  增组额度（带组笔的 `group_used_after` 等于执行前值），无风险组请求只走
  单笔规则。`risk_groups` 合并本次输入风险组与引擎已登记组，每项含
  `used` / `limit` / `remaining`，未在本批使用的组保留已登记已用额度；
  同组上限冲突、未登记组、空组标识或非法上限抛 `InvalidRiskGroupError`。
- 多币种预演各币种余额独立滚动，不换汇、不使用汇率；未使用币种在
  `validated_available_balances` 中保留期初原值。
- 只读且幂等：不追加 `audit_log` 事件、不占流水号、不改变 `result_of` /
  `has_transaction` / `recovery_of` / 余额、风险组额度、坏账余额与审计
  核对；重复或交叉调用结果一致。
- 业务拒绝不抛异常，对应项给出 `approved=False`、`rejection_reason`、输入
  余额与零分配；放行、风险占用与坏账归因同正式提交。
- 校验失败（含空批次 `EmptyBatchError`、缺币种 `InvalidCurrencyError`、
  混合币种 `MixedCurrencyError`、重复流水号 `DuplicateTransactionError`、
  空债权清单 `EmptyCreditorListError`、风险系数越界
  `InvalidRiskFactorError`、非法金额 `ValueError`）不留任何状态。
- 以相同输入随后调用对应正式入口，逐笔状态、拒绝原因、`group_used_after`、
  余额与风险组额度与预演一致，正式结果只增 `event_id` 并写台账。

## 多币种批次结算

`ClearingEngine.process_multicurrency_batch(opening_pool_balances,
requests)` 在同一批次内按币种独立记账处理多笔结算，不换汇、不使用汇率：

```python
batch = engine.process_multicurrency_batch(
    opening_pool_balances={           # 币种 -> 非负期初资金池余额
        "USD": Decimal("100"),
        "EUR": Decimal("50"),
    },
    requests=[
        {   # 字段沿用 process_batch 单项，并增加 currency
            "transaction_id": "TX-201",
            "currency": "USD",        # 必须存在于期初余额映射
            "settlement_amount": Decimal("40"),
            "notional_exposure": Decimal("0"),
            "base_limit": Decimal("100"),
            "risk_factor": Decimal("0"),
            "creditors": [("senior", Decimal("60"))],
        },
        # ... 后续请求，可混排不同币种
    ],
)
```

- 每个请求只动用自身币种的滚动余额：放行按池内实际分配扣款，拒绝不动
  余额；各币种互不影响。
- 批次先做整体校验再执行；校验失败不生成事件、不占流水号、不改余额或
  坏账台账，修正后可整体重提。
- 异常顺序：空请求清单 → `EmptyBatchError`；期初映射非法、余额键空白或
  任何数值非法 → `ValueError`；请求币种缺失、空白或不在期初映射中 →
  `InvalidCurrencyError`；单笔债权混合币种 → `MixedCurrencyError`；
  流水号批内或与台账重复 → `DuplicateTransactionError`；空债权清单 →
  `EmptyCreditorListError`；风险系数越界 → `InvalidRiskFactorError`。
- 逐笔规则（限额、瀑布、补充资本、坏账归因、两种拒绝原因码）与单币种
  批次一致；放行或拒绝各产生一条既有结构审计事件，拒绝后继续处理。
- 未覆盖坏账按各自币种进入存续坏账台账，可由 `process_recovery` 冲减、
  由 `process_writeoff` 核销结清。

返回不可变的 `MulticurrencyBatchResult`：

| 字段 | 含义 |
| --- | --- |
| `results` | 与 `requests` 同序的 `SettlementResult` 元组；每笔的 `validated_available_balance` 为本币种执行后的即时余额 |
| `event_ids` | 与 `results` 同序的审计事件标识 |
| `validated_available_balances` | 全部币种到最终可用余额的只读映射；未被请求使用的币种保留期初原值 |

模块级 `vault_guard.process_settlement_multicurrency_batch(
opening_pool_balances, requests)` 使用一次性引擎返回结果，不跨批次保留
状态。

## 批次重试与断点恢复

已经进入清算处理的批次可以通过三个**可重试入口**提交，分别与三个正式
批次入口一一对应（清算输入、金额精度、业务时间语义、滚动余额、限额命中
口径、清偿顺序、坏账归属与审计记录完全沿用既有实现）：

```python
# 同币种批次，对应 process_batch
batch = engine.process_batch_retry(
    "BATCH-2026-001",       # 稳定批次标识：同一批次的首次与全部重试相同
    "exec-0007",            # 本次执行标识：重试时可不同
    "USD", Decimal("100"), requests,
)

# 风险组批次，对应 process_risk_group_batch
batch = engine.process_risk_group_batch_retry(
    "BATCH-2026-002", "exec-0001",
    "USD", Decimal("100"), {"G1": Decimal("100")}, requests,
)

# 多币种批次，对应 process_multicurrency_batch
batch = engine.process_multicurrency_batch_retry(
    "BATCH-2026-003", "exec-0001",
    {"USD": Decimal("100"), "EUR": Decimal("50")}, requests,
)
```

- 系统先按 `batch_id` 检查该批次是否处理过，再按既有入口顺序完成整体
  校验、逐笔限额校验、清算瀑布分配、坏账归因与审计记账。
- **首次成功**返回原有批次结果（`BatchSettlementResult` /
  `RiskGroupBatchResult` / `MulticurrencyBatchResult`），其内容与对应
  既有正式入口逐字段一致。
- **内容一致的重试**（相同 `batch_id` + 相同请求内容，无论
  `execution_id` 是否相同）返回与首次**完全相同**的清算结果（同一结果
  对象）和审计内容：不重复扣减、不重复归因、不重复追加审计记录，
  `audit_log`、流水号占用、风险组额度、坏账台账与审计核对均不再变化。
  金额按引擎既有归一化口径比较：`int` / `float` / `Decimal` 的等价金额
  （如 `40` / `40.0` / `Decimal("40.00")`）视为相同内容。
- **断点恢复**：处理中断后再次提交相同请求，从首个尚未完成的步骤继续；
  已完成步骤不再次产生副作用（不出现两套金额分配或两条等价审计轨迹）。
  单步执行意外失败时只回滚该步骤（事件、序号、流水号、该步风险组占用、
  坏账台账），下一轮从同一请求重新执行。
- **并发提交**：同一 `batch_id` 的并发请求只允许一个推进处理，其余请求
  等待并取得同一最终结果；不同批次互不阻塞。

重试入口在以下两种情况下**在产生任何资金或审计副作用之前**拒绝，返回
唯一的拒绝结果（不抛异常、不登记批次、不写入台账），且不与既有失败 /
部分成功 / 金额不足等业务结果混用：

| 结果 | `outcome` 常量 | 触发情况 |
| --- | --- | --- |
| `BatchRetryConflict` | `BATCH_RETRY_CONFLICT` | 相同 `batch_id` 配**不同请求内容**；可观察到 `batch_id`、`original_request_digest`、`incoming_request_digest`（sha256 十六进制摘要）与 `original_execution_id` |
| `BatchIdentifierInvalid` | `BATCH_IDENTIFIER_INVALID` | `batch_id` / `execution_id` 缺失或去首尾空白后为空（`reason` 分别为 `MISSING_BATCH_ID` / `MISSING_EXECUTION_ID`），或请求摘要无法计算（`UNCOMPUTABLE_REQUEST_DIGEST`） |

- 冲突拒绝不改变任何状态；之后以与首次相同的内容重试，仍返回首次结果。
- 批次内容本身的既有校验异常（空批次 `EmptyBatchError`、缺币种
  `InvalidCurrencyError`、混合币种 `MixedCurrencyError`、重复流水号
  `DuplicateTransactionError`、空债权清单 `EmptyCreditorListError`、
  风险系数越界 `InvalidRiskFactorError`、非法金额 `ValueError` 等）仍按
  原口径抛出，不被新拒绝结果替代；校验失败不登记批次、不产生副作用，
  修正后可用同一 `batch_id` 重新首次提交。
- 重试语义只在引擎实例内内存中保留，无落盘行为；三个可重试入口仅提供
  `ClearingEngine` 方法，没有模块级一次性函数。

## 清算批次组合限额试算

`ClearingEngine.evaluate_settlement_batch(batch_id, currency,
reservation_snapshot, risk_policy, records)` 以占额快照与风险策略为输入，
对 `treasury_id` 与 `debtor_id` 两层累计占额做组合限额预占与确定性试算
（试算依赖跨批次结算标识去重，仅提供引擎方法，无模块级一次性入口）：

```python
evaluation = engine.evaluate_settlement_batch(
    batch_id="BATCH-900",            # 批次标识
    currency="USD",                  # 批次币种
    reservation_snapshot={           # 占额快照：本批之前的累计已占额
        "treasury": {"T1": Decimal("50")},
        "debtor": {"D1": Decimal("25")},
    },
    risk_policy={                    # 风险策略：两层标识 -> 非负上限
        "treasury_limits": {"T1": Decimal("1000")},
        "debtor_limits": {"D1": Decimal("500")},
    },
    records=[
        {   # settlement_id / debtor_id / amount / priority 必填，treasury_id 可选
            "settlement_id": "STL-001",
            "debtor_id": "D1",
            "treasury_id": "T1",     # 缺省则不占用 treasury 层
            "amount": Decimal("100"),           # 有限正数
            "priority": 1,                      # 整数
            "currency": "USD",                  # 可选，缺省视为批次币种
            "creditors": [("senior", Decimal("60"))],  # 可选，三种既有形式
            "supplementary_capital": Decimal("0"),     # 可选，默认 0
        },
        # ... 后续记录
    ],
)
```

- **处理顺序**：记录按 `priority` 升序、同优先级按 `settlement_id` 的
  Unicode 码点升序处理；相同输入结果一致。
- **两层预占**：先加快照占额，再加本批已受理金额；`treasury_id` 存在时
  treasury 层与 debtor 层限额都有余量才整笔受理（边界取等号受理），任一
  不足则整笔拒绝，`reason_code` 为 `LIMIT_EXCEEDED`，不得部分受理。
  单层限额不足是可预期拒绝，不抛异常。
- **拒绝语义**：拒绝记录不进瀑布、不产生坏账、不改占额，也不影响后续
  记录；其金额仅计入所引用限额的 `rejected_reserved` 统计。
- **瀑布与坏账**：受理记录以 `amount` 为资金池按既有清算瀑布分配，
  `supplementary_capital` 补足未受偿债权，逐项归因坏账（口径同
  `SettlementResult`）。

返回不可变的 `SettlementBatchEvaluation`：

| 字段 | 含义 |
| --- | --- |
| `batch_id` / `currency` | 归一化后的批次标识与币种 |
| `results` | 按处理顺序排列的 `SettlementRecordResult`（受理状态、`reason_code`、瀑布分配与坏账归因） |
| `treasury_limits` / `debtor_limits` | 两层各标识到 `LimitReservation` 的只读映射（`initial_reserved` / `accepted_reserved` / `rejected_reserved` / `final_reserved`），覆盖策略与快照中出现的全部标识 |
| `audit_events` | 与 `results` 同序的 `SettlementAuditEvent`（每条记录恰好一条，含 `reason_code`） |
| `accepted_count` / `rejected_count` | 受理 / 拒绝记录数 |

- **试算语义**：占额快照、风险策略与记录等输入只服务本次调用——不写
  资金池、坏账台账、审计台账、风险组额度或流水号登记，仅把本批
  `settlement_id` 登记到引擎内存用于跨批次去重；不新增文件、数据库表
  或消息约定。
- **异常**（不产生受理、坏账或审计结果，也不登记任何结算标识，修正后
  可整体重提）：缺批次标识、币种或风险策略 → `InvalidSettlementBatchError`；
  `settlement_id` 批内或已登记重复 → `DuplicateSettlementIdError`；
  记录引用的 `treasury_id` / `debtor_id` 未在策略中登记限额 →
  `RiskPolicyNotFoundError`；记录币种与批次币种不一致 →
  `CurrencyMismatchError`；`amount` 非有限正数 →
  `InvalidSettlementAmountError`；`priority` 非整数 →
  `InvalidSettlementPriorityError`。

## 处理规则（确定顺序）

1. **输入校验**：负数金额/余额、重复流水号、缺币种、空债权清单、系数越界
   分别抛出对应异常（见下），不静默修正输入。
2. **限额校验**：风险占用 = 拟清算金额 + 名义敞口 × 风险系数。
   - 拟清算金额 > 可用余额，或风险占用 > 基础限额 → 拒绝；
   - 拒绝时 `approved=False`、可用余额不变、各层分配全为 0、不确认坏账，
     并生成一条拒绝审计（`rejection_reason` 区分两种原因）。
   - 边界取等号放行；基础限额为 0 时仅允许风险占用为 0 的请求通过。
3. **清算瀑布**：请求通过后，资金池（本次拟清算金额）按优先债权清单先后
   顺序分配，高优先级债权先受偿；每层实际分配不超过其债权金额与剩余资金。
4. **补充资本**：在资金池之后，仍按清单顺序补足各债权的剩余未偿部分，
   直至资本用尽。
5. **坏账归因**：最终未覆盖坏账 = 两层分配后仍未受偿的债权总额；逐项归因
   给出每笔债权的池内分配、资本承担与坏账金额，逐项坏账合计恒等于未覆盖
   坏账，且对每笔债权满足 `池内 + 资本 + 坏账 = 债权金额`。

## 存续坏账回收

已放行结算留下的未覆盖债权进入存续坏账台账，可用
`ClearingEngine.process_recovery(recovery_transaction_id, currency,
recovery_amount)` 提交回收（回收依赖台账状态，仅提供引擎方法）：

```python
recovery = engine.process_recovery("RC-001", "USD", Decimal("25"))
```

- 冲减顺序：先按审计事件顺序、再按债权清单顺序，只冲减币种相符且仍有
  坏账的债权；前项清零后处理后项，一笔回收可部分覆盖单项坏账。
- 返回不可变 `RecoveryResult`：回收流水号、币种、回收额、按冲减顺序排列
  的 `RecoveryAllocation`（来源结算流水号、债权名、本次冲减、剩余坏账）、
  回收合计、回收后该币种存续坏账与事件标识。
- 历史 `SettlementResult` 不回写；回收成功追加一条标识为
  `EVT-{recovery_transaction_id}-recovery` 的审计事件（序号继续递增，
  `recovery_allocations` 保存同额明细；结算事件该字段恒为空元组）。
- 有坏账时零额回收生成一条空明细事件；失败（重复流水号 / 缺币种 / 非法
  金额 / 无存续坏账 / 超额）不生成事件、不占流水号、不改状态。
- 只读查询不触发清算：`engine.recovery_of(recovery_transaction_id)` 返回
  `RecoveryResult` 或 `None`；`engine.outstanding_bad_debts(currency)`
  按台账顺序返回该币种剩余明细（来源流水号、债权名、币种、余额），其他
  币种不受影响。

## 坏账核销

无法收回的存续坏账可用
`ClearingEngine.process_writeoff(writeoff_transaction_id, currency,
writeoff_amount)` 结清（核销依赖台账状态，仅提供引擎方法）：

```python
writeoff = engine.process_writeoff("WO-001", "USD", Decimal("10"))
```

- 核销顺序：先按审计事件顺序、再按债权清单顺序，只核销币种相符且仍有
  余额的坏账明细；前项清零后处理后项，一笔核销可部分覆盖单项坏账。
- 返回不可变 `WriteoffResult`：核销流水号、币种、核销额、按核销顺序排列
  的 `WriteoffAllocation`（来源结算流水号、债权名、本次核销、剩余坏账）、
  核销合计、核销后该币种存续坏账与事件标识。
- 核销只减少存续坏账，不改其他状态；历史 `SettlementResult` 不回写。
  核销成功追加一条标识为 `EVT-{writeoff_transaction_id}-writeoff` 的
  审计事件（序号继续递增，`approved=True`、`validation_result=WRITEOFF`、
  `risk_occupancy=0`，`writeoff_allocations` 保存同额明细；结算与回收
  事件该字段恒为空元组，回收事件的 `recovery_allocations` 不受影响）。
- 有坏账时零额核销合法，生成一条空明细事件；失败（流水号缺失 / 为空 /
  重复、缺币种、非法金额、无存续坏账、超额）不生成事件、不占流水号、
  不改状态。
- 只读查询不触发清算：`engine.writeoff_of(writeoff_transaction_id)` 返回
  `WriteoffResult` 或 `None`。

## 债权人维度坏账报告

`ClearingEngine.creditor_bad_debt_report(currency, creditor_names=None)`
在审计基线上按债权人合并该币种的坏账全貌，返回不可变的
`CreditorBadDebtSummary` 元组（金额均为 `Decimal`，位置与字段名均可读）：

```python
rows = engine.creditor_bad_debt_report("USD")
rows = engine.creditor_bad_debt_report("USD", ("alice", "bob"))
```

| 字段 | 含义 |
| --- | --- |
| `creditor` | 债权人名（与放行归因中的名称完全一致） |
| `source_transaction_ids` | 放行归因坏账大于零的来源结算流水号，按事件顺序（同事件按债权清单顺序）去重 |
| `recovery_transaction_ids` | 实际归属该债权人的回收流水号，按事件顺序去重；空明细零额回收不归属 |
| `writeoff_transaction_ids` | 实际归属该债权人的核销流水号，按事件顺序去重；空明细零额核销不归属 |
| `initial_bad_debt` | 放行归因坏账合计 |
| `recovered_amount` | 既有回收分配归属该债权人的冲减额合计 |
| `written_off_amount` | 既有核销分配归属该债权人的核销额合计 |
| `outstanding_bad_debt` | 初始坏账 - 回收 - 核销，等于 `outstanding_bad_debts` 中该债权人余额合计 |

- **入表口径**：仅放行归因 `bad_debt > 0` 的债权人有行；拒绝结算不确认
  坏账，零额回收 / 核销即使生成空明细事件也不归属任何债权人。全额结清
  （回收 / 核销后存续余额为零）的债权人保留零额行。
- **合并与排序**：同一债权人跨事件、跨来源结算的记录合并为一行；行默认按
  该债权人首次坏账来源结算在台账中首次出现的顺序排列，同一事件内首次出现
  多个新债权人时按债权清单顺序。
- **过滤参数**：`creditor_names=None` 返回全部命中行；空元组 / 空列表返回
  空元组；传名单时只返回命中行，未命中的名称不报错、不产生占位行；名单中
  名称按去首尾空白后匹配。
- **校验**：`currency` 非字符串或去首尾空白后为空抛 `InvalidCurrencyError`；
  `creditor_names` 非 `list` / `tuple`（且非 `None`），或元素非字符串 /
  空白字符串，均抛 `ValueError`。
- **只读**：重复调用结果确定，不新增事件、不占序号与流水号、不改
  `audit_log`、结果索引、风险组、资金池、坏账 / 回收 / 核销状态，不落盘、
  不换汇、不估时。空台账或该币种无坏账返回空元组。

## 坏账处理轨迹

`ClearingEngine.bad_debt_trail(transaction_id)` 按**来源结算流水号**串联
该笔坏账从首次确认到回收、核销的完整处理轨迹，返回不可变的
`BadDebtTrail`：

```python
trail = engine.bad_debt_trail("TX-001")
```

| 字段 | 含义 |
| --- | --- |
| `transaction_id` | 命中的已登记来源结算流水号（精确匹配，不去首尾空白） |
| `initial_bad_debt` | 首次坏账，取原结算的 `uncovered_bad_debt`；拒绝或未形成坏账的放行结算为 0 |
| `recovered_amount` | 命中该来源的回收明细金额之和 |
| `written_off_amount` | 命中该来源的核销明细金额之和 |
| `outstanding_bad_debt` | 首次坏账 − 回收 − 核销，与 `outstanding_bad_debts` 中该来源的同来源余额合计一致 |
| `recoveries` | 命中的回收操作 `BadDebtOperation` 元组，按审计顺序、再按事件内明细顺序排列 |
| `writeoffs` | 命中的核销操作 `BadDebtOperation` 元组，顺序规则同上 |

每条 `BadDebtOperation` 为不可变元组，字段固定为 `operation_id`（回收 /
核销自身流水号）、`creditor`、`amount`（本次金额）、`remaining`（该债权
处理后的坏账余额）。同一债权多次部分处理不合并、不覆盖；空明细的零额
回收 / 核销不产生轨迹明细。

- **命中口径**：只接受已登记结算流水号并精确匹配；拒绝结算与未形成坏账
  的放行结算同样命中，返回零额、空明细轨迹。
- **未命中**：回收 / 核销流水号不是来源结算流水号，**不作替代查询**，
  一律返回 `None`；台账中不存在的流水号同样返回 `None`。
- **校验**：参数不是字符串或去首尾空白后为空抛内建 `ValueError`。
- **只读**：空台账、不存在与重复查询结果确定，不生成事件、不占流水号、
  不改台账、结果索引、风险组额度、资金池或坏账状态，不落盘、不换汇、
  不估时。

## 风险组坏账责任报告

`ClearingEngine.risk_group_bad_debt_report(currency)` 汇总该币种携带
`risk_group_id` 的结算及其坏账处理记录（无风险组记录不参与），按风险组
标识字典序返回不可变的 `RiskGroupBadDebtSummary` 元组（金额为
`Decimal`，计数为 `int`）：

```python
rows = engine.risk_group_bad_debt_report("USD")
```

| 字段 | 含义 |
| --- | --- |
| `risk_group_id` | 风险组标识 |
| `settlement_count` / `approved_count` / `rejected_count` | 组内结算事件总数与放行 / 拒绝数 |
| `risk_occupancy` | 仅放行请求的风险占用合计；拒绝仅计数 |
| `initial_bad_debt` | 组内放行归因坏账（大于零者）合计 |
| `recovered_amount` / `written_off_amount` | 按来源结算回溯本组的回收 / 核销（含定向操作）合计；空明细零额操作不计 |
| `outstanding_bad_debt` | 首次坏账 − 回收 − 核销，与组内各来源在 `outstanding_bad_debts` 中的余额合计一致 |
| `source_transaction_ids` | 放行归因坏账大于零的来源结算流水号，按首次出现顺序去重 |
| `recovery_transaction_ids` / `writeoff_transaction_ids` | 实际归属本组的回收 / 核销流水号，按首次出现顺序去重 |
| `creditor_summaries` | 组内按债权人名称字典序排列的 `CreditorBadDebtSummary` 元组，口径同债权人报告 |

- **恒等式**：每组 `initial_bad_debt - recovered_amount
  - written_off_amount == outstanding_bad_debt`；组内各债权人存续坏账
  之和等于该组 `outstanding_bad_debt`；该币种坏账全部来自带风险组的
  结算时，各组存续之和等于 `outstanding_bad_debts(currency)` 余额合计。
- **空结果**：空台账、币种不存在或该币种仅有无风险组记录时返回空元组。
- **校验**：`currency` 非字符串或去首尾空白后为空抛
  `InvalidCurrencyError`，匹配时去除首尾空白。
- **只读**：重复调用结果确定，不新增事件、不占序号与流水号、不改结果
  索引、风险组额度、资金池与坏账状态，不落盘、不换汇、不估时。

## 返回结构

成功与拒绝路径使用同构的 `SettlementResult`：

| 字段 | 含义 |
| --- | --- |
| `transaction_id` | 业务流水号 |
| `approved` | 放行状态 |
| `validated_available_balance` | 校验后可用余额（拒绝时等于输入余额） |
| `creditors` | 按清单顺序的债权名称元组 |
| `pool_allocations` | 各债权的池内实际分配 |
| `capital_allocations` | 各债权的补充资本承担 |
| `attributions` | 逐项 `CreditorAttribution`（含 `bad_debt`） |
| `uncovered_bad_debt` | 未覆盖坏账总额（拒绝路径为 0） |
| `risk_occupancy` | 风险占用 |
| `event_id` | 本请求唯一审计事件标识 |
| `rejection_reason` | 拒绝原因码，放行时为 `None` |

## 异常类型

| 情况 | 异常 |
| --- | --- |
| 负数金额/余额、非数值、空流水号 | `ValueError`（内建） |
| 重复流水号 | `vault_guard.DuplicateTransactionError` |
| 缺币种 | `vault_guard.InvalidCurrencyError` |
| 空债权清单 | `vault_guard.EmptyCreditorListError` |
| 风险系数越界（< 0 或 > 1） | `vault_guard.InvalidRiskFactorError` |
| 单笔请求内混合币种 | `vault_guard.MixedCurrencyError` |
| 空批次请求清单 | `vault_guard.EmptyBatchError` |
| 回收币种无存续坏账（含此时零额回收 / 核销） | `vault_guard.NoOutstandingBadDebtError` |
| 回收额超过该币种存续坏账 | `vault_guard.RecoveryAmountExceedsOutstandingError` |
| 核销额超过该币种存续坏账 | `vault_guard.WriteoffAmountExceedsOutstandingError` |
| 清算批次标识 / 币种 / 风险策略缺失或结构非法 | `vault_guard.InvalidSettlementBatchError` |
| 结算标识批内或已登记重复 | `vault_guard.DuplicateSettlementIdError` |
| 记录引用的限额未在风险策略中登记 | `vault_guard.RiskPolicyNotFoundError` |
| 记录币种与批次币种不一致 | `vault_guard.CurrencyMismatchError` |
| 结算金额非有限正数 | `vault_guard.InvalidSettlementAmountError` |
| 结算优先级非整数 | `vault_guard.InvalidSettlementPriorityError` |

校验异常不产生任何半成品分配，也不写入审计台账；失败请求的流水号不被占用，
可在修正后用同一流水号重新提交。

## 审计台账

- 每个被引擎接受处理的请求（放行或拒绝）**恰好**生成一个事件标识，台账为
  实例内内存中的追加式序列，序号单调递增。
- 事件包含流水号、输入摘要、校验结果、风险占用、各层分配与最终未覆盖金额。
- 只读查询不触发清算：`engine.audit_log`（快照）、`engine.events()`、
  `engine.get_event(transaction_id)`、`engine.result_of(transaction_id)`、
  `engine.recovery_of(recovery_transaction_id)`、
  `engine.writeoff_of(writeoff_transaction_id)`、
  `engine.outstanding_bad_debts(currency)`、
  `engine.audit_reconciliation(currency)`、
  `engine.creditor_bad_debt_report(currency, creditor_names=None)`、
  `engine.bad_debt_trail(transaction_id)`、
  `engine.risk_group_bad_debt_report(currency)`、
  `engine.has_transaction(transaction_id)`。

### 单币种审计核对快照

`ClearingEngine.audit_reconciliation(currency)` 返回不可变的
`CurrencyAuditSummary`，把限额拒绝、清算分配、坏账形成、回收冲减与核销
结清放在同一口径下核对。按审计顺序统计该币种事件，金额均为 `Decimal`：

| 字段 | 含义 |
| --- | --- |
| `currency` | 去首尾空白后的查询币种 |
| `settlement_count` | 结算事件总数（放行 + 拒绝，不含回收与核销） |
| `approved_count` | 放行结算事件数 |
| `rejected_count` | 拒绝结算事件数 |
| `recovery_count` | 回收事件数（含空明细的零额回收） |
| `writeoff_count` | 核销事件数（含空明细的零额核销） |
| `approved_risk_occupancy` | 仅放行请求的风险占用合计 |
| `pool_allocated` | 放行请求池内分配按明细求和 |
| `capital_allocated` | 放行请求补充资本按明细求和 |
| `initial_bad_debt` | 放行时确认的首次坏账合计（未覆盖坏账） |
| `recovered_amount` | 各回收事件冲减额按明细求和（只计回收） |
| `written_off_amount` | 各核销事件核销额按明细求和（只计核销） |
| `outstanding_bad_debt` | `outstanding_bad_debts(currency)` 余额合计 |
| `rejection_counts` | 按原因码排序的不可变原因次数映射，省略零次原因 |
| `event_ids` | 该币种全部事件标识，保持台账顺序（结算、回收与核销） |

核对恒等式：`initial_bad_debt - recovered_amount - written_off_amount ==
outstanding_bad_debt`，且 `settlement_count == approved_count +
rejected_count`。

- 查询只读：重复调用不改变审计序号、事件、结果索引、风险组额度、坏账、
  回收与核销状态。
- `currency` 非字符串或去首尾空白后为空抛 `InvalidCurrencyError`；匹配时
  去除首尾空白；台账中未出现的币种除 `currency` 外全为零，
  `rejection_counts` 与 `event_ids` 为空序列，空台账结果确定。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 约定

- 公开行为以 README 与源码为准。
- 后续需求在此基线上增量实现，既有入口语义保持不变。
