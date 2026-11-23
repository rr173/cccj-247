# 历史分类码迁移目录（可逆性核对）

把"声明的合法代码域 + 带优先级的偏移规则"编译成两份**可查询、可复查**的目录：

- **源端有效分段目录**：对每个源空间的合法域逐段给出状态、胜出规则身份、变换和
  目标区间；同段同状态的整数合并成一行，不逐码展开。
- **目标端反向分段目录**：对目标空间的合法域逐段给出 `unique` / `ambiguous` /
  `unused`，`unique` 直接给出逆算原码与规则身份，`ambiguous` 给出两份不同旧码的
  见证，并支持查询任意目标点的全部合法原码。

所有区间都是**闭区间**，整数用 Python 任意精度，宽于 32/64 位的区间（例如
`10**30` 到 `10**30 + 10**21`）同样是**一条目录行**，不会物化码对。

## 语义（严格按题面）

| 概念 | 处理 |
| --- | --- |
| 合法域 | 每个空间由若干带符号整数**闭区间**的并集声明；规则的 `source_range` 只在该空间合法域内生效（交集） |
| 优先级 | 只在合法域内比较；`priority` 为整数，大者胜 |
| 变换 | `new = direction * old + offset`，`direction ∈ {+1,-1}`（双射，精确可逆）；或 `action: "disable"` 明确停用 |
| 并列最高 | 目标空间+方向+偏移完全相同，或同为停用 → 合并证据（`rule_ids` 列出全部），仍是一个正常映射；否则该源位置 **conflict**，不落实际映射 |
| 方向交叉 | 两条方向不同的规则可能只在一个源点同值；该点单独切成 1 码段记为正常映射，两侧才是冲突 |
| 缺规则 | **missing**，不是停用 |
| 越界 | 胜出映射落在目标空间合法域外 → **out_of_range**，标为无效，**不降级**去用低优先级规则 |
| 停用 | `disabled`，本身不是失败，不进阻塞项 |
| 同一源位置 | 至多一个正常映射 |

### 源端状态

`mapped`（唯一有效映射）· `out_of_range`（越界，无效）· `disabled`（停用）·
`conflict`（并列最高但不一致）· `missing`（无适用规则）· 查询时越出合法域返回
`out_of_domain`（仅查询用，不进入目录）。

### 目标端状态

`unique` · `ambiguous`（≥2 份不同旧码）· `unused` · 查询时 `out_of_domain`。

### 迁移批准

阻塞项 = `missing`、`conflict`、`out_of_range`（源端）以及 `ambiguous`（目标端）。
`disabled` 与 `unused` **不阻塞**。`report` 子命令输出 `approval_ready` 与逐项计数。

## 结构

```
migration_catalog/
  intervals.py   闭区间与区间集代数（normalize / subtract / intersect_pieces）
  model.py       Shift / Rule / MigrationSpec 及严格 JSON 校验
  core.py        符号化目录构建、正反向查询、报告、文本渲染
  cli.py         JSON 命令行界面
tests/
  reference.py   逐码枚举的独立参考实现（仅测试用，作 oracle）
  test_catalog.py 50 个用例：语义、边界、宽整数、规则删除、随机穷举比对
examples/
  tiny.json      边界小例（负码、单点交叉、双段合法域）
  example.json   综合例（重叠优先级、单点交叉、停用、越界、缺映射、10^21 宽区间）
  demo.py        可复跑演示：边界 / 正反查 / 宽整数 / 删规则后唯一转多义
```

## 规格文件格式

```json
{
  "source_spaces": {
    "legacy": [[0, 1000], [5000, 5009], [1000000000000000000000000000000, 1000000001000000000000000000000]]
  },
  "target_spaces": {
    "modern": [[1000000, 1000999], [2000000, 2000999]]
  },
  "rules": [
    {
      "id": "core-shift",
      "source_space": "legacy",
      "source_range": [0, 800],
      "priority": 50,
      "target_space": "modern",
      "direction": 1,
      "offset": 1000000
    },
    {
      "id": "retire",
      "source_space": "legacy",
      "source_range": [500, 520],
      "priority": 70,
      "action": "disable"
    }
  ]
}
```

- `source_spaces` / `target_spaces` 必须非空；规则引用的空间必须已声明。
- 映射规则必须同时给 `target_space`、`direction`、`offset`；停用规则不得带这些字段。
- 规则 id 唯一。无法解析或自相矛盾的规格直接报错（退出码 2），不做"尽力而为"猜测。

## 使用

