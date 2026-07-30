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
trading_calendar.py  交易日历，采集层与派生层共用一份假日表
tools/*.mjs       Playwright 验证脚本
```

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
要锁定在到期月上才会单调走向 1。详见 `find_roll_pair()` 的注释。

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
python3 derive_term_structure.py --test    # 派生逻辑 10 项
python3 fetch_oi.py --test                 # 采集校验 23 项（不联网）
node tools/verify-ui-fixes.mjs             # 柱对齐 / 按钮态 / 轴刻度 / 合约列表
node tools/verify-playback.mjs             # 回放交互
node tools/verify-gapframe.mjs             # 断层帧
node tools/verify-isolation.mjs            # 注入渲染故障，验证模块隔离
node tools/verify-live.mjs                 # 线上端到端
```

Playwright 脚本不要用 `waitUntil:'networkidle'` —— Chart.js 走 CDN，网络不畅时
该事件永不触发（实测卡满 30s 超时）。改用
`waitForFunction(() => Chart.getChart('oiChart'))`。

读柱子几何要用 `getProps([...], true)` 取终态：`el.y` 在动画期间是插值中间态，
24 万手的柱子也会读成高度 0。

`minBarLength` 的实际几何是「传入值 − borderWidth/2」，要净高 2px 得传 2.5。
