# 交接摘要（决策侧）：COMEX 黄金期货分析看板

> 本文档是**决策侧**交接，配合 Claude Code 生成的**技术侧**文档一起使用。
> 技术侧含：git 状态、代码结构、行号索引、护栏当前红绿。**行号以技术侧为准**（已多次移位且经校验脚本核对）。
> 本文档含：已定决策、待办清单、协作方式。
> 两份都贴进新窗口。

---

## 1. 背景与角色

COMEX 黄金（GC）多源数据分析看板，域名 www.zhangtongxue.com，用 Claude Code 开发。
已有五块：期限结构图（含移仓回放）、COT 持仓、金价、库存、仓库明细。

**核心定位：数据观察工具，不是交易系统。**
贯穿原则：诚实标注不确定性——区分 0 / null / 字段缺失（三态不塌成两态）、
不在数据不足时硬拍阈值、护栏必须真能抓错。

**助手（你）的角色**：帮我审计划、审验证结果、把关方向，**不是替我写代码**。
- 要质疑我和 Claude Code 的方案，指出「用连续指标回答二元问题」「相对于漂移基准的阈值」
  「同一个列表被用在两个不同语义的地方」这类系统性问题。
- 我给的方案错了要直接推翻（已发生多次，且推翻是对的）。
- 每轮结束提醒我还挂着哪些未落地的 TODO，防止它们只活在对话里。
- 涉及交易/投资内容，提醒我你不是投资顾问、这是数据观察非交易建议。
- 我状态累时会直说，此时优先建议我停在干净存盘点（local=remote、tree clean、无半拉子），
  而不是硬推进度。

**当前所处阶段**：「三件地基重构」的第③件（代号 P1）——抽统一落盘模板 + 四源信封化，
为接入新数据源（FRED 宏观利率、SPDR ETF、上海黄金、COMEX 期权）理顺架构分层。

---

## 2. 已定决策（不用再议）

### 分层与契约
- 三层：采集层（fetch，只抓取落盘）→ 计算层（derive，派生指标）→ 展示层（前端只读）。
  跨源计算必须落在计算层。
- 派生/落盘 JSON 用信封：`{schema_version, source, freq, generated_at, date_field,
  coverage, derived_from, warnings, info, data}`，业务数据全在 data。
  schema_version 从 0 开始；strict-reader 收口后仍保持 0，等未来新源接入并冻结
  格式时再单独决定是否升 1。
- 当前受跟踪生产 JSON 已统一为 schema v0 envelope，生产读取端只接受合法信封；
  bare 输入是负向回归场景，不再是兼容路径。
- `oi_chg` 一律从 OI 存量差分自算，源站字段仅作交叉验证。

### io_utils 骨架 vs 语义边界
- io_utils 只提供机械操作：`atomic_write_json` / `atomic_write_bytes` / `read_json_or` /
  `upsert_by_key` / `apply_retention` / `quarantine_write` / `sweep_stale_tmp`。
- 硬约束：禁任何 `value or 0` 兜底；**不调 sys.exit()，一次都不**；
  不做任何失败/无新数据的语义判断。
- 留在各调用方：quarantine 触发条件、exit code 语义、「无新数据」判定、0/null 语义、
  merge 回调、compact 参数。
- `upsert_by_key` 返回 `(列表, 动作, 原因)`；KEEP_OLD 时列表对象原样返回（`out is REC`），
  调用方据此跳过写盘。merge 必须传，不传一律 KEEP_OLD 不猜；merge 返回未知动作 raise。
- merge 三态：KEEP_OLD / TAKE_NEW(reason="revised" 值变了 / "backfilled" 缺失变有) /
  REJECT 交给 validate 不在 merge 判。**info 措辞必须区分修订与补全。**
- 原子写失败时**不删临时文件**（诊断线索，sweep 下次清）。
- **不反向迁就骨架**：形态不匹配的函数就不用。cot/gold 都是七用五，理由各不相同。

### exit code 三态
0 正常（含幂等跳过）/ 1 需人工介入 / 2 上游未更新。
**Python 侧和 workflow yml 必须同时改**，否则 continue-on-error 把 1 和 2 都记成 failure。
yml 用 `case` + `0|"")` 空串分支（防步骤被跳过时误报）。

COT 的长期边界：DNS/连接/timeout/TLS、HTTP 403/408/425/429/5xx、明确的临时错误页
或 WAF 响应属于 2；HTTP 成功但非预期 JSON、顶层 schema 变化、字段无法解析，以及
JSON 可解析后的业务校验失败属于 1。exit 2 不覆盖旧 `cot.json`、不伪造 raw、只告警；
exit 1 保留可用响应证据并报红。workflow 必须读取 COT step 输出的真实进程码，
不得退回只看 step outcome。

### generated_at 语义
表示「数据这次真变了」，不是「脚本跑了」。数据逐字段相同 → 文件完全不变、git 无 diff。
幂等判断**只比业务数据（data 内容），不含信封元数据**。
cot 比整个 data（latest+weekly 全量，因 CFTC 会修订历史期）；gold 直接比自己的 data，
**不以 cot 的 generated_at 为判据**。
若数据相同但产生新 warnings → 仍跳过不写盘，warning 打 stdout。

### total_oi 口径（三层收窄，已定死）
`total_oi = Σ OI over month ∈ ever_front`，即「已当过持仓最大月」的已确立主角之和。零阈值。
不含到期清算残余、不含从未当过主角的名义月、**不含尚在积累未坐正的末端承接月（当前 FEB27）**。
不用 `major_months()` 返回值（含未跑分布的拍值 `MIN_NEXT_OI_RATIO=0.01`）。
与价差/主力月口径**有意略窄**：价差含承接月（问「往哪移」）、total_oi 只含已确立主角（问「盘子多大」）。
实测：07/29=302,267、06/26=320,600（旧值 380,608 / 361,195 已作废）。

