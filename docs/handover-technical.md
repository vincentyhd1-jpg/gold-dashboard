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

当前基线包含 C18B 新增护栏。**Python 侧经 `wsl -d Ubuntu-22.04 --cd <repo> -- python3`，
前端 `.mjs` 从 PowerShell 原生调 `node`** —— 执行侧错配会产生假红/假绿，见 CLAUDE.md
「执行侧陷阱」一节。

### 常规护栏

**本表须与测试条数变化在同一 commit 内更新。**

| 护栏 | exit | 结果 |
|---|---|---|
| `derive_term_structure.py --test` | 0 | 21 passed, 0 failed |
| `fetch_cot.py --test` | 0 | 66 passed, 0 failed |
| `fetch_gold.py --test` | 0 | 42 passed, 0 failed |
| `fetch_oi.py --test` | 0 | 63 passed, 0 failed |
| `fetch_stocks.py --test` | 0 | 27 passed, 0 failed |
| `fetch_fred.py --test` | 0 | 67 passed, 0 failed |
| `fetch_treasury_debt.py --test` | 0 | 32 passed, 0 failed |
| `derive_macro.py --test` | 0 | 53 passed, 0 failed |
| `tools/verify-fetch-gates.py` | 0 | 63 passed, 0 failed |
| `tools/verify-io-utils.py` | 0 | 109 passed, 0 failed |
| `tools/verify-browser-launch.mjs` | 0 | 13 passed, 0 failed |
| `tools/verify-injection-wrappers.mjs` | 0 | 64 passed, 0 failed |
| `tools/verify-ui-fixes.mjs` | 0 | 38 passed, 0 failed；page errors: none |
| `tools/verify-contract-contango.mjs` | 0 | 29 passed, 0 failed；page errors: none |
| `tools/verify-playback.mjs` | 0 | page errors: none |
| `tools/verify-gapframe.mjs` | 0 | page errors: none |
| `tools/verify-isolation.mjs` | 0 | 37 passed, 0 failed |
| `tools/verify-schema-coupling.mjs` | 0 | 3 passed, 0 failed |
| `tools/verify-envelope-helper-raw-inputs.mjs` | 0 | 8 passed, 0 failed |
| `tools/verify-cot-sentinel-strict.mjs` | 0 | 4 passed, 0 failed |
| `tools/verify-macro-page.mjs` | 0 | 86 passed, 0 failed；page errors: none |
| `fetch_treasury_fiscal.py --test` | 0 | 28 passed, 0 failed |
| `derive_fiscal_stress.py --test` | 0 | 34 passed, 0 failed |
| `derive_fiscal_risk_monitor.py --test` | 0 | 52 passed, 0 failed |
| `derive_gold_vs_debt.py --test` | 0 | 23 passed, 0 failed |
| `fetch_cbo_baseline.py --test` | 0 | 22 passed, 0 failed |
| `derive_cbo_scenario_basis.py --test` | 0 | 26 passed, 0 failed |
| `tools/verify-fiscal-stress-page.mjs` | 0 | 46 passed, 0 failed；page errors: none |
| `tools/verify-cbo-baseline-page.mjs` | 0 | 24 passed, 0 failed；page errors: none |
| `tools/verify-cbo-scenario-engine.mjs` | 0 | 20 passed, 0 failed |
| `tools/verify-cbo-scenario-page.mjs` | 0 | 22 passed, 0 failed；page errors: none |
| `tools/verify-fiscal-risk-monitor-page.mjs` | 0 | 41 passed, 0 failed；page errors: none |
| `tools/verify-fiscal-risk-monitor-snapshot-contract.mjs` | 0 | 8 passed, 0 failed |
| `tools/verify-gold-debt-python.mjs` | 0 | 23 passed, 0 failed；动态 WSL root/cwd 一致 |
| `tools/verify-treasury-enhancements.mjs` | 0 | 30 passed, 0 failed；page errors: none |
| `tools/verify-static-build.mjs` | 0 | 52 passed, 0 failed；41 个公开文件逐字节对账 |
| `tools/verify-static-build-injection.mjs` | 0 | 38 passed, 0 failed；六项部署破坏均红且恢复 |

前端 verify 中 ui-fixes(38)/contract-contango(29)/isolation(37)/
schema-coupling(3)/envelope-helper-raw-inputs(8)/cot-sentinel-strict(4)/
macro-page(86)/fiscal-stress-page(46)/cbo-baseline-page(24)/static-build(52) 有计数；
playback/gapframe 无 passed/failed 累加器，
仅凭 exit code，清点全绿时不构成计数证据。

`verify-ui-fixes` 与 `verify-contract-contango` 的通过数各 +1（12→13 / 28→29）：
`44aecf2` 反转了那条编码旧口径的断言（「JUN26 已到期已剔除」→「已到期合约仍保留
X 轴列位（末帧已不挂牌）」），措辞与 derive 的 `info` 文案一致，断言未删。

### 注入类护栏（十二条，含 wrapper meta guard）

每条自身 exit 0 表示「注入→红、还原→绿」的完整序列成立。逐阶段实测：

