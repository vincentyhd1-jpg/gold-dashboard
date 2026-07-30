// term-structure.js — 主图（OI 柱 + 结算价线）与 delta 面板
// 从 index.html 拆出，纯搬运，逻辑未改动。
// 依赖全局 Chart（CDN）与同页其他模块的全局函数，加载顺序见 index.html。

// ── COMEX GC 期限结构图 + 回放引擎 ──────────────────────────────────────────
let _oiChart      = null;
let _oiDeltaChart = null;
let _oiRollChart  = null;

// 回放状态
const _play = {
  series:    null,   // term-structure-series.json payload
  frameIdx:  0,
  playing:   false,
  speed:     1,
  timer:     null,
  reducedMotion: window.matchMedia('(prefers-reduced-motion: reduce)').matches,
};


// 持仓量按数值大小映射蓝紫色深浅（深色主题）
function _oiBarColor(oi, maxOI, isFront) {
  const t = maxOI > 0 ? oi / maxOI : 0;
  // 亮度下限 38% 确保小持仓月份在深色背景仍可见
  const L = isFront ? 65 : Math.round(38 + t * 22);
  const alpha = isFront ? 0.90 : (0.55 + t * 0.35).toFixed(2);
  return `hsla(220,70%,${L}%,${alpha})`;
}

// ── X 轴对齐同步（复用现有方案）─────────────────────────────────────────────
const _syncPadPlugin = {
  id: '_syncPad',
  afterLayout(chart) {
    window._oiChartArea = {
      left:  Math.round(chart.chartArea.left),
      right: Math.round(chart.width - chart.chartArea.right)
    };
    for (const dep of [_oiDeltaChart, _oiRollChart]) {
      if (dep && !dep._syncUpdating) {
        dep._syncUpdating = true;
        dep.update('none');
        dep._syncUpdating = false;
      }
    }
  }
};
const _syncPadDepPlugin = {
  id: '_syncPadDep',
  beforeLayout(chart) {
    const ca = window._oiChartArea;
    if (ca) chart.options.layout.padding.left = ca.left;
  }
};
function _afterFitRight(scale) {
  const ca = window._oiChartArea;
  if (ca) scale.width = ca.right;
}

// 微小持仓的最小柱高：非活跃月只有几十手，按比例算不足 0.1px，会整列消失。
// X 轴按合约存续状态过滤（不按持仓阈值），所以这些列必然存在，需要渲染成细线。
//
// 用 Chart.js 内置的 minBarLength，不自己改元素几何 —— 手改 el.y 会和动画
// 插值器抢同一批属性，动画期间被覆盖，看起来像完全没生效。
//
// 传 2.5 得到净高 2px：minBarLength 的实际几何是「传入值 − borderWidth/2」，
// 边框内缩半像素（实测 2→1.5px、3→2.5px、4→3.5px）。
const _MIN_BAR_PX = 2.5;

