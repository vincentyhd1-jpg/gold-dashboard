# 技术交接（截至 3995959）

供新会话接续。只记事实与行号，不含建议与计划。

本文档全部行号与数值在 `3995959` 上重新取值（前一版为 `f8f5204`，其间
`contracts` 口径变更与断言范围变更使行号多次移位，未沿用旧文任何数字）。

## 1. git 状态

```
local  sha = 3995959c9e77cea20200639c16729cd62d8914e3
remote sha = 3995959c9e77cea20200639c16729cd62d8914e3
local == remote = YES
未推送 commit 数 = 0
工作区 = 干净（git status --porcelain 无输出）
分支 = main，跟踪 origin/main
```

数据现状：`oi.json` 25 帧，范围 `2026-06-26 ~ 2026-07-31`，首帧 `2026-06-26`。
派生 `term-structure-series.json`：25 帧 × **15 列**，`schema_version = 0`。

```
contracts = ['JUN26','JUL26','AUG26','SEP26','OCT26','NOV26','DEC26',
             'JAN27','FEB27','MAR27','APR27','MAY27','JUN27','JUL27','AUG27']
info      = ['已到期合约仍保留 X 轴列位（末帧已不挂牌）：JUN26, JUL26']
```

## 2. 最近五个 commit

| sha | 内容 |
|---|---|
| `3995959` | CLAUDE.md 补「执行侧陷阱」一节（四种形态 + 五条硬规则）；按硬规则重验 `ZZZ99` 注入，原结论（`unreliable_chg` 无前端消费端、前端 verify 0 条断言）成立 |
| `3719857` | MISMATCH 抽查 2 的比对范围由 `contracts` 收到该帧 `window_months`；加 `checked` 计数与空转保护 |
| `44aecf2` | `contracts` 改为全序列各帧 `window_months` 的并集（13 → 15 列）；反转两条编码旧口径的前端断言 |
| `a31cdcb` | 落盘 float 统一 `round(12)`，消除跨平台 1 ULP 的 git flapping；加 ROUND fixture |
| `779f480` | 交接文档在 `f8f5204` 上重跑护栏，补「会随数据滑动而消失的红」一节 |

再往前：`f8f5204`（技术交接文档首版）、`36a5504`（Actions 每日数据提交，`oi.json` 24 → 25 帧）、
`bf1cc6b`（`unreliable_chg` 改走 `oi_maps_raw`，加 WINDOW fixture）。

## 3. derive_term_structure.py 结构

### 两份 OI map 分工

建于 `derive()` 内，按字段语义分工：

| 变量 | 定义行 | 装填行 | 内容 | 语义 |
|---|---|---|---|---|
| `oi_maps` | :403 | :407 | `window_months(r["months"])` 筛后 | as-of-now，展示视图 |
| `oi_maps_raw` | :404 | :408 | `r["months"]` 原样，零过滤 | as-of-that-frame，帧级取证 |

```
:406   wm = window_months(r.get("months") or [])
:407   oi_maps.append({m["month"]: m["oi"] for m in wm})
:408   oi_maps_raw.append({m["month"]: m.get("oi") for m in (r.get("months") or [])})
```

字段归属：

| 走 `oi_maps_raw` | 走 `oi_maps` |
|---|---|
| `unreliable_chg`（:485 `prev_oi_map`、:486 遍历） | 其余 15 个帧字段 |

`oi_maps` 的消费点：:418（`peak_oi` / `ever_front` 汇总）、:434（`oi_map = oi_maps[idx]`）、
:510（`prev_oi` 供 `oi_chg` 差分）。

16 个帧字段中 15 个经过至少一道随时间移动的筛，仅 `date` 未经过滤。

### contracts 并集口径（`44aecf2`）

```
:377   last_listed = {m["month"] for m in window_months(records[-1]...)}   ← 仅供 expired 用
:379   all_labels: set[str] = set()
:381   for m in window_months(r.get("months") or []): all_labels.add(...)
:383   expired = sorted(all_labels - last_listed, key=month_key)
:384   contracts = sorted(all_labels, key=month_key)          ← 并集，不再 & last_listed
:388   info = [f"已到期合约仍保留 X 轴列位（末帧已不挂牌）：..."]
```

`last_listed`（:377）保留但只用于算 `expired` 填 `info`，**不再参与 `contracts` 求交**。