| 护栏 | exit | 阶段序列 |
|---|---|---|
| `tools/verify-kpi-injection.mjs` | 0 | wrapper 32/0；基线 29/0 → 4 种注入逐项红且 marker 命中 → 恢复 29/0 + hash 一致 |
| `tools/verify-spread-injection.mjs` | 0 | wrapper 11/0；基线 29/0 → spread 注入 28/1 → 恢复 29/0 + hash 一致 |
| `tools/verify-totaloi-injection.mjs` | 0 | wrapper 11/0；基线 29/0 → 旧 total_oi 口径 28/1 → 恢复 29/0 + hash 一致 |
| `tools/verify-isolation-injection.mjs` | 0 | wrapper 18/0；基线 37/0 → 两种隔离破坏分别红 → 恢复 37/0 + hash 一致 |
| `tools/verify-cot-index-null-injection.mjs` | 0 | wrapper 18/0；基线 38/0 → current null→50 为 31/7、chart null→50 为 36/2 → 恢复 38/0 + hash 一致 |
| `tools/verify-debt-overview-injection.mjs` | 0 | wrapper 81/0；基线 86/0 → C15 四项及 C14 七项注入均真实变红 → 恢复 86/0 + hash 一致 |
| `tools/verify-noise-injection.py` | 0 | 基线 21/0 → 3 种注入均 exit=1 且命中对应 NOISE 断言 → 恢复后 21/0；生产文件 hash 一致 |
| `tools/verify-injection-wrappers.mjs` | 0 | 43/0；静态锁九 wrapper，动态覆盖成功、基线红、signal、假绿、no-op、restore mismatch、patch throw |
| `tools/verify-static-build-injection.mjs` | 0 | 23/0；缺 assets.directory、缺 macro.html、泄漏 Python 均真实红 → 恢复 34/0 + config hash 一致 |
| `tools/verify-fiscal-stress-injection.mjs` | 0 | MTS 11/0 + derive 46/0 + frontend 74/0；原八项、C17.1 五项及 Fiscal Gap 图五项注入均真实红，三个目标最终 hash 一致 |
| `tools/verify-cbo-baseline-injection.mjs` | 0 | parser 32/0 + frontend 18/0；四项 parser/vintage 与两项 frontend 注入均真实红，两个目标最终 hash 一致 |
| `tools/verify-cbo-scenario-injection.mjs` | 0 | engine 46/0 + page 18/0；八项 engine/page 注入均真实红，两个目标最终 hash 一致 |

C10-C18A 的八个 Node wrapper 都从运行前同一份原始 bytes 为每个 case 重建，backup 位于
系统临时目录；每 case、恢复后基线与最外层 `finally` 都校验 SHA-256。任一 wrapper
exit 0 已包含「未污染下一个 wrapper」的证据，不再需要在 wrapper 之间重跑 derive。
`verify-noise-injection.py` 继续独立使用同等严格的 WSL/Python 恢复契约。
`verify-static-build-injection.mjs` 因同时覆盖 config 与 ignored dist 产物，使用专门的
多目标 wrapper；配置备份仅放系统临时目录，A/B/C 每案后重建 dist，最终与 finally
同时校验 config SHA-256 和 guard 28/0。

### verify-noise-injection 执行链（C5 已修复）

```
wsl -d Ubuntu-22.04 --cd /mnt/d/VScode/test/gold-dashboard -- \
  python3 -B tools/verify-noise-injection.py
```

旧脚本从 WSL Python 内再次调用 Windows `wsl.exe`，报 `FileNotFoundError`；Windows
侧又只有 Store Python 存根，因此两条启动路径都不可用。C5 改为在 WSL 内用
`sys.executable` + `subprocess.run(..., shell=False)` 直跑目标测试，并以
`CompletedProcess.returncode` 判定真实红绿。脚本自身只有在基线/恢复均 exit 0、
三种注入均非零且命中预期 NOISE 断言时才 exit 0。

源码备份位于系统临时目录；每个 case 后及最外层 `finally` 都恢复，并校验原文件
SHA-256。实测三种注入分别让 `roll_noise` round(4)、`roll_noise_ma` round(4)、
`roll_noise × 2` 的对应断言变红，脚本最终 exit 0。另将首个注入临时改成 no-op，
脚本以「注入未落地，文件 hash 未变化」exit 1，证明包装器不会只打印结果后恒绿。

### derive --test 全绿的判读

当前全部是 PASS，**无 SKIP**（条数见上方基线表，不在此处复述）。抽查 2 现在报的是：

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

### 前端五个读取点（index.html，C6 strict）

| 读取点 | 文件 | 解包 |
|---|---|---|
| COT | `data/cot.json` | `loadJson()` strict；map 保留信封 `generated_at` |
| gold | `data/gold_price.json` | `loadJson()` strict |
| stocks | `data/stocks.json` | `loadJson()` strict，模块级 catch 隔离 |
| OI | `data/oi.json` | `loadJson()` strict，模块级 catch 隔离 |
| term structure | `data/derived/term-structure-series.json` | 保留完整信封，`initOIPlayback()` strict 解包 |

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
index.html       const stamp = cot.generated_at;
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
- **schema v0 strict-reader 收口（C6）**：受跟踪生产 JSON 均已 envelope 化；
  `fetch_gold` 的 COT 上游、四个采集器旧文件读取、`upstream_ref()` 与两个旧 `.mjs`
  护栏不再接受 bare。文件首次不存在仍按原有业务语义处理。
- **schema v1 未启动**：`SCHEMA_VERSION=0`、`KNOWN_SCHEMA_VERSIONS={0}` 保持不变，
  等未来新源接入并冻结格式后再单独决策。
- **COT Index null 语义已于 C11 收口**：weekly Index 由 `fetch_cot.py` 预计算，
  前端不再回算；单侧/双侧 null 的 DOM、Chart 与综合信号由真实 fixture 和 injection
  wrapper 锁死。

### 偶发失败

- **`tools/verify-macro-page.mjs`** —— 页面从 CDN 取 `chart.umd.min.js`，取不到时
  `Chart` 始终 undefined、一张图都建不起来。本机四连跑中两次触发（重试 1~2 次后成功）。
  脚本内已加 3 次重试 + 每次重试清空 `errors`；三次全失败才红，且红的文案会点明
  「Chart.js（CDN）未加载：环境问题」以免被当成页面缺陷。
- **`tools/verify-ui-fixes.mjs`** —— 批量连跑六个前端 verify 时出现过一次
  `waitForFunction` 30s 超时、exit=1；单独重跑 exit=0 / 13 passed。当时数据文件
  完好、dev server 正常。表现为并发争用，非代码缺陷。

### 曾静默失效或仍需注意的护栏

