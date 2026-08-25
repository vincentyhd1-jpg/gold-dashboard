# gold-dashboard

COMEX 黄金持仓仪表盘。数据每交易日由 GitHub Actions 采集，前端为单文件
`index.html`（Chart.js via CDN），无构建步骤。


## 报告输出

每轮执行完毕后，把完整输出保存为 markdown 报告：

- 路径：`reports/YYYYMMDD_HHMMSS_<主题>.md`
- 主题用短横线连接的英文小写，如 `macro-commit1-helper-extract`
- 示例：`reports/20260821_143022_macro-commit1-helper-extract.md`

报告必须包含：

- 本轮执行的指令摘要（一两句）
- 完整 git diff 原文（不省略、不概括）
- 新增文件默认不会出现在 `git diff` 中；需先执行 `git add -N <文件>`，或单独 `cat` 新文件内容，避免完整 diff 漏掉本轮核心新文件
- 全部验证输出（逐条 PASS/FAIL + 计数，不写“全绿”了事）
- 未做的项目，显式写「未做」
- 遇到的中间态、返工、计划外改动，如实记录

聊天里仍按原规则只贴关键片段（diff + 验证输出）；报告文件是完整存档，两者不互相替代。

`reports/` 是过程存档，不是产物，不入库；与 `screenshots/` 里的一次性注入脚本同类处置。

## 命令临时文件

命令输出的临时文件一律写系统临时目录（`mktemp` / `$TMPDIR`），不得写入仓库根
或任何被 git 跟踪的目录。

写进仓库的临时文件会混进 `git status` 与完整 diff，让本轮真实改动和一次性中间
产物无法区分；忘记删就变成游离文件被误提交。取证输出、diff 快照、测试 stdout
全部同此处置。

## 本机凭据与密钥：先报方案，等确认

凡需取用本机凭据/密钥的手段 —— `git credential fill` / `gh auth token` /
读 `~/.git-credentials`、`~/.netrc`、`.env`、keyring / 任何 API key 或 token ——
**先把方案报出来等确认，不得先执行后报备。**

已发生过一次：为触发 `workflow_dispatch`，未先报方案就用 `git credential fill`
取出本机 GitHub token 调 API。事后报备不等于授权 —— 凭据一旦被读出，就可能进
命令历史、进日志、进报告，撤不回去。「事情办成了」不是理由。

报方案说清三件事：要取哪个凭据、用什么手段取、拿它做什么。等到确认再动手。
若有不取凭据的替代路径（改用网页手动触发、让用户自己跑一条命令），优先报那条。

## 架构

```
fetch_cot.py      CFTC COT 周报        → data/cot.json
fetch_gold.py     Yahoo Finance 金价   → data/gold_price.json
fetch_stocks.py   CME 库存             → data/stocks.json
fetch_oi.py       CME Section 62 PDF   → data/oi.json        （含 4 项写入前校验）
derive_term_structure.py                → data/derived/term-structure-series.json
fetch_fred.py      FRED 13 个序列        → rates / CPI / debt / GDP 原始信封
derive_macro.py                         → macro_rates / macro_cpi / macro_debt
index.html        期限结构回放 + 各图表
macro.html        利率 / CPI / 美国联邦债务面板
term-3d.html      Plotly 3D 曲面页，**已从导航移除**（无运行时入口，见下方专节）
trading_calendar.py  交易日历，采集层与派生层共用一份假日表
data_envelope.py  统一落盘信封 + write_json 单点落盘
tools/*.mjs       Playwright 验证脚本
```

**Playwright 启动方式（C9）**：`tools/` 下 11 个 browser-owning 脚本统一调用
`tools/_browser.mjs` 的 `launchChromium()`。helper 使用当前项目 Playwright 的
`chromium.executablePath()` 取得同版本完整 Chromium，再显式启动；不扫描缓存、
不选择最大 revision、不绑定用户目录、不 fallback 或自动下载。解析、访问或启动失败
均抛 `BrowserEnvironmentError` 并保持非零；EPERM 属环境失败，不是 PASS/SKIP。
`tools/verify-browser-launch.mjs` 同时锁死静态调用边界、真实页面 JS 执行、ENOENT
不 fallback 与 EPERM 继续抛出。

**前端破坏注入（C10-C12）**：六个 `verify-*-injection.mjs` 统一经
`tools/_injection.mjs` 执行 `baseline green → injection red → restore green`。
每个 case 必须证明 patch 与业务锚点真实改变、目标 guard 非零且命中稳定 FAIL
marker；备份只进系统临时目录，每 case 与最外层 `finally` 都按原始 bytes/SHA-256
恢复。任一条件不满足，wrapper 自身 exit 1。`tools/verify-injection-wrappers.mjs`
用临时 fixture 锁死基线已红、注入假绿、no-op、restore mismatch、patch throw 与
子进程异常都不能转成成功。