### contracts 并集口径（`44aecf2` 已定）
`contracts` = 全序列各帧 `window_months` 的并集，按月份顺序排序。
**到期合约保留 X 轴列位**，不与「末帧是否仍挂牌」求交。
论据见第 4 节。**断言不得再编码「已到期即剔除」**——
两条编码旧口径的前端断言已于同一 commit 反转（反转而非删除，删了该路径零覆盖）。
轴长度恒定，前端 `_initCharts` 一次性写 `labels` 零改动可用。
代价：空列比例 7.4% → 13.3%，730 帧外推约 48 列（日历月法）。

### 移仓相关
`front_by_expiry`（到期月）与 `dominant_by_oi`（持仓最大月）严格分开。
活跃月用序数信号 `ever_front`/`major_months` 判定，不是 OI 绝对阈值。
`front_remaining` 保留 round(4)（纯展示比率）；`roll_noise`/`roll_noise_ma` 全精度落盘。
CONTANGO 用 AUG26−DEC26 主力−次主力，显示年化率。

### 联邦债务面板语义（C4，C12 可视化覆盖）

- C13 将 GFDEBTN / FYGFDPUN / FDHBATN / FDHBFIN / GDP 五条债务/GDP 季频源的
  请求起点固定为 `1990-01-01`；其余 rates/CPI FRED 序列仍从 `2016-01-01`
  开始。前端展示派生文件的全量真实历史，不通过裁轴、补 0、前值填充或跨序列反算
  制造覆盖。若结构源有真实缺口，允许总债务/GDP线继续而结构 stack 留空。
- 前端只消费 `macro_debt.json` 的派生字段，不做百万→十亿换算，不重算
  `domestic_public_bn`，也不以 0 或前值填补 foreign。
- 债务结构是 `intragov_bn + domestic_public_bn + foreign_bn` 的共同 stack。
  foreign 右端滞后一季时，三项同时保持 null；宁可显示结构缺口，也不画一个会被
  误读成债务骤降的不完整堆叠。
- 总债务、GDP 与比率按各自真实 coverage 延伸：结构缺口季度仍可显示 `total_bn`、
  `gdp_bn`、`debt_gdp_pct`。结构 coverage 与金额/ratio coverage 不强行对齐。
- C12 按明确产品要求将债务区域收口为一张双 Y 轴综合图：三项结构柱、`total_bn`
  与 `gdp_bn` 共用左轴 `yAmount`（`USD bn`），`debt_gdp_pct` 独占右轴 `yPct`（`%`）。
  `public_gdp_pct` 不在主图展示但不从派生契约删除。所有六个 dataset 直接读取派生
  字段，折线 `spanGaps=false` 且不做平滑插值或前值填充。
- rates/CPI 与 debt 是两个独立前端加载边界，任一组失败不得拖掉另一组。

### 日频债务与低频真实性（C14）

- update cadence 与 source frequency 分开：workflow 每天检查，不代表所有指标日频。
- total/public/intragov 优先使用 Treasury Debt to the Penny 的真实观测；美元只在
  采集层除以 `1e9`。1990 至各字段 Treasury 首个真实观测前保留 FRED 季度历史，
  起点后源缺值就留 null，不拿季度值或前值补日频空洞。
- foreign 当前继续 FDHBFIN/FRED 季度频率；GDP 与正式 debt/GDP 继续季度。
  禁止 forward-fill、插值、0 fallback，也禁止用“每日债务 / 最近季度 GDP”冒充
  正式每日 debt/GDP。
- 低频折线的渲染输入只包含真实 `{x, y}` observations，并直接连接相邻真实观测；
  这只是可视化表示，不增加中间日期的数据点。不能用与日频全集等长的 null 数组配合
  `spanGaps:false` / `pointRadius:0`，否则 1993 年后季度曲线会实际不可见。
- Treasury 源字段自身异常按分量隔离：可信 total 可继续保留，不一致的 public /
  intragov 同日一起置 null 并写 warning，不能为满足恒等式反算任一分量。
- 债务图可在 fine pointer 环境左键拖框缩放 X 轴；Y 轴保持全历史范围。移动端不启用
  drag zoom，reset 按钮始终存在。真实鼠标操作而非源码 grep 是验收依据。

### 债务堆叠与 mixed-frequency tooltip（C15）

- 用户可见 dataset 固定为六个：三条季度结构柱、`total_bn` hybrid 折线、季度 GDP
  折线和季度 debt/GDP 折线。公众持有、政府内部持有不再另画日频独立线；图例不使用
  “结构快照”等实现术语。
- 三条结构柱只消费三分量同时有效季度的真实 `{x, y}` observation，共用同一 stack
  和金额轴，并以固定可见宽度呈现。任一分量缺失时整个季度结构不画，不允许 0、前值、
  插值或不完整堆叠；这不阻止同季度有效的 total、GDP、debt/GDP 延伸。
- tooltip 是展示期查询，不是数据变换：hover 任一真实日期时统一展示六项；日频 total
  使用该日真实 Treasury 值，结构、GDP、debt/GDP 分别取不晚于 hover 日期的最近真实
  低频 observation，并各自标明 as-of。查询值不得写回 dataset，也不产生任何日频低频值。
- C14 的 fine-pointer X 轴拖框、按钮/双击 reset、固定 Y 轴与移动端无溢出继续保留；
  真实 Playwright hover 与 drag 是主要验收证据。

### Cloudflare 静态资产发布边界（C16）

- 本项目是纯静态站点，不增加虚构的 Worker entry point；`wrangler.jsonc` 只配置
  `assets.directory = ./dist`，不设置 `main` 或 bindings。
- 禁止直接把仓库根目录作为 assets。`dist/` 必须由跨平台 Node 脚本从显式文件白名单
  重建：三个用户 HTML、当前 assets/css/js 文件和 24 个公开 JSON；不得按扩展名自动
  纳入未来新增文件。
- Python、Markdown、PDF、AGENTS/CLAUDE/README、tools、docs、workflow、Git 元数据、
  quarantine 与临时文件均属于非公开开发面；构建 guard 同时锁 manifest、源/产物
  SHA-256 和 forbidden paths，不能只靠排除列表。