口径变更的量化依据：旧口径（`all_labels & last_listed`）下到期合约整列从全序列历史
消失，实测 06-29 掉 `JUN26`、07-30 掉 `JUL26`，两列在仍存续的 23 帧上有真实
`settle`/`oi` 却无列可放（24 格）。模拟 `AUG26` 到期，它在全部 25 帧失去列位，
其中 19 帧它是 `front`、24 帧它是 `roll_from`。代价：空列比例 7.4% → 13.3%。

轴长度恒定（不逐帧变），前端 `_initCharts` 一次性写 `labels` 零改动可用。

### _round_floats() 落盘位置（`a31cdcb`）

```
:1183  ROUND_NDIGITS = 12
:1186  def _round_floats(o)          递归；bool 是 int 子类不被 round；int 保持 int
:1238  data=_round_floats({...})     ← 唯一的生产调用点，在 envelope() 内、写盘前最后一刻
```

`derive()` 的返回值仍是全精度。`roll_noise_ma` 的 3 帧滚动用的是**未 round** 的
`roll_noise`，算完才在 :1238 统一 round。`front_remaining` 在计算层已 `round(4)`，
再 round(12) 是幂等的。

`--test` 路径的 :1110 `envelope(...)` **刻意不过** `_round_floats`（见 :1108-1109 注释）：
那条测的是「信封不改业务数据」，与「落盘时降精度」是两件事。

### 关键常量

```
:38    MIN_NEXT_OI_RATIO   = 0.01    末端承接月补位下限（未跑分布的拍值）
:42    ROLL_DONE_OI_RATIO  = 0.02    移仓完毕判定
:45    ROLL_WINDOW_OI_RATIO= 0.5     in_roll_window 判定
:50    ROLL_NOISE_MA_DAYS  = 3       roll_noise 移动平均窗口
:1183  ROUND_NDIGITS       = 12      落盘精度
```

### 写盘链

```
:27    from io_utils import atomic_write_json, sweep_stale_tmp
:1216  swept = sweep_stale_tmp(OUT_PATH)
:1235  payload = envelope(...)  含 :1238 的 _round_floats
:1250  atomic_write_json(OUT_PATH, payload)
```

`io_utils` 的序列化参数（注入脚本必须复用，否则 diff 被重排淹没）：

```
io_utils.py:95-96   json.dumps(payload, ensure_ascii=False, separators=(",", ":"))   紧凑档
io_utils.py:98      json.dumps(payload, ensure_ascii=False, indent=2)                缩进档
```

## 4. 护栏清单与当前状态

全部在 `3995959` 上实测。**Python 侧经 `wsl -d Ubuntu-22.04 --cd <repo> -- python3`，
前端 `.mjs` 从 PowerShell 原生调 `node`** —— 执行侧错配会产生假红/假绿，见 CLAUDE.md
「执行侧陷阱」一节。

### 常规护栏

**本表须与测试条数变化在同一 commit 内更新。**

| 护栏 | exit | 结果 |
|---|---|---|
| `derive_term_structure.py --test` | 0 | 21 passed, 0 failed |
| `fetch_cot.py --test` | 0 | 26 passed, 0 failed |
| `fetch_gold.py --test` | 0 | 38 passed, 0 failed |
| `fetch_oi.py --test` | 0 | 62 passed, 0 failed |
| `fetch_stocks.py --test` | 0 | 22 passed, 0 failed |
| `tools/verify-fetch-gates.py` | 0 | 37 passed, 0 failed |
| `tools/verify-io-utils.py` | 0 | 100 passed, 0 failed |
| `tools/verify-ui-fixes.mjs` | 0 | 22 passed, 0 failed；page errors: none |
| `tools/verify-contract-contango.mjs` | 0 | 29 passed, 0 failed；page errors: none |
| `tools/verify-playback.mjs` | 0 | page errors: none |
| `tools/verify-gapframe.mjs` | 0 | page errors: none |
| `tools/verify-isolation.mjs` | 0 | 37 passed, 0 failed |
| `tools/verify-schema-coupling.mjs` | 0 | 3 passed, 0 failed |
| `tools/verify-envelope-helper-raw-inputs.mjs` | 0 | 5 passed, 0 failed |
| `tools/verify-cot-sentinel-strict.mjs` | 0 | 4 passed, 0 failed |

前端 verify 中 ui-fixes(22)/contract-contango(29)/isolation(37)/
schema-coupling(3)/envelope-helper-raw-inputs(5)/cot-sentinel-strict(4) 有计数；
playback/gapframe 无 passed/failed 累加器，
仅凭 exit code，清点全绿时不构成计数证据。

