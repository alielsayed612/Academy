// Loads the REAL api.js (copied next to this file by the Python test) with a fake browser and reports what it did.
// argv[2] = JSON {"href": "...", "winSecret": "..."|null}
const sc = JSON.parse(process.argv[2]);
const u = new URL(sc.href);
const calls = [];
globalThis.window = { FinanceEngine: {}, location: u, history: { state: null, replaceState(s, t, url) { calls.push(url); } } };
if (sc.winSecret) globalThis.window.__ALAHD_WSEC__ = sc.winSecret;
globalThis.document = { querySelector() { return null; }, querySelectorAll() { return []; }, getElementById() { return null; },
  createElement() { return { style: {}, classList: { add() {}, remove() {} } }; } };
const m = await import('./api.js');
process.stdout.write(JSON.stringify({ secret: m.getWindowSecretFromUrl(), replaceCalls: calls }));