- production/preview 都消费同一 dist；区别只在 deploy command：production 使用
  `wrangler deploy`，preview 使用 `wrangler versions upload`。本地验收只能 dry-run，
  不从开发机直接部署或改变 route/domain/traffic。

### 财政可持续性口径（C17）

- 核心债务口径是公众持有债务/GDP；总债务/GDP 只作背景，不能代替模型中的 `d`。
- MTS Table 9 的 Receipts/Total、Net Outlays/Total、Net Outlays/Net Interest 必须按
  hierarchy 先选、line/data/record type 再校验。使用 current-month cash amount，
  不用 FYTD，不以 gross interest 或 DGS10 代替 net interest/effective `r`。
- 所有财政流量按截至季度末的 12 个连续日历月 TTM 汇总；缺一月或任一字段缺失，
  该季度整组财政 TTM 为 null。GDP 使用季度名义 GDP SAAR，不能除以 4。
- `effective_r = TTM net interest / 同窗口有效日频 public debt 均值`。周末/假日不是
  缺失点，不填充；但 12 个日历月各自必须至少有一个真实公众债务观测。
- `g` 固定为名义 GDP 同比 `GDP_t/GDP_t-4 - 1`。primary balance 盈余为正：
  `receipts - total_outlays + net_interest`。
- `p* = (r-g)*d/100`；`fiscal_gap = p* - actual_primary_balance`。gap 正表示当前
  观测组合需要额外初级调整；`r>g` 是放大因素，不是唯一判断门槛。
- 观测 QoQ `Δd` 与模型 annual-rate RHS `/4` 后比较；差额保留为 stock-flow residual，
  不把 residual 强制归零，也不把它自动解释成模型失败。
- 九项一致历史从 2016-Q1 开始，不向 1990 拼接其它财政流量。schema 保持 v0。
  C17 历史模块只做实际历史/当前算术监测：`stress_level=unscored`、
  `threshold_version=null`，不输出失控年份、不拍 GREEN/YELLOW/ORANGE/RED 阈值。
- Fiscal Gap 的产品判决严格沿用 `gap = p* - actual`：`gap <= 0` 为“稳定条件满足”，
  `gap > 0` 为“稳定条件不满足”，unknown 不判断；前端只读逐季 gap/trajectory，禁止
  重算。负 gap 的绝对值称“当前稳定缓冲”，正 gap 的值称“当前财政调整缺口”，仅是
  当期数学 adjustment gap，不转换成必须立即削减的美元金额。
- p* 本身是动态判据线；0% GDP 只区分初级盈余/赤字，必须叫“参考线”，不能冒充稳定
  判据。判决颜色只表示 condition met/not met，不是 Fiscal Stress Score 或风险等级。
- 上一条只针对 actual 与 p* 两线图。独立 Fiscal Gap 图中，0% GDP 正是 gap 的数学
  判据线：曲线 `<= 0` 表示稳定条件满足，`> 0` 表示需要财政调整。两张图不得混淆
  这条 0% 线的语义；Fiscal Gap 曲线仍只能读取派生字段，不在前端重算。
- “稳定条件满足”不等于观测债务/GDP 当期必然下降；stock-flow residual 必须继续展示，
  且页面和 tooltip 都保留这一区分。C17.1 不引入阈值缓冲带或 near-threshold 状态。

### CBO 官方 Baseline（C18A）

- CBO forecast 是有版本含义的官方 baseline，不是滚动历史源。每个 workbook 以
  publication month、官方 URL、完整 sheet 集和 SHA-256 固定为 immutable vintage；
  旧 vintage 永不覆盖，latest 只在全量 schema/单位/范围验证成功后切换。
- 当前 vintage 是 2026-02-11 的 *The Budget and Economic Outlook: 2026 to 2036*，
  Table 1-1；2025 为 actual，2026..2036 为 projection。全链使用 CBO fiscal year，
  不与 Economic workbook 的 calendar-year 表混用。
- primary balance 统一采用 surplus-positive：官方 Primary Deficit 负值原样对应赤字。
  debt/GDP、net interest/GDP、receipts/GDP、outlays/GDP 都直接消费官方百分比字段，
  前端不得通过金额重新构造官方 baseline。
- 页面必须把 C17 历史 actual 与 CBO projection 视觉分段，并标明 publication、vintage、
  actual-through 与 projection horizon。baseline 是条件路径，不是确定性预测；不得输出
  “危机年份”、压力颜色或伪精确概率。
- CBO 当前 workbook 没有与 C17 `effective_r` 可严格桥接的 forward rate，市场利率不能
  冒充有效利率。因此 C18A 不计算 forward p* / Fiscal Gap；缺严格共同口径时结论必须是
  unavailable，而不是在前端拼公式。
- 更新策略为人工审计新 vintage：每日 workflow 只运行离线 parser guard，不自动下载
  或发布 CBO 文件。vintage 记录人工下载时间；解析异常写本地 diagnostics 且不切换
  latest。原始 XLSX/diagnostics 不进 Git/静态站；版本 JSON 可追溯，浏览器只公开 latest。

### CBO 财政情景实验室（C18B）

- 官方 CBO Baseline 始终只读、单独标识；用户情景是 deterministic sensitivity，
  不是 CBO forecast，不输出概率、危机/失控年份、风险颜色、presets 或 forward Fiscal Gap。
- 会计 basis 以 2025 actual 为锚。2026..2036 每年保存
  `deficit=-overall_balance_pct*GDP` 与
  `SFA=official debt-prev official debt-deficit`；SFA 只负责闭合官方债务金额，不能解释成风险。
- shock 从用户选择年度永久生效：名义 GDP 增速加 growth shock；官方 overall balance
  加 primary-balance shock、减 interest-spending shock。正 primary shock 表示改善，
  正 interest shock 表示利息支出增加。scenario SFA 维持 baseline SFA/GDP 比例并按情景
  GDP 缩放，债务按 previous debt + deficit + SFA 递推。