- **`tools/verify-noise-injection.py`** 的跨层启动与假绿路径已由 C5 修复（见第 4 节）。
- **`tools/verify-isolation.mjs`** 历史上有过「只打印不断言」的时期（退出码仅反映
  脚本是否抛异常），`63e28c5` 补齐后现为 37 条真断言。
- **注入类 verify 的相互污染已由 C10 修复**（见第 4、12 节）。

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

## 8. C4/C12 联邦债务前端与 workflow 闭环

`macro.html` 通过公共 `loadJson()` 严格解包
`data/derived/macro_debt.json`，与 rates/CPI 使用独立 Promise 错误边界。
债务读取或渲染失败只更新 `debtStatus`，不会移除 UST/Fed/CPI；rates/CPI
加载失败也不会阻止债务图建起。

债务卡片在 C12 收口为一张 `debtOverviewChart` 双 Y 轴综合图，共六个 dataset：

- `intragov_bn`、`domestic_public_bn`、`foreign_bn` 是共同 `debtStructure` stack
  的三条互斥柱，绑定左轴 `yAmount`（`USD bn`）。完整季度三项之和与 `total_bn`
  对账；完整 public 不进入 stack，避免 foreign 重复计算。
- `total_bn` 与原本已存在于派生文件的 `gdp_bn` 是左轴金额折线。
- `debt_gdp_pct` 是右轴 `yPct`（`%`）比例折线。`public_gdp_pct` 仍保留在
  schema v0 派生数据中，但不再作为 C12 主图 dataset。

前端不做单位换算、不重算 `domestic_public_bn`、不 forward-fill。`stack_last`
之后三个结构字段保持 null；同季度 `total_bn`、`gdp_bn` 与 `debt_gdp_pct` 若派生
文件仍有值，三条线继续按真实 coverage 显示。

`tools/verify-macro-page.mjs` 当前为 **86 passed, 0 failed**，覆盖真实请求、单一
canvas、六 dataset 逐点对账、共同 stack 与像素几何、左右轴/单位、真实鼠标统一
tooltip、完整季度恒等式映射、foreign 右端缺口、金额/比例独立延伸、drag/reset/移动端，
以及 debt ↔ rates/CPI 双向加载故障隔离。`verify-debt-overview-injection.mjs` 当前
**81/0**，11 项破坏均红且恢复后重新 86/0、文件 SHA-256 一致。

`.github/workflows/update-cot.yml` 的 FRED 基线为 13 个序列。提交清单包含五个
债务/GDP 原始信封（`debt_total.json`、`debt_held_public.json`、
`debt_intragov.json`、`debt_foreign.json`、`gdp_nominal.json`）和
`data/derived/macro_debt.json`；宏观派生说明与错误信息按 rates/CPI/debt 三链维护。

## 9. C7 COT 网络失败与 workflow 退出码

`fetch_cot.py` 现以 `CotFetchFailure` / `CotFormatFailure` 明确分开临时上游故障与
确定性响应格式错误。DNS、连接、timeout、TLS、HTTP 403/408/425/429/5xx 和可识别的
临时错误页/WAF 返回 exit 2；成功响应的非 JSON、顶层 schema 变化、字段解析失败，
以及既有五项业务校验失败返回 exit 1。两类失败都不覆盖旧 `cot.json`；只有取得了
可诊断响应的格式/业务失败写 quarantine，完全未取得响应的网络失败不伪造证据。

workflow 的 COT step 将 Python 的真实退出码写入 `steps.cot.outputs.code`，统一闸门
按 `0/1/2/*` 分流：1 和未预期值报红，2 只 warning 并等待下一次任务重试。
`fetch_cot.py --test` 的临时目录 sentinel SHA-256 测试与
`tools/verify-fetch-gates.py` 的子进程/workflow 结构断言共同保护该契约。

## 10. C8 FRED 数据安全回归

`fetch_fred.py --test` 在 C8 当时为 **60 passed, 0 failed**。新增测试全部使用
`TemporaryDirectory` 与固定 fake response，不访问 FRED 网络、不写真实 `data/`：

- 相同业务输入第二次处理时，`atomic_write_json` spy 必须保持零调用，目标文件
  bytes 与 SHA-256 同时不变。
- `"."` 观测不会生成 0、前值或插值点；输出 data 精确为两个硬编码有效点，
  coverage 固定为 `first=2026-01-01 / last=2026-01-03 / count=2`。
- c 类写入独立的 payload/raw quarantine，原因与原响应可追溯，同时旧主文件
  bytes/SHA-256 不变。
- d 类写入 schema v0 failure envelope：`data:null`、`coverage:null`、真实原因、
  series_id 与 units 完整，旧 sentinel 不得泄漏；payload/raw quarantine 均存在。
- 四序列 fixture 证明 a/c 失败后后续两个成功序列仍按顺序执行并实际落盘，最终
  严重度为 1。

反恒真证据：`worse_exit()` 临时退回 `max()` 时 60 passed / 3 failed；禁用幂等
跳过时 60 passed / 1 failed，明确命中“相同输入发生第二次写盘”；把 `"."` 伪造为
0 时 60 passed / 4 failed，data 精确列表、coverage.count 与无伪点断言同时变红。
三项破坏均已恢复，恢复后重新为 60 passed / 0 failed。C8 未修改 FRED 生产逻辑。

## 11. C9 Playwright Chromium 启动基础设施

`tools/` 下 11 个直接持有 Browser 生命周期的脚本现全部经
`tools/_browser.mjs::launchChromium()` 启动。helper 调用当前项目 Playwright 的
`chromium.executablePath()`，验证该完整 Chromium executable 可访问后显式传给
`chromium.launch()`；不再扫描 `LOCALAPPDATA`/`HOME` 缓存、不选择最大 revision，
也不 fallback 到系统 Chrome、其它缓存或自动下载。

当前 Windows 环境中，Playwright 默认 headless 启动会寻找未安装的独立 headless
shell；完整 Chromium 已安装且与项目 Playwright revision 对应。新 helper 的真实动态
测试已成功创建 Browser/context/page，并执行 DOM 与 JavaScript。