// ── 图表初始化（只建一次，之后只更新数据）────────────────────────────────────
function _initCharts(series) {
  const { contracts, scale } = series;
  const labels = contracts;

  // 销毁旧图
  if (_oiChart)      { _oiChart.destroy();      _oiChart = null; }
  if (_oiDeltaChart) { _oiDeltaChart.destroy(); _oiDeltaChart = null; }
  if (_oiRollChart)  { _oiRollChart.destroy();  _oiRollChart = null; }

  // ── 主图 ────────────────────────────────────────────────────────────────
  const mainCanvas = document.getElementById('oiChart');
  _oiChart = new Chart(mainCanvas, {
    type: 'bar',
    plugins: [_syncPadPlugin],
    data: {
      labels,
      datasets: [
        // dataset 0: current OI bars
        { type:'bar', label:'持仓 OI', data: new Array(labels.length).fill(null),
          backgroundColor: [], borderColor: [], borderWidth:1, borderRadius:3,
          yAxisID:'yOI', order:2, minBarLength:_MIN_BAR_PX },
        // dataset 1: price line
        { type:'line', label:'结算价', data: new Array(labels.length).fill(null),
          borderColor:'#58a6ff', backgroundColor:'rgba(88,166,255,0.08)',
          borderWidth:2, pointRadius:4, pointHoverRadius:6,
          pointBackgroundColor:'#58a6ff', tension:0.3,
          yAxisID:'yPrice', order:1 },
      ]
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      animation: { duration: 300, easing: 'easeInOutQuad' },
      interaction: { mode:'index', intersect:false },
      plugins: {
        legend: { display: false },
        tooltip: {
          confine: true,
          callbacks: {
            label: ctx => {
              if (ctx.dataset.label === '结算价')
                return '  结算价: $' + ctx.parsed.y.toFixed(2);
              if (!ctx.dataset.label.startsWith('持仓')) return null;
              const fr = series.frames[_play.frameIdx];
              const ci = ctx.dataIndex;
              const oi = fr.oi[ci];
              const chg = fr.oi_chg[ci];
              const chgStr = chg == null ? '' :
                ('  持仓变化: ' + (chg >= 0 ? '+' : '') + chg.toLocaleString('en-US'));
              return ['  持仓 OI: ' + (oi ?? 0).toLocaleString('en-US'), chgStr].filter(Boolean);
            },
            labelColor: ctx => {
              if (ctx.dataset.label !== '持仓 OI') return undefined;
              const c = _oiBarColor(
                series.frames[_play.frameIdx].oi[ctx.dataIndex] ?? 0,
                scale.oi_max, false
              );
              return { borderColor: c, backgroundColor: c };
            }
          }
        }
      },
      scales: {
        x: {
          ticks: { font:{size:11}, color:'#8b949e', maxRotation:45 },
          grid: { color:'#21262d' }
        },
        yPrice: {
          position: 'left',
          min: scale.settle_min * 0.995,
          max: scale.settle_max * 1.005,
          ticks: { font:{size:10}, color:'#58a6ff', callback: v => '$'+v.toFixed(0) },
          grid: { color:'#21262d' },
          title: { display:true, text:'结算价 (USD)', color:'#58a6ff', font:{size:10} }
        },
        yOI: {
          position: 'right',
          min: 0,
          max: scale.oi_max * 1.25,
          ticks: { font:{size:10}, color:'#8b949e',
            callback: v => v >= 1000 ? Math.round(v/1000)+'k' : v },
          grid: { drawOnChartArea: false },
          title: { display:true, text:'持仓 (手)', color:'#8b949e', font:{size:10} }
        }
      }
    }
  });

  // ── Delta 面板 ──────────────────────────────────────────────────────────
  const deltaCanvas = document.getElementById('oiDeltaChart');
  _oiDeltaChart = new Chart(deltaCanvas, {
    type: 'bar',
    plugins: [_syncPadDepPlugin],
    data: {
      labels,
      datasets: [{ label:'持仓变化', data: new Array(labels.length).fill(0),
        backgroundColor: [], borderColor: [], borderWidth:1, borderRadius:2,
        borderSkipped:false, minBarLength:_MIN_BAR_PX }]
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      animation: { duration: 300, easing: 'easeInOutQuad' },
      layout: { padding: { left:0, right:0, top:0, bottom:0 } },
      interaction: { mode:'index', intersect:false },
      plugins: {
        legend: { display:false },
        tooltip: {
          confine: true,
          callbacks: {
            title: items => items[0]?.label || '',
            label: ctx => {
              const v = ctx.parsed.y;
              return '  持仓变化: ' + (v >= 0 ? '+' : '') + v.toLocaleString('en-US') + ' 手';
            }
          }
        }
      },
      scales: {
        x: { display:false, grid:{ color:'#21262d' } },
        y: {
          position: 'right',
          min: -(scale.delta_abs_max * 1.3 || 1),
          max:  (scale.delta_abs_max * 1.3 || 1),
          ticks: { font:{size:9}, color:'#6e7681', maxTicksLimit:3,
            callback: v => Math.abs(v) >= 1000 ? (v/1000).toFixed(0)+'k' : v },
          grid: { color: ctx => ctx.tick.value === 0 ? '#444c56' : '#21262d' },
          afterFit: _afterFitRight
        }
      }
    }
  });

  // ── 移仓进度面板 ────────────────────────────────────────────────────────
  const rollCanvas = document.getElementById('oiRollChart');
  const rollDates = series.dates.map(d => d.slice(5).replace('-', '/'));
  _oiRollChart = new Chart(rollCanvas, {
    type: 'line',
    plugins: [_syncPadDepPlugin, {
      id: '_rollCursor',
      afterDraw(chart) {
        const idx = _play.frameIdx;
        const meta = chart.getDatasetMeta(0);
        if (!meta.data[idx]) return;
        const x = meta.data[idx].x;
        const ctx2 = chart.ctx;
        const { top, bottom } = chart.chartArea;
        ctx2.save();
        ctx2.beginPath();
        ctx2.strokeStyle = '#58a6ff';
        ctx2.lineWidth = 1.5;
        ctx2.setLineDash([3, 3]);
        ctx2.moveTo(x, top);
        ctx2.lineTo(x, bottom);
        ctx2.stroke();
        ctx2.restore();
      }
    }],
    data: {
      labels: rollDates,
      datasets: [{
        // 画「近月剩余」= 到期月当前 OI / 其历史峰值 OI，从满到空（1→0）。
        // 不画旧的 roll_to/(roll_from+roll_to) —— 那是迁移占比，会随承接月
        // 换月跳变。这条线只是背景参考，不加交互与标记。
        label: '移仓进度',
        data: series.frames.map(f => f.front_remaining),
        borderColor: '#a78bfa',
        backgroundColor: 'rgba(167,139,250,0.12)',
        borderWidth: 1.5,
        pointRadius: 0,
        pointHoverRadius: 4,
        tension: 0.3,
        fill: true,
        spanGaps: true,
      }]
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      layout: { padding: { left:0, right:0, top:0, bottom:0 } },
      interaction: { mode:'index', intersect:false },
      plugins: {
        legend: { display:false },
        tooltip: {
          confine: true,
          callbacks: {
            title: items => items[0]?.label || '',
            label: ctx => {
              const v = ctx.parsed.y;
              return v == null ? '  近月剩余: --'
                : '  近月剩余: ' + (v * 100).toFixed(1) + '%';
            }
          }
        }
      },
      scales: {
        // 这个面板横轴是日期，上面两个是交割月 —— 必须显示刻度，
        // 否则会被误读为与主图共享同一条横轴
        x: {
          display: true,
          ticks: { font:{size:9}, color:'#6e7681', maxTicksLimit:8,
            maxRotation:0, autoSkipPadding:12 },
          grid: { color:'#1b2027' },
          border: { color:'#30363d' }
        },
        y: {
          position: 'right',
          min: 0, max: 1,
          ticks: { font:{size:9}, color:'#6e7681', maxTicksLimit:3,
            callback: v => Math.round(v*100)+'%' },
          grid: { color:'#21262d' },
          title: { display:true, text:'移仓', color:'#6e7681', font:{size:9} },
          afterFit: _afterFitRight
        }
      }
    }
  });
}