**COT Index 不可知语义（C11）**：`fetch_cot.py` 用当前 52 周窗口统一计算
`weekly[*].mf_index/comm_index`，`latest` 直接复用最后一条 weekly Index；指标定义
不变，也不扩展为真正的历史 trailing-52。`index.html` 只展示这些后端字段，禁止
max/min 重算、`null → 50`、0/前值填补。任一侧 null 时有效侧仍单独显示，但综合
信号统一为“数据不足 · 暂不判断”，无方向风险、无规则高亮；管理基金图保留 null
缺口。`null` 是不可知，不是中性。schema_version 仍为 0。

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
`derived_from` 记上游身份，能看出派生数据基于哪一版原始数据算的。

**迁移状态（C6）**：当前受跟踪生产 JSON 已全部统一为 schema v0 envelope，
生产 Python/前端读取路径只接受合法信封；bare list/dict 回归由行为测试锁死。
`upstream_ref()` 对不存在的上游仍返回 `None` 以保留首次运行语义，但已存在的
bare 或损坏信封会明确失败，不再生成 `envelope:false` 元数据。

**FRED 数据安全回归（C8）**：`fetch_fred.py --test` 已用临时目录与 fake
payload 锁死磁盘级幂等（第二次 writer 零调用 + bytes/SHA-256 不变）、`"."`
缺失观测不补点且 coverage 只计有效点、c 类两份 quarantine 证据与旧主文件逐字节
不变、d 类明确 failure envelope 且旧业务数据不泄漏，以及多序列失败不短路后续
成功落盘。所有 fixture 禁止真实联网及写入真实 `data/`。

## 常见陷阱

### cwd 陷阱

外壳 cwd 与 wsl 内路径不同源；`cd /mnt/...` 在外壳会失败；cwd 漂到父目录后相对路径 `grep/ls` 静默返回 No such file，看起来像「文件无改动」——文档类改动前先 `pwd + ls` 确认。

### 测试基线表

测试条数变化必须同 commit 更新 `docs/handover-technical.md` 基线表。

**schema 版本决策**：strict-reader 收口不等于格式冻结。`SCHEMA_VERSION` 继续为
`0`，`KNOWN_SCHEMA_VERSIONS` 继续只接受 `{0}`；等 FRED / SPDR / 上海黄金 /
COMEX options 等新源接入并冻结格式后，再单独评估 schema v1。

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

### term-3d.html 已从导航移除，不再是生产读取点

`index.html` 里那个「3D 曲面 ↗」链接已删除，`term-3d.html` 文件保留但无任何
运行时入口引用它（全仓仅文档与注释提及）。它不再是 `oi.json` 的生产读取点，
改 `oi.json` 结构时不必再顾及这一页。

**若要重新启用，先把复刻的计算下沉到 derive。** 该页 `:122-146` 有三段
`windowMonths()` / `monthIndex()` / `sortedUnionLabels()`，是 derive 里
`window_months()` / `month_key()` / `contracts` 并集口径的 JS 复刻。实测两边
当前输出一致（各 15 列，集合与顺序均相同），但那是巧合而非契约 —— 两份实现
各自演化，口径一改就会分叉，且该页**无任何 verify 覆盖**（Playwright 脚本
全都只测 `index.html`），分叉不会有任何测试变红。

重新启用的前置条件：让它读派生产物的 `contracts` / `frames`，而不是自己
从 `oi.json` 重算一遍窗口与并集。

本地 Python 被 Application Control 拦截（`python`/`python3` 是 Store 占位符）。
用 WSL 跑：`wsl -d Ubuntu-22.04 -- bash -c "cd /mnt/d/VScode/test/gold-dashboard && python3 ..."`

### WSL 取证通则：跨 shell 传字符串必写成文件

**任何在 Windows 侧构造、再传进 WSL 的字符串，其中 `$` 开头的变量都可能被
Windows shell 先展开，导致取证恒真/恒假。取证脚本一律写成文件执行，不内联。**

已知形态：`echo $?`、`| tail -N`、`case "$1"` —— **形态会继续变，规避方式只有
写成文件。** 三个都是实测踩过的，每次都以「显示全绿」的样子出现：

| 形态 | 表现 |
|---|---|
| `echo $?` | `$?` 被 Windows 侧展开成上一条命令的码，恒 0。注入后测试打印「72 passed, 9 failed」，取证却报 exit=0 |
| `\| tail -N` | 取码时接管道，shell 返回管道**末命令**状态，python 非零码被吞 |
| `case "$1"` | `$1`/`$c` 内联时被先展开成空串，`case` 恒匹配空串分支，五个分支全走同一条却五条全 PASS |