解析、访问或启动失败统一抛 `BrowserEnvironmentError`，诊断含 browser type、尝试
路径、原始 code 与错误信息。Codex 受限沙箱不能启动用户缓存中的浏览器时，guard
如实 exit 1；授权宿主环境重跑为 **13 passed, 0 failed**。环境失败不得转成
PASS/SKIP。

静态 guard 锁死：普通脚本不得直接 `chromium.launch()`，不得包含用户缓存绝对路径、
固定 Chromium revision 或自行扫描缓存，`executablePath` 只允许出现在 helper 与其
基础设施测试。反恒真结果：固定 revision 注入为 12/1；普通脚本直接 launch 注入为
12/1；不存在 executable 的独立调用抛 ENOENT 且 exit 1。全部注入已恢复。

迁移后 11 个 browser-owning 脚本及四个既有 injection wrapper 全部 exit 0；原有
22/29/37/3/8/4/54 等业务断言基线与 page/console error 语义未变化。C9 未修改生产
页面、数据、Python、workflow、package 版本或依赖。

## 12. C10 前端 injection wrapper 自动判定

新增 `tools/_injection.mjs` 统一 Node wrapper 的执行状态机；C10 首批四个，C11/C12
各增加一个业务 wrapper。helper 保存原始 bytes/hash，
在系统临时目录备份，要求 baseline exit 0；每个 patch 必须改变文件与指定业务锚点，
目标 guard 必须正常返回非零且命中稳定 FAIL marker；每 case 从原始 bytes 重建，
恢复后重新跑 guard，并在 case/finally 两层校验 SHA-256。spawn error、signal、timeout
或恢复错误都记为 wrapper failure，wrapper 明确设置自身 exit code。

`verify-injection-wrappers.mjs` 当前 **34 passed, 0 failed**。静态锁定六个 wrapper，
动态临时 fixture 覆盖
成功状态机、红色基线不执行 injection、spawn error、signal、注入仍绿、no-op patch、
restore mismatch 与 patch throw 后恢复。反恒真结果：吞掉“注入仍绿”累计、放行
no-op、禁用 restore hash 三次均使 meta guard exit 1；所有临时破坏均已恢复。

真实 wrapper 最终结果：total_oi 11/0、spread 11/0、KPI 32/0、isolation 18/0、
COT Index null 18/0、debt overview 81/0；目标 guard 恢复后分别保持各自基线。
C10 未修改
`index.html`、派生 JSON、业务断言、浏览器 helper、workflow 或生产逻辑。

## 13. C11 COT Index null / 不可知语义

`fetch_cot.py` 在本次 52 周窗口上为每条 weekly 记录增加 additive
`mf_index/comm_index`，`latest` 直接复用 weekly 最后一条；原 date/net/OI、coverage、
envelope schema v0 均不变。空窗口或 max==min 继续返回 null，未改数学定义，也未实施
真正的历史 trailing-52 指标。

`index.html` 删除 current fallback 与历史 max/min 重算。任一侧 null 时该侧显示 `--`、
bar 为 muted 0%，有效侧仍显示真实值；综合信号固定为“数据不足 · 暂不判断”，不输出
方向风险、不高亮规则。管理基金自身有效时仍可高亮真实区间；无效时全部区间取消 active。
图表直接消费 weekly Index，null 保持 gap，tooltip 对 null 显示不可知。

`verify-ui-fixes.mjs` 当前 **38 passed, 0 failed**，三个 schema v0 route fixture 覆盖
mf 单侧 null、comm 单侧 null 与双侧 null。新 wrapper 当前 **18/0**：current null→50
使目标 guard **31/7**，chart null→50 使其 **36/2**，恢复后 38/0 且 hash 一致。
Python 反恒真将退化分支改回 50 时 `fetch_cot.py --test` 为 **61/5**；恢复后 66/0。

## 14. C13 联邦债务历史扩展至 1990

`fetch_fred.py` 以 `observation_start_for(series_id)` 分流请求起点：GFDEBTN、
FYGFDPUN、FDHBATN、FDHBFIN、GDP 五条为 `1990-01-01`，其余八条仍为
`2016-01-01`。`--test` 通过 fake opener 捕获 Request URL 并逐条解析 query，
不是静态 grep 常量。

FRED 官方完整序列覆盖核实：GFDEBTN 1966-Q1（241 点）、FYGFDPUN 1970-Q1
（225 点）、FDHBATN 1981-Q1（181 点）、FDHBFIN 1970-Q1（224 点）、GDP
1947-Q1（318 点）；五条在 1990-Q1 均有真实值。本仓只保存 1990 起历史，五个
raw envelope 分别为 145 / 145 / 145 / 144 / 146 点，派生 `macro_debt.json`
为 1990-Q1..2026-Q2 共 146 行。

结构从 1990-Q1 即可形成。既有恒等式闸如实将 2000-Q3、2013-Q4、2014-Q1
三季的结构三分量同时置 null；2026-Q1/Q2 因 foreign 右端 coverage 滞后也保持
结构 null，但总债务、GDP、debt/GDP 按各自 coverage 延伸。没有补 0、前值填充
或前端重算。

`macro.html` 原本已全量消费派生 rows，无 2016 裁剪；C13 只补 `.card{min-width:0}`
以修复桌面初始化后缩至 390px 时 canvas min-content 撑出横向页面溢出。X 轴继续
使用 `maxTicksLimit=12` 自动减刻度。`verify-macro-page.mjs` 锁住 1990 首季、
146 点逐字段映射、真实结构起点、缺口三项同时 null、tick 上限与移动端无溢出。

反恒真结果：GFDEBTN 起点退回 2016 时 67/1，GDP 起点退回 2016 时 67/1；
前端临时裁掉 2016 前 rows 时宏观护栏 57/11。三项破坏均恢复后重新全绿。

## 15. C14 Treasury 日频债务与 drag-to-zoom

