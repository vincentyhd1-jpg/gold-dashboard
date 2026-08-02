# 技术交接（截至 f8f5204）

供新会话接续。只记事实与行号，不含建议与计划。

护栏计数已在 `f8f5204`（含 Actions 数据提交 `36a5504`，`oi.json` 25 帧）上重跑，
与 `bf1cc6b` 时点（24 帧）逐条一致。

## 1. git 状态

```
local  sha = f8f5204323103d85d1023c24e25e5fd49f981c26
remote sha = f8f5204323103d85d1023c24e25e5fd49f981c26
local == remote = YES
未推送 commit 数 = 0
工作区 = 干净（git status --short 无输出）
分支 = main，跟踪 origin/main
```

## 2. 最近三个 commit

| sha | 内容 |
|---|---|
| `f8f5204` | 本文档 |
| `36a5504` | Actions 每日数据提交 2026-08-01：`oi.json` 24 → 25 帧，`cot.json` / `gold_price.json` / 派生文件同步更新 |
| `bf1cc6b` | `unreliable_chg` 遍历改走 `oi_maps_raw`（该帧原始 months），脱离 `window_months` 窗口筛；加 WINDOW fixture；`--test` 尾部补失败总条数 |

再往前：`3382585`（`unreliable_chg` 从 `contracts` 改走 `oi_map`，拆掉「存续」筛，窗口筛当时仍在）、`3604e16`（`fetch_gold` exit code 三态分离）。

## 3. derive_term_structure.py 结构

两份 OI map 并存，按字段语义分工。建于 `derive()` 内：

| 变量 | 建于 | 内容 | 语义 |
|---|---|---|---|
| `oi_maps` | :389, :393 | `window_months(r["months"])` 筛后 | as-of-now，展示视图 |
| `oi_maps_raw` | :390, :394 | `r["months"]` 原样，零过滤 | as-of-that-frame，帧级取证 |

```
:392   wm = window_months(r.get("months") or [])
:393   oi_maps.append({m["month"]: m["oi"] for m in wm})
:394   oi_maps_raw.append({m["month"]: m.get("oi") for m in (r.get("months") or [])})
```

字段归属：

| 走 `oi_maps_raw` | 走 `oi_maps` |
|---|---|
| `unreliable_chg`（:471 `prev_oi_map`、:472 遍历） | 其余 15 个帧字段 |

`oi_maps` 的消费点：:404（`oi_max` 汇总）、:420（`oi_map = oi_maps[idx]`）、:496（`prev_oi` 供 `oi_chg` 差分）。

`oi_chg` 的列位由 `contracts` 决定、取值由 `oi_maps`（:496）决定，两道筛并存 —— 这是第 4 节那条红的成因。

16 个帧字段中 15 个经过至少一道随时间移动的筛，仅 `date` 未经过滤。

## 4. 护栏清单与当前状态

| 护栏 | exit | 结果 |
|---|---|---|
| `derive_term_structure.py --test` | 1 | **15 PASS / 1 failed** |
| `fetch_cot.py --test` | 0 | 26 passed, 0 failed |
| `fetch_gold.py --test` | 0 | 12 passed, 0 failed |
| `fetch_oi.py --test` | 0 | 23 passed, 0 failed |
| `fetch_stocks.py --test` | 0 | 22 passed, 0 failed |
| `tools/verify-fetch-gates.py` | 0 | 24 passed, 0 failed |
| `tools/verify-io-utils.py` | 0 | 100 passed, 0 failed |
| `tools/verify-ui-fixes.mjs` | 0 | 13 passed, 0 failed |
| `tools/verify-contract-contango.mjs` | 0 | 29 passed, 0 failed |
| `tools/verify-playback.mjs` | 0 | page errors: none |
| `tools/verify-gapframe.mjs` | 0 | page errors: none |
| `tools/verify-isolation.mjs` | 0 | 37 passed, 0 failed |
| `tools/verify-schema-coupling.mjs` | 0 | 双形状兼容生效 |

计数在 `f8f5204`（`oi.json` 25 帧）上重跑得到，与 `bf1cc6b`（24 帧）时点逐条相同 ——
新增一帧未改变任何护栏的通过数。

### 红的那一条

```
MISMATCH 2026-07-24:
    AUG27: stored=+11  computed=None
```

- 断言位置：`derive_term_structure.py:801-805`（抽查 2，累加进 `mismatches`）
- 读的字段：`f24["oi_chg"][ci]`（:801）—— 是 `oi_chg` 数组，**不是** `unreliable_chg`
- `ci` 来自 `contract_idx.get(contract)`（:798）
- 成因：`AUG27` 在 `contracts` 内（第 13 列，列位 12），但 2026-07-24 帧的 `window_months` 末端为 `JUL27`，`AUG27` 落窗口外 → `oi_maps` 无此键 → `oi_chg[12] = None`；而 `oi.json` 该帧 `AUG27 oi=572`、`stored oi_chg=+11`
- 已量化：改 `unreliable_chg` 的输入来源对此条无影响（`unreliable_chg` 与 `oi_chg` 是两条独立代码路径）
- 该帧 13 个「stored 非 None 且在 contracts 内」的合约中，只有 `AUG27` 一个不符

