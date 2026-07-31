// 破坏注入：把 derive 某帧 spread 改 999.9，verify-contract-contango 必须变红。
// 改回后必须恢复绿。
import { spawn } from 'child_process';
import fs from 'fs';

const SERIES = 'data/derived/term-structure-series.json';
const BACKUP = 'series.spread-injection-backup.json';

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
  const lines = r.out.split('\n').filter(l => /FAIL|passed,/.test(l));
  for (const l of lines.slice(0, 6)) console.log('   ', l.trim().slice(0, 120));
};

fs.copyFileSync(SERIES, BACKUP);
try {
  show('注入前（基线）', await run());

  const s = JSON.parse(fs.readFileSync(BACKUP, 'utf8'));
  const target = s.data.frames[s.data.frames.length - 1];
  console.log(`\n注入：帧 ${target.date} 的 spread ${target.spread} → 999.9\n`);
  target.spread = 999.9;
  fs.writeFileSync(SERIES, JSON.stringify(s));

  show('注入后', await run());

  fs.copyFileSync(BACKUP, SERIES);
  console.log('\n已改回原值\n');
  show('改回后', await run());
} finally {
  fs.copyFileSync(BACKUP, SERIES);
  fs.unlinkSync(BACKUP);
}