`fetch_treasury_debt.py` 读取财政部 Fiscal Data `debt_to_penny`，schema v0 daily
信封落 `data/treasury_debt_daily.json`，源美元在采集层除以 `1e9`。2026-08-25
实查官方 coverage 为 `1993-04-01..2026-08-21`、8377 条。total 全期有值；
public/intragov 的早期官方字段为字符串 `"null"`，原样落 JSON null，不补 0 或前值。
官方 2025-08-04 分项合计与 total 相差 100 亿美元，该日仅 public/intragov 置 null
并写 warning，total 保留。网络失败 exit 2；格式/业务失败 exit 1 并隔离；相同业务
数据 exit 0 且 bytes/SHA-256 不变。

债务图使用 1990 起季度日期与 Treasury 日日期的去重并集。金额字段各按自身首个
Treasury 真实观测衔接，此前保留 C13 FRED 季度值；起点后 Treasury 某日缺值就留
gap，不回退季度。foreign、GDP 与正式 debt/GDP 继续季度，结构 stack 也只保留真实
完整季度 observation。C15 后 GDP 与 debt/GDP 两条低频折线只把真实 `{x, y}`
交给 Chart.js；不创建日频值，但所有真实季度观测都形成可见 point/segment。页面分别显示 debt
`YYYY-MM-DD`、foreign `YYYY-MM`、GDP `YYYY-Qn`。

Chart.js 仍为 4.4.0，只增加兼容的 chartjs-plugin-zoom 2.2.0 + Hammer.js 2.0.8。
fine pointer 下左键右向左/左向右拖框只缩 X，threshold=12；两条 Y 轴冻结全历史范围。
reset 按钮与双击可恢复，touch 环境 drag 关闭且页面可滚动。真实 Playwright 基线为
86/0；C15 扩展后的十一项 wrapper 为 81/0，其中 drag disabled、reset handler 删除、季度字段
forward-fill 与 reviewer 指出的 daily-union null-array / pointRadius=0 表示均使对应 guard
非零且命中稳定 FAIL marker，恢复后 86/0、SHA-256 一致。

workflow 每日 UTC 22:00 运行 Treasury；周六 UTC 18:00 COT 专场跳过该步，避免同日
重复。commit 清单含 `data/treasury_debt_daily.json`，末尾 gate 按 0/1/2 三态处理。

## 16. C15 债务结构堆叠与统一 tooltip

`debtOverviewChart` 最终恰有六个用户可见 dataset。前三个是完整季度
`intragov_bn` / `domestic_public_bn` / `foreign_bn` 的共同 `debtStructure` stack；
每条只含三分量同时有效季度的真实 `{x,y}`，固定 `barThickness=6`、
`maxBarThickness=8`。其余是 `total_bn` 季度→Treasury 日频 hybrid 折线、真实季度
GDP 折线和真实季度 debt/GDP 折线。公众/政府内部日频独立折线与“结构快照”技术文案
已移除，派生文件及 source frequency 未改。

统一 tooltip 由 hover 日期驱动，显示总额、三项结构、GDP、debt/GDP 六项。总额在
Treasury 覆盖期只认该日真实记录；三项结构只认不晚于 hover 日期的最近完整季度，
GDP 与 debt/GDP 各按自身最近真实季度，并分别显示 as-of。lookup 不修改 dataset，
因此不会制造日频 foreign/GDP/ratio。真实 Playwright 分别 hover 最新日频点和历史
结构柱；全历史及 2022-08-16..2026-06-30 约 3.9 年窗口完成截图验收，缩放前后 Y 轴
固定且移动端无溢出。

`verify-debt-overview-injection.mjs` 覆盖 11 案：C15 必需的删除 GDP tooltip 行、拆散
foreign stack、恢复“结构快照”标签、恢复公众/政府内部日频线，以及错误 ratio 轴、
重复 public、删除 GDP dataset、禁用 drag、删除 reset、结构 forward-fill、恢复不可见
daily-union null-array 表示。每案目标 guard 都非零并命中 marker；最终 81/0，恢复主
guard 86/0，`macro.html` SHA-256 与注入前一致。

## 17. C16 Cloudflare Workers Static Assets 发布

C15 已合并到 `afc18e2`，但该 merge commit 没有触发 production check；线上文件哈希
仍精确等于 C14 `97d946d`。C15 PR head 的 Cloudflare build 报
`Missing entry-point to Worker script or to assets directory`，根因是仓库没有 Worker
entry point，也没有 Wrangler assets 配置。

C16 新增无 `main`/bindings 的 `wrangler.jsonc`：worker 名称保持 `gold-dashboard`，
`compatibility_date=2026-08-25`，`assets.directory=./dist`。构建命令
`node tools/build-static-site.mjs` 先安全删除仓库内固定 dist，再按 34 项路径逐个复制：

- 根页面：`index.html`、`macro.html`、`term-3d.html`；
- 当前 5 个 assets、1 个 CSS、3 个 JS；
- 当前 22 个公开 JSON（逐项枚举，不按扩展名自动吸收未来文件）。

当前产物为 34 文件：3 HTML、5 assets、1 CSS、3 JS、22 JSON，共 3,773,347 bytes。
`data/section62_sample.pdf`、`data/quarantine/`、全部 Python/Markdown、AGENTS/CLAUDE/
README、docs/tools/.github/.git、测试与临时文件都不公开。`dist/` 和 Wrangler 临时目录
`.wrangler/` 均在 `.gitignore`，不进入 commit。

Cloudflare Dashboard 的 Workers Builds 必须配置：

| 设置 | 值 |
|---|---|
| Root directory | `/` |
| Build command | `node tools/build-static-site.mjs` |
| Production deploy command | `npx wrangler deploy` |
| Preview deploy command | `npx wrangler versions upload` |

