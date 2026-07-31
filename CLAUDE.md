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

**迁移状态**：`term-structure-series.json`、`stocks.json` 已用信封。
`cot/gold_price/oi.json` 仍是裸格式（cot 的**读取端**已容双形状，写入端待切）。

**TODO（四源全迁完后）**：统一 `unwrap(strict=True)` + 删前端双形状兼容
（`payload?.data ?? payload`、cot 的 `p?.data ? {...} : p`）+ 删 `generated_at
?? updated_at` 的 `updated_at` 分支。**过渡期兼容不许永久化** —— 双形状分支
留着就永远有一半代码路径不被真实数据走到，坏了也不会有人发现。

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
- **`total_oi` 口径 B2（已执行）**：`total_oi = Σ OI over month ∈ ever_front`
  —— 即「已当过持仓最大月」的已确立主角之和。**零阈值。**

  不含三类：到期清算残余、从未当过主角的名义月（当前 OCT26）、尚在积累未坐正
  的末端承接月（当前 FEB27）。

  与价差/主力月共享 `major_months` 家族但**口径有意略窄**：价差含承接月
  （问「往哪移」，用 `major_months`），`total_oi` 只含已确立主角（问「盘子多大」，
  用 `ever_front`）。两者不同口径是有意的，不是不一致。

  实现上传 `ever_front` 而非 `major_months()` 的返回值 —— 后者会用
  `MIN_NEXT_OI_RATIO`（未跑分布的拍值）给末端承接月补位，那会让 `total_oi`
  依赖一个没锚定的阈值。`ever_front` 由逐帧取持仓最大月累积得出，纯序数、
  尺度无关、零阈值。

  旧口径（全部挂牌月求和）作废。切换实测：07/29 `380,608 → 302,267`、
  06/26 `361,195 → 320,600`，五处 KPI 卡显示值单向变小，`front` /
  `deltaData` / 图表存活均未变 —— 是口径变更不是回归。
- **计算层全精度，展示层才格式化**。`spread` / `spread_annualized_pct` 原始
  float 落盘，派生层不做任何舍入（实测落盘值形如 `60.69999999999982` /
  `4.499230954497755`）。`toFixed(2)` 只出现在前端 `_renderFrame`。

  曾经为了「跨语言末位对齐」在派生层 `round(x, 2)`，那是错的：断言比的是截断
  后的值，绿得没意义；且精度一旦在计算层丢掉就拿不回来。断言改为直接比落盘
  全精度值、容差 `1e-9`，**不准用 `round`/`toFixed` 对齐后再比**。

  `verify-contract-contango` 另有一条专门的反舍入断言：落盘值若恰等于自身 2 位
  舍入、而独立复算的真值不是，即判定精度在派生层就丢了。

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

### WSL exit code 取证禁忌

两个独立的 bug，都会把非零码读成 0，让坏代码看起来通过 CI。适用于**所有**
verify / `--test` 取证，不限于某一步。

**禁忌 1：在 WSL 内部 `echo $?`。** `$?` 被 Windows 侧 shell 先展开成上一条
命令的码，恒为 0，与 WSL 内实际结果无关。

```bash
# 坏：注入缺陷后测试打印「72 passed, 9 failed」，这里却报 exit=0
wsl -d Ubuntu-22.04 -- bash -c "... && python3 tools/xxx.py; echo exit=$?"
```

**禁忌 2：取码时接管道。** `| tail` / `| head` / `| grep` 让 shell 返回**管道末
命令**的状态，python 的非零码被吞掉。

```bash
# 坏：python 明明 exit 1，这里报 exit=0（tail 成功了）
wsl ... -- bash -c "python3 x.py 2>&1 | tail -6"
```

**正确：让 WSL 进程退出码自然传出、在 Windows 侧读。** 需要截断输出时用
`> /dev/null`，或把「看输出」与「取码」分成两步跑。

```bash
wsl -d Ubuntu-22.04 -- bash -c "cd ... && python3 tools/xxx.py > /dev/null 2>&1"
echo "exit=$?"                      # 在 WSL 命令之外读，且不接管道

wsl ... -- bash -c "... python3 x.py 2>&1 | tail -6"   # 单独一步只看输出
```

**应该红却显示绿时，先怀疑取证方式，再怀疑被测对象。** 实测遇到过一次：
stocks 校验闸注入后报 exit=0，第一反应若是「闸坏了」就会去改本来正确的代码；
先查测量方式才发现是 `| tail -6` 吞了退出码，去掉管道后如实报 exit=1。

这与「验证护栏靠注入破坏确认」是同一类陷阱：不注入就不会发现读到的是假码。

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

