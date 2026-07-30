// contango.js — 价差指标卡：年化率计算所需的日历天数差
// 从 index.html 拆出，纯搬运，逻辑未改动。
// 依赖全局 Chart（CDN）与同页其他模块的全局函数，加载顺序见 index.html。

// 两个交割月之间的日历天数差。年化价差需要真实天数，不能按「月数 × 30」
// 估算 —— AUG26→DEC26 是 122 天，按 4×30=120 算会让年化率偏高 1.7%。
// 以每月 1 日为基准：合约实际到期日在月中，但两端同样偏移，差值不受影响。
const _MON_NUM = { JAN:1, FEB:2, MAR:3, APR:4, MAY:5, JUN:6,
                   JUL:7, AUG:8, SEP:9, OCT:10, NOV:11, DEC:12 };
function _monthGapDays(a, b) {
  const parse = s => {
    const m = /^([A-Z]{3})(\d{2})$/.exec(s || '');
    return m ? Date.UTC(2000 + +m[2], _MON_NUM[m[1]] - 1, 1) : null;
  };
  const ta = parse(a), tb = parse(b);
  if (ta == null || tb == null) return 0;
  return Math.round((tb - ta) / 86400000);
}
