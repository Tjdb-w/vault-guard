# Vault Guard

金库与清算风控引擎：限额校验、清算瀑布、坏账归因与审计台账。

## 范围

本仓库从零开始实现上述方向的可用工具，不依赖外部同类实现，仅使用 Python 标准库。

当前版本兼容范围：**同一币种的单笔结算请求**。多币种换算与外部定价不在范围内，
检测到混合币种直接抛出 `MixedCurrencyError`。无任何落盘行为，审计台账为内存中的
追加式序列。

## 公开入口

```python
from decimal import Decimal as D
from vault_guard import ClearanceEngine, SettlementRequest, Creditor

engine = ClearanceEngine()
result = engine.submit(SettlementRequest(
    transaction_id="TX-1",            # 唯一业务流水号
    currency="USD",                   # 账户币种
    pool_balance=D("1000"),           # 资金池可用余额
    settlement_amount=D("100"),       # 本次拟清算金额
    notional_exposure=D("200"),       # 账户名义敞口
    base_limit=D("200"),              # 账户基础限额
    risk_factor=D("0.5"),             # 风险系数，区间 [0, 1]
    creditors=[                       # 优先债权清单，按先后顺序受偿
        Creditor("senior", D("80"), "USD"),
        Creditor("junior", D("70"), "USD"),
    ],
    supplementary_capital=D("20"),    # 补充资本，债权之后按清单顺序补足
))
```

也可使用函数式入口 `vault_guard.process_settlement(...)`，传入已有
`engine=` 可复用同一审计台账。

金额统一使用 `Decimal`（同时接受 `int` 与经字符串语义传入的数值），不做静默修正。

## 处理规则（确定顺序）

1. **输入校验**：任一失败即抛出对应异常，不产生分配、不产生审计事件。
2. **风险占用** = 拟清算金额 + 名义敞口 × 风险系数。
3. **限额校验**：
   - 拟清算金额 > 资金池可用余额 → 拒绝（`INSUFFICIENT_POOL_BALANCE`）；
   - 风险占用 > 基础限额 → 拒绝（`RISK_LIMIT_EXCEEDED`）；
   - 余额检查优先于风险检查。
4. **拒绝路径**：`approved=False`，校验后可用余额与输入一致，不扣减任何余额，
   无分配与坏账，追加一条拒绝审计事件。
5. **放行路径 — 清算瀑布**：按优先债权清单先后顺序，高优先级先受偿，
   每笔债权先使用池内资金、再使用补充资本；实际分配不超过各自债权金额与剩余资金。
6. **坏账归因**：池内与资本分配后仍未受偿的部分为该笔债权坏账；
   逐项归因满足 `池内分配 + 资本承担 + 坏账 == 债权金额`，
   各笔坏账合计等于最终未覆盖坏账。
7. **审计**：每个被受理的请求（放行或业务拒绝）恰好生成一个事件标识
   （`EVT-000001` 起顺序编号），记录流水号、输入摘要、校验结果、各层分配与
   最终未覆盖金额；先构建完整结果再一次性追加，杜绝半成品事件与重复入账。

**边界确定性行为**：基础限额为零时仅允许风险占用为零的请求通过；补充资本为零、
风险系数为零、完全覆盖、完全未覆盖、拟清算金额大于债权总额（剩余留池）均有确定结果。

## 返回结构

`SettlementResult` 为稳定可重复的公开结构（成功与拒绝同构）：

- `approved`、`rejection_reason`
- `verified_balance`：校验后可用余额（放行时为池余额减池内实际分配）
- `risk_usage`：风险占用
- `pool_allocations` / 属性别名 `attributions`：每笔债权的
  `pool_allocation`、`capital_allocation`、`bad_debt`
- `total_pool_allocated`、`total_capital_allocated`、`uncovered_bad_debt`
- `audit_event_id`

## 错误类型

每类失败只使用对应的一个类型（均继承 `VaultGuardError`）：

| 触发情形 | 异常 |
| --- --- |
| 金额/余额等为负，或流水号缺失 | `ValueError` |
| 重复业务流水号 | `DuplicateTransactionError` |
| 币种缺失（账户或债权） | `InvalidCurrencyError` |
| 优先债权清单为空 | `EmptyCreditorListError` |
| 风险系数超出 [0, 1] | `InvalidRiskFactorError` |
| 同一请求混入不同币种 | `MixedCurrencyError` |

## 审计台账（内存、追加式、只读）

```python
engine.events()                       # 按顺序的全部事件只读快照
engine.ledger.latest_event()          # 最近一条事件，不触发清算
engine.get_event("EVT-000001")        # 按事件标识查询
engine.get_by_transaction("TX-1")     # 按流水号查询
```

只读查询不会触发清算，也不改变台账。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 状态

第一版公开功能已实现：限额校验、清算瀑布、坏账归因、内存审计台账与全部错误类型。
后续需求在此基线上增量实现。
