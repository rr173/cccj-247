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
