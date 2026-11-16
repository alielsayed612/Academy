/* ============================================================================
   modules/api.js — Granular API Client, Optimistic Concurrency & Save Status
   ============================================================================ */

import { $, showToast } from './dom.js';
import { state, uiState } from './store.js';

const API_PREFIX = '/api';

// F-03: capture the per-launch secret ONCE at load, keep it in memory, and strip it from the address bar so it never
// stays in history / copied links / referrers. After a reload the httpOnly cookie planted by the bootstrap response
// carries authentication (same-origin fetch sends it automatically), so no secret needs to stay in the URL.
const BOOT_WSEC = (() => {
  try {
    const url = new URL(window.location.href);
    const secret = url.searchParams.get('wsec') || window.__ALAHD_WSEC__ || '';
    if (url.searchParams.has('wsec')) {
      url.searchParams.delete('wsec');
      window.history.replaceState(window.history.state, '', url.pathname + url.search + url.hash);
    }
    return secret;
  } catch (e) {
    return '';
  }
})();

export function getWindowSecretFromUrl() {
  return BOOT_WSEC;
}

export function extractApiErrorMessage(errJson, fallbackStatus = 500) {
  const raw = errJson && errJson.detail !== undefined ? errJson.detail : errJson;
  if (typeof raw === 'string' && raw.trim()) {
    return raw.trim();
  }
  if (raw && typeof raw === 'object') {
    if (typeof raw.message === 'string' && raw.message.trim()) {
      return raw.message.trim();
    }
    if (Array.isArray(raw) && raw.length > 0) {
      return raw.map((item) => (item && item.msg) ? item.msg : JSON.stringify(item)).join(' | ');
    }
  }
  return `HTTP ${fallbackStatus}`;
}

export function updateSaveStatusUI() {
  const chip = $('#syncStatusChip');
  const retryBtn = $('#btnRetrySave');
  if (!chip) return;

  if (uiState.pendingSaves > 0) {
    chip.textContent = '⏳ جاري الحفظ...';
    chip.className = 'sync-status-chip saving';
    if (retryBtn) retryBtn.style.display = 'none';
  } else if (uiState.saveFailed) {
    chip.textContent = `❌ فشل الحفظ: ${uiState.lastSaveError || 'خطأ في الخادم'}`;
    chip.className = 'sync-status-chip failed';
    if (retryBtn) retryBtn.style.display = 'inline-flex';
  } else {
    chip.textContent = `✓ محفوظ (مراجعة #${state.rev})`;
    chip.className = 'sync-status-chip';
    if (retryBtn) retryBtn.style.display = 'none';
  }
}

export function wireSaveGuardAndRetry(reloadFullStateFn) {
  window.addEventListener('beforeunload', (e) => {
    if (uiState.pendingSaves > 0 || uiState.saveFailed) {
      e.preventDefault();
      e.returnValue = 'توجد تعديلات قيد الحفظ أو تعذر حفظها. هل تريد المغادرة؟';
      return e.returnValue;
    }
  });

  const retryBtn = $('#btnRetrySave');
  if (retryBtn) {
    retryBtn.addEventListener('click', () => {
      if (typeof uiState.lastFailedRetryFn === 'function') {
        const fn = uiState.lastFailedRetryFn;
        uiState.lastFailedRetryFn = null;
        fn().catch(() => {});
      } else if (typeof reloadFullStateFn === 'function') {
        reloadFullStateFn();
      }
    });
  }

  const conflictBtn = $('#btnReloadAfterConflict');
  if (conflictBtn) {
    conflictBtn.addEventListener('click', () => {
      const modal = $('#conflictModal');
      if (modal) modal.classList.remove('open');
      if (typeof reloadFullStateFn === 'function') {
        reloadFullStateFn();
      }
    });
  }
}