适用于**所有** verify / `--test` / 闸门逻辑取证，不限于某一步。

**正确：取证逻辑写进文件，让退出码自然传出、在 Windows 侧读。**

```bash
wsl -d Ubuntu-22.04 -- bash -c "cd ... && python3 tools/xxx.py > /dev/null 2>&1"
echo "exit=$?"                      # 在 WSL 命令之外读，且不接管道

wsl ... -- bash -c "... python3 x.py 2>&1 | tail -6"   # 单独一步只看输出
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

**全 PASS 也要怀疑。** `case "$1"` 那次五个分支全绿，但绿的原因是五条输入全被
展开成空串、走了同一个分支 —— 断言从未区分过它们。取证脚本自身也要能被注入
证伪：改一个分支的期望值，看它是否变红。

这与「验证护栏靠注入破坏确认」是同一类陷阱：不注入就不会发现读到的是假码。

### 执行侧陷阱：用哪个 shell / 哪个解释器启动会静默改变结果

上一节讲的是「传字符串」，覆盖不住已发生的全部形态。更一般的根因是：
**同一条命令，从 PowerShell 启动、从 Git Bash 启动、从 WSL 启动，或经
`bash -c` 包一层，结果可以不同 —— 而且红绿两个方向都能错。**
一个「假绿」让你以为验证过了，一个「假红」让你去改本来正确的代码。

四种实测形态：

| 形态 | 表现 | 错的方向 |
|---|---|---|
| Windows 侧 `python` / `python3` | 命中 Microsoft Store 存根，打印安装提示后退出，**脚本静默不执行**。源文件从未被修改，注入恒不落地，被测对象照旧 —— 断言仍绿 | **假绿**（假「已验证」） |
| `bash -c "... ; echo $?"` 包装取码 | `$?` 取到包装层里某条中间命令的退出码，不是解释器的。实测 `derive --test` 明明打印 `1 failed`，取证报 `exit=0` | **假绿** |
| WSL bash 跑 `.mjs` | Playwright 的 `executablePath` 是 Windows 路径（`C:\Users\...\ms-playwright\...\chrome.exe`），WSL 里不存在 → `browserType.launch` 抛 `executable doesn't exist`，`exit 1` | **假红** |
| 注入脚本写盘格式与生产写入端不一致 | 注入用 `indent=2`、生产（`io_utils.atomic_write_json`）用 `separators=(",",":")` → 整文件重排。实测 `git diff --numstat` 从 `1 1` 涨到 `1739 1`，真实改动被淹没，**落地确认失去分辨力** | **无法判断注入是否落地** |

前两种最危险：它们同时骗过注入与断言。注入脚本"跑完"没报错，被测文件其实
一个字节没变，于是"注入后仍绿"被读成"这个破坏无影响"——**而真相是破坏
从未发生**。本仓踩过两次，一次是反转 X 轴断言时（改回旧口径后两条断言仍绿），
一次是早期 `ZZZ99` 注入（结论已重验，见下文该字段的护栏说明）。

第四种不改变红绿，但把「落地确认」这一步废掉了：diff 上千行时你无法一眼确认
改的是不是你想改的那一处，于是又回到"跑完没报错就算落地"。

**硬规则：**

- **前端 verify（`.mjs`）必须从 PowerShell 原生调 `node`**，不经 bash、不经 WSL
- **Python 取证必须 `wsl -d Ubuntu-22.04 -- python3 ...`**，不经 `bash -c` 包装
- **任何注入必须以 `git diff` 确认改动真的落地**（贴出 diff 行）；
  **未落地则整条取证作废重做**，不许拿"跑完没报错"当落地
- **exit code 直接取自解释器**，不取包装层：
  `wsl -d Ubuntu-22.04 --cd "$(pwd)" -- python3 x.py > log 2>&1` 然后读 `$?`
- **注入写盘必须复用生产写入端的序列化参数**（与 `io_utils.atomic_write_json`
  同格式：紧凑档 `separators=(",",":")`），**不得自行选择 `indent`** ——
  否则 diff 被重排淹没，落地确认失去分辨力

推论：**「注入后无变化」只有在落地已被 `git diff` 证实的前提下才是证据。**
否则它什么都不证明，包括那些据此写进本文件的结论 —— 换执行侧后都要重验一次。

## 数据规则

### 合约列表 = 全序列 window_months 并集，不按持仓阈值也不按存续过滤

`derive_term_structure.py` 的 X 轴 `contracts` 是全序列各帧 `window_months`
的并集（按月份顺序排序）。仍在挂牌的一律保留，无论持仓多小 —— 微小持仓靠前端
`minBarLength` 渲染成细线。