`roll_noise` 与 `roll_noise_ma` **全精度落盘，不舍入**。阈值待定、将来要在这一列
上跑分布，`round(4)` 会垫一层量化地板（相邻值被吸附到同一格），影响拐点定位。
`derive --test` 的 NOISE fixture 用 `1000/3001` 这种无限小数守着：断言容差
`1e-12`，并检查落盘值不等于自身 4 位舍入 —— 退回 `round(4)` 会立刻变红。

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

四个采集脚本都在写盘之前跑 `validate_*()`，任一判据命中则坏数据与原始响应进
`data/quarantine/`，目标 JSON 保持上一份，`exit 1`。

| 脚本 | 判据数 | 判据 |
|---|---|---|
| `fetch_oi.py` | 4 | Trade Date / 逐字段重复 / 合约数量突变 / 主力月持仓 |
| `fetch_cot.py` | 5 | 全期归零 / 单期归零 / OI 非正 / 期数骤降 / 日期分布 |
| `fetch_gold.py` | 5 | 全 null / 缺失比例 / 最新一期 null / 价格越界 / 序列退化 |
| `fetch_stocks.py` | 5 | 总量归零 / registered 非正 / 仓库数 / 明细核对 / 总量骤变 |

**判据必须按源分别定义，不能照搬。** `fetch_oi` 的四条硬套到另三个源会系统性
误报：「逐字段相同」对周频源是每周 6 次的常态（日频 workflow 跑周频数据），
「Trade Date 容差 0」在周频源上差 3~10 天，「合约数量」「主力月 OI」在库存和
金价里没有对应概念。三个源真正共享的只有骨架（隔离区写法、exit code 语义、
失败则不覆盖旧文件），判据本身各写一套。

拦截点必须在采集层：坏数据一旦落盘，既显示在页面上，也成为下一交易日差分的
输入，把错误传播到后续所有帧。派生层不该因可重算的计算而拒绝出数据。

闸的位置有两条硬要求：

- **在归一化之前**。`fetch_cot` 的闸必须在 `cot_index()` 之前 —— 后者会把
  退化输入粉饰成中性值，闸放后面看到的是被加工过的数字。
- **在幂等判断之前**。`fetch_stocks` 的闸在 `date in existing_dates` 之前 ——
  坏数据即使日期重复也该被隔离，不能让幂等 `return` 抢先吞掉。

### 时间戳缺失显示「未知」，不许回退 `new Date()`

页面「页面更新」读 `cot.generated_at ?? cot.updated_at`，两者皆缺时显示
**「未知」**。曾经的 `: new Date()` 兜底是把「不知道数据多新」粉饰成「刚刚
更新」—— 数据停更多久页面都显示当前时刻，陈旧完全看不出来。显示不出时间是
小事，谎报新鲜度是大事。

`screenshots/diag-cot-timestamp-injection.mjs` 有一条反恒真注入守着：把兜底
改回 `new Date()`，「显示未知」那条断言必须变红。不变红说明断言只是碰巧成立。

### warning 不刷 `generated_at`，要持久化就走单独日志

数据未变但本次运行产生了新 warning 时：**跳过写盘**，warning 打 stdout。
`generated_at` 只在业务数据真变时刷新 —— 为 warning 刷它会让「文件变了」与
「数据变了」再次脱钩，而这正是幂等要建立的等价关系。

将来某类 warning 确需持久化审计，走**单独日志文件**，不塞进数据文件。

### 静默归零／归 null 是最危险的一类损坏

三个采集脚本都有把解析失败静默转成 0 或 null 的兜底，全部在出口拦截：

| 位置 | 兜底行为 | 拦它的判据 |
|---|---|---|
| `fetch_cot` `i()` | `float(row.get(key) or 0)` → 字段改名变 0 | a) b) c) |
| `fetch_cot` `cot_index()` | 曾经 `mx == mn → 50` | **已改为返回 None** |
| `fetch_gold` `align_price()` | 对不上返回 None | a) b) c) |
| `fetch_stocks` `_to_float()` | 任何异常返回 `0.0` | d) 明细与总量交叉核对 |

`cot_index()` 原先返 50 是**第二层掩盖**：上游全 0 被 `i()` 吞掉，再被归一化
粉饰成「中性 50%」，页面上完全看不出异常。现改为返回 `None`（落盘 null，语义
是"不可知"，与 0"确实为零"严格区分）。

`fetch_stocks` 的 d) 是抓这类问题最强的一条 —— 明细与顶层 total 独立解析，
单仓库静默归零必然让两者不符。

### 判据阈值靠真实数据定，不靠拍

`fetch_stocks` 的 d) 容差是 `max(2 * 仓库数, 32)` oz，绝对阈而非相对阈。

来源：30 期真实数据里 `sum(details) − total` 恒为 −8 ~ −10 oz，追查到是每仓库
`int()` 截断的累积残余（每仓库 < 1 oz，10 个仓库合成个位数），与库存量级无关。