- zero shock 必须逐年精确返回官方 debt amount 与官方 debt/GDP。金额层先以 residual
  验证闭合；官方百分比有发布舍入，零冲击不得以金额/GDP 重算出伪精度覆盖官方值。
- C18B 不读取 C17 `effective_r`，也不由 net-interest/GDP 宣称有效利率。浏览器纯函数
  是“生产 source/derived 前端只读”原则的窄例外：只生成内存中的 synthetic user
  scenario，不 mutate 输入、不写 baseline/JSON/localStorage、不冒充官方 observation。
- Scenario Lab、CBO Baseline 与 C17 各自故障隔离；任一数据链失败不拖掉另两块。

### 帧级 vs 展示视图二分（重要）
**帧级取证字段（as-of-that-frame）与展示视图字段（as-of-now）是两类东西。**
取证字段不得经任何**随时间移动的边界**过滤：
- `contracts`（**已于 `44aecf2` 改为全序列 `window_months` 并集**，不再随存续状态变动）
- `window_months`（near + 1 年截窗，末端随 near 右移，实测 06-29、07-30 各移一次）
- 任何跨帧汇总列表（`ever_front` / `peak_oi` / `major_months`）

实现方式：**两份 map 分开建，不共用一份再过滤**——
`oi_maps`（窗口筛后，展示用）与 `oi_maps_raw`（该帧原始 months，取证用）。
语义分离落在数据结构上而非条件判断里，不容易被误合并。

**推广形式（`3719857` 之后）**：同一个列表不得同时充当**展示范围**与**校验范围**。
判断某断言该用哪个范围，先问：**这个字段是在哪个范围上算出来的？**
校验范围跟计算范围对齐，不跟展示范围对齐。

**背景（为什么有这条）**：`unreliable_chg` 是帧级历史取证字段，原先被 `contracts` 过滤，
等于让今天的存续状态回头篡改历史帧的取证记录。JUL26 到期后，7/27 那天它被修订过的事实消失了。
失真单向、只漏报不误报、无视觉异常，所以长期未被发现，且会随合约到期节奏反复发作。

### 测试与取证（血泪教训）
- 护栏靠注入证明真会红，不靠读代码推断。每条闸单独注入验证。
- **执行侧陷阱：用哪个 shell / 哪个解释器启动会静默改变结果**（本仓已实测四种形态，
  红绿两个方向都能错）：
  1. Windows 侧 `python`/`python3` 命中 Microsoft Store 存根 → **脚本静默不执行**，
     源文件从未被修改，注入恒不落地，断言仍绿（假「已验证」）。**最危险**
  2. `bash -c "... ; echo $?"` 包装取码 → 取到中间命令退出码（假绿）
  3. WSL bash 跑 `.mjs` → Playwright 的 `executablePath` 是 Windows 路径、WSL 里不存在
     → `executable doesn't exist`，exit 1（假红）
  4. 注入脚本写盘格式与生产写入端不一致（注入用 `indent=2`、生产用
     `separators=(",",":")`）→ 整文件重排，diff 上千行淹没真实改动，落地确认失去分辨力

  **硬规则**：前端 verify（`.mjs`）必须从 PowerShell 原生调 `node`；
  Python 取证必须 `wsl -d Ubuntu-22.04 -- python3`，不经 `bash -c`；
  exit code 直接取自解释器不取包装层；
  注入写盘必须复用生产写入端的序列化参数，不得自行选择 `indent`。
- **「注入后无变化」只有在落地已被 `git diff` 证实的前提下才是证据。**
  否则它什么都不证明——包括那些据此写进 CLAUDE.md 的结论。**换执行侧后都要重验一次。**
  仅"脚本跑完没报错"不算落地。
- **注入还要证明被测端确实吃到了改动**：改数据文件后，还需确认服务端送出的就是那份、
  页面 `fetch` 到的就是那份（缓存 / `?_=` 时间戳 / dev server 路径都可能插一脚）。
  ZZZ99 重验补的正是这一环。
- **范围收窄类改动必须同时加防恒真闸**：收窄到零时断言会变恒真。
  抽查 2 收窄到 `window_months` 后加了 `if not checked` 计数器，
  窗口内一条可对账都没有时报「抽查 2 自身失效」而非 PASS。
- **收窄后要做双向注入**：范围内注入必红（证明断言还活着）、
  范围外注入按定义应绿（证明是"符合定义"而非"误放行"）。
- **应该红却显示绿时，先怀疑取证方式，再怀疑被测对象。**
- **秒精度时间戳参与的比较，同秒执行会使结果失去分辨力，红绿两个方向都会错。**
  （已知：同秒 timestamp 相同致假红；同秒连跑 derive 致幂等假绿。）
  幂等类取证必须强制跨秒或用哨兵时钟（如 `2099-01-02T03:04:05Z`）。
- 防恒真断言：`X or True`、set 差集恒为空。
- **比对基准要取 `git show HEAD` 的版本，不要取磁盘文件**——磁盘那份可能已被前几轮重跑覆盖，
  拿它比是自比自。
- **改了写入端格式，必须临时造出新格式文件让所有读取端在新格式下也跑一遍。**
- 一红七绿比全红更有信息量（证明断言之间无耦合、分辨力够）。
- 阈值一律先跑分布再定。
- **对现有数据零影响的改动，必须造 fixture 才能证伪**——真实数据测不出来的就造数据测。
  fixture 要自带锚点自检（如「该合约必须在窗口外、必须不在 contracts」），
  锚点失效要自曝而非静默通过。
- **取证脚本的临时文件不受 `sweep_stale_tmp` 覆盖**（它只清 OUT_PATH 同名 tmp），
  同一轮内自行清理。

### Playwright 浏览器解析策略（C9）

- 所有 `tools/` browser-owning guard 只经 `tools/_browser.mjs` 启动 Chromium。
- executable 必须来自当前项目 Playwright 的 `chromium.executablePath()`，不得扫描
  浏览器缓存、选择“最大 revision”、硬编码用户目录或固定 revision。