// ── 帧渲染（不重建图表）──────────────────────────────────────────────────────
function _renderFrame(frameIdx, animated) {
  if (!_play.series) return;
  const { series } = _play;
  const { frames, contracts, scale } = series;
  const fr = frames[frameIdx];
  const maxOI = scale.oi_max;

  // 找主力月索引
  const frontIdx = fr.front ? contracts.indexOf(fr.front) : -1;

  // 当前帧 OI bars (dataset index 0)
  const mainDs = _oiChart.data.datasets[0];
  mainDs.data = fr.oi.map(v => v ?? null);
  mainDs.backgroundColor = contracts.map((_, ci) => {
    const oi = fr.oi[ci] ?? 0;
    return _oiBarColor(oi, maxOI, ci === frontIdx);
  });
  mainDs.borderColor = mainDs.backgroundColor.map(c => c.replace(/,[^,]+\)$/, ',1)'));

  // price line (dataset index 1)
  _oiChart.data.datasets[1].data = fr.settle.map(v => v ?? null);

  // ── Delta 面板 ────────────────────────────────────────────────────────
  const deltaDs = _oiDeltaChart.data.datasets[0];
  const absMax = scale.delta_abs_max || 1;
  const threshold = absMax * 0.005;
  const allNull = fr.oi_chg.every(v => v == null);
  if (allNull) {
    deltaDs.data = new Array(contracts.length).fill(0);
    deltaDs.backgroundColor = new Array(contracts.length).fill('transparent');
    deltaDs.borderColor     = new Array(contracts.length).fill('transparent');
  } else {
    deltaDs.data = fr.oi_chg.map(v => v ?? 0);
    deltaDs.backgroundColor = fr.oi_chg.map(v => {
      if (v == null || Math.abs(v) < threshold) return 'transparent';
      return v > 0 ? 'rgba(88,166,255,0.65)' : 'rgba(248,81,73,0.65)';
    });
    deltaDs.borderColor = fr.oi_chg.map(v => {
      if (v == null || Math.abs(v) < threshold) return 'transparent';
      return v > 0 ? '#58a6ff' : '#f85149';
    });
  }

  // ── 更新所有图表 ────────────────────────────────────────────────────────
  const mode = animated && !_play.reducedMotion ? undefined : 'none';
  _oiChart.update(mode);
  _oiDeltaChart.update(mode);
  _oiRollChart.update('none');  // roll chart only redraws cursor, no data change

  // ── KPI 区域 ──────────────────────────────────────────────────────────
  const months = contracts
    .map((label, ci) => ({ month: label, settle: fr.settle[ci], oi: fr.oi[ci] ?? 0 }))
    .filter(m => m.settle != null);
  if (!months.length) return;

  const frontMonth = months.find(m => m.month === fr.front) || months[0];
  const totalOI  = months.reduce((s, m) => s + m.oi, 0);

  // 价差锚点显式绑定合约角色，不用 months[0] / months[last]：
  // 首列正在到期、末列只有几手，两端都是交易所推定价，算出来的是结算程序
  // 而不是市场。roll_from = front_by_expiry，roll_to = next_active。
  const nearM = months.find(m => m.month === fr.roll_from);
  const farM  = months.find(m => m.month === fr.roll_to);
  const ctgEl = document.getElementById('oiContango');
  const ctgLabelEl = document.getElementById('oiContangoLabel');
  if (nearM && farM && nearM.settle > 0) {
    const spread = farM.settle - nearM.settle;
    const days = _monthGapDays(nearM.month, farM.month);
    const ann = days > 0 ? spread / nearM.settle * (365 / days) * 100 : null;
    ctgEl.textContent = ann == null ? '--'
      : (ann >= 0 ? '+' : '') + ann.toFixed(2) + '%';
    ctgEl.style.color = spread >= 0 ? '#3fb950' : '#f85149';
    ctgLabelEl.textContent =
      `${farM.month} − ${nearM.month}：${spread >= 0 ? '+' : ''}${spread.toFixed(2)}`;
  } else {
    ctgEl.textContent = '--';
    ctgEl.style.color = '#8b949e';
    ctgLabelEl.textContent = '无活跃次月';
  }

  document.getElementById('oiDate').textContent =
    '数据日期：' + fr.date.slice(5).replace('-', '/') + ' · 来源：CME Group';
  document.getElementById('oiFrontPrice').textContent = frontMonth.settle.toFixed(2);
  document.getElementById('oiFrontMonth').textContent = frontMonth.month + ' · 主力月';
  document.getElementById('oiVal').textContent = totalOI.toLocaleString('en-US');

  // 播放条日期
  document.getElementById('oiPlayDate').textContent = fr.date.slice(5).replace('-', '/');
}
