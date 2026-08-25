// data-helpers.js — 公共信封解包 / JSON 读取
// 只放纯数据处理，不碰 DOM。

function unwrapEnvelope(payload, name = 'payload', strict = true, map = null) {
  const required = [
    'schema_version', 'source', 'freq', 'generated_at', 'date_field',
    'coverage', 'derived_from', 'warnings', 'info', 'data',
  ];
  const isObject = payload && typeof payload === 'object' && !Array.isArray(payload);
  const missing = isObject ? required.filter(k => !(k in payload)) : required;
  if (isObject && missing.length === 0 && payload.schema_version === 0) {
    return typeof map === 'function' ? map(payload) : payload.data;
  }
  if (strict) {
    if (isObject && missing.length === 0) {
      throw new Error(name + ': 未知 schema_version=' + String(payload.schema_version));
    }
    throw new Error(name + ': 期望信封格式' +
      (isObject ? '（缺字段：' + missing.join(', ') + '）' : ''));
  }
  return payload;
}

async function loadJson(url, { name = url, strict = true, map = null, unwrap = true } = {}) {
  const res = await fetch(url);
  if (!res.ok) throw new Error(res.status);
  const payload = await res.json();
  return unwrap ? unwrapEnvelope(payload, name, strict, map) : payload;
}
