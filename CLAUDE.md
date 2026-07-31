# gold-dashboard

COMEX 黄金持仓仪表盘。数据每交易日由 GitHub Actions 采集，前端为单文件
`index.html`（Chart.js via CDN），无构建步骤。

## 架构

```
fetch_cot.py      CFTC COT 周报        → data/cot.json
fetch_gold.py     Yahoo Finance 金价   → data/gold_price.json
fetch_stocks.py   CME 库存             → data/stocks.json
fetch_oi.py       CME Section 62 PDF   → data/oi.json        （含 4 项写入前校验）
derive_term_structure.py                → data/derived/term-structure-series.json
index.html        期限结构回放 + 各图表
term-3d.html      Plotly 3D 曲面页，**直接读 data/oi.json**（不经派生层）
trading_calendar.py  交易日历，采集层与派生层共用一份假日表
data_envelope.py  统一落盘信封 + write_json 单点落盘
tools/*.mjs       Playwright 验证脚本
```

### 落盘统一信封（schema_version = 0）

所有派生文件走 `data_envelope.py` 的 `envelope()` + `write_json()`：

```
{
  "schema_version": 0,          // 0 = 尚未稳定，加完新源冻结格式后升 1
  "source":       "cme_section62_term_structure",
  "freq":         "daily",
  "generated_at": "...Z",
  "date_field":   "date",       // 跨源 join 的契约，不靠猜
  "coverage":     {"first","last","count"},
  "derived_from": [{source, generated_at, coverage, envelope}],
  "warnings":     [],
  "info":         [],
  "data":         { ...业务数据原样... }
}
```

元数据在外、业务数据在 `data` 里 —— 加新源时信封字段不会和业务字段撞名。
`derived_from` 记上游身份，能看出派生数据基于哪一版原始数据算的；上游若还是
裸格式则标 `envelope:false`，便于审计哪些源没迁。

**迁移状态**：只有 `term-structure-series.json` 用了信封。四个采集脚本产出的
原始文件（`cot/gold_price/stocks/oi.json`）仍是裸格式。

前端在 `initOIPlayback()` 入口用 `payload?.data ?? payload` 兼容双形状，
其余代码零改动。等所有派生文件迁完可简化为 `payload.data` —— 该行注释里写了
可删除条件。读派生文件的测试脚本（`tools/verify-gapframe.mjs`）也要同样解包。

### 指标算术在派生层，前端只读字段

前端不做指标算术。`_renderFrame` 只读帧字段填 DOM，计算全在
`derive_term_structure.py`：

| 字段 | 算法 |
|---|---|
| `total_oi` | X 轴合约的 OI 求和（只计有结算价的） |
| `spread` | `roll_to.settle − roll_from.settle` |
| `spread_gap_days` | 两月日历天数差，以每月 1 日为基准 |
| `spread_annualized_pct` | `spread / near × (365 / gap_days) × 100` |

这样算术进 `--test` 有回归保护，别的页面要用也不必重写一遍。
角色锚定（`front` / `roll_from` / `roll_to`）同样在派生层，见 `find_roll_pair()`。

三处易错，都有断言守着：

- **天数不能按「月数 × 30」估**。AUG26→DEC26 是 122 天，按 4×30=120 算年化
  偏高 1.7%。`spread_gap_days == 122` 那条断言就是防这个回归。
- **`total_oi` 的口径是「X 轴合约」而非「原始窗口月份」**。两者差在已到期月：
  实测首帧按窗口月份求和会多出 JUN26 的 7 手（该合约当日仍挂牌，但在序列
  末帧已到期、已从 X 轴剔除）。派生层要显式按 `contracts` 构造入参。
- **跨语言舍入**。Python `round(x, 2)` 与 JS `toFixed(2)`：年化率在派生层就
  舍入到 2 位，与前端显示精度对齐，避免末位不一致。降级路径（`renderOI()`）
  在前端就地复算，那里的 `Math.round(x * 100) / 100` 必须与 Python 同口径。

### 改 oi.json 结构时必须同步检查 term-3d.html

`term-3d.html` 绕过派生层直接 `fetch('data/oi.json')`，且**没有任何 verify
脚本覆盖它** —— 5 个 Playwright 脚本全都只测 `index.html`。

它依赖的字段：`r.date`、`r.months[]`、`m.month`、`m.settle`、`m.oi`
（见 term-3d.html 的 `windowMonths()` 与 X/Y/Z 轴构造）。

改动 `oi.json` 的结构（换字段名、改嵌套、加落盘模板）时若只跑现有 verify，
全绿也不代表这一页没坏 —— 它会静默变成空白或错图，而 CI 与本地测试都不会
报错。改完手动打开 `term-3d.html` 看一眼，或给它补一个 verify 脚本。

本地 Python 被 Application Control 拦截（`python`/`python3` 是 Store 占位符）。
用 WSL 跑：`wsl -d Ubuntu-22.04 -- bash -c "cd /mnt/d/VScode/test/gold-dashboard && python3 ..."`

## 数据规则