原计划拍的 `total * 0.01` = 27 万 oz **会放过最小仓库 STONEX（17 万 oz）整仓
归零** —— 相对阈在这里恰好是错的工具。绝对阈下同样的注入差值是阈值的 8500 倍。

### exit code 三态

`0` 正常（含幂等跳过）/ `1` 校验失败需人工介入 / `2` 上游未更新。

`fetch_stocks` 的 CME WAF 封锁原先 `exit 0`，把"抓不到"当成正常态、workflow
静默绿灯，现改为 `exit 2`。

`continue-on-error: true` 会把 1 和 2 都记成 `outcome=failure`，闸门分不清。
所以 stocks 步骤显式捕获 exit code 存进 `$GITHUB_OUTPUT`，末尾闸门按 `case`
分三态：`1` → 红，`2` → 只 `::warning::` 不红，其他非零 → 红（未预期）。

**TODO(P1 三态时处理)**：`fetch_gold` 现在有两条语义不同的 `exit 1` 压在同一码上 ——
下载层「Stooq 与 Yahoo 两源皆失败」（属上游不可达，接近 `exit 2` 的语义）与
解析层「新闸 5 项判据命中」（属真损坏，需人工介入）。前者重跑可能自愈，后者不会。
统一模板做 exit code 三态时要把下载层那条分到 `2`，否则闸门无法区分「网络抖动」
与「数据损坏」，会对前者也发红灯。`fetch_cot` 的 `fetch_api()` 网络异常直接崩
（非零退出）同理，也需一并归入 `2`。

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
python3 derive_term_structure.py --test    # 派生逻辑 13 项（含信封契约、KPI）
python3 fetch_oi.py --test                 # 采集校验 23 项（不联网）
python3 fetch_cot.py --test                # 采集校验 16 项（含 cot_index 退化）
python3 fetch_gold.py --test               # 采集校验 12 项（含退化边界）
python3 fetch_stocks.py --test             # 采集校验 18 项（含缺字段 SKIP）
python3 tools/verify-fetch-gates.py        # 端到端注入：闸真的拒绝落盘 24 项
node tools/verify-ui-fixes.mjs             # 柱对齐 / 按钮态 / 轴刻度 / 合约列表
node tools/verify-contract-contango.mjs    # 合约过滤 / 最小柱高 / 价差锚点
node tools/verify-playback.mjs             # 回放交互
node tools/verify-gapframe.mjs             # 断层帧
node tools/verify-isolation.mjs            # 注入渲染故障，验证模块隔离（37 项断言）
node tools/verify-schema-coupling.mjs      # 注入 schema 破坏，验证护栏会变红
node tools/verify-kpi-injection.mjs        # 注入错误 KPI 值，验证护栏会变红
node tools/verify-totaloi-injection.mjs    # 注入旧口径 total_oi，验证护栏会变红
node tools/verify-isolation-injection.mjs  # 注入隔离失效，验证 isolation 会变红
node tools/verify-live.mjs                 # 线上端到端
```

### 验证护栏是否有效，靠注入破坏，不靠读代码推断

「这个脚本看起来会覆盖到」是不可靠的判断。要确认一条护栏真的有效，
往被测对象里注入一个已知破坏，看它是否**变红**。

已四次证明这一步不可省：

| 场景 | 读代码的判断 | 注入实测的结果 |
|---|---|---|
| `verify-isolation` 探针 | 「它注入故障验证模块隔离，有效」 | `initOIPlayback` 搬到 `js/playback.js` 后，脚本只拦 root HTML，注入变成空操作 —— 静默通过，从此不再验证任何东西 |
| 信封化后的四个脚本 | 「它们不直接读 JSON，可能对 schema 破坏无感」 | 注入 `data` 缺失 / `frames` 为空，三图表全建不起来，几何断言必然失败 —— 实际是 fail loudly，无需改动 |
| KPI 算术下沉 | 「四个字段都有断言覆盖」 | 年化率/spread 三种注入都红，但 `total_oi` 改成 1 仍全绿 —— 缺一条断言 |
| `verify-isolation` 判定机制 | 「它是六个 verify 之一，全绿就是隔离正常」 | 该脚本**根本没有断言**，只打印状态；退出码仅反映「脚本自身是否抛异常」。三模块全挂只要不抛也是 exit 0 —— 历史上所有「六个 verify 全绿」里，isolation 那条一直不携带信息 |

方向各不相同：以为有效实际失效、以为失效实际有效、以为覆盖实际缺断言、
以为在断言实际连断言都没有。四次都只有注入才看得出来。

**「没有断言」比「断言写错」更隐蔽。** 断言写错至少会在某次注入里露出来，
而一个只打印不断言的脚本永远是绿的，且它出现在「全绿」清单里会制造安全感。
新增 verify 脚本时先确认三件事：有累加器、结束时按失败数退出、注入能让它红。

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