**到期合约保留 X 轴列位是既定口径。** 曾与"末帧是否仍挂牌"求交，合约一到期
就把整列从全序列历史里抹掉：实测 06-29 掉 JUN26、07-30 掉 JUL26，这两列在仍
存续的 23 帧上有真实 settle/oi 却无列可放（24 格，量级最高达该帧主力月 oi 的
2.87%）。更要紧的是移仓起点 —— 模拟 AUG26 到期，它在**全部 25 帧**失去列位，
其中作为 `front` 的 19 帧、作为 `roll_from` 的 24 帧，AUG→DEC 的
272518 → 2908 流出全程无处呈现，回放只剩承接端 DEC26 的上升。

轴长度恒定（不逐帧变），前端 `_initCharts` 一次性写 `labels` 的模式零改动可用。
代价是空列比例 7.4% → 13.3%。

断言不得再编码"已到期即剔除"。`verify-ui-fixes.mjs` /
`verify-contract-contango.mjs` 各有一条已反转为「已到期合约仍保留 X 轴列位」，
措辞与 derive 的 info 文案一致。

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

`roll_noise` 与 `roll_noise_ma` **计算链全程全精度，不在计算层舍入**。阈值待定、
将来要在这一列上跑分布，`round(4)` 会垫一层量化地板（相邻值被吸附到同一格），
影响拐点定位。`derive --test` 的 NOISE fixture 用 `1000/3001` 这种无限小数守着：
断言容差 `1e-12`，并检查落盘值不等于自身 4 位舍入 —— 退回 `round(4)` 会立刻变红。

### 落盘 float 统一 round(12)

**Actions runner 与本机对同一输入算出的 float 末位可能不一致**，实测
`roll_noise_ma` 3 帧差 1 ULP（Δ 量级 `1e-17`）。谁最后跑谁的值进仓库：本机跑
derive 改成本机值、下次 Actions 跑又改回去，每次往返 3 行无意义 git diff。

落盘时统一 `round(12)`（`derive_term_structure.py` 的 `_round_floats()`，
在 `main()` 写盘前最后一刻调用）：

- **只在写 JSON 那一刻 round，计算链全程保持全精度** —— `roll_noise_ma` 的
  3 帧滚动必须用未 round 的 `roll_noise` 算，算完再 round。`derive()` 的返回值
  仍是全精度，信封契约测试比对的也是全精度值。
- 12 位远超业务精度需求、又低于 float64 的 ~15-17 位有效数字：既不丢真实信息，
  又把两平台末位分歧吃掉。
- 已有意 `round(4)` 的字段（`front_remaining`）不受影响 —— 对已 round 的值再
  round(12) 是幂等的。

**只降低概率，不消除。** 某值第 13 位若恰在舍入边界，两平台仍可能一边进一边退。

**禁止改用容差比对代替。** 容差会把真实的微小变化也判为「没变」——
幂等判断的前提是「数据真变了才写盘」，用容差就等于承认「变了一点点不算变」，
而 CFTC/CME 的历史修订恰恰可能只差几个单位。宁可偶尔多一行 diff。

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

### MISMATCH 断言的比对范围 = 该帧 window_months，不是 contracts

`derive --test` 里拿 stored 与差分对账的抽查（2026-07-24 一带），**比对范围必须
是该帧的 `window_months`** —— 也就是 derive 实际计算过 `oi_chg` 的那个范围 ——
不是 `contracts`（展示列位）。

**同一个列表不得同时充当展示范围与校验范围。** 这两个范围的定义天生不同：

| 列表 | 是什么 | 谁决定 |
|---|---|---|
| `contracts` | 展示列位，全序列各帧 `window_months` 的**并集** | 全序列所有帧 |
| `window_months` | 该帧实际计算 `oi_chg` 的范围，`near + 1 年`截窗 | 该帧自己 |

`contracts` 改为并集口径后，**其中含从未进入该帧窗口的月份**：那些格子 derive
根本没算，值是 `None`，代表"未计算"而不是"算错了"。拿 `contracts` 当校验范围，
就会把"未计算"误判为"计算错误"。

实测 2026-07-24：`contracts` 15 列，该帧 `window_months` 13 个，差集
`['JUN26', 'AUG27']`。`AUG27` 有列位但不在该帧窗口内，stored `+11` /
computed `None` —— 这正是并集口径落地后 `derive --test` 那条红的全部内容。
把范围收到 `window_months` 后，该帧 stored 有值且在窗口内的 13 个合约**全部相符**
（不符 0 条）。

范围收窄不等于放宽校验：窗口外的 14 条 stored（`AUG27` 及 `SEP27`..`DEC29`）本
就无人计算，其中 13 条连列位都没有，原断言靠 `contract_idx.get() is None` 跳过 ——
真正被"跳过"以外的路径漏进来的只有 `AUG27` 这一条，因为并集给了它列位。

