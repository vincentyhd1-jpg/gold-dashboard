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
}

// ── 公开入口 ──────────────────────────────────────────────────────────────────
function initOIPlayback(series) {
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
      // 单帧算不出 roll_progress（需要跨帧的持仓迁移），但价差卡只需要两个
      // 锚点，可以从单个快照近似：到期月取日历序上第一个持仓过 5% 的月，
      // 承接月取其后持仓最大的月。
      ...(() => {
        const tot = months.reduce((s,m) => s + (m.oi||0), 0);
        const from = months.find(m => (m.oi||0) >= tot * 0.05) || months[0];
        const after = months.filter(m => m.month !== from.month
          && months.indexOf(m) > months.indexOf(from));
        const to = after.length
          ? after.reduce((a,b) => b.oi > a.oi ? b : a) : null;
        return { roll_from: from.month, roll_to: to ? to.month : null };
      })(),
      roll_progress: null,
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