仓库配置不能自动改 Dashboard trigger；C16 merge 前后均需由用户确认这四项已经保存。
Wrangler 4.125.0 的 `versions upload --dry-run` 与 `deploy --dry-run` 均 exit 0，后者明确
读取 `dist` assets、无 bindings，二者都不再报 missing entry-point/assets。Windows
首次 `npx` 因 npm 缓存缺 `@cloudflare/workerd-windows-64` 失败，属于本机 optional
dependency 环境问题；在系统 TEMP 以 `--include=optional` 隔离安装后两个 dry-run
真实通过，临时安装已删除，未修改仓库 package 依赖。

`verify-static-build.mjs` 为 28/0：锁定 config、必需页面、完整 manifest、逐字节一致和
内部文件不公开。三项负向 wrapper 为 23/0：删除 `assets.directory` 使 26/2，删除
`dist/macro.html` 使 26/2，将 `fetch_treasury_debt.py` 放入 dist 使 25/3；每案恢复，
最终重新 28/0。`verify-macro-page.mjs --site-root dist` 对真实产物完整通过 86/0。

## 18. C17 美国财政可持续性监测

`fetch_treasury_fiscal.py` 采集 Fiscal Data MTS Table 9，原始 strict monthly envelope
落 `data/treasury_mts_fiscal.json`。生产选择先按 `parent_id/classification_id` 找到
Receipts 与 Net Outlays 两棵 hierarchy，再选择各自 Total 与 Net Interest；当前
120/T/SL、320/D/F、340/T/SL 只作为 schema 漂移锚。只读取
`current_month_rcpt_outly_amt`，USD `/1e9` 恰好一次；MTS 月度 net interest 是有符号
净额，官方负月原样保留。当前覆盖 `2015-03-31..2026-07-31`，137 个月。

`derive_fiscal_stress.py` 独立于 `derive_macro.py`，消费 MTS、Debt to the Penny 日频
公众债务与 `macro_debt.json`。每个季度要求截至季末 12 个连续月完整财政字段；公众
债务均值使用同窗口全部有效业务日观测，并要求 12 个月各有至少一个真实点，不填周末、
假日或缺月。GDP YoY 严格用 t/t-4；GDP SAAR 不除以 4。季度输出从 2016-Q1 起，
当前最新完整期 2026-Q1。

核心字段为九项 KPI、实际/模型 QoQ `Δd`、stock-flow residual、完整性、trajectory 与
连续正 gap 季数。模型 RHS 是年度/TTM rate，除以 4 后才与实际 QoQ percentage-point
变化比较；residual 保留真实非零。`latest` 指向最新完整季度，meta 分别记录 MTS、
日频公众债务、GDP、public debt/GDP 与完整模型 as-of。`stress_level=unscored`，
`threshold_version=null`。

`macro.html` 在联邦债务卡下新增独立财政可持续性卡：九 KPI 按 4/3/2 排列，两图只读
派生的 r/g/r-g 与 actual primary balance/p*；null 显示“未知”。模块拥有独立 load/
`_safeRender` 边界，fiscal 与 rates/CPI/debt 双向故障隔离。页面明确 2016-Q1 历史边界、
四个独立 as-of 与 stock-flow residual 方法警告，不计算 r/g/primary/p*/gap/residual，
不提供预测年份或颜色评级。

workflow 每日检查 MTS 后再运行 fiscal derive，二者各自捕获真实进程码；raw MTS 与
derived fiscal 分别进入 commit 清单。MTS exit 2 只告警，exit 1 报红并保留旧 raw；
derive exit 1 保留旧 derived，均不短路现有采集/派生链。静态白名单增加 MTS raw 与
fiscal derived 两个浏览器数据文件，当前 dist 共 36 文件（24 JSON），Python、测试、
quarantine 仍不公开。

`verify-fiscal-stress-injection.mjs` 通过公共 `_injection.mjs` 运行三个 suite、八案：
Receipts/Outlays 选反、primary 符号反转、边际利率替代 effective r、缺月填充、GDP /4、
p* 缺 /100、total debt 分母、前端重算 fiscal gap。每案真实非零并命中固定 marker，
逐案与 finally 均恢复 bytes/SHA-256；Windows Node bridge 直接读取无 shell 的 `wsl.exe`
进程码，避免 `$?`/管道假绿。

## 19. C17.1 Fiscal Gap 判决与 p* 判据线

`macro.html` 在九项 KPI 与历史图之间设置独立 Fiscal Gap 判决卡，直接消费最新完整
季度的 `trajectory_condition` 与 `fiscal_gap_pct_gdp`：当前 `2026-Q1` 为
`stabilizing_condition_met`、gap `-0.7099% GDP`，页面显示“稳定条件满足”与
“当前稳定缓冲 0.71% GDP”；正 gap 显示“稳定条件不满足/当前财政调整缺口”，
unknown 保持 `--`。颜色只表达该二元数学条件，不增加 stress score 或风险阈值。

初级余额图保留真实 actual 与 p* 逐季 observation，将 p* 标为虚线动态“判据线”；
新增的常数 0% dataset 明确叫“参考线”且仅用于展示。真实鼠标 hover 会同时显示季度、
actual、p*、派生 Fiscal Gap 和派生判决。前端没有 `p* - actual` 重算，未修改 Python、
派生 JSON 或 schema。stock-flow residual 与“稳定条件满足不等于实际债务率当期下降”
警告继续显示。

actual/p* 图下方另有独立 `Fiscal Gap（% GDP）` 图，时间轴与完整季度序列一致，曲线
逐点直接读取 `quarterly[*].fiscal_gap_pct_gdp`，null 不补 0。该图的 0% dataset 明确
命名为“判据线（0% GDP）”：与 actual/p* 图中的 0% 参考线不同，它直接区分 gap
`<= 0` 与 `> 0`。真实 tooltip 同时显示季度、gap、actual、p* 与派生 trajectory 判决。

