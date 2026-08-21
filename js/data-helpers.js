// data-helpers.js — 公共信封解包 / JSON 读取
// 只放纯数据处理，不碰 DOM。

function unwrapEnvelope(payload, name = 'payload', strict = true, map = null) {
  if (payload && typeof payload === 'object' && 'data' in payload) {
    return typeof map === 'function' ? map(payload) : payload.data;
  }
  if (strict) {
    throw new Error(name + ': 期望信封格式');
  }
  return payload;
}

async function loadJson(url, { name = url, strict = true, map = null, unwrap = true } = {}) {
  const res = await fetch(url);
  if (!res.ok) throw new Error(res.status);
  const payload = await res.json();
  return unwrap ? unwrapEnvelope(payload, name, strict, map) : payload;
}