- 当前环境只安装完整 Chromium、未安装独立 headless shell；helper 因此显式启动
  Playwright 对应的完整 Chromium，不使用默认 headless-shell 路径。
- executable 不存在、不可访问或启动失败必须抛 `BrowserEnvironmentError` 并非零退出；
  不 fallback 到其它浏览器、不自动安装、不把 EPERM 记成 PASS/SKIP。
- `verify-browser-launch.mjs` 必须真实创建 page 并执行 JS，同时静态守住普通脚本不得
  直接 launch。改变此策略前先修改这条 guard 并做反恒真，而不是在单个脚本加例外。

### 前端 injection wrapper 契约（C10）

- Node 破坏注入统一经 `tools/_injection.mjs`，业务 patch 留在各 wrapper，不放进 helper。
- wrapper 的成功必须同时证明：baseline 绿、patch/业务锚点真实改变、injection 红并
  命中稳定 marker、逐 case 恢复、restored baseline 绿、最终 SHA-256 与运行前一致。
- backup 只放系统临时目录；case 与最外层 `finally` 都恢复。baseline 本来已红、spawn
  error、signal、timeout、no-op、锚点未命中、注入仍绿或恢复错配均必须使 wrapper exit 1。
- `verify-injection-wrappers.mjs` 是公共状态机的 meta guard；改变 helper 的失败累计或
  恢复语义前必须先用其临时 fixture 证伪，不得只观察真实 wrapper 输出。

### COT Index null / 不可知语义（C11）

- COT Index 算术归 `fetch_cot.py` 所有：在当前 52 周窗口上为每条 weekly 记录预计算
  `mf_index/comm_index`，`latest` 直接复用最后一条。前端只展示字段，不做 max/min
  重算。本轮不把算法改成真正的历史 trailing-52。
- `null` 表示不可知，绝不等于 50、中性、0 或最近值。管理基金图保留 null gap。
- 任一侧 null 时，有效侧仍可显示真实百分位及其单指标区间；综合双指标信号固定为
  “数据不足 · 暂不判断”，不输出偏拥挤/偏清淡或方向风险，不高亮任何规则。
- `weekly[*].mf_index/comm_index` 是 schema v0 envelope 内的 additive 业务字段，
  不触发 schema_version 升级；原 date/net/OI 与 coverage 不变。

### 破窗规则
改格式时，**读取端先容双形状 → 写入端再切**。因为数据文件要等下次 Actions 才变形，
本地 verify 全绿、破窗在下次 CI 才炸、归因窗口已关。
但要看读取端的**失败性质**：会崩的（KeyError）必须抢先容错；
只静默退化的（`Array.isArray()` 退化成空 map、金价线整条消失但 pageerror 为空）——
先补断言再改，把「补断言」和「改读取点」放同一 commit。

### 其他既定
图表绝不用双 Y 轴；颜色编码含义不编码顺序；持仓量单色深浅，红绿只留给变化量正负。
回测和 AI 策略推荐明确推迟（数据不足）。
本地 Python 走 WSL：`wsl -d Ubuntu-22.04 -- bash -c "cd /mnt/d/VScode/test/gold-dashboard && python3 ..."`

---

## 3. 进度

### 已完成并推送
- 第①件信封格式 ✅、第②件 KPI 算术下沉 derive ✅、KPI 全精度落盘 ✅
- P0 三源防伪闸 ✅（cot/gold/stocks 各 5 条判据 + quarantine + 三态 exit）
- `roll_noise`/`roll_noise_ma` 全精度 ✅
- P1 第 0 步 total_oi B2 口径 ✅（`c123981`）
- P1 第 1 步 stocks 接 io_utils + 信封化 ✅（`4f4834e`）
- verify-isolation 补断言累加器（0 → 37 条真断言）✅（`63e28c5`）
- P1 第 2 步 cot 双形状 + 写入端切换 ✅（`b069a9c` / `9e4af97` / `eab5833`）
- **`3382585`**：`unreliable_chg` 改走 `oi_map`（拆掉 contracts 存续筛）
- **`bf1cc6b`**：取证路径彻底去窗口（`oi_maps_raw`）+ WINDOW fixture + CLAUDE.md 二分法
- **`f8f5204`**：技术侧交接文档 `docs/handover-technical.md`
- **`779f480`**：文档更新（float 判别结论 + 「已知会随数据滑动而消失的红」一节）
- **`a31cdcb`**：落盘 float 统一 round(12) + ROUND fixture + CLAUDE.md
- **`44aecf2`**：`contracts` 改为全序列 `window_months` 并集口径 + 两条前端断言反转
- **`3719857`**：MISMATCH 断言比对范围收到该帧 `window_months`
- **`3995959`**：CLAUDE.md 补执行侧陷阱 + 重验 ZZZ99 注入结论
- **`a8c733a`**：交接文档更新至 `3995959` + CLAUDE.md 补第四种执行侧形态
- 期间 Actions 提交 `36a5504`（每日数据，oi.json 增至 25 帧，范围 06-26 ~ 07-31）

**当前 HEAD = `a8c733a`，local=remote，工作区干净。**
派生规模 25 帧 × 15 列。`derive --test` **exit 0，17 PASS / 0 failed / 无 SKIP**。
`oi_chg` 口径线已完结（详见第 4 节）。

### P1 第 3 步 gold（进行中，未完）
- commit1 exit 三态分离 + workflow yml ✅（`3604e16`，已随 `3382585` 推送）
- **commit2、commit3 未开始**（详见待办）

---

## 4. `oi_chg` / `contracts` 口径 —— 已完结（决策记录）

> 此节保留为决策记录，**不再是待办**。保留理由：结论的论据不易重建，
> 且将来若有人想把 `contracts` 改回交集，需要看到为什么不能。

### 当时的问题
`contracts`（列位，按最后一帧存续）与 `window_months`（取值，near+1 年截窗）
两道筛并存且集合不同，导致两类错位：
- **有列无值** 24 格：AUG27/JUL27 等远端月，占该帧主力月 OI 的 0.0017%~0.2196%（噪声）
- **有值无列** —— 更严重：到期合约整列从全序列历史消失。
  实测 06-29 掉 JUN26、07-30 掉 JUL26，这两列在仍存续的 23 帧上有真实 settle/oi 却无列可放。