判断新断言该用哪个范围，先问：**这个字段是在哪个范围上算出来的？** 校验范围跟
计算范围对齐，不跟展示范围对齐。

### 帧级取证字段 vs 展示视图字段：两类字段的过滤规则相反

字段先分类，再决定能不能筛：

| 类别 | 语义 | 过滤规则 |
|---|---|---|
| **帧级取证**（as-of-that-frame） | 该帧当时的历史事实，事后不可变 | **不得经任何随时间移动的边界** |
| **展示视图**（as-of-now） | 今天该画什么 | 该跟着当前窗口/存续状态走 |

取证字段禁止经过的三类过滤，共同点是边界会移动：

| 过滤 | 边界 | 实测移动 |
|---|---|---|
| `contracts` 存续 | 最后一帧是否仍挂牌 | JUL26 到期即从全部历史帧消失 |
| `window_months` 窗口 | `near + 1 年` | 末端两次前移：06-29 `JUN27→JUL27`、07-30 `JUL27→AUG27` |
| 跨帧汇总列表 | `ever_front` / `peak_oi` 等由全序列算出 | 新帧会改写早期帧的判定 |

拿会移动的边界去筛历史记录，等于让今天的窗口位置回头篡改昨天的取证结论。
**目前唯一的取证字段是 `unreliable_chg`；其余 15 个帧字段都是展示视图字段**
（见下方「帧级字段过滤扫描」的分类）。新增字段时先归类。

`unreliable_chg` 因此走 `oi_maps_raw`（该帧原始 `months`，零过滤），
与展示字段用的 `oi_maps`（`window_months` 筛后）是分开建的两份 map。

曾在 `for label in contracts` 循环里顺带算，于是**合约一到期就把自己在所有历史
帧里的修订记录一起带走**。实测 JUL26 在 `2026-07-27` 确有修订（stored `-5` /
diff `+3`），到期后该帧的 `unreliable_chg` 里就没有它了 —— 回放到 7/27 看不出
那天数据被 CME 修订过。

这条比一般数据错更隐蔽，三个原因叠在一起：

- **失真单向**：只漏报、不误报，不会出现「有标记却指向不存在的柱子」的矛盾
- **无视觉异常**：该合约整列都不在图上，页面看不出任何不对
- **随到期节奏反复发作**：每个合约到期都会带走自己的记录，越老的记录越
  「干净」而那是假的干净。审计轨本身在缩水

`for label in contracts` 那个循环仍然只算 `settle` / `oi` / `oi_chg` 三个数组
（它们确实是按 X 轴对齐的展示数据）。改动时别把取证字段挪回去。

同一个错犯了两次，第二次只是筛子换了个名字：先是 `contracts`（到期筛），
改掉之后遍历的 `oi_map` 仍是 `window_months` 的产物（窗口筛）。**「不经
contracts」不等于「未经过滤」** —— 拆筛子要拆到原始 `months`。

**护栏只能加在 Python 侧。** `unreliable_chg` 全仓**无前端消费端**：
`index.html` 出现 0 次，`js/*.js` 仅 `js/playback.js:169` 的 mock 兜底字面量
（写非读），`_renderFrame` 不读它。六个前端 verify 对它**零条断言** ——
`verify-gapframe.mjs` 虽提到该字段，但只是把它置 `null` 用来构造断层帧，
断言对象是断层帧的渲染行为。

实测注入幽灵合约 `ZZZ99` 到某帧的 `unreliable_chg`：`pageerror` 0 条、
X 轴标签数不变、DOM 文本不含 `ZZZ99`、三图全存活 —— 页面与基线逐项一致。
**该字段怎么错都不会有页面症状**，所以指望前端 verify 兜底是空的。

**该结论已按执行侧硬规则重验（2026-08-03）**，早先那次注入可能命中 Store 存根
从未落地，"无变化"本来什么都不证明。重验做法与结果：

- 注入器经 `wsl -- python3` 执行，写盘用 `separators=(",",":")` 与 io_utils 同格式
- `git diff` 确认落地：`1 1` 行，`"unreliable_chg":null` →
  `"unreliable_chg":["ZZZ99"]`（帧 12 / `2026-07-15`）
- **确认浏览器真的吃到那份文件**：静态服务器记录送出的 series
  `{bytes:14917, hasGhost:true}`，页面内 `fetch` 复查 `rawHasGhost:true`、
  `framesWithGhost:[{i:12,date:"2026-07-15",u:["ZZZ99"]}]`
