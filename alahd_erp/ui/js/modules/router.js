/* ============================================================================
   modules/router.js — Hash Router & Deep-Link Navigation (Phases 4, 5, 8)
   ============================================================================ */

import { $, $all } from './dom.js';
import { state, uiState } from './store.js';

let _onRouteChangedCallback = null;

export function initRouter(onRouteChanged) {
  _onRouteChangedCallback = onRouteChanged;
  window.addEventListener('hashchange', () => {
    handleCurrentHash();
  });
}

export function navigateTo(hashOrView) {
  if (!hashOrView) return;
  const targetHash = hashOrView.startsWith('#') ? hashOrView : `#/${hashOrView}`;
  if (window.location.hash === targetHash) {
    handleCurrentHash();
  } else {
    window.location.hash = targetHash;
  }
}

export function switchView(viewName, skipHashUpdate = false) {
  const validViews = [
    'dashboard', 'statistics', 'alerts', 'students', 'profile',
    'collections', 'payroll', 'accounting', 'attendance',
    'memorization', 'madrasa', 'certificates', 'teachers',
    'import', 'exports', 'system'
  ];
  const target = validViews.includes(viewName) ? viewName : 'dashboard';
  uiState.activeView = target;

  $all('.nav-btn').forEach((btn) => {
    btn.classList.toggle('active', btn.getAttribute('data-view') === target);
  });
  $all('.view-panel').forEach((panel) => {
    panel.classList.toggle('active', panel.id === 'view-' + target);
  });

  if (!skipHashUpdate) {
    const newHash = `#/${target}`;
    if (window.location.hash !== newHash && !window.location.hash.startsWith(`${newHash}/`) && !window.location.hash.startsWith(`${newHash}?`)) {
      history.replaceState(null, '', newHash);
    }
  }

  if (typeof _onRouteChangedCallback === 'function') {
    _onRouteChangedCallback(target);
  }
}

export function handleCurrentHash() {
  const rawHash = window.location.hash || '#/dashboard';
  const cleaned = rawHash.replace(/^#\/?/, '');
  const [pathPart, queryPart] = cleaned.split('?');
  const segments = (pathPart || 'dashboard').split('/').filter(Boolean);
  const params = new URLSearchParams(queryPart || '');

  const primary = segments[0] || 'dashboard';
  const subId = segments[1] ? decodeURIComponent(segments[1]) : '';
  const monthParam = params.get('month') || '';
  const highlightParam = params.get('highlight') || '';

  if (monthParam && /^\d{4}-\d{2}$/.test(monthParam)) {
    uiState.selectedMonth = monthParam;
    const mSel = $('#globalMonthSelect');
    if (mSel) mSel.value = monthParam;
  }

  if (primary === 'students') {
    if (subId) {
      uiState.selectedProfileId = subId;
    }
    const teacherFilter = params.get('teacher');
    const typeFilter = params.get('type');
    const payFilter = params.get('payStatus');
    const studyFilter = params.get('studyStatus');
    const searchFilter = params.get('search');
    if (teacherFilter !== null && $('#stuFilterTeacher')) $('#stuFilterTeacher').value = teacherFilter;
    if (typeFilter !== null && $('#stuFilterType')) $('#stuFilterType').value = typeFilter;
    if (payFilter !== null && $('#stuFilterPayStatus')) $('#stuFilterPayStatus').value = payFilter;
    if (studyFilter !== null && $('#stuFilterStudyStatus')) $('#stuFilterStudyStatus').value = studyFilter;
    if (searchFilter !== null && $('#stuFilterSearch')) $('#stuFilterSearch').value = searchFilter;

    switchView('students', true);
    if (subId) {
      setTimeout(() => highlightStudentRow(subId, highlightParam), 60);
    }
    return;
  }

  if (primary === 'profile') {
    if (subId) {
      uiState.selectedProfileId = subId;
    }
    switchView('profile', true);
    return;
  }

  if (primary === 'teachers' && subId) {
    if ($('#stuFilterTeacher')) {
      $('#stuFilterTeacher').value = subId;
    }
    switchView('students', true);
    return;
  }

  const teacherParam = params.get('teacher');
  if (teacherParam !== null) {
    if (primary === 'statistics' && $('#statsTeacherFilter')) $('#statsTeacherFilter').value = teacherParam;
    if (primary === 'collections' && $('#collTeacherFilter')) $('#collTeacherFilter').value = teacherParam;
    if (primary === 'attendance' && $('#attTeacherFilter')) $('#attTeacherFilter').value = teacherParam;
    if (primary === 'memorization' && $('#memTeacherFilter')) $('#memTeacherFilter').value = teacherParam;
  }

  switchView(primary, true);

  if (highlightParam) {
    setTimeout(() => highlightElementByAttr(highlightParam), 60);
  }
}

export function highlightStudentRow(studentId, fieldName = '') {
  const row = document.querySelector(`tr[data-student-row="${CSS.escape(studentId)}"]`);
  if (!row) return;
  row.classList.remove('row-highlight-pulse');
  void row.offsetWidth;
  row.classList.add('row-highlight-pulse');
  row.scrollIntoView({ behavior: 'smooth', block: 'center' });
  if (fieldName) {
    const inp = row.querySelector(`[data-field="${CSS.escape(fieldName)}"]`);
    if (inp && typeof inp.focus === 'function') {
      inp.focus();
    }
  }
}

export function highlightElementByAttr(targetId) {
  const el = document.querySelector(`[data-row-id="${CSS.escape(targetId)}"]`) ||
             document.getElementById(targetId);
  if (!el) return;
  el.classList.remove('row-highlight-pulse');
  void el.offsetWidth;
  el.classList.add('row-highlight-pulse');
  el.scrollIntoView({ behavior: 'smooth', block: 'center' });
}
