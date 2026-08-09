// playback.js — 播放条：播放控制、滑块、倍速、键盘步进
// 从 index.html 拆出，纯搬运，逻辑未改动。
// 依赖全局 Chart（CDN）与同页其他模块的全局函数，加载顺序见 index.html。

// ── 播放控制 ──────────────────────────────────────────────────────────────────
function _playTick() {
  if (!_play.playing) return;
  const { series } = _play;
  if (_play.frameIdx >= series.frames.length - 1) {
    _play.playing = false;
    document.getElementById('oiPlayBtn').innerHTML = '&#9654;';
    return;
  }
  _play.frameIdx++;
  document.getElementById('oiPlaySlider').value = _play.frameIdx;
  _renderFrame(_play.frameIdx, true);
  _play.timer = setTimeout(_playTick, 400 / _play.speed);
}

function _oiPlay() {
  if (_play.playing) {
    _play.playing = false;
    clearTimeout(_play.timer);
    document.getElementById('oiPlayBtn').innerHTML = '&#9654;';
  } else {
    if (_play.frameIdx >= (_play.series?.frames.length ?? 1) - 1) {
      _play.frameIdx = 0;
      document.getElementById('oiPlaySlider').value = 0;
    }
    _play.playing = true;
    document.getElementById('oiPlayBtn').innerHTML = '&#9646;&#9646;';
    _playTick();
  }
}

// ── 初始化播放条控件 ────────────────────────────────────────────────────────
function _initPlaybarControls(series) {
  const slider = document.getElementById('oiPlaySlider');
  slider.max = series.frames.length - 1;
  slider.value = series.frames.length - 1;

  slider.addEventListener('input', () => {
    clearTimeout(_play.timer);
    _play.playing = false;
    document.getElementById('oiPlayBtn').innerHTML = '&#9654;';
    _play.frameIdx = parseInt(slider.value);
    _renderFrame(_play.frameIdx, false);
  });

  document.getElementById('oiPlayBtn').addEventListener('click', _oiPlay);

  const speedBtns = document.querySelectorAll('.oi-speed-btns button');
  speedBtns.forEach(btn => {
    btn.addEventListener('click', () => {
      _play.speed = parseFloat(btn.dataset.speed);
      speedBtns.forEach(b => b.setAttribute('aria-pressed', String(b === btn)));
    });
  });

  // 键盘左右方向键逐帧步进
  document.addEventListener('keydown', e => {
    if (_oiViewMode !== 'snapshot') return;
    if (e.target.tagName === 'INPUT') return;
    if (e.key === 'ArrowRight' || e.key === 'ArrowLeft') {
      e.preventDefault();
      clearTimeout(_play.timer);
      _play.playing = false;
      document.getElementById('oiPlayBtn').innerHTML = '&#9654;';
      const dir = e.key === 'ArrowRight' ? 1 : -1;
      _play.frameIdx = Math.max(0, Math.min(
        (_play.series?.frames.length ?? 1) - 1,
        _play.frameIdx + dir
      ));
      slider.value = _play.frameIdx;
      _renderFrame(_play.frameIdx, false);
    }
    if (e.key === ' ') {
      e.preventDefault();
      _oiPlay();
    }
  });

  // 默认停在最新帧，reduced-motion 禁用自动播放
  _play.frameIdx = series.frames.length - 1;
  _renderFrame(_play.frameIdx, false);
  // Chart.js:建图后第一次 update('none') 不会让 line 元素从 null 初值落位
  // （bar 不受影响）;需再补一次 update('none') 才设终态。零动画代价。
  // 必须延后到初始 responsive resize 之后 —— 同步补会被 resize 重置回基线
  // （实测 earliest=193,而 resize 后任何一次 'none' 都永久生效）。
  // 这是 workaround 非根因修复。删除此行 → 结算价线首屏贴底,
  // 由 verify-ui-fixes [8]a 守。
  setTimeout(() => _oiChart.update('none'), 0);
}

// ── 公开入口 ──────────────────────────────────────────────────────────────────
function initOIPlayback(payload) {
  // term-structure-series.json 已信封化：取 data，裸格式抛错（与 index.html 四处一致）。
  const series = (payload && typeof payload === 'object' && 'data' in payload)
    ? payload.data
    : (() => { throw new Error('term-structure-series.json: 期望信封格式'); })();

  if (!series || !series.frames || !series.frames.length) return;
  _play.series = series;
  _initCharts(series);
  _initPlaybarControls(series);
}

