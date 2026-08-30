import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { replaceExactly, runInjectionSuite, sha256 } from './_injection.mjs';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const rawTarget = 'data/wgc_official_reserves.json';
const dataTarget = 'data/derived/official_reserve_composition.json';
const pageTarget = 'macro.html';
const guard = 'tools/verify-official-reserve-contract.mjs';
const targets = [rawTarget, dataTarget, pageTarget].map(file => path.join(ROOT, file));
const originalHashes = Object.fromEntries(targets.map(file => [file, sha256(fs.readFileSync(file))]));
const rawBaseline = JSON.parse(fs.readFileSync(path.join(ROOT, rawTarget), 'utf8'));
const jsonPatch = mutate => bytes => {
  const payload = JSON.parse(bytes.toString('utf8'));
  mutate(payload);
  return `${JSON.stringify(payload, null, 2)}\n`;
};

const derivedResult = await runInjectionSuite({
  name: 'C18C.3B statistical-universe derived injections',
  target: dataTarget,
  guard,
  cases: [
    {
      name: 'different gold/total reporters are independently aggregated then divided',
      patch: jsonPatch(payload => {
        const row = payload.data.observations.at(-1);
        row.official_gold_value_usd += 1_000_000;
        row.official_gold_share_pct = row.official_gold_value_usd
          / row.total_official_reserve_assets_usd * 100;
      }),
      verifyPatch: (_, patched) => {
        const rawLatest = rawBaseline.data.at(-1);
        const row = JSON.parse(patched).data.observations.at(-1);
        return { ok: rawLatest.gold_reporting_entities_count
            !== rawLatest.total_reserves_reporting_entities_count
          && Math.abs(row.official_gold_share_pct
            - row.official_gold_value_usd / row.total_official_reserve_assets_usd * 100) < 1e-10,
        detail: 'formula remains arithmetically valid but numerator no longer matches the entity intersection' };
      },
      expectedFailureMarkers: [
        'FAIL derived gold sample is coupled to current matched WGC aggregation',
      ],
    },
    {
      name: 'global TIC numerator is divided by partial WGC denominator',
      patch: jsonPatch(payload => {
        for (const row of payload.data.observations) {
          row.foreign_official_ust_share_pct = row.foreign_official_ust_value_usd
            / row.total_official_reserve_assets_usd * 100;
        }
        payload.data.methodology.ust_ratio_status = 'direct';
        payload.data.methodology.tic_numerator_denominator_same_entity_universe = true;
      }),
      verifyPatch: (_, patched) => {
        const payload = JSON.parse(patched);
        return { ok: payload.data.observations.every(row =>
          Number.isFinite(row.foreign_official_ust_share_pct))
          && payload.data.methodology.ust_ratio_status === 'direct',
        detail: 'a global TIC ratio is now produced against the partial WGC sample' };
      },
      expectedFailureMarkers: [
        'FAIL global TIC numerator is not divided by partial WGC denominator',
      ],
    },
    {
      name: 'COFER USD share substituted into the excluded ratio path',
      patch: jsonPatch(payload => {
        payload.data.methodology.cofer_usd_share_is_not_ust_share = false;
        payload.data.sources.foreign_official_ust = 'IMF COFER USD share';
      }),
      verifyPatch: (_, patched) => {
        const payload = JSON.parse(patched);
        return { ok: payload.data.methodology.cofer_usd_share_is_not_ust_share === false
          && payload.data.sources.foreign_official_ust === 'IMF COFER USD share',
        detail: 'COFER has replaced the TIC source semantics' };
      },
      expectedFailureMarkers: [
        'FAIL COFER USD share is forbidden as UST share',
        'FAIL TIC/FRED source and foreign-official scope are explicit',
      ],
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
      expectedFailureMarkers: [
        'FAIL quarter alignment forbids fill interpolation and exact-day TIC claims',
      ],
    },
  ],
});