`verify-ui-fixes` 与 `verify-contract-contango` 的通过数各 +1（12→13 / 28→29）：
`44aecf2` 反转了那条编码旧口径的断言（「JUN26 已到期已剔除」→「已到期合约仍保留
X 轴列位（末帧已不挂牌）」），措辞与 derive 的 `info` 文案一致，断言未删。

### 注入类护栏（五条）

每条自身 exit 0 表示「注入→红、还原→绿」的完整序列成立。逐阶段实测：

| 护栏 | exit | 阶段序列 |
|---|---|---|
| `tools/verify-kpi-injection.mjs` | 0 | 基线绿 → 4 种注入（年化改错值 / 年化置 null / spread 改错值 / total_oi 改 1）全部 exit=1 变红 |
| `tools/verify-spread-injection.mjs` | 0 | 绿 → 红 → 改回后绿（29 passed, 0 failed） |
| `tools/verify-totaloi-injection.mjs` | 0 | 绿 → 红 → 改回后绿（29 passed, 0 failed） |
| `tools/verify-isolation-injection.mjs` | 0 | 基线绿 → 2 种注入（`_safeRender` 吞异常 / 隔离失效双模块受害）均红 → 改回绿 |
| `tools/verify-noise-injection.py` | **无法运行** | 见下 |

**注入类 verify 之间无隔离。** 它们都改 `data/derived/term-structure-series.json`
再改回，但改回用的是自己记的原值，不是 derive 重算的产物。连跑时前一条的残留会让
后一条的基线阶段假红 —— 实测 `verify-totaloi-injection` 因此显示「改回后 exit=1 红」，
重跑 derive 后转绿。**一条跑完必须重跑 `python3 derive_term_structure.py` 再跑下一条。**

### verify-noise-injection 静默失效（「全绿」不含它）

```
WSL 侧运行:      exit=1  FileNotFoundError: [Errno 2] No such file or directory: 'wsl'
Windows 侧 python3: C:\Users\vince\AppData\Local\Microsoft\WindowsApps\python3.exe（Store 存根）
```

该脚本内部 `subprocess.run(["wsl", "-d", "Ubuntu-22.04", ...])`，必须从 Windows 侧
启动才能调到 WSL；而本机 Windows 侧只有 Store 存根（运行即打印安装提示后退出），
没有真实 Windows Python。两条路都不通。

**清点「全绿」时不得把它算进去。** 待修：改成不依赖执行侧的调用方式
（按 `platform.system()` 分支选 `python3` 直调，或改写成 `.mjs`）。

### derive --test 全绿的判读

当前 17 条全部是 PASS，**无 SKIP**。抽查 2 现在报的是：

```
PASS  2026-07-24: 窗口内 13 条 oi_chg 与 CME stored 相符
      （窗口 13 个月份，stored 共 27 条，窗口外 14 条未计算不参与对账）
```

`f8f5204` 时点那条红（`MISMATCH 2026-07-24: AUG27 stored=+11 computed=None`）
已由 `3719857` 通过收窄比对范围消除 —— **不是改断言迁就**：注入证明确认该断言
仍会红（见第 7 节）。

## 5. fixture 清单与自检锚点

| fixture | 定义行 | 测什么 | 自检锚点 |
|---|---|---|---|
| `FIXTURE` | :614 | day1 首帧全 None / day2 差分与 CME 一致且无标记 / day3 修订取 diff 并标记 | 无独立锚点 |
| `ROLL_FIXTURE` | :667 | `front_remaining` 1.0→0.05 单调递减；`front` 交叉切换而 `roll_from` 不动；无承接月时 `roll_to=None` 但 `front_remaining` 仍有值 | 无独立锚点 |
| `TRENDS` | :755 | 5 种趋势场景（涨 20x ~ 跌 1/20）主力月判定均无失效帧 | 场景自带趋势倍率，任一帧失效即红 |
| `KPI_FIXTURE` | :866 | `total_oi=200000`（B2 只含 `ever_front`）/ `spread=122.0` / `gap=122天` / 年化 `9.125` 全精度 | 断言直接比全精度值，容差 `1e-9` |
| `B2_FIXTURE` | :936 | `total_oi=270000` 只含已坐正主角 | 三重：得 `286000` 判定误用 `major_months`（含 FEB27 补位）、得 `318000` 判定退回全部挂牌月旧口径、`FEB27` 须在 `major_months` 却不在 `total_oi` |
| `NOISE_FIXTURE` | :978 | `roll_noise` 落在无限小数（`1000/3001` 形态），实测 `0.3332222592469177` | 落盘值若等于自身 `round(4)` 即判定被舍入；容差 `1e-12`（当前误差 `8.23e-14`，仅差一个数量级，若将来 round 收紧到 10 位这条会踩线失效） |
| `WINDOW_FIXTURE` | :1028 | 窗口外修订合约 `DEC27`（stored `-7` ≠ 差分 `+50`）必须进 `unreliable_chg` | 三重：`DEC27` 必须不在 `window_months` 返回值（:1045 取值比对）、必须不在 `contracts`、`unreliable_chg` 须只含 `DEC27` |
| `rf_in`（ROUND fixture） | :1073 | `_round_floats()` 本身：float→12 位、int/bool/None/str 原样、嵌套递归、幂等 | 幂等自检 :1098 `_round_floats(rf) != rf`；bool 专项（bool 是 int 子类，不得被 round） |

