// 破坏注入：KPI 字段下沉后，verify-contract-contango 是否仍会变红？
// 它反算年化率并与卡片显示比对。若前端改成读字段后该断言失效，
// 注入错值就会静默通过 —— 那正是 CLAUDE.md 记的那类失效。
import { spawn } from 'child_process';
import fs from 'fs';

const SERIES = 'data/derived/term-structure-series.json';
const BACKUP = 'series.injection-backup.json';

function run(script) {
  return new Promise(resolve => {
    const p = spawn('node', ['tools/' + script], { shell: false });
    let out = '';
    p.stdout.on('data', d => out += d);
    p.stderr.on('data', d => out += d);
    p.on('close', code => resolve({ code, out }));
  });
}

fs.copyFileSync(SERIES, BACKUP);

const CASES = [
  ['年化率改错值 (+99.99%)', s => {
    for (const f of s.data.frames) {
      if (f.spread_annualized_pct != null) f.spread_annualized_pct = 99.99;
    }
    return s;
  }],
  ['年化率置 null', s => {
    for (const f of s.data.frames) f.spread_annualized_pct = null;
    return s;
  }],
  ['spread 改错值 (999.9)', s => {
    for (const f of s.data.frames) {
      if (f.spread != null) f.spread = 999.9;
    }
    return s;
  }],
  ['total_oi 改错值 (1)', s => {
    for (const f of s.data.frames) f.total_oi = 1;
    return s;
  }],
];

try {
  console.log('=== 基线（未注入）===');
  const base = await run('verify-contract-contango.mjs');
  console.log(`  verify-contract-contango  exit=${base.code}  ${base.code === 0 ? '绿' : '红'}`);

  for (const [label, patch] of CASES) {
    const orig = JSON.parse(fs.readFileSync(BACKUP, 'utf8'));
    fs.writeFileSync(SERIES, JSON.stringify(patch(orig)));

    const r = await run('verify-contract-contango.mjs');
    const failed = r.code !== 0;
    console.log(`\n=== 注入：${label} ===`);
    console.log(`  exit=${r.code}  ${failed ? '变红 ✓ 护栏有效' : '仍绿 ✗ 静默通过'}`);
    if (failed) {
      const lines = r.out.split('\n').filter(l => /FAIL/.test(l)).slice(0, 3);
      for (const l of lines) console.log('   ', l.trim().slice(0, 110));
    }
  }
} finally {
  fs.copyFileSync(BACKUP, SERIES);
  fs.unlinkSync(BACKUP);
  console.log('\n派生文件已恢复');
}