export async function apiRequest(subpath, options = {}) {
  const method = String(options.method || 'GET').toUpperCase();
  const isMutation = method !== 'GET' && method !== 'HEAD' && !options.readOnlyPost;
  const headers = Object.assign({}, options.headers || {});
  const wsec = getWindowSecretFromUrl();
  if (wsec) {
    headers['X-Window-Secret'] = wsec;
  }
  if (isMutation && options.sendRev !== false) {
    headers['X-Client-Rev'] = String(state.rev);
  }
  let body = options.body;
  if (body && typeof body === 'object' && !(body instanceof FormData) && !(body instanceof Blob)) {
    headers['Content-Type'] = 'application/json';
    if (isMutation && options.sendRev !== false && body.rev === undefined) {
      body = Object.assign({}, body, { rev: state.rev });
    }
    body = JSON.stringify(body);
  }

  if (isMutation) {
    uiState.pendingSaves += 1;
    uiState.saveFailed = false;
    updateSaveStatusUI();
  }

  try {
    const resp = await fetch(API_PREFIX + subpath, {
      method,
      headers,
      body,
      credentials: 'same-origin'
    });

    if (resp.status === 409) {
      const errJson = await resp.json().catch(() => ({}));
      const rawDetail = errJson && errJson.detail;
      const errCode = (rawDetail && typeof rawDetail === 'object' && rawDetail.code) ? rawDetail.code : '';
      const msg = extractApiErrorMessage(errJson, 409);
      if (errCode === 'CONCURRENCY_CONFLICT') {
        const modal = $('#conflictModal');
        if (modal) modal.classList.add('open');
      }
      const err = new Error(msg);
      err.status = 409;
      err.code = errCode || 'CONFLICT';
      err.detail = rawDetail;
      throw err;
    }

    if (!resp.ok) {
      const errJson = await resp.json().catch(() => ({}));
      const rawDetail = errJson && errJson.detail;
      const errCode = (rawDetail && typeof rawDetail === 'object' && rawDetail.code) ? rawDetail.code : '';
      const detail = extractApiErrorMessage(errJson, resp.status);
      const err = new Error(detail);
      err.status = resp.status;
      err.code = errCode;
      err.detail = rawDetail;
      throw err;
    }

    const contentType = resp.headers.get('content-type') || '';
    let data = null;
    if (contentType.includes('application/json')) {
      data = await resp.json();
      if (data && typeof data.rev === 'number') {
        state.rev = data.rev;
      }
    } else {
      data = await resp.text();
    }

    if (isMutation) {
      uiState.pendingSaves = Math.max(0, uiState.pendingSaves - 1);
      uiState.saveFailed = false;
      uiState.lastSaveError = '';
      uiState.lastFailedRetryFn = null;
      updateSaveStatusUI();
    }
    return data;
  } catch (err) {
    if (isMutation) {
      uiState.pendingSaves = Math.max(0, uiState.pendingSaves - 1);
      uiState.saveFailed = true;
      uiState.lastSaveError = err.message || 'خطأ في الاتصال بالخادم';
      uiState.lastFailedRetryFn = () => apiRequest(subpath, options);
      updateSaveStatusUI();
      if (!options.suppressErrorToast) {
        showToast(`تعذّر الحفظ: ${uiState.lastSaveError}`, true);
      }
    }
    throw err;
  }
}

export async function downloadBlobFromApi(subpath, fallbackFilename, successMsg, postBody = null) {
  const headers = {};
  const wsec = getWindowSecretFromUrl();
  if (wsec) headers['X-Window-Secret'] = wsec;
  const fetchOpts = {
    method: postBody ? 'POST' : 'GET',
    headers,
    credentials: 'same-origin'
  };
  if (postBody) {
    headers['Content-Type'] = 'application/json';
    fetchOpts.body = JSON.stringify(postBody);
  }
  const resp = await fetch(API_PREFIX + subpath, fetchOpts);
  if (!resp.ok) {
    const errJson = await resp.json().catch(() => ({}));
    const msg = extractApiErrorMessage(errJson, resp.status);
    showToast(`تعذّر التحميل: ${msg}`, true);
    throw new Error(msg);
  }
  const cd = resp.headers.get('Content-Disposition') || '';
  const match = cd.match(/filename="?([^"]+)"?/i);
  const fname = (match && match[1]) ? match[1] : fallbackFilename;
  const blob = await resp.blob();
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = fname;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  setTimeout(() => URL.revokeObjectURL(url), 2500);
  if (successMsg) showToast(successMsg);
}