真实数据抽查（非 fixture）：

| 抽查 | 行 | 内容 | 当前 |
|---|---|---|---|
| 1 | :782-794 | 序列首帧 `oi_chg` 全 None（断言「首帧」而非固定日期） | PASS `2026-06-26` |
| 2 | :808-842 | `2026-07-24` **窗口内** `oi_chg` 与 CME stored 逐合约比对 | PASS 13 条相符 |
| 3 | :846-858 | `2026-07-27` `known_revised` 四合约必须被标记 | PASS |

### 抽查 2 的空转保护（`3719857`）

范围收窄带来一个新失效模式：若窗口内一条 stored 都不剩，循环空转、`mismatches`
为空，断言会静默变绿。故加 `checked` 计数：

```
:815   window24 = {m["month"] for m in window_months(r24.get("months") or [])}
:817   checked = 0
:823   if contract not in window24: continue     ← 校验范围跟计算范围对齐
:825   ci = contract_idx.get(contract)
:828   checked += 1
:832   if not checked:  errors.append("抽查 2 自身失效：...无一条 stored oi_chg 可对账")
:838   elif mismatches: errors.append("MISMATCH 2026-07-24: ...")
:840   else:            print("PASS ...窗口内 {checked} 条...")
```

`known_revised = {"AUG26","DEC26","OCT26","JUL26"}`（:853）是会过期的事实集合 ——
合约到期后该集合不再对应当前数据。

三条抽查均带滑窗保护：帧已滑出窗口（:810 判定 / :811 打印 SKIP）或成为序列首帧
（:812 判定 / :813 打印 SKIP）时 SKIP 而非 FAIL。

## 6. 关键行号索引

全部在 `3995959` 上重取。

### derive_term_structure.py

```
:323   def window_months()
:377   last_listed —— 经 window_months(records[-1])，仅供 expired
:381   all_labels 累积 —— 经 window_months
:384   contracts = sorted(all_labels, key=month_key)      并集口径
:403   oi_maps 定义        :404  oi_maps_raw 定义
:406   wm = window_months(...) 逐帧（建 map 用）
:418   for om in oi_maps —— peak_oi / ever_front 汇总
:432   wm = window_months(...) 逐帧（建帧用）
:434   oi_map = oi_maps[idx]
:485   prev_oi_map = oi_maps_raw[idx - 1]      取证字段专用
:486   for label, oi_v in oi_maps_raw[idx].items()
:510   prev_oi = oi_maps[idx - 1].get(label)   oi_chg 差分取值
:554   frames.append({...})   :563 roll_noise   :565 unreliable_chg
:777   contract_idx = {c: i for i, c in enumerate(result["contracts"])}
:815   window24 —— 抽查 2 的比对范围
:829   computed = f24["oi_chg"][ci]            MISMATCH 读此
:838   errors.append("MISMATCH 2026-07-24: ...")
:853   known_revised = {"AUG26","DEC26","OCT26","JUL26"}
:1045  w_window —— WINDOW fixture 自检
:1163  print(f"\n{len(errors)} failed")  :1164 sys.exit(1)  :1166 "0 failed"
:1183  ROUND_NDIGITS = 12    :1186 def _round_floats()
:1216  sweep_stale_tmp(OUT_PATH)
:1238  data=_round_floats({...})   生产落盘唯一 round 点
:1250  atomic_write_json(OUT_PATH, payload)
```

`window_months()` 调用处共 6 个：:377、:381、:406、:432、:815（抽查 2，新增）、
:1045（fixture 自检）。

### 前端五个读取点（index.html）

