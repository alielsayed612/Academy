// Parity runner: loads the REAL alahd_erp/ui/js/finance-engine.js (no copy, no mock) and evaluates JSON cases.
// stdin : {"cases": [{"fn": "roundHalfUp", "args": [...]}, ...]}
// stdout: {"results": [{"ok": value} | {"err": "message"}, ...]}
'use strict';
const fs = require('fs');
const path = require('path');

const FE = require(path.join(__dirname, '..', '..', 'alahd_erp', 'ui', 'js', 'finance-engine.js'));
// JSON has no `undefined`; the sentinel "__undef__" is turned back into undefined (app.js passes undefined, not null,
// when a student has no receipts array, and the two behave differently in the engine).
function revive(v) {
  if (v === '__undef__') return undefined;
  if (Array.isArray(v)) return v.map(revive);
  if (v && typeof v === 'object') { const o = {}; for (const k of Object.keys(v)) o[k] = revive(v[k]); return o; }
  return v;
}
const input = JSON.parse(fs.readFileSync(0, 'utf8'));

const results = input.cases.map((c) => {
  try {
    const fn = FE[c.fn];
    if (typeof fn !== 'function') return { err: 'no such function: ' + c.fn };
    const v = fn.apply(null, revive(c.args));
    // JSON cannot carry undefined/NaN; make them explicit so a mismatch is visible, not silently null.
    if (v === undefined) return { ok: '__undefined__' };
    if (typeof v === 'number' && !Number.isFinite(v)) return { ok: '__nonfinite__' };
    return { ok: v };
  } catch (e) {
    return { err: String(e && e.message || e) };
  }
});
process.stdout.write(JSON.stringify({ results }));
