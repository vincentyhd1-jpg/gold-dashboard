import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { replaceExactly, runInjectionSuite, sha256 } from './_injection.mjs';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const dataTarget = 'data/derived/official_reserve_composition.json';
const pageTarget = 'macro.html';
const guard = 'tools/verify-official-reserve-contract.mjs';
const targets = [dataTarget, pageTarget].map(file => path.join(ROOT, file));
const originalHashes = Object.fromEntries(targets.map(file => [file, sha256(fs.readFileSync(file))]));
const jsonPatch = mutate => bytes => {
  const payload = JSON.parse(bytes.toString('utf8'));
  mutate(payload);
  return `${JSON.stringify(payload, null, 2)}\n`;
};

const dataResult = await runInjectionSuite({
  name: 'C18C.3B official reserve data-contract injections',
  target: dataTarget,
  guard,
  cases: [
    {
      name: 'COFER USD share substituted for official UST share',
      patch: jsonPatch(payload => {
        payload.data.observations[0].foreign_official_ust_share_pct = 58.0;
        payload.data.sources.foreign_official_ust = 'IMF COFER USD share';
      }),
      verifyPatch: (_, patched) => {
        const payload = JSON.parse(patched);
        return { ok: payload.data.observations[0].foreign_official_ust_share_pct === 58
          && payload.data.sources.foreign_official_ust === 'IMF COFER USD share',
        detail: 'UST share now carries a COFER percentage and source' };
      },
      expectedFailureMarkers: [
        'FAIL UST share uses same common denominator',
        'FAIL TIC/FRED source label present',
      ],
    },
    {
      name: 'UST share uses a different denominator',
      patch: jsonPatch(payload => {
        const row = payload.data.observations[0];
        row.foreign_official_ust_share_pct = row.foreign_official_ust_value_usd
          / (row.total_official_reserve_assets_usd * 1.1) * 100;
      }),
      verifyPatch: (original, patched) => ({
        ok: JSON.parse(original).data.observations[0].foreign_official_ust_share_pct
          !== JSON.parse(patched).data.observations[0].foreign_official_ust_share_pct,
        detail: 'UST percentage now divides by 110% of the common denominator',
      }),
      expectedFailureMarkers: ['FAIL UST share uses same common denominator'],
    },
    {
      name: 'TIC/FRED source label removed',
      patch: jsonPatch(payload => { payload.data.sources.foreign_official_ust = ''; }),
      verifyPatch: (_, patched) => ({
        ok: JSON.parse(patched).data.sources.foreign_official_ust === '',
        detail: 'derived source identity removed',
      }),
      expectedFailureMarkers: ['FAIL TIC/FRED source label present'],
    },
    {
      name: 'quarterly UST observation is forward-filled',
      patch: jsonPatch(payload => {
        payload.data.observations[1].ust_source_date
          = payload.data.observations[0].ust_source_date;
      }),
      verifyPatch: (_, patched) => {
        const rows = JSON.parse(patched).data.observations;
        return { ok: rows[1].ust_source_date === rows[0].ust_source_date,
          detail: 'two quarters now reuse one monthly observation' };
      },
      expectedFailureMarkers: ['FAIL quarter alignment forbids fill/interpolation'],
    },
  ],
});

const pageResult = await runInjectionSuite({
  name: 'C18C.3B official reserve presentation injections',
  target: pageTarget,
  guard,
  cases: [
    {
      name: 'foreign official UST renamed as dollar reserves',
      patch: bytes => replaceExactly(bytes.toString('utf8'),
        "lineDataset('外国官方机构持有美债额'",
        "lineDataset('美元储备'"),
      verifyPatch: (_, patched) => ({ ok: patched.toString('utf8').includes("lineDataset('美元储备'"),
        detail: 'canonical TIC holding label replaced by a currency-reserve label' }),
      expectedFailureMarkers: ['FAIL canonical foreign official UST label'],
    },
    {
      name: 'official gold mislabeled as global gold market value',
      patch: bytes => replaceExactly(bytes.toString('utf8'),
        "lineDataset('全球央行持有黄金金额'",
        "lineDataset('全球黄金总市值'"),
      verifyPatch: (_, patched) => ({ ok: patched.toString('utf8').includes("lineDataset('全球黄金总市值'"),
        detail: 'official-sector gold was mislabeled as all above-ground gold' }),
      expectedFailureMarkers: ['FAIL official gold is not global gold market value'],
    },
  ],
});

let restored = true;
for (const [file, hash] of Object.entries(originalHashes)) {
  const ok = sha256(fs.readFileSync(file)) === hash;
  restored = restored && ok;
  console.log(`${ok ? 'PASS' : 'FAIL'} final SHA-256 restored ${path.relative(ROOT, file)}`);
}
const result = { ok: dataResult.ok && pageResult.ok && restored };
process.exitCode = result.ok ? 0 : 1;