- 六个前端 verify 从 PowerShell 原生调 `node`：全部 exit 0，通过数与基线逐项相同
- 页面症状：`pageerror` 0、`console error` 0、X 轴 15 列不含 `ZZZ99`、
  DOM 文本与 outerHTML 均不含、三图存活、KPI 与基线一致；
  逐帧扫全部 25 帧 DOM 文本，`ZZZ99` 命中 0
- 全仓静态读取点只有两处，均非消费：`js/playback.js:169`（降级 mock 的字面量，
  写非读）、`tools/verify-gapframe.mjs:26`（把它置 `null` 用来造断层帧）

结论成立：无前端消费端、六个前端 verify 对它 0 条断言。护栏只能在 Python 侧。

两条断言守着：

- `derive --test` 的 `2026-07-27`：改回经 `contracts` 筛立刻变红
  （实测 → `FAIL 2026-07-27: 修订合约未被标记 unreliable: {'JUL26'}`）
- `derive --test` 的 **WINDOW fixture**：合成帧里 `DEC27` 落在窗口外且
  stored `-7` ≠ 差分 `+50`，断言它必须被标记。改回经 `oi_map` 立刻变红
  （实测 `1 failed → 2 failed`）

WINDOW fixture 必须是**合成数据**：当前 24 帧的修订合约恰好全在窗口内，
改走原始 `months` 对落盘产物零影响（新增 0 条），真实数据证伪不了这条约束 ——
不造数据就等于没有断言，退回 `oi_map` 会全绿通过。

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

页面「页面更新」只读 COT 信封的 `generated_at`，缺失时显示**「未知」**。
曾经的 `: new Date()` 兜底是把「不知道数据多新」粉饰成「刚刚
更新」—— 数据停更多久页面都显示当前时刻，陈旧完全看不出来。显示不出时间是
小事，谎报新鲜度是大事。

`screenshots/diag-cot-timestamp-injection.mjs` 有一条反恒真注入守着：把兜底
改回 `new Date()`，「显示未知」那条断言必须变红。不变红说明断言只是碰巧成立。

### 宏观 d 类阈值待实测后确定

新增 FRED 宏观数据时，d 类硬失败先只保留可确定性判断：当前 CPI、debt、
debt_foreign、GDP 值 ≤ 0。日期重复或非法属于 c 类 ParseFailure，先 quarantine
并逐字节保留旧主文件；成功 envelope 的 coverage 由实际有效点生成，C8 用硬编码
数据与 coverage 期望锁死缺点不补。有效点数下限、滞后天数上限、利率值域只写
warnings，不触发 d 类、不写 data:null。没有真实运行样本前不要拍阈值，避免误用
data:null 覆盖好数据；攒几周后再决定哪些 warning 升级成闸门。

### warning 不刷 `generated_at`，要持久化就走单独日志

数据未变但本次运行产生了新 warning 时：**跳过写盘**，warning 打 stdout。
`generated_at` 只在业务数据真变时刷新 —— 为 warning 刷它会让「文件变了」与
「数据变了」再次脱钩，而这正是幂等要建立的等价关系。

将来某类 warning 确需持久化审计，走**单独日志文件**，不塞进数据文件。

### 隔离区两份证据不许同名

`quarantine_write` 的 `raw_ext` 不能是 `"json"`：raw 与 payload 都会落到
`<prefix>-<stamp>.json`，写完只剩一份，且留下哪份取决于写入顺序。丢的那份
让事后无法区分「API 返回就是坏的」与「parse 解析错了」—— 而隔离区是坏数据的
唯一快照（CME/CFTC 无历史归档），错过就永久拿不回来。

`fetch_cot` 存 Socrata 的 JSON 响应时真踩到了：撞名后 `parsed_weekly` 静默
消失。现已在 `quarantine_write` 里断言撞名即抛（错开用 `raw_ext="raw.json"`），
`verify-io-utils` 有对应用例。**注释挡不住误用，断言才行** —— 与删掉
`data_envelope.write_json()` 同一判断。

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

C11 把同一计算扩展到每条 weekly，并删除前端两条 COT Index 回算。单侧或双侧
退化都必须把 null 原样传到数值、百分位条、综合信号与图表，不能再把缺失解释成
“另一侧未确认”或 50% 中性。

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
所以 stocks 与 gold 步骤都显式捕获 exit code 存进 `$GITHUB_OUTPUT`，末尾闸门
按 `case` 分三态：`1` → 红，`2` → 只 `::warning::` 不红，其他非零 → 红（未预期）。
`0|""` 那条空串分支不能省：步骤被跳过时 `outputs.code` 为空，会落到 `*` 误报。

**Python 侧与 yml 侧必须同时改。** 只把 `exit 1` 改成 `exit 2` 而不动 yml，
闸门仍按 `outcome=failure` 一律发红，分离等于没做。