```bash
# 批准状态与阻塞项
python3 -m migration_catalog examples/example.json report

# 源端分段目录（文本；不加 --text 输出 JSON）
python3 -m migration_catalog examples/example.json source --space legacy --text

# 目标端反向分段目录
python3 -m migration_catalog examples/example.json target --space modern --text

# 单点正向：状态、规则身份、目标值
python3 -m migration_catalog examples/example.json source-point --space legacy --code 850

# 单点反向：unique 给原码+规则；ambiguous 给 witness 与全部原码
python3 -m migration_catalog examples/example.json target-point --space modern --code 1000805

# 只要全部合法原码
python3 -m migration_catalog examples/example.json target-preimages --space modern --code 1000850

# 可复跑演示
python3 examples/demo.py

# 测试
python3 -m unittest discover -s tests -v      # 或 python3 -m pytest tests
```

库用法：

```python
from migration_catalog import Catalog, load_spec

catalog = Catalog(load_spec("examples/example.json"))
catalog.source_lookup("legacy", 10**30 + 7)      # 正向
catalog.target_lookup("modern", 10**40 + 7)      # 反向（唯一/多义 + 见证）
catalog.source_segments("legacy", 0, 1000)       # 任意窗口的分段视图
catalog.report().approval_ready                  # 批准状态
```

## 关键行为示例

`examples/example.json` 中：

- `[700, 800]`：`core-shift` 与 `late-shift` 同优先级同变换 → 合并证据为一条正常映射。
- `[820, 849]` / `[851, 880]`：`late-shift` 与 `reverse-alias` 方向相反 → 冲突；
  只有 `850` 这个点两式同值（1000850），单独成段为正常映射。
- `[400, 450]`：高优先级 `out-of-range` 胜出但目标越界 → 无效，不降级。
- `[500, 520]`：停用，不阻塞。
- `[951, 1000]`、`[5000, 5009]`：无规则 → missing，阻塞。
- `modern` 的 `[1000800, 1000859]` 部分被 `aux` 空间 `aux-mirror` 反方向覆盖 →
  多义，给出两份不同旧码见证。
- `10**30 .. 10**30+10**21` 宽区间：一条源段、一条目标段，正反查精确往返。

`tests/test_catalog.py::RuleRemovalTests` 与 `examples/demo.py` 演示：删掉高优先级的
`shadow` 规则后，原本 `unique` 的目标段 `[200, 209]` 变为 `ambiguous`，并给出触发
该变化的规则与旧码对。

## 设计取舍

- **符号化**：只按规则/域端点切分，复杂度与区间段数相关，与码值宽度无关；遍历码值的
  参考实现只存在于 `tests/reference.py`，用于在小域上穷举校验（随机样例已跑数千组）。
- **目标空间参与并列判定**：两条并列最高规则若把同一分段送到不同目标空间，也记为
  conflict，因为"同一源位置只可能有一个正常映射"。
- **越界不降级**：`out_of_range` 记录胜出规则和目标值，便于定位是规则还是目标域需要修正。
- **见证栈**：`ambiguous` 的 `witness` 固定取两个**不同源位置**（空间+码），不会把
  同一位置被多条规则覆盖误判成多义。
- **不物化**：目录与查询均以区间 + 仿射参数表示，`--text` 只打印区间端点。

---

# 本地 SQLite 账本（migration_ledger）

目录只回答"某个码在规则下意味着什么"，但财务切换需要一张随**实际入账**与**规则更正**
持续更新的余额表。`migration_ledger/` 在不重复查询迁移目录的前提下，提供一个可重开的
单文件 SQLite 账本。

## 语义（严格按批次结束时的事实与规则）

| 概念 | 处理 |
| --- | --- |
| 事实键 | 业务行 ID；内容为旧码空间、合法旧码、带符号整分金额（任意精度，金额/码均可超 64 位） |
| 批次动作 | 整行写入（可改金额、可移到别的旧码）或按 ID 删除；同一 ID 一批只出现一次；撤销不存在的 ID 是输入错误 |
| 规则 | 批次可整体替换规则，但合法域在初始化后冻结；批次不含 `rules` 时沿用上批已提交规则，`"rules": []` 是显式清空 |
| 入表条件 | 旧码在批次结束目录里是有效 `mapped`，**且**新码反向状态为 `unique`（按声明域恰有一个合法旧码来源，与别处有没有入账无关） |
| 阻塞不连带 | `disabled` / `missing` / `conflict` / `out_of_range` 的旧码与 `ambiguous` 的新码不产生余额行；其他码段仍合格的事实照常入表 |
| 暂不能入表的事实 | 仍留在库中保留最新内容，之后每次刷新都按最新规则重新参与汇总（竞争出现→余额行消失；竞争解除→纳入其间已更正且未撤销的事实） |
| 余额行 | 每个有贡献的新码给出事实条数 `fact_count` 与精确金额和 `amount_sum`；金额为零仍可能有事实，无事实则无该行 |
| 批次号 | 从 1 连续递增；同号同字节文件重放为幂等 no-op；同号不同内容、跳号、非法输入一律报错且不改变已提交状态 |
| 原子性 | 一次提交的事实、规则版本、余额、批次行在**单个 SQLite 事务**内发布；进程中途被杀，重开只能看到整批之前或之后 |