`bf1cc6b` 未改此断言，未动 `oi_chg` 取值来源。

### 已知会随数据滑动而消失的红

**抽查 2（`derive_term_structure.py:786-807`）的红是会自行消失的，但消失不等于修复。**

该断言开头有两个 SKIP 分支（:788、:790）：

```
:788   if f24 is None or r24 is None:      → SKIP「2026-07-24 已滑出窗口」
:790   elif f24["date"] == first_date:     → SKIP「2026-07-24 已成为序列首帧（无前驱）」
```

`oi.json` 保留 730 条滚动窗口。当 `2026-07-24` 滑出窗口、或成为序列首帧时，
断言转 SKIP，`derive --test` 会变成 `0 failed` **全绿** —— 但 `oi_chg` 的口径问题
（`contracts` 决定列位、`window_months` 决定取值，两道筛边界不一致）一行代码都没改。

`f8f5204` 时点实测：`oi.json` 25 帧、范围 `2026-06-26 ~ 2026-07-31`、
首帧 `2026-06-26`，两个 SKIP 条件均不成立，断言实际执行并 FAIL。

同类性质的还有抽查 1（`2026-06-26` 首帧）与抽查 3（`2026-07-27` `known_revised`
四合约），三条都绑定了具体日期，都会随窗口滚动转 SKIP。抽查 3 还额外绑定了
`known_revised = {"AUG26","DEC26","OCT26","JUL26"}`（:818）这个会过期的事实集合
—— 合约到期后该集合不再对应当前数据。

**判读规则：看到 `derive --test` 全绿时，先确认抽查 1/2/3 是 PASS 还是 SKIP。**
输出里 SKIP 与 PASS 是不同前缀，不会混淆，但总计行只数 `errors`，SKIP 不计入。

## 5. fixture 清单与自检锚点

| fixture | 定义行 | 测什么 | 自检锚点 |
|---|---|---|---|
| `FIXTURE` | :600 | day1 首帧全 None / day2 差分与 CME 一致 / day3 修订取 diff 并标记 | 无独立锚点 |
| `ROLL_FIXTURE` | :653 | `front_remaining` 1.0→0.05 单调递减；`front` 交叉切换而 `roll_from` 不动；无承接月时 `roll_to=None` | 无独立锚点 |
| `KPI_FIXTURE` | :831 | `total_oi=200000` / `spread=122.0` / `gap=122天` / 年化 `9.125` 全精度；无承接月时价差三字段 None | 断言直接比全精度值，容差 `1e-9` |
| `B2_FIXTURE` | :901 | `total_oi=270000` 只含 `ever_front` 已坐正主角 | 若得 `318000` 即判定退回全部挂牌月旧口径；`FEB27` 在 `major_months` 却须不在 `total_oi` |
| `NOISE_FIXTURE` | :943 | `roll_noise` 落在无限小数（`1000/3001` 形态），容差 `EPS_N = 1e-12` | 落盘值若等于自身 `round(4)` 即判定被舍入 |
| `WINDOW_FIXTURE` | :993 | 窗口外修订合约 `DEC27`（stored `-7` ≠ 差分 `+50`）必须进 `unreliable_chg` | 三重：`DEC27` 必须不在 `window_months` 返回值（:1015）、必须不在 `contracts`（:1019）、`unreliable_chg` 须只含 `DEC27` |

真实数据抽查（非 fixture）：

| 抽查 | 行 | 内容 |
|---|---|---|
| 1 | — | `2026-06-26` 首帧 `oi_chg` 全 None |
| 2 | :786-807 | `2026-07-24` `oi_chg` 与 CME stored 逐合约比对 ← **当前红** |
| 3 | :811-822 | `2026-07-27` `known_revised` 四合约必须被标记 |

三条抽查均带滑窗保护：帧已滑出窗口或成为首帧时 `SKIP` 而非 FAIL。

## 6. 关键行号索引

### 前端五个读取点（index.html）

| 行 | 文件 | 解包 |
|---|---|---|
| :1252 | `data/cot.json` | `p?.data ? {...p.data, generated_at: p.generated_at} : p` —— 双形状 |
| :1255 | `data/gold_price.json` | 直接 `r.json()`，无双形状兼容 |
| :1258 | `data/stocks.json` | 有双形状兼容 |
| :1262 | `data/oi.json` | 直接 `r.json()`，`.catch(() => [])` |
| :1263 | `data/derived/term-structure-series.json` | 有双形状兼容 |

汇合于 :1266 `.then(([cotData, goldData, stocksData, oiData, seriesData])`，:1269 `_safeRender('COT/金价', ...)`。

### 时间戳