**`fetch_fred` 是四态**：多出 `3` = `FRED_API_KEY` 缺失或无效。从前它混在 `2`
里只告警，能连日不红 —— 配置问题等不来自愈，必须有人去看，故单独成一态并报红。

**多序列脚本的汇总不能用 `max()`。** 退出码的严重度与数值大小无关：本仓的序是
`1 > 3 > 2 > 0`。`fetch_fred` 一个进程跑十三个序列，用 `max(code)` 汇总时，
一个序列 d 类失败(1) 撞上另一个序列下载失败(2)，进程码会变成 2 —— 把「要人管」
报成「上游没更新，正常」。故显式映射 `EXIT_SEVERITY = {0:0, 2:1, 3:2, 1:3}`
（`fetch_fred.py`，配 `_severity()` / `worse_exit()` / `run_all()`），
未登记的码视为最严重，以免新增的退出路径被静默吞掉。

这个缺陷曾在 yml 侧用一条数 `": [cd] exit 1"` 行的 grep 绕过顶着；源头修好后
绕过已删。回归闸门在 `fetch_fred.py --test`（"c/d 与 a 同批时进程码=1"、
"d 与 key 同批时进程码=1"、"key 缺失时进程码=3" 等 5 条汇总断言）——
**闸门只看进程码，所以汇总一旦退回 `max()`，红只会出现在 `--test` 里。**

**gold 已分离（P1 第 3 步 commit1）**：下载层「两源皆不可达」→ `2`，
新增「上游 `cot.json` 无 weekly」→ `2`，解析层五项闸命中仍 `1`。
两条 `exit 2` 都**不隔离** —— 没有坏数据可留证，隔离区只存真损坏。

「上游无 weekly」必须单独成一条：不拦的话空 `cot_dates` 算出空 `result`，
被闸的 a) 判据抓成「全部 0 周对齐失败」，归因指向对齐逻辑而真因是上游没数据。
实测注入确认过：去掉这道闸后空 weekly 报 `exit 1`、缺键直接 `KeyError`。

**COT 网络失败已于 C7 分离**：`fetch_api()` 将 DNS / connection / timeout / TLS、
HTTP 403/408/425/429/5xx 与可识别的临时错误页/WAF 响应归为 `CotFetchFailure`，
`run_once()` 明确返回 `2`，不覆盖旧 `cot.json`、不伪造 raw、不写 quarantine。
HTTP 成功但非预期 JSON、顶层 schema 变化、字段无法解析归为 `CotFormatFailure`，
返回 `1` 并保留响应证据；JSON 可解析但五项业务校验失败仍返回 `1`。
workflow 的 COT step 显式写出真实进程码，末尾按 `0/1/2/*` 分流；`2` 只 warning，
`1` 与未预期退出码报红。`tools/verify-fetch-gates.py` 同时守住 step output、case
分支和 exit 2 不调用 `fail()`。

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

- `index.html` 的 COT Index 只读 `cot.json` 的 `latest` 与 `weekly[*]` 预计算字段。
  任一侧 null 时有效侧可以单独展示，但综合信号必须暂不判断、不高亮规则；管理基金
  Index 区间只在自身值有效时高亮，图表 null 保持缺口。
- `macro.html` 的联邦债务总览是一张双 Y 轴综合图：`intragov_bn` /
  `domestic_public_bn` / `foreign_bn` 三项共用左轴 `yAmount` 的同一 stack，
  `total_bn` / `gdp_bn` 是同轴金额线，`debt_gdp_pct` 是右轴 `yPct` 比例线。
  前端不做 `/1000`、不重算本国公众持有、不填补 foreign；`stack_last` 后三项
  保持 null，但金额/比例线按各自真实 coverage 继续延伸。`public_gdp_pct` 仍保留
  在派生文件中，但 C12 主图不展示。
- macro rates/CPI 与 debt 使用独立加载错误边界；任一组失败不得拖掉另一组。
- Y 轴 min/max 全部取自派生 JSON 的 `scale`，回放期间 Chart.js 不自动缩放，
  帧间柱高可直接比较
- 升级 Chart.js 或改动 `_initCharts` 初始化路径时，必须复跑
  `verify-ui-fixes` 的 [8]a —— 首屏 update 时序依赖 `setTimeout(…,0)`
  排在初始 responsive resize 之后（playback.js 初始化末尾那次补充
  `update('none')`），该假设不受任何契约保证
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

本节只列命令与用途，**不写测试项数** —— 项数唯一维护点是
`docs/handover-technical.md` 的基线表，两处各存一份必然漂（曾漂到 13/23/12 三个错数）。