| 行 | 文件 | 解包 |
|---|---|---|
| :1252 | `data/cot.json` | `p?.data ? {...p.data, generated_at: p.generated_at} : p` —— 双形状 |
| :1255 | `data/gold_price.json` | 直接 `r.json()`，**无**双形状兼容 |
| :1258 | `data/stocks.json` | 有双形状兼容 |
| :1262 | `data/oi.json` | 直接 `r.json()`，`.catch(() => [])` |
| :1263 | `data/derived/term-structure-series.json` | 有双形状兼容 |

汇合于 :1266 `.then(([cotData, goldData, stocksData, oiData, seriesData])`。
`_safeRender` 定义在 :1235，调用 :1269-1279（正常路径五处）、:1286-1290（mock 兜底五处）。

### 前端 X 轴（js/term-structure.js）

```
:70    function _initCharts(series)      —— 全序列建图一次
:71    const { contracts, scale } = series
:72    const labels = contracts
:85    labels（oiChart data.labels）      :88/:92  new Array(labels.length)
:164   labels（oiDeltaChart）             :165     new Array(labels.length)
:229   labels: rollDates（roll 图用日期轴，与合约轴无关）
:291   function _renderFrame(frameIdx, animated)  —— 逐帧只改 data，不重建 labels
:299   contracts.indexOf(fr.front)  :304 backgroundColor
:319-321  new Array(contracts.length)   :341 KPI months
```

**X 轴 labels 建图时固定一次，回放逐帧不重建。** `_initCharts` 仅由
`js/playback.js:100` 调用一次；`_renderFrame` 全函数无 `data.labels` 赋值。

### 时间戳

```
index.html:479   const stamp = cot.generated_at ?? cot.updated_at;
index.html:480-482  缺失时 '未知'，不回退 new Date()
index.html:484   label 文案「页面更新：」
```

来源为 `data/cot.json`（:1252），**不是**派生文件。`fetch_cot.py` 有幂等跳过，
比对 `unwrap()` 后的业务数据全量，信封元数据排除 —— 该时间戳不因脚本重跑而跳动。

`term-structure-series.json` 的 `generated_at` 不进页面显示；derive **无**幂等跳过
（:1250 `atomic_write_json` 无条件写；未从 `io_utils` 导入 `read_json_or`，见 :27），
每跑必刷。实测：本机重跑 derive 后 `git diff --numstat` = `1 1`，逐字段比对
`dates`/`contracts`/`scale`/`warnings`/`info`/`coverage` 全相同、**帧字段差异 0 处**，
差异仅 `generated_at` 一处（`10:09:51Z` → `11:06:58Z`）。

### 静默退化点

| 位置 | 兜底 |
|---|---|
| `fetch_cot.py:64-65` | `def i()` → `float(row.get(key) or 0)` |
| `fetch_cot.py:99-100` | `cot_index()` `mx == mn` → 返 `None`（原返 50） |
| `fetch_gold.py:126` | `align_price()` 对不上 → `return None` |
| `fetch_stocks.py:139-143` | `_to_float()` 任何异常 → `0.0` |
| `fetch_oi.py:336` | `max(months, key=lambda r: r.get("oi") or 0)` |
| `index.html:470` | `mfVals.map(v => (mx===mn) ? 50 : ...)` —— 与 `cot_index()` 返 `None` 语义不一致，前端仍粉饰成 50%；无 verify 覆盖 |

## 7. 已知隐患

### 无 verify 覆盖

- **`term-3d.html`** —— 绕过派生层直接 `fetch('data/oi.json')`（:274），自建
  `xLabels`（:161），不读派生 `contracts`。全部 Playwright 脚本只测 `index.html`。
  依赖字段 `r.date` / `r.months[]` / `m.month` / `m.settle` / `m.oi`。
  **`44aecf2` 时手验过一次**（15 列 `JUN26`~`AUG27`、`z` 矩阵 25×15、`surface` trace
  存活、pageerror 0）—— 但**该手验是用 Plotly stub 顶替 CDN 做的**（本机
  `cdn.plot.ly` 离线），只证明了数据通路跑通，**未验证真实 Plotly 渲染**。
  真实渲染是否正常仍未知。