### 决定性证据：AUG26 到期模拟
模拟 AUG26 退市后重跑 `contracts` 推导：AUG26 **在全部 25 帧失去列位**，
其中 19 帧它是 `front`、24 帧是 `roll_from`。
OI 从 272,518 → 2,908 的整个流出过程无处呈现，回放只剩承接端 DEC26 的上升——
**看得见承接、看不见流出**，而移仓回放是本看板的核心功能。AUG26 即将到期，是硬时限。

### 三个方向与选择
- **A（取值服从 contracts）**：只填「有列无值」的远端月噪声，不解决到期侵蚀。淘汰。
- **B（contracts 逐帧变）**：数据正确，但 `_initCharts` 只在 `js/playback.js:100`
  调一次、`_renderFrame` 全函数无 `data.labels` 赋值 → **必须动前端约 20 个读取点**，
  且回放时轴会滑动。淘汰。
- **C（contracts = 全序列各帧 window_months 的并集）** ← **已采用**
  - `roll_noise`/`roll_noise_ma` 与 B **逐帧数值完全相同**（|Δ|max 9.59e-02，22 帧）
    → 证明 `contracts` 只决定展示列位、不参与派生计算，C 完全支配 B
  - 轴长度恒定 → 前端零改动
  - 代价：15 列（+JUN26/JUL26），空列比例 7.4% → 13.3%；
    730 帧外推约 48 列（日历月法，黄金近月逐月挂牌）或 71 列（实测速率法）。
    **日历月法更可信**，建议实施后按季度实测校正。

### roll_noise 漏掉的是信号不是噪声
B/C 多算进来的部分，取证证实：差异最大三帧（07-01 Δ=-0.0959、07-17、07-16）
新增计入的**全部是 JUL26 单一合约，100% 临近到期月，远端月 0 个**，
Δ 由分母变化单一解释。JUL26 是前一轮 JUN→AUG 移仓的到期端，其 OI 流出**就是移仓活动本身**。
→ 旧口径的 `roll_noise` 历史序列漏掉的是信号。**阈值分布必须在新口径下跑**，此前不得跑。
（另：窗口移动造成的台阶在新旧口径下都存在、落差变化 ≤4e-4，窗口移动本身不是污染源。）

### 连带的两处修正
1. **两条前端断言反转**（`verify-ui-fixes.mjs` / `verify-contract-contango.mjs`）：
   原断言「JUN26（已到期）已剔除」编码的正是被判定为错误的行为。
   **反转而非删除**——删了该路径零覆盖。已附注入证明。
2. **MISMATCH 断言比对范围收到该帧 `window_months`**：
   `contracts` 改并集后含从未进入该帧窗口的月份，那些格子 derive 根本没算、
   值是 None 代表「未计算」而非「算错了」。拿 `contracts` 当校验范围会把两者混淆。
   **同一个列表不得同时充当展示范围与校验范围。**
   收窄后加了 `if not checked` 空转保护（窗口内一条可对账都没有时报「抽查 2 自身失效」，
   防止收窄导致断言恒真）。双向注入已验：窗口内注入必红、窗口外注入按定义应绿且实测绿。

### 仍需知道的
- **覆盖率事实**：窗口 13 个月份、stored 共 27 条、实际对账 13 条，
  **14 条窗口外的 stored 从不参与任何校验**。这是定义的必然结果（derive 没算它们），
  不是缺陷；它们将来进入窗口时用差分自算而非 stored，不影响正确性。记录备查。
- **抽查 2 仍有滑窗 SKIP 风险**：帧滑出 730 条滚动窗口或成首帧时转 SKIP。
  730 帧约需两年，但届时口径正确性不再被任何断言检查。

---

## 5. 待办清单

### schema v0 envelope 迁移（C6）
- 受跟踪生产 JSON 已全部 envelope 化，生产 Python/前端读取路径完成 strict 收口。
- 首次不存在与已存在但 bare/损坏严格区分；前者保留原业务语义，后者明确失败。
- `schema_version` 继续为 0；本轮不代表 schema 已冻结，也不启动 v1。
- `term-3d.html` 已下线且无生产入口，刻意不纳入本轮；若未来重新启用，必须先迁移
  读取协议并补 verify。

### 护栏与取证的欠账（新增，勿忽略）
- **`tools/verify-noise-injection.py` 静默失效已由 C5 修复**：从 Windows PowerShell
  以 WSL Python 启动；脚本内部用 `sys.executable` 直跑目标测试并读取真实
  `returncode`，不再跨 WSL → Windows → shell。三种注入必须各自变红且命中预期
  NOISE 断言，基线/恢复必须绿，否则脚本自身 exit 1。备份在系统临时目录，
  `finally` 恢复并校验 SHA-256。
- **注入类 verify 之间的隔离已由 C10 完成**：当前六个 Node wrapper 每 case 从运行前
  原始 bytes 重建，系统临时目录备份，并在 case/finally 校验 SHA-256；wrapper exit 0
  已包含恢复后目标 guard 再次全绿的证据。
- **抽查 2 的滑窗 SKIP**：见第 4 节末，约两年后到期。
- **`term-3d.html`**：无 verify 覆盖 + 真实渲染未验。

### 独立小项
- **derive 无幂等跳过**：落盘路径无条件写，未导入 `read_json_or`，每跑必刷 `generated_at`。
  采集层（cot/gold/stocks）都有幂等，唯独派生层没有，分层是反的。
  目前只表现为 git 噪声（其 `generated_at` 不进页面显示）。
  **前置依赖 round(12) 经 Actions 验证**，否则跨平台比对恒判「数据变了」，幂等等于白做。
