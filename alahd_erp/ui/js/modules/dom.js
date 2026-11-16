/* ============================================================================
   modules/dom.js — Safe DOM & Escaped Tagged Template Helper (`html` / `esc`)
   ============================================================================ */

export function $(sel, root = document) {
  return root.querySelector(sel);
}

export function $all(sel, root = document) {
  return Array.prototype.slice.call(root.querySelectorAll(sel));
}

export function esc(val) {
  return String(val === null || val === undefined ? '' : val).replace(/[&<>"']/g, function (c) {
    return {
      '&': '&amp;',
      '<': '&lt;',
      '>': '&gt;',
      '"': '&quot;',
      "'": '&#39;'
    }[c];
  });
}

export class RawHtml {
  constructor(str) {
    this.value = String(str === null || str === undefined ? '' : str);
  }
}

export function raw(str) {
  return new RawHtml(str);
}

function formatInterpolated(val) {
  if (val instanceof RawHtml) {
    return val.value;
  }
  if (Array.isArray(val)) {
    return val.map(formatInterpolated).join('');
  }
  return esc(val);
}

export function html(strings, ...values) {
  let out = '';
  for (let i = 0; i < strings.length; i++) {
    out += strings[i];
    if (i < values.length) {
      out += formatInterpolated(values[i]);
    }
  }
  return new RawHtml(out);
}

export function renderHtml(elOrSel, templateResult) {
  const el = typeof elOrSel === 'string' ? $(elOrSel) : elOrSel;
  if (!el) return;
  if (templateResult instanceof RawHtml) {
    el.innerHTML = templateResult.value;
  } else {
    el.innerHTML = String(templateResult === null || templateResult === undefined ? '' : templateResult);
  }
}

export function showToast(msg, isError = false) {
  const box = $('#toastContainer');
  if (!box) return;
  const el = document.createElement('div');
  el.className = 'toast-msg' + (isError ? ' error' : '');
  el.textContent = String(msg || '');
  box.appendChild(el);
  setTimeout(function () {
    if (el.parentNode) el.parentNode.removeChild(el);
  }, 3600);
}