### 合约列表只按存续状态过滤，不按持仓阈值

`derive_term_structure.py` 的 X 轴合约列表只剔除**已到期**（最后一帧不再挂牌）
的合约。仍在挂牌的一律保留，无论持仓多小 —— 微小持仓靠前端 `minBarLength`
渲染成细线。

历史上用过两个持仓阈值，都被推翻：

| 常量 | 问题 |
|---|---|
| `MIN_VISIBLE_OI_RATIO` | 把渲染问题当数据问题解。被剔除的合约仍有结算价，删列会把相邻点横向间距从 1 个月变成 2 个月，扭曲价格曲线几何。期限结构图的主体是那条线，柱子是辅助。 |
| `MAJOR_PEAK_RATIO` | 依赖会移动的 `oi_max`。压测显示持仓涨 5 倍时最早一个周期整段失效，跌到 1/5 时换成最晚一个周期失效。改局部基准也没用（半径按合约索引取，密度一变含义就变）。 |

绝对阈值同样不行：非活跃月持仓随合约变远连续衰减（实测
7906 → 3475 → 280 → 272 → 42 → 14 → 8），不存在稳定分界。看似存在的"断层"
只是这条曲线当前最陡的一段，会随时间平移；NOV26 今天 280 手，衰减到 90 手时
固定 100 手的阈值照样砍掉它。

需要"主力月"概念时用序数信号（是否曾经当过持仓最大的月），与量级无关 ——
见 `major_months()`。

### 指标必须显式锚定合约角色

凡是引用"第一列 / 最后一列"（`months[0]` / `months[months.length-1]`）的指标
都是脆的：列表一变含义就变，且两端合约往往流动性最差。

价差卡曾用 `months[0]` / `months[last]`，取到的是正在到期的 JUL26 和只有
971 手的 JUN27，两端都是交易所推定价，算出来的是结算程序而不是市场。

显式锚定到这三个角色之一：

| 角色 | 含义 | 字段 |
|---|---|---|
| `front_by_expiry` | 日历序上最近的活跃月（正在到期） | `frame.roll_from` |
| `next_active` | 其后的承接月 | `frame.roll_to` |
| `dominant_by_oi` | 持仓最大的月（展示用主力月） | `frame.front` |

`front` 与 `roll_from` 必须分开：`front` 会在移仓过半时切到承接月，而移仓分母
要锁定在到期月上才会随到期单调变化。详见 `find_roll_pair()` 的注释。

### roll_noise 阈值需覆盖至少一个完整移仓周期

`roll_noise = |到期月 OI 变化| / Σ|全部合约 OI 变化|`（3 日均值存为
`roll_noise_ma`）。字段已在算并存进 JSON，但**暂无阈值、前端不画不标记**。

当前 22 帧数据全部落在移仓加剧的上升段：`ma3` 从 0.041（07-06）单调爬到
0.519（07-28），末段 6 帧密集堆在 0.44~0.53 之间，间隙全部 <0.01。没有双峰，
也没有可用的谷 —— 排序后最大间隙 +0.1185 隔出的是首个有效帧（5 日窗口只有
1 个样本的预热假象），不是市场特征。

在单调段上找"噪音结束"的拐点，等于在上升曲线上找下降点，数据里不存在。
定阈值需要等数据覆盖一次完整周期（噪音升上去再落回来），也就是 AUG26 彻底
到期、DEC26 成为唯一主力之后的若干帧。

移动平均窗口取 3 而非 5：22 帧上 5 日均值平滑过度，把 06-30 的真实低谷
（raw 0.0182）抹成 0.1430 且滞后一帧。数据攒够后再调回。

### front_remaining 在峰值前会 <1，这是正常的

`front_remaining = 到期月当前 OI / 该合约历史峰值 OI`，从 1 降到 0。分子分母
是同一合约，不受承接月选取影响。

峰值出现之前持仓还在增长，比值必然小于 1（实测 AUG26 峰值在序列第 3 帧，
前两帧为 0.9909 / 0.9985）。这是"移仓尚未开始"的正确表达，不要为了让曲线
从 1 起步而改成"窗口内首帧"做分母 —— 窗口滚动后首帧可能已在移仓中途，
那才会让起点真正失真。

旧字段 `roll_progress`（`roll_to/(roll_from+roll_to)`）已删除：它是迁移占比，
方向 0→1 读起来像进度条，但分母会随承接月换月跳变。

### oi_chg 一律由存量差分重算

不读 CME 报表里的 `oi_chg` 字段：`oi_chg[t] = oi[t] − oi[t-1]`（同一合约月、
相邻交易日）。06-29..07-23 期间该字段静默全为 0，差分值只依赖存量 `oi`，
可交叉验证。stored 与 diff 不一致时以 diff 为准，并把该合约记入帧级
`unreliable_chg`（CME 盘后修订前一日存量所致）。

`0` 与 `null` 语义严格区分：`0` 是"确实没变"，`null` 是"不可知"。

### 坏数据拦在采集层