```
python3 derive_term_structure.py --test    # 派生逻辑（含信封契约、KPI）
python3 derive_macro.py --test             # 宏观派生（rates/cpi/debt 三链、warnings 拆分）
python3 fetch_oi.py --test                 # 采集校验（不联网）
python3 fetch_cot.py --test                # 采集校验（含 build_payload 幂等前提）
python3 fetch_gold.py --test               # 采集校验（含退化边界）
python3 fetch_stocks.py --test             # 采集校验（含缺字段 SKIP）
python3 fetch_fred.py --test               # 采集校验（13 个 FRED 序列，四态 + 磁盘安全）
python3 tools/verify-fetch-gates.py        # 端到端注入：闸真的拒绝落盘
python3 tools/verify-io-utils.py           # 落盘骨架（含隔离区撞名断言）
node tools/verify-ui-fixes.mjs             # UI 几何 / COT Index 单侧与双侧 null
node tools/verify-contract-contango.mjs    # 合约过滤 / 最小柱高 / 价差锚点
node tools/verify-playback.mjs             # 回放交互
node tools/verify-gapframe.mjs             # 断层帧
node tools/verify-isolation.mjs            # 注入渲染故障，验证模块隔离
node tools/verify-schema-coupling.mjs      # 注入 schema 破坏，验证护栏会变红
node tools/verify-envelope-helper-raw-inputs.mjs  # 前端解包 helper 不吃裸格式
node tools/verify-cot-sentinel-strict.mjs  # COT 哨兵值严格拒绝
node tools/verify-kpi-injection.mjs        # 注入错误 KPI 值，验证护栏会变红
node tools/verify-spread-injection.mjs     # 注入错误 spread，验证护栏会变红
node tools/verify-totaloi-injection.mjs    # 注入旧口径 total_oi，验证护栏会变红
node tools/verify-isolation-injection.mjs  # 注入隔离失效，验证 isolation 会变红
node tools/verify-cot-index-null-injection.mjs  # 注入 COT null→50，验证 UI 会变红
node tools/verify-injection-wrappers.mjs   # wrapper 状态机/恢复/退出码基础设施
node tools/verify-debt-overview-injection.mjs  # C12 双轴/公众重复/GDP 删除三项注入
node tools/verify-macro-page.mjs           # macro.html 图形态 / CPI 右端 / 债务单图双轴与隔离
python3 tools/verify-noise-injection.py    # WSL 内运行；三种 noise 注入必须红，恢复后绿
node tools/verify-live.mjs                 # 线上端到端
```

**九个前端 verify 需要本地静态 server 监听 3001**（脚本一律 `goto
http://localhost:3001`，没起 server 时全部 `ERR_CONNECTION_REFUSED`）。
server 必须 **`.listen(3001)` 双栈**，只绑 `127.0.0.1` 会让 `verify-isolation`
挂在 `page.goto` 超时 60s：它用 `route.fetch()` 由 **Playwright Node 侧**重新发
请求（`tools/verify-isolation.mjs:38-43`），按 `localhost` 先试 `::1`；其余脚本
走浏览器的网络栈，IPv4 回退正常，照常全绿。

推论：**「同一个 server 上别的脚本能过」不能证明 server 没问题** —— 浏览器与
Playwright Node 侧走两条不同的网络栈，一条通不代表另一条通。

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

### tools/verify-noise-injection.py 从 WSL 直接运行（C5）

运行方式：`wsl -d Ubuntu-22.04 --cd <repo-wsl-path> -- python3 -B
tools/verify-noise-injection.py`。脚本已经在 WSL Python 内，子测试用
`sys.executable` + `shell=False` 直跑，不再从 WSL 反向调用 Windows `wsl.exe`，
也不经过 `bash -c` / PowerShell；`CompletedProcess.returncode` 就是目标测试的
真实退出码。

脚本必须同时证明：基线与恢复后 exit 0；三种 noise 破坏均非零且命中预期 NOISE
断言。任一条件不满足，脚本自身 exit 1，不能只打印红绿后仍返回 0。源码备份放系统
临时目录，每个 case 后及最外层 `finally` 恢复，并以 SHA-256 校验无污染。

同类陷阱：跨 shell 传 Python 代码时 `python`/`python3` 可能命中 Store 存根而
**静默不执行**，`$?` 仍可能为 0，注入看似完成实际没改任何东西 —— 本轮反转断言
的注入证明就先踩过一次（改回旧口径后两条断言仍绿，实为源文件从未被修改）。
注入后务必用 `git diff` 确认改动真的落地，再跑被测脚本。

读柱子几何要用 `getProps([...], true)` 取终态：`el.y` 在动画期间是插值中间态，
24 万手的柱子也会读成高度 0。

`minBarLength` 的实际几何是「传入值 − borderWidth/2」，要净高 2px 得传 2.5。
