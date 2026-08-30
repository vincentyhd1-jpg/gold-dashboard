import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const DIST = path.join(ROOT, 'dist');
const PUBLIC_FILES = [
  'index.html',
  'macro.html',
  'term-3d.html',
  'assets/favicon-16.png',
  'assets/favicon-180.png',
  'assets/favicon-32.png',
  'assets/favicon.ico',
  'assets/favicon.svg',
  'assets/vendor/hammerjs-2.0.8/hammer.min.js',
  'assets/vendor/chartjs-plugin-zoom-2.2.0/chartjs-plugin-zoom.min.js',
  'assets/vendor/licenses/hammerjs-2.0.8-MIT.txt',
  'assets/vendor/licenses/chartjs-plugin-zoom-2.2.0-MIT.txt',
  'assets/js/cbo-scenario-engine.js',
  'css/chart.css',
  'js/data-helpers.js',
  'js/playback.js',
  'js/term-structure.js',
  'data/cot.json',
  'data/cpi_cpiaucsl.json',
  'data/cpi_cpilfesl.json',
  'data/debt_foreign.json',
  'data/debt_held_public.json',
  'data/debt_intragov.json',
  'data/debt_total.json',
  'data/fed_effective_rate.json',
  'data/fed_target_lower.json',
  'data/fed_target_upper.json',
  'data/gdp_nominal.json',
  'data/gold_price.json',
  'data/foreign_official_ust.json',
  'data/wgc_official_reserves.json',
  'data/oi.json',
  'data/stocks.json',
  'data/treasury_debt_daily.json',
  'data/treasury_mts_fiscal.json',
  'data/ust_dgs10.json',
  'data/ust_dgs2.json',
  'data/ust_dgs30.json',
  'data/derived/macro_cpi.json',
  'data/derived/macro_debt.json',
  'data/derived/macro_rates.json',
  'data/derived/macro_fiscal_stress.json',
  'data/derived/fiscal_risk_monitor.json',
  'data/derived/gold_vs_debt.json',
  'data/derived/official_reserve_composition.json',
  'data/derived/cbo_baseline_latest.json',
  'data/derived/cbo_scenario_basis.json',
  'data/derived/term-structure-series.json',
];

function assertSafeDist() {
  if (path.dirname(DIST) !== ROOT || path.basename(DIST) !== 'dist') {
    throw new Error(`拒绝清理非预期目录：${DIST}`);
  }
}

function build() {
  assertSafeDist();
  fs.rmSync(DIST, { recursive: true, force: true });
  fs.mkdirSync(DIST, { recursive: true });

  const stats = { files: 0, bytes: 0, groups: {} };
  for (const relative of PUBLIC_FILES) {
    const source = path.join(ROOT, relative);
    const destination = path.join(DIST, relative);
    const metadata = fs.lstatSync(source);
    if (metadata.isSymbolicLink() || !metadata.isFile()) {
      throw new Error(`静态白名单只接受普通文件：${source}`);
    }
    fs.mkdirSync(path.dirname(destination), { recursive: true });
    fs.copyFileSync(source, destination);
    const group = relative.endsWith('.html') && !relative.includes('/')
      ? 'html' : relative.split('/')[0];
    stats.files++;
    stats.bytes += metadata.size;
    stats.groups[group] = (stats.groups[group] || 0) + 1;
  }

  console.log(`Static site built: ${stats.files} files, ${stats.bytes} bytes`);
  for (const [group, count] of Object.entries(stats.groups)) {
    console.log(`  ${group}: ${count} files`);
  }
}

build();