`fetch_oi.py` 的 `validate()` 在写入 `data/oi.json` 之前跑 4 项校验
（Trade Date / 逐字段重复比对 / 合约数量突变 / 主力月持仓）。任一命中则坏数据
与原始 PDF 进 `data/quarantine/`，`oi.json` 保持上一份，`exit 1`。

拦截点必须在采集层：坏数据一旦落盘，既显示在页面上，也成为下一交易日差分的
输入，把错误传播到后续所有帧。派生层不该因可重算的计算而拒绝出数据。

隔离区要提交进仓库 —— CME 只提供 `current` 当日文件、无历史归档，坏数据错过
就永久拿不回来。

CME 在 T+1 早间发布 T 日公报，所以 `current` 里**永远是上一个交易日**的数据。
第一版按"当天"建模，20 次真实运行里 16 次误判为陈旧。改动发布时刻模型前先看
`PUBLISH_HOUR_UTC` 处的实测记录。

### 原始数据无条件落盘

workflow 里所有 fetch/derive 步骤都带 `continue-on-error`，commit 步骤
`if: always()`，末尾统一闸门按层报 `::error::`。原始数据不可再生，派生数据
随时可重算 —— 不能让可重算的计算失败连累不可再生的采集。

## 前端

- Y 轴 min/max 全部取自派生 JSON 的 `scale`，回放期间 Chart.js 不自动缩放，
  帧间柱高可直接比较
- 三面板共用 X 轴对齐：主图 `afterLayout` 写 `window._oiChartArea`，
  下方两图 `beforeLayout` 同步 `padding.left`、右轴 `afterFit` 钳制 `width`
- 移仓面板横轴是**日期**（上两图是交割月），必须显示刻度 + 分隔线，
  否则会被误读为共享横轴
- 多个 bar dataset 共存时要么设 `grouped:false`，要么减到一个 —— 并排摊开会
  让柱子变窄且整体左移，与下方面板错位
- 渲染异常不能伪装成"数据文件未找到"：每个模块用 `_safeRender()` 单独
  try/catch，异常带模块名打到 console。曾有 Chart.js 配置错误被外层 `.catch()`
  吞掉，页面静默降级为 mock 数据且控制台干净

## 验证

```
python3 derive_term_structure.py --test    # 派生逻辑 11 项（含信封契约）
python3 fetch_oi.py --test                 # 采集校验 23 项（不联网）
node tools/verify-ui-fixes.mjs             # 柱对齐 / 按钮态 / 轴刻度 / 合约列表
node tools/verify-contract-contango.mjs    # 合约过滤 / 最小柱高 / 价差锚点
node tools/verify-playback.mjs             # 回放交互
node tools/verify-gapframe.mjs             # 断层帧
node tools/verify-isolation.mjs            # 注入渲染故障，验证模块隔离
node tools/verify-schema-coupling.mjs      # 注入 schema 破坏，验证护栏会变红
node tools/verify-kpi-injection.mjs        # 注入错误 KPI 值，验证护栏会变红
node tools/verify-live.mjs                 # 线上端到端
```

### 验证护栏是否有效，靠注入破坏，不靠读代码推断

「这个脚本看起来会覆盖到」是不可靠的判断。要确认一条护栏真的有效，
往被测对象里注入一个已知破坏，看它是否**变红**。

已两次证明这一步不可省：

| 场景 | 读代码的判断 | 注入实测的结果 |
|---|---|---|
| `verify-isolation` | 「它注入故障验证模块隔离，有效」 | `initOIPlayback` 搬到 `js/playback.js` 后，脚本只拦 root HTML，注入变成空操作 —— 静默通过，从此不再验证任何东西 |
| 信封化后的四个脚本 | 「它们不直接读 JSON，可能对 schema 破坏无感」 | 注入 `data` 缺失 / `frames` 为空，三图表全建不起来，几何断言必然失败 —— 实际是 fail loudly，无需改动 |

两次的方向相反：一次是以为有效实际失效，一次是以为失效实际有效。
两次都只有注入才看得出来。

**规程**：凡改动 schema 或做重构后，对相关 verify 脚本做一次破坏注入，
确认它 fail loudly。第三次证明这一步不可省：KPI 算术下沉后注入四种错值，
年化率 / spread 三种都变红，但 `total_oi` 改成 1 仍然全绿 —— 缺一条断言，
补上后才变红。`tools/verify-schema-coupling.mjs` 是信封格式的现成模板
（注入 `data` 缺失 / `frames` 为空 / 旧平铺格式三种情形）。

Playwright 脚本不要用 `waitUntil:'networkidle'` —— Chart.js 走 CDN，网络不畅时
该事件永不触发（实测卡满 30s 超时）。改用
`waitForFunction(() => Chart.getChart('oiChart'))`。

读柱子几何要用 `getProps([...], true)` 取终态：`el.y` 在动画期间是插值中间态，
24 万手的柱子也会读成高度 0。

`minBarLength` 的实际几何是「传入值 − borderWidth/2」，要净高 2px 得传 2.5。