// 保留旧 renderOI 作为降级（如果 series 文件 404 则用 oi.json 数据渲染最新帧）
function renderOI(oiData) {
  const records = Array.isArray(oiData) ? oiData : [];
  const latest = [...records].reverse().find(r => Array.isArray(r.months) && r.months.length);
  if (!latest || _play.series) return;  // series already loaded, skip

  // 构造单帧 series 用于降级渲染
  const allMonths = latest.months;
  const nearM = allMonths[0];
  const [nearMon, nearYr] = [nearM.month.slice(0,3), parseInt(nearM.month.slice(3))];
  const cutoffLabel = nearMon + (nearYr + 1);
  const cutIdx = allMonths.findIndex(r => r.month === cutoffLabel);
  const months = cutIdx >= 0 ? allMonths.slice(0, cutIdx + 1) : allMonths;

  const contracts = months.map(m => m.month);
  const maxOI = Math.max(...months.map(m => m.oi));
  const deltaAbsMax = Math.max(...months.map(m => Math.abs(m.oi_chg ?? 0)));
  const fallbackSeries = {
    dates: [latest.date],
    contracts,
    frames: [{
      date: latest.date,
      settle: months.map(m => m.settle),
      oi: months.map(m => m.oi),
      oi_chg: months.map(m => m.oi_chg ?? null),
      front: months.reduce((a,b) => b.oi > a.oi ? b : a, months[0]).month,
      // 价差卡只需要两个锚点，可以从单个快照近似：到期月取日历序上第一个
      // 持仓过 5% 的月，承接月取其后持仓最大的月。
      // KPI 算术已下沉到 derive，降级路径只能就地补上 —— 这里没有派生层。
      ...(() => {
        const tot = months.reduce((s,m) => s + (m.oi||0), 0);
        const from = months.find(m => (m.oi||0) >= tot * 0.05) || months[0];
        const after = months.filter(m => m.month !== from.month
          && months.indexOf(m) > months.indexOf(from));
        const to = after.length
          ? after.reduce((a,b) => b.oi > a.oi ? b : a) : null;

        // 与 derive 的 month_gap_days 同口径：以每月 1 日为基准算日历天数差，
        // 不用「月数 × 30」（AUG26→DEC26 是 122 天，4×30=120 会让年化偏高 1.7%）
        const MON = { JAN:1,FEB:2,MAR:3,APR:4,MAY:5,JUN:6,
                      JUL:7,AUG:8,SEP:9,OCT:10,NOV:11,DEC:12 };
        const ts = s => {
          const m = /^([A-Z]{3})(\d{2})$/.exec(s || '');
          return m ? Date.UTC(2000 + +m[2], MON[m[1]] - 1, 1) : null;
        };
        let spread = null, gapDays = null, ann = null;
        if (to && from.settle > 0 && to.settle != null) {
          spread = Math.round((to.settle - from.settle) * 1e4) / 1e4;
          const ta = ts(from.month), tb = ts(to.month);
          gapDays = (ta != null && tb != null)
            ? Math.round((tb - ta) / 86400000) : 0;
          if (gapDays > 0) {
            ann = Math.round(spread / from.settle * (365 / gapDays) * 100 * 100) / 100;
          }
        }
        return {
          roll_from: from.month,
          roll_to: to ? to.month : null,
          total_oi: months.reduce((s,m) =>
            s + (m.settle != null ? (m.oi || 0) : 0), 0),
          spread, spread_gap_days: gapDays, spread_annualized_pct: ann,
        };
      })(),
      // 单帧无历史峰值可比，近月剩余与噪音比都算不出来
      front_remaining: null, roll_noise: null, roll_noise_ma: null,
      in_roll_window: false, unreliable_chg: null
    }],
    scale: {
      oi_max: maxOI,
      delta_abs_max: deltaAbsMax,
      settle_min: Math.min(...months.map(m => m.settle)),
      settle_max: Math.max(...months.map(m => m.settle)),
    }
  };
  initOIPlayback(fallbackSeries);
}