- **float 平台差异 —— 已定 round(12)，`a31cdcb` 已实施，待 Actions 验证**：
  Actions 提交 `36a5504` 曾把 3 处 `roll_noise_ma` 末位**全部翻回**，Δ 均为 1 ULP，
  差异帧恰好是记录的那 3 个、无第四处 → 判定为 runner 与本机 float 末位不一致的永久性 flapping。
  已实施：落盘统一 `_round_floats()` round(12)，**只在写盘那刻 round，计算链全程全精度**
  （`roll_noise_ma` 的 3 帧滚动用未 round 的 `roll_noise` 算）。
  `front_remaining` 有意 round(4) 保持不动；int/bool/None/str 原样。
  一次性 diff 94 处（Δ 均 1e-13 量级），非 float 变化 0 处，落盘后无 >12 位小数。
  意外收益：顺带消掉了 `spread` 的本机浮点减法残差（`59.90000000000009 → 59.9`），
  那不是平台差异，是一直在污染落盘值的自身残差。
  新增 ROUND fixture 覆盖 `_round_floats()` 本身（新代码路径，
  `bool` 是 `int` 子类这类细节错了不会有任何护栏发现）。

  **⚠ 真正的验证尚未发生**：本地跑不出平台差异。
  **下次 Actions 跑完，判据是那 3 处 `roll_noise_ma` 不再被翻回。**
  若仍被翻回，说明 12 位不够（第 13 位在舍入边界），要再议。
  **禁止用容差比对代替**：容差会让真实微小变化也被判为没变，而 CFTC/CME 的历史修订
  可能只差几个单位。
  余量备注：NOISE_FIXTURE 当前误差 8.23e-14 vs 容差 1e-12，余一个数量级；
  若将来把 round 收紧到 10 位，这条 fixture 会踩线失效。
- **COT Index 前端重算已于 C11 删除**：三个真实 route fixture 与 C10 状态机 wrapper
  分别锁住 current null 和 chart null，任何 `null → 50` 恢复都会使 guard 变红。
- **「页面更新」文案覆盖不全**：时间戳源自 `cot.json` 的 `generated_at`（读取点 `:1252`），
  只代表 COT 一源，但页面还有金价、库存、期限结构。
  若 stocks 因 WAF 封锁 exit 2 停在旧数据而 COT 正常更新，用户会以为全页都是新的。
  不是撒谎，是文案比实际范围大。最省事改文案（「COT 更新」），彻底做法是每块各显示各的。
- **`total_oi`/`ever_front` 追溯性未定义**：历史帧 T 的 `total_oi` 用的是
  「截至 T 的 ever_front」还是「截至今天的 ever_front」？
  若是后者，FEB27 将来坐正时历史帧数值会回头改变——与 JUL26 同族的历史篡改，方向相反。
  现在 `ever_front = ['AUG26','DEC26']` 都在窗口深处所以不表现。**不表现不等于没有。**
  是定义问题不是 bug，等有事逼它表现再谈。
- **`roll_noise` 阈值 —— 已解锁**：口径已改为并集，旧口径漏掉的移仓信号已补回，
  分布可以跑了。round(12) 对分布无影响（1e-13 vs 真实值 0.079~0.48），不构成量化地板。
  仍建议等 AUG26 彻底到期后再跑，届时分布才有谷可锚。
- **`verify-ui-fixes` 偶发超时**：批量连跑时 `waitForFunction` 30s 超时 exit=1，
  单独重跑 13 passed。归因并发争用。与已知的 verify-gapframe 400ms 帧竞态是两回事，
  别让它变成「一直都这样」。

### P1 之后
四象限信号散点图（quad_x/quad_y，换月当天用新主力自身前后日结算价、**禁跨合约相减**）；
接 FRED / SPDR ETF / 上海黄金 / COMEX 期权。

### 已知技术债
`js/*.js` 共享全局作用域 + 加载顺序依赖（后续改 ES modules）。

---

## 6. 关键信息

**路径**：Windows `D:\VScode\test\gold-dashboard`，WSL `/mnt/d/VScode/test/gold-dashboard`。
GitHub：`vincentyhd1-jpg/gold-dashboard`。

**数据锚点**：主力 DEC26；AUG26 移仓中（7/28 剩 71,430 手）；移仓路径 AUG→DEC（跳过流动性差的 OCT）；
`ever_front = ['AUG26','DEC26']`，`major_months` 多一个 FEB27（末端补位）。
窗口末端移动实测：06-29（JUN27→JUL27）、07-30（JUL27→AUG27）。

**数据源**：COT 来自 CFTC Socrata API（每周五约 15:30 ET 发布上周二数据）；
库存来自 CME Section 62 PDF（有 WAF 封锁风险 → exit 2）；金价 Stooq/Yahoo 级联，日期完全跟随 cot。

**CLAUDE.md** 是项目记忆，含所有既定约定 + WSL 取证禁忌 + 各类 TODO。

---

## 7. 给 Claude Code 下指令的方式

- 只给动作 + 验收标准，不重复讲 CLAUDE.md 里已有的背景
- 结尾加「只贴 diff / 只贴验证输出，不解释」
- 指路不让它找；给假设让它验证、不让它从零排查
- 每件事分阶段：**先出计划 → 我审 → 再动手 → 做完停下不自动进下一件**
- 守住范围，不许「顺便优化」
- 量化类任务要**预设停止线**（如「若超现有 10 倍则停下报告，不许顺手加守卫」）
- 验收项里明确写「预期仍红哪一条、属哪个已知问题、不许改断言迁就、
  除它以外不得有任何其他 FAIL」——既防它凑绿，也防它拿已知红当借口掩盖新 FAIL
- 少用截图验证（贵且不如数字准），让它文字报 PASS/FAIL + 正确 exit code

**省 token**：
- 禁止 `cat` 整个 `oi.json` / `term-structure-series.json`，用脚本提取后打印摘要
- 逐帧输出改为「只输出有差异的帧 + 汇总统计」
- 不要复述文件内容、不要贴未改动的周边代码
- 一件事做完就 `/clear`（有本文档 + CLAUDE.md 作外部记忆，切了不亏）
- 长验证输出只报汇总行，失败时才贴详情