const rawResult = await runInjectionSuite({
  name: 'C18C.3B entity-identity source injections',
  target: rawTarget,
  guard,
  cases: [
    {
      name: 'same reporter count hides different entity identities',
      patch: jsonPatch(payload => {
        const row = payload.data.at(-1);
        const matched = row.reporting_entities.matched;
        const goldOnly = row.reporting_entities.gold_reserves
          .find(name => !matched.includes(name));
        row.reporting_entities.total_reserves = [...row.reporting_entities.total_reserves];
        row.reporting_entities.total_reserves[0] = goldOnly;
        row.reporting_entities.total_reserves.sort();
      }),
      verifyPatch: (original, patched) => {
        const before = JSON.parse(original).data.at(-1);
        const after = JSON.parse(patched).data.at(-1);
        return { ok: before.reporting_entities.total_reserves.length
            === after.reporting_entities.total_reserves.length
          && JSON.stringify(before.reporting_entities.total_reserves)
            !== JSON.stringify(after.reporting_entities.total_reserves),
        detail: 'count is unchanged while one total-reserves reporter identity differs' };
      },
      expectedFailureMarkers: [
        'FAIL gold numerator and denominator use identical entity intersection',
      ],
    },
    {
      name: 'World aggregate is added beside member economies',
      patch: jsonPatch(payload => {
        const row = payload.data.at(-1);
        for (const field of ['gold_reserves', 'gold_reserves_tonnes', 'total_reserves', 'matched']) {
          row.reporting_entities[field].push('WLD');
          row.reporting_entities[field].sort();
        }
        row.gold_reporting_entities_count += 1;
        row.gold_tonnes_reporting_entities_count += 1;
        row.total_reserves_reporting_entities_count += 1;
        row.matched_reporting_entities_count += 1;
      }),
      verifyPatch: (_, patched) => ({
        ok: Object.values(JSON.parse(patched).data.at(-1).reporting_entities)
          .every(list => list.includes('WLD')),
        detail: 'WLD is present alongside country/economy series in every metric universe',
      }),
      expectedFailureMarkers: [
        'FAIL WGC source has no World region or institution aggregate series',
      ],
    },
  ],
});

const pageResult = await runInjectionSuite({
  name: 'C18C.3B scope and period presentation injections',
  target: pageTarget,
  guard,
  cases: [
    {
      name: 'dynamic reporting sample is mislabeled as Global Total Official Reserves',
      patch: bytes => replaceExactly(bytes.toString('utf8'),
        'WGC 报告经济体官方储备样本：黄金 vs 外国官方机构持有美债',
        '全球官方储备构成：黄金 vs 外国官方机构持有美债'),
      verifyPatch: (_, patched) => ({
        ok: patched.toString('utf8').includes('全球官方储备构成：黄金 vs 外国官方机构持有美债'),
        detail: 'partial reporting sample is now labeled global' }),
      expectedFailureMarkers: [
        'FAIL dynamic reporting sample is not labeled Global Total Official Reserves',
      ],
    },
    {
      name: 'TIC monthly period is presented as an exact daily as_of',
      patch: bytes => replaceExactly(bytes.toString('utf8'),
        'return `  来源：${ctx.dataset.sourceLabel} · source period ${sourcePeriod}`;',
        'return `  来源：${ctx.dataset.sourceLabel} · as_of ${row.ust_source_date}`;'),
      verifyPatch: (_, patched) => ({
        ok: patched.toString('utf8').includes('as_of ${row.ust_source_date}'),
        detail: 'monthly observation label is now presented as an exact date' }),
      expectedFailureMarkers: [
        'FAIL quarter alignment forbids fill interpolation and exact-day TIC claims',
      ],
    },
  ],
});

let restored = true;
for (const [file, hash] of Object.entries(originalHashes)) {
  const ok = sha256(fs.readFileSync(file)) === hash;
  restored = restored && ok;
  console.log(`${ok ? 'PASS' : 'FAIL'} final SHA-256 restored ${path.relative(ROOT, file)}`);
}
const result = { ok: derivedResult.ok && rawResult.ok && pageResult.ok && restored };
process.exitCode = result.ok ? 0 : 1;
