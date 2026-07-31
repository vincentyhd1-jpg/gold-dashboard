// 破坏注入：改 derive 落盘的 total_oi → verify-contract-contango 必须变红。
// 改回后必须恢复绿。
import { spawn } from 'child_process';
import fs from 'fs';

const SERIES = 'data/derived/term-structure-series.json';
const BACKUP = 'series.totaloi-injection-backup.json';

function run() {
  return new Promise(resolve => {
    const p = spawn('node', ['tools/verify-contract-contango.mjs'], { shell: false });
    let out = '';
    p.stdout.on('data', d => out += d);
    p.stderr.on('data', d => out += d);
    p.on('close', code => resolve({ code, out }));
  });
}

const show = (label, r) => {
  console.log(`=== ${label} ===`);
  console.log(`  exit=${r.code}  ${r.code === 0 ? '绿' : '红'}`);
  for (const l of r.out.split('\n')) {
    if (/FAIL|passed,/.test(l)) console.log('   ', l.trim().slice(0, 120));
  }
};

fs.copyFileSync(SERIES, BACKUP);
try {
  show('注入前（基线）', await run());

  const s = JSON.parse(fs.readFileSync(BACKUP, 'utf8'));
  const f = s.data.frames[s.data.frames.length - 1];
  const orig = f.total_oi;
  // 注入旧口径的值：这是最危险的回归形态 —— 退回「全部挂牌月求和」
  const oldCaliber = s.data.contracts
    .reduce((acc, c, i) => acc + (f.settle[i] != null ? (f.oi[i] || 0) : 0), 0);
  console.log(`\n注入：帧 ${f.date} 的 total_oi ${orig} → ${oldCaliber}（旧口径值）\n`);
  f.total_oi = oldCaliber;
  fs.writeFileSync(SERIES, JSON.stringify(s));

  show('注入后', await run());

  fs.copyFileSync(BACKUP, SERIES);
  console.log('\n已改回原值\n');
  show('改回后', await run());
} finally {
  fs.copyFileSync(BACKUP, SERIES);
  fs.unlinkSync(BACKUP);
}