`verify-fiscal-stress-page.mjs` 为 **46 passed, 0 failed**：除 C17 基线外，真实 hover
验证 `2026-Q1` 负 gap 与真实 `2025-Q4` 正 gap，并覆盖 positive latest fixture、unknown、
p* 样式、两种 0% 线语义、三图布局、presentation dataset 与无前端重算。C17.1 五项注入分别反转判决、
删除 p* 判据线标识、把 0% 错叫判据线、删除 tooltip gap、恢复前端 gap 重算；每项均
使目标 guard 非零并命中固定 marker。Fiscal Gap 图另以五项注入覆盖 null→0、判据线
误叫参考线、正 gap 判决反转、tooltip gap 删除与前端重算；恢复后重新 46/0，frontend
wrapper **74/0**，
最终 `macro.html` SHA-256 与注入前一致。

## 20. C18A CBO 官方 Baseline

`fetch_cbo_baseline.py` 解析 CBO 于 `2026-02-11` 发布的 *The Budget and Economic
Outlook: 2026 to 2036* Budget workbook。当前人工审计源为官方 publication
`https://www.cbo.gov/publication/61882` 与文件
`51118-2026-02-Budget-Projections.xlsx`，SHA-256 为
`06593fcc3b8517806994090a6a9ffe748cfdd19514d2f9d2f18a1841b64b33a5`。解析器锁定完整
sheet 集、Table 1-1 标题/坐标/单位、2025 actual 与 2026..2036 projection 分界；百分数
必须按官方 percentage points 读取，primary deficit 转为 surplus-positive 口径。
同一 publication 的 Economic workbook `51135-2026-02-Economic-Projections.xlsx`
（SHA-256 `ae8f4920702fabf8fb3136bc94a42c53466cff5e890dec22396d8dc49dc2f776`）已审计但
未消费：C18A 所需财政年度指标都在 Budget Table 1-1，避免混入 calendar-year 表。

成功输出 strict schema v0 annual envelope。不可变版本落
`data/cbo/baseline-2026-02.json`，浏览器指针落
`data/derived/cbo_baseline_latest.json`；vintage 另存人工 source artifact 的
`downloaded_at=2026-08-27T07:58:05Z`。相同 source 重跑 bytes 与 generated_at 不变，
不同业务内容拒绝覆盖旧 vintage，latest 只在完成验证后切换；异常诊断写入 ignored
`data/cbo/diagnostics/`。原始 Budget/Economic XLSX 位于 ignored `data/cbo/source/`，
不进 Git、不进 dist。每日 workflow 只运行离线
`fetch_cbo_baseline.py --test`，不自动下载或发布新 vintage。

年度输出直接含 debt held by public/GDP、primary balance、net interest、receipts、
outlays、overall balance 与债务变化描述量。2036 终点为 debt/GDP `120.209%`、net
interest/GDP `4.591%`、primary balance/GDP `-2.079%`；2025 actual 至 2036 baseline
债务率上升 `20.834 pp`。这些是 conditional baseline，不是危机年份或确定性预测。

`macro.html` 新增独立 CBO 卡片与三图：C17 Q4 actual 实线和 CBO annual baseline 虚线
分段债务率、projection primary balance/net interest、projection receipts/outlays；2026
由垂直 marker 标出。页面直接消费官方百分比，不以金额/GDP重算。CBO 数据失败不影响
C17，C17 bridge 失败时 projection 仍显示但不伪造 actual。当前 workbook 无法提供与
C17 `effective_r` 严格同口径的 forward rate，因此 forward p*/Fiscal Gap 明确 unavailable。

静态白名单只增加 `data/derived/cbo_baseline_latest.json`，产物为 37 文件（25 JSON）；
`verify-static-build.mjs` 明确拒绝 `fetch_cbo_baseline.py` 和整个 `data/cbo/`。C18A
定向护栏为 parser 22/0、页面 24/0、injection parser 32/0 + frontend 18/0、wrapper
meta 40/0、static build 34/0、static injection 23/0；所有注入最终恢复源码 SHA-256。

## 21. C18B CBO Fiscal Scenario Lab

`derive_cbo_scenario_basis.py` strict 读取 `cbo_baseline_latest.json`，输出 schema v0 annual
`cbo_scenario_basis.json`。2025 是 anchor，2026..2036 为 projection。每年保存官方
debt/GDP、debt bn、GDP bn、nominal growth、primary/net-interest/overall balance，及
`baseline_sfa_bn = debt_t - debt_(t-1) - (-overall_balance_t/100*GDP_t)` 与对应 GDP 比例。
当前 11 年闭合最大误差为 `7.28e-12 USD bn`；SFA 从 2026 的 `70.21351158 bn`
（`0.22009121% GDP`）到 2036 的 `-66.06474998 bn`（`-0.14142950% GDP`）。

`assets/js/cbo-scenario-engine.js` 是无副作用纯函数。shock 从 start year 起永久生效：
`g_s=g_base+growth shock`，`GDP_s=GDP_s(prev)*(1+g_s/100)`，
`overall_s=overall_base+primary shock-interest shock`，
`debt_s=debt_s(prev)-overall_s/100*GDP_s+SFA_base_pct/100*GDP_s`。
输入 bounds 为 growth/primary ±3pp、interest ±2pp，step 由 UI 固定 0.25pp。
zero shock 金额闭合后 canonical 返回官方 debt amount/debt-GDP，2026..2036 每年 exact。

`macro.html` 在 CBO Baseline 后新增独立 Scenario Lab：四 controls、Reset、五 KPI 与
CBO Baseline/User Scenario 双线图。tooltip 显示 fiscal year、两条路径与 difference；
免责声明明确 deterministic、not CBO forecast、no probability、no crisis/default/loss-of-control
claim。scenario basis/CBO/C17 三块互相故障隔离，移动端无横向溢出。