## 局部刷新：不扫描全部存量事实

规则替换后，受影响的只有"新旧目录映射发生变化"的位置。刷新分三步，全部走
`(old_space, code_key)` 索引，不做全表扫描：

1. 本批点名的事实（写入后的新位置 + 写入前的旧位置，保证移动/删除能回收旧行）；
2. 符号化对比新旧目录的源端分段，对分类变化的源区间做索引范围查询；
3. 符号化对比目标端反向分段（含贡献者源区间端点在目标空间的投影，处理同一 unique
   段内逐码原码身份变化），回收其中的存量余额行，并按贡献者源区间索引取事实。

`RefreshStats` 暴露实际触及的事实行数与受影响区间，可直接核对局部性；示例在 20,000 条
无关"冷事实"上做局部规则变更，刷新只触及个位数事实，并用 `EXPLAIN QUERY PLAN` 证明
查询计划是 `SEARCH ... USING INDEX fact_position_idx` 而非 `SCAN fact`。

## 结构

```
migration_ledger/
  keys.py     任意精度整数的可排序文本键（数值序，支持负数与超 64 位码）
  refresh.py  新旧目录的符号化差异与局部余额刷新（不展开闭区间）
  ledger.py   SQLite schema、批次严格校验、单事务提交、余额/事实读取 API
  cli.py      init / submit / balance / head / facts / seqs 命令行
bin/migration-ledger  隔离模式（python3 -I）入口
examples/ledger_demo.py  可复跑端到端示例（含真实进程中断与冷事实局部性核对）
tests/test_ledger.py     79 项测试，含 150 组随机批次对逐码枚举 oracle 的交叉验证
```

## 批次文件格式

```json
{
  "seq": 2,
  "rules": [ /* 可选；整体替换，规则格式与迁移目录规格一致；省略则继承 */ ],
  "facts": [
    {"id": "INV-1", "old_space": "legacy", "old_code": 850, "amount": 12345},
    {"id": "INV-2", "old_space": "legacy", "old_code": 851, "amount": -500},
    {"id": "INV-OLD", "action": "delete"}
  ]
}
```

顶层只允许 `seq` / `rules` / `facts`，未知字段、布尔金额、越界旧码、批内重复 ID 等都在
写事务之前报错。

## 使用

```bash
# 用现有目录规格初始化一个空库（合法域冻结，规则取规格中的初始规则，版本号 0）
python3 -I bin/migration-ledger init path/to/ledger.db examples/example.json

# 提交批次（按文件原始字节做 sha256；输出含局部刷新统计）
python3 -I bin/migration-ledger submit path/to/ledger.db batch_002.json

# 当前余额表（可按目标空间/新码点查；每行附 head_seq 与 updated_seq）
python3 -I bin/migration-ledger balance path/to/ledger.db --space modern --code 1000805
python3 -I bin/migration-ledger head path/to/ledger.db

# 可复跑示例（在 examples/ledger_run/ 下生成规格、批次与一个全新的库）
python3 -I examples/ledger_demo.py

# 测试
python3 -m unittest discover -s tests -v
```

库用法：

```python
from migration_catalog import load_spec
from migration_ledger import Ledger

ledger = Ledger.initialize("finance.db", load_spec("examples/example.json"))
result = ledger.submit_file("batch_001.json")
result.refresh.fact_rows_touched      # 实际触及的事实行数（局部性证据）
ledger.get_balance("modern", 1000805) # None 表示该新码当前无贡献余额行
ledger.list_balances("modern")        # 当前全部余额行，均附 head_seq
ledger.close()

Ledger.open("finance.db")             # 任意时刻重开，状态完全来自 SQLite
```

## 设计取舍

- **码与金额按十进制 TEXT 存储**：SQLite INTEGER 只有 64 位；数值顺序由 `keys.py` 的
  定形可排序键承担，`BETWEEN` 范围查询与点查都走同一索引，金额和在 Python 侧精确求和。
- **只物化余额，不物化码对**：规则版本整体存为 JSON 并在内存中编译为 `Catalog`；目录
  仍由 `migration_catalog` 符号化构建，闭区间从不逐整数展开。
- **余额表只放有贡献的 unique 新码**：唯一性按声明域计算，无事实的 unique 码、阻塞码
  都不建行；事实表与 `fact_history` 保留全部已声明事实及每次写入/撤销的审计轨迹。
- **单连接单写者**：不支持并发写；`BEGIN IMMEDIATE` + WAL + `synchronous=FULL` 保证
  单写者下的崩溃原子性与重开一致性。