---

## 8. 风格偏好

- **中文回答，精简，不要开场白和铺垫。**

## C18C 财政风险监测长期决策

- C18C 是 descriptive multi-indicator monitor，不是 risk score；不定义综合分数、
  概率、危机/违约/失控年份、交通灯或动态风险颜色。
- 历史结构只消费 C17 已派生指标，前端不重算 Fiscal Gap、r-g、同比或债务率。
- 同比必须按同季度上一年键匹配；缺少合法对照时为 null，禁止 index-4 猜测、补 0、
  forward-fill 或插值。
- Fiscal Gap=0 与 r-g=0 仅是数学符号边界；债务率和净利息/收入不增加人为阈值。
- DGS2/10/30 是各自日期的市场背景，绝不替代 TTM effective r，也不进入 forward
  debt dynamics。CBO 上下文只读官方 FY2026/FY2036 字段，不由金额重算。
- C17 历史、市场 rates、CBO baseline、C18B scenario basis 的来源和截至时间分离；
  任一上下文失败不能隐藏其他来源，C18B sliders 不能改变 C18C。
- committed derived freshness 与 dist freshness 都必须比较当前 C17 source；新 source
  配旧 monitor 必须在测试或部署闸门变红。
- C18C 每日生产测试只锁算法与映射，不锁某个季度、当前符号或固定 lag。latest
  observed/complete/lag 必须从当期 source 动态推导；页面 condition 按派生枚举映射，
  hover 按 `latest_complete_quarter` 找 label。rolling 与反向 condition fixture 防止测试
  重新耦合某日生产快照。

## C18C.1 Treasury 图表增强长期决策

- 全球黄金总市值是 World Gold Council end-2025 地上存量 220,700 公吨乘当期周频
  USD/oz 的透明估值，不声称是每日库存普查；存量 vintage 与公式必须随输出披露。
- 周频 USD/oz 输入是 dashboard valuation proxy，不是固定的 canonical spot 声明。
  派生必须从 upstream `price_source=` 解析并透传实际 source/instrument/proxy flag；
  Yahoo `GC=F` 必须明确为 COMEX gold futures，Stooq `XAUUSD` 才显示相应 XAUUSD
  代理。无法识别时失败，页面不得写死数据商，source metadata 变化也属于 freshness。
- 美债对比只使用 Treasury `Total Public Debt Outstanding`，并只在黄金观测日存在
  精确同日值时配对。任一源缺口只让对应字段保持 null，不隐藏同日另一真实字段；
  禁止最近值、forward-fill、插值或换用公众持有债务。
- 历史 UST 仍以 FRED 为事实源；vendored Hammer/zoom plugin 保证缩放依赖不受 CDN
  抖动影响。hybrid input 用 `any-pointer`/`any-hover` 判断外接鼠标；框选按可见真实
  观测自适应 Y，绘图区双击与按钮必须共同复原 X/Y。dblclick hit-test 必须通过
  Chart.js 标准 relative-position helper 进入 chart 坐标系，禁止直接假设 CSS pixel、
  DPR 与 `chartArea` 同尺度；touch-only 关闭 drag/dblclick。
- TradingView Advanced Widget 的真实第三方探测确认 `TVC` Treasury yields 及
  `CBOT:ZT1!`/`ZN1!`/`ZB1!` 均受限。production 不请求或隐藏这些 symbol，只展示
  unavailable 卡；没有 licensed market-data API 时不得继续换随机 symbol 或将 FRED
  日频冒充 intraday。
- TradingView 不需要项目 API key，不落盘、不进入 derived JSON、不替代 FRED，也不
  进入 C17 effective r、Fiscal Gap、C18B scenario 或 C18C monitor。第三方/CDN 失败
  只降级 Live card，属于独立外部资源边界。
- committed comparison 与 dist 必须耦合当前 gold/debt source；新源配旧派生必须在
  derive 或 static 闸门变红。页面/公式/口径/fallback 的关键保护由真实 injection 证明。
- 当前黄金历史估值是固定 end-2025 220,700 t 存量的 valuation proxy，不是历史 stock
  reconstruction。Historical Global Gold Valuation v2 作为独立 TODO，须先取得官方
  随年份变化的 above-ground stock；本阶段禁止插值历史存量。

## C18C.3B 全球官方储备构成长期决策

- macro 首卡改为“全球官方储备构成：黄金 vs 外国官方机构持有美债”单图双轴四线；
  旧 `gold_vs_debt` 管线保留但不再作为页面产品展示，避免删除历史依赖与扩大范围。
- 两条比例线只能共用 `Total Official Reserve Assets`。当前分母是 WGC Central Bank
  Dashboard 的 `Total reserves` 报告国合计，按 WGC 方法学为 IMF IFS-compatible、
  含黄金口径；不得换成 COFER allocated FX、COFER USD share 或另一独立分母。
- 官方黄金金额直接采用 WGC `Gold reserves (US$ Millions)` 的季末市场价值；不得
  使用全球全部地上黄金总市值，也不得把央行账面成本当市场价值。
- 美债金额严格为 TIC/FRED `FORTREASPOS99990` Foreign Official U.S. Treasury
  Holdings。正式中文名固定为“外国官方机构持有美债额”；不能改成美元储备、外国
  政府持有、所有外国持有或全球央行持有。TIC foreign official 范围需持续披露。
- 目标频率固定季度。FRED 月频只取 3/6/9/12 月实际值，以 `YYYY-Qn` 与 WGC 对齐；
  publication date、nearest date、forward-fill、插值和复制日/月值均禁止。
- WGC 最新季度可能存在大量 awaited 国别值；production 只接受三个核心指标各至少
  90 个报告经济体的季度。覆盖国随 vintage 变化是来源限制，必须在 metadata/方法学
  中披露，不能用 0 或旧值补齐。
- 页面双轴只用于同一经济问题的比例与金额视图：左轴 `% of Total Official Reserve
  Assets`，右轴 `USD tn`。前端只做 USD→tn 显示缩放，不重算 share 或分母。