- **`unreliable_chg` 无前端消费端** —— 全仓静态读取点只有两处，均非消费：
  `js/playback.js:169`（降级 mock 字面量 `unreliable_chg: null`，写非读）、
  `tools/verify-gapframe.mjs:26`（把它置 `null` 用于构造断层帧，断言对象不是它）。
  六个前端 verify 对它 **0 条断言**。
  **`3995959` 已按执行侧硬规则重验**：注入 `ZZZ99` 经 `git diff` 确认落地
  （`1 1` 行，`"unreliable_chg":null` → `["ZZZ99"]`，帧 12 `2026-07-15`），
  并确认浏览器真吃到那份文件（服务端送出 `{bytes:14917, hasGhost:true}`、页面内
  `fetch` 复查命中）。结果：六个 verify 全 exit 0 且通过数与基线相同、
  `pageerror` 0、`console error` 0、X 轴 15 列不含 `ZZZ99`、25 帧逐帧扫 DOM 文本
  命中 0。**该字段的护栏只能加在 Python 侧。**
- **`index.html:1255`（gold_price.json）** 无双形状兼容 —— 该文件信封化时此处会断，
  且断裂本地不暴露（磁盘文件要等下次 Actions 跑才变形）。
- **`index.html:470`** COT Index 显示值 `mx===mn → 50`，与采集层 `cot_index()` 返
  `None` 语义不一致，无任何 verify 校验该显示值。

### 偶发失败

- **`tools/verify-ui-fixes.mjs`** —— 批量连跑六个前端 verify 时出现过一次
  `waitForFunction` 30s 超时、exit=1；单独重跑 exit=0 / 13 passed。当时数据文件
  完好、dev server 正常。表现为并发争用，非代码缺陷。

### 静默失效的护栏

- **`tools/verify-noise-injection.py`** 本机跑不起来（见第 4 节）。**「全绿」不含它。**
- **`tools/verify-isolation.mjs`** 历史上有过「只打印不断言」的时期（退出码仅反映
  脚本是否抛异常），`63e28c5` 补齐后现为 37 条真断言。
- **注入类 verify 无相互隔离**（见第 4 节），连跑会互相污染基线。

### 抽查 2 会随窗口滚动转 SKIP，而口径问题未必已解

`oi.json` 保留 730 条滚动窗口。当 `2026-07-24` 滑出窗口（:810）或成为序列首帧
（:812）时，抽查 2 转 SKIP。**转 SKIP 后 `derive --test` 依然全绿，但这块的口径
正确性从此不再被任何断言检查。**

`3719857` 收窄比对范围解决的是「拿展示范围当校验范围」这一处。它是否解决了
`oi_chg` 全部的口径问题，在断言转 SKIP 之后无法从测试颜色判断 —— 只能读代码确认
`window24`（:815）这条路径是否还在。

抽查 1、抽查 3 同样绑定具体日期，都会随窗口滚动转 SKIP。抽查 3 还额外绑定
`known_revised`（:853）这个会过期的事实集合。

**判读规则：看到 `derive --test` 全绿时，先确认抽查 1/2/3 是 PASS 还是 SKIP。**
输出前缀不同不会混淆，但总计行只数 `errors`，SKIP 不计入。
当前（`3995959`）三条均为 PASS，无 SKIP。

### 浮点末位差（机制仍在，round(12) 后未再复现）

`bf1cc6b` 时点：`roll_noise_ma` 3 帧与仓库基线末位不一致，量级 `1e-17`；
`36a5504`（Actions 在 CI 机器重跑）把这 3 个值全部改回，Δ 均为 1 ULP，
差异帧恰好是记录的那 3 个（`2026-07-06` / `2026-07-09` / `2026-07-23`）。

`a31cdcb` 已实施 `_round_floats()` round(12)（:1238，只在落盘那一刻），
一次性 diff 94 处（Δ 均 1e-13 量级）。**真正的验证是「下一次 Actions 跑完，那 3 处
`roll_noise_ma` 不再被翻回」** —— 本机跑不出平台差异，只能等 CI。若仍被翻回，
说明 12 位不够（第 13 位在舍入边界），需复议。

禁止用容差比对代替：容差会让真实的微小变化也被判为没变，而 CFTC/CME 的历史修订
可能只差几个单位。

### 取证禁忌（已进 CLAUDE.md）

两节：**「WSL 取证通则：跨 shell 传字符串必写成文件」**（`echo $?` / `| tail -N` /
`case "$1"` 三种形态）与 **「执行侧陷阱：用哪个 shell / 哪个解释器启动会静默改变
结果」**（Store 存根静默不执行 / `bash -c` 包装取错码 / WSL bash 跑 `.mjs` 假红 /
注入写盘格式与生产不一致致 diff 被重排淹没，四种形态 + 五条硬规则）。

PowerShell 侧 `$?` 是布尔值，取码须用 `$LASTEXITCODE`。