```
index.html:479   const stamp = cot.generated_at ?? cot.updated_at;
index.html:480-482  缺失时 '未知'，不回退 new Date()
index.html:484   label 文案「页面更新：」
```

来源为 `data/cot.json`（:1252），**不是** 派生文件。`fetch_cot.py:276-281` 有幂等跳过，比对 `unwrap()` 后的业务数据全量，信封元数据排除 —— 该时间戳不因脚本重跑而跳动。

`term-structure-series.json` 的 `generated_at` 不进页面显示；derive 无幂等跳过（`atomic_write_json` 无条件写，`derive_term_structure.py:1141`；未从 `io_utils` 导入 `read_json_or`，见 :27），每跑必刷。间隔 2 秒连跑两次必产生 `generated_at` 一行 diff。

### 静默退化点

| 位置 | 兜底 |
|---|---|
| `fetch_cot.py:64-65` | `def i()` → `float(row.get(key) or 0)` |
| `fetch_cot.py:99-100` | `cot_index()` `mx == mn` → 返 `None`（原返 50） |
| `fetch_gold.py:126` | `align_price()` 对不上 → `return None` |
| `fetch_stocks.py:139-143` | `_to_float()` 任何异常 → `0.0` |
| `fetch_oi.py:336` | `max(months, key=lambda r: r.get("oi") or 0)` |

### 其他

```
derive_term_structure.py:323   def window_months()
derive_term_structure.py:364   last_listed —— 经 window_months(records[-1])
derive_term_structure.py:368   ever_front 累积 —— 经 window_months
derive_term_structure.py:418   wm = window_months(...) 逐帧
derive_term_structure.py:818   known_revised = {"AUG26","DEC26","OCT26","JUL26"}
derive_term_structure.py:801   computed = f24["oi_chg"][ci]  ← MISMATCH 读此
derive_term_structure.py:805   errors.append("MISMATCH 2026-07-24: ...")
derive_term_structure.py:1090  --test 尾部失败总条数（0 时走 :1093）
```

`window_months()` 调用处共 5 个：:364、:368、:392、:418、:1010（fixture 自检）。

## 7. 已知隐患

### 无 verify 覆盖

- **`term-3d.html`** —— 绕过派生层直接 `fetch('data/oi.json')`，5 个 Playwright 脚本全部只测 `index.html`。依赖字段 `r.date` / `r.months[]` / `m.month` / `m.settle` / `m.oi`。改 `oi.json` 结构时现有 verify 全绿不代表此页未坏，会静默变空白或错图。
- **`unreliable_chg` 无前端消费端** —— 全仓 `js/*.js`、`index.html`、`term-3d.html` 中该字段仅 1 处出现，且是写非读（`js/playback.js:169` mock 兜底字面量 `unreliable_chg: null`）。六个前端 verify 对它 **0 条断言**。`tools/verify-gapframe.mjs:26` 提到该字段是把它置 `null` 用于构造断层帧，断言对象不是它本身。该字段的护栏只能加在 Python 侧。
- **`index.html:1255`（gold_price.json）** 无双形状兼容 —— 该文件信封化时此处会断，且断裂本地不暴露（磁盘文件要等下次 Actions 跑才变形）。

### 偶发失败

- **`tools/verify-ui-fixes.mjs`** —— 批量连跑六个前端 verify 时出现过一次 `waitForFunction` 30s 超时、exit=1；单独重跑 exit=0 / 13 passed。当时数据文件完好且 dev server 正常服务（`curl` 确认）。表现为并发争用，非代码缺陷。

### 浮点末位差（已翻回，机制仍在）

`bf1cc6b` 时点记录：`roll_noise_ma` 3 帧与仓库基线末位不一致，量级 `1e-17`。

`36a5504`（Actions 在 CI 机器上重跑 derive 并提交）把这 3 个值全部改了回去：

| 帧 | bf1cc6b | f8f5204 | Δ |
|---|---|---|---|
| 2026-07-06 | `0.07951028607531797` | `0.07951028607531796` | `-1.39e-17` |
| 2026-07-09 | `0.4795674493184508` | `0.47956744931845074` | `-5.55e-17` |
| 2026-07-23 | `0.44424759253511864` | `0.4442475925351186` | `-5.55e-17` |

两 ref 共有 24 帧，差异恰好这 3 个，其余帧一致。

即本机重算与 CI 重算的 `roll_noise_ma` 在这 3 帧上稳定地相差 1 ULP，
谁最后跑谁的值进仓库。本机跑 derive 会把它们改成本机值，
下次 Actions 跑又会改回去 —— 每次都产生 3 行 git diff。

### 取证禁忌（已进 CLAUDE.md）

WSL exit code 两个读法会把非零码读成 0：在 WSL 内部 `echo $?`（被 Windows 侧 shell 先展开）、取码时接管道（返回管道末命令状态）。PowerShell 侧 `$?` 是布尔值，取码须用 `$LASTEXITCODE`。