定向 guard 为 basis Python 26/0、engine 20/0、page 22/0。八项 injection 覆盖 primary
符号、interest 符号、删除 SFA、引入 C17 effective_r、覆盖 baseline 字段、破坏 zero-shock、
伪装 CBO Projection 标签与删除免责声明；每案真实红并命中 marker，恢复后 guard 全绿、
源码 SHA-256 一致。静态白名单新增 basis JSON 与 engine JS，共 39 文件（26 JSON）；
CBO source/vintage/diagnostics、Python、tests/docs 继续拒绝公开。每日 workflow 只运行离线
`derive_cbo_scenario_basis.py --test`，不生成、commit 或保存用户 scenario。

## 22. C18C U.S. Fiscal Risk Monitor v0

`derive_fiscal_risk_monitor.py` 只 strict 读取 C17 的
`macro_fiscal_stress.json`，输出 schema v0 quarterly
`fiscal_risk_monitor.json`。所有 C17 指标逐字段复制；六项变化用
`YYYY-Qn -> (YYYY-1)-Qn` 键匹配，当前或上年同季非 complete、缺失或 null 时保持
null，不按数组位置、不补 0。输出另含财政缺口、r-g、初级余额和债务同比的描述性
正负号字段；0 只是数学边界，不是政策阈值。

`latest_complete` 是全部核心字段有效的最后季度；`latest_observed_quarter`、
`latest_complete_quarter` 与 `complete_lag_quarters` 分开保存。methodology 明确禁止
risk/composite score、概率、危机年份、动态风险颜色、forward-fill、插值和以市场
收益率替代 C17 effective r。`--test` 会把提交的 production monitor 与当前 source
fresh derivation 比较（忽略顶层 generated_at）；固定 stale marker 防止新 C17 + 旧
C18C 组合进入提交。

production snapshot 测试不锁某个日期或当前正负状态：latest observed 从 source 最后一
行取值，latest complete 从 complete 且核心字段有效的 source 行动态筛选，lag 用季度
索引相减；latest complete 全行再与对应派生行逐字段比较。独立 rolling fixture 覆盖
lag=0 与新增 incomplete 季度后的正 lag，condition fixture 覆盖与当前生产快照相反及
零边界状态。页面 condition 由派生枚举动态映射，hover 以
`latest_complete_quarter` 查 label index，禁止恢复数组倒数位置假设。

`macro.html` 在 C17 与 CBO Baseline 之间增加独立 C18C 卡片：六项 current + 同比、
四张季度小图，以及分源上下文。DGS2/10/30 各显示自身最后真实日期且仅作市场背景；
CBO FY2026/FY2036 债务率、初级余额、净利息直接读取官方年度字段。C18B slider 与
scenario basis 不进入 C18C。monitor/rates/CBO/scenario 四类失败均有独立验证边界。

每日 workflow 先产 C17 再产 C18C，真实 exit code 按 0/1/* 汇总，失败保留旧 monitor
且不连累原始数据；commit 清单含新 JSON。静态白名单现为 40 文件、27 JSON；static
guard 同时核对 derived_from、逐字段复制和同季度 YoY，防止部署 stale 组合。

C18C 定向结果：Python 52/0、页面 41/0、snapshot contract 8/0；十一项 injection
分为派生 39/0、页面 25/0、stale source 11/0、Python/page snapshot coupling 各
11/0，所有 target/source/output SHA-256 恢复一致；wrapper meta 56/0。static build
46/0，五项 static injection 33/0。Wrangler versions upload 与
deploy dry-run 均 exit 0，未执行真实 production deploy。

## 23. C18C.1 Treasury chart enhancements

`derive_gold_vs_debt.py` 生成 strict schema v0 weekly
`data/derived/gold_vs_debt.json`。全球黄金总市值使用 World Gold Council end-2025
地上存量 220,700 公吨 × `32150.74656862798 oz/metric tonne` × 周频 USD/oz，单位
USD tn；美债使用 Treasury Debt to the Penny 的 `Total Public Debt Outstanding`，
只在黄金观测日存在精确同日债务时输出 USD tn。无同日值保持 null，禁止前值、最近值、
forward-fill 和插值。派生测试与 static guard 都按当前 gold/debt source 重算业务内容，
忽略顶层 generated_at，阻止 stale comparison 发布。

黄金 USD/oz 输入被明确视为 dashboard valuation proxy。派生层从当前
`gold_price.info` 的唯一 `price_source=` 解析实际来源：Yahoo Finance `GC=F` 映射为
COMEX gold futures，Stooq `XAUUSD` 映射为 XAUUSD gold spot proxy；无法识别时派生失败，
不得默认为 spot。methodology 透传 source、instrument、显示标签及
`gold_price_is_proxy=true`，页面据此动态显示实际代理，而不是写死某一数据商或品种。

`macro.html` 在历史 UST 前增加独立黄金估值 vs 美债图；历史 UST 桌面左键框选后 X
缩放并以窗口内全部真实 tenor 值自适应 Y，Reset 同时还原 X/Y，移动端 drag 关闭。
其下增加 TradingView 官方 Advanced Real-Time Chart Widget：默认 `TVC:US10Y`，
2Y/10Y/30Y 映射 `TVC:US02Y`/`US10Y`/`US30Y`；分时、1D、5D 分别配置
5m/1D、15m/1D、60m/5D。该第三方图不需要 API key，不写 JSON、不替代 FRED，且
不进入 C17 effective r、C18C 或 Fiscal Gap；CDN/widget 失败只显示本卡 unavailable。

C18C.1 定向结果：Python 28/0、动态 Windows→WSL bridge 28/0、页面 35/0；九项
injection 覆盖 Y 自适应、分钟范围、第三方异常、attribution、Reset、黄金公式、债务
口径、stale 数值源及 stale `price_source`，全部真实红、命中稳定 marker，恢复后全绿且
四个目标 SHA-256 一致。wrapper meta 为 64/0。静态白名单为 41 文件、28 JSON，
static guard 53/0；static injection 43/0，并分别阻止“新 gold 数值源 + 旧 comparison”与
“新 gold proxy metadata + 旧 methodology”。每日 workflow 的派生与 commit 清单加入
comparison JSON，并保持每一步真实 exit code 与故障隔离。
