/* ============================================================================
   app.js — Main ES Module Orchestrator for Yawm Jadid Academy Suite (v1.1.0)
   - Uses granular CRUD endpoints with optimistic concurrency (`rev`).
   - Enforces strict independence between `paymentDate` and `actualStartDate`.
   - Uses dynamic months, receipt-driven payment status, and net subscriptions.
   ============================================================================ */

import { $, $all, esc, html, raw, renderHtml, showToast } from './modules/dom.js';
import { csvCell } from './modules/csv.js';
import {
  FE,
  state,
  uiState,
  applyLoadedState,
  recomputeDynamicMonths,
  money,
  monthLabel,
  cleanTeacherName,
  isTeacherArchived,
  isTeacherActive,
  getActiveTeachers,
  getAllTeachersSorted
} from './modules/store.js';
import { apiRequest, downloadBlobFromApi, updateSaveStatusUI, wireSaveGuardAndRetry } from './modules/api.js';
import { initRouter, navigateTo, handleCurrentHash } from './modules/router.js';
import { renderStatisticsView, wireStatisticsEvents } from './modules/statistics.js';
import { refreshAlertsAndBadges, wireAlertsEvents } from './modules/alerts.js';
import { wireImportEvents } from './modules/import_view.js';
import {
  applyBrandingAndSettingsToUI,
  openOnboardingWizard,
  checkPinAndIdleLock,
  renderSystemHealthView,
  wireWizardAndSystemEvents
} from './modules/wizard_and_system.js';

const THEME_KEY = 'alahd_erp_theme_v1';

const SESSION_STATUS_OPTIONS = [
  { val: 'scheduled', label: 'مجدولة' },
  { val: 'done', label: 'انعقدت ✓' },
  { val: 'absent', label: 'غياب بلا عذر' },
  { val: 'excused', label: 'اعتذار مسبق' },
  { val: 'late', label: 'حضرت متأخرة' },
  { val: 'cancel_teacher', label: 'ألغيت من المعلمة' },
  { val: 'cancel_admin', label: 'ألغيت إداريًا' },
  { val: 'postponed', label: 'مؤجلة' },
  { val: 'makeup', label: 'تعويضية' }
];

function getPrevMonthKey(mk) {
  const parts = String(mk || '').split('-').map(Number);
  if (parts.length !== 2 || !parts[0] || !parts[1]) return mk;
  let y = parts[0];
  let m = parts[1] - 1;
  if (m < 1) {
    m = 12;
    y -= 1;
  }
  return `${y}-${String(m).padStart(2, '0')}`;
}

function getFinanceOptions() {
  return {
    wageBasis: (state.settings && state.settings.wageBasis) || 'net',
    wageAccrualMode: (state.settings && state.settings.wageAccrualMode) || 'accrued',
    referenceDate: uiState.referenceDate,
    durationDays: Number((state.settings && state.settings.defaultDurationDays) || 30),
    expiringSoonDays: Number((state.settings && state.settings.expiringSoonDays) || 5)
  };
}

export async function reloadFullStateFromServer() {
  try {
    const serverState = await apiRequest('/sync');
    uiState.backendOnline = true;
    applyLoadedState(serverState);
    updateSaveStatusUI();
    applyBrandingAndSettingsToUI();
    renderAll();
    checkPinAndIdleLock();
    if (!state.onboarded && state.students.length === 0) {
      openOnboardingWizard();
    }
    await refreshAlertsAndBadges();
  } catch (err) {
    uiState.backendOnline = false;
    showToast(`تعذّر الاتصال بخادم قاعدة البيانات المحلية: ${err.message}`, true);
  }
}

function populateModalTeacherSelect(preserveCurrentTeacher = '') {
  const modalTeacherEl = $('#modalStudentTeacher');
  if (!modalTeacherEl) return;
  const activeTeachers = getActiveTeachers();
  const extraArchived = (preserveCurrentTeacher && !activeTeachers.includes(preserveCurrentTeacher) && state.teachers.includes(preserveCurrentTeacher))
    ? [preserveCurrentTeacher]
    : [];
  const optsHtml = html`
    <option value="">بدون معلمة</option>
    ${activeTeachers.map((t) => html`<option value="${t}">${t}</option>`)}
    ${extraArchived.map((t) => html`<option value="${t}">${t} (غير نشطة / مؤرشفة)</option>`)}
  `;
  const curVal = preserveCurrentTeacher || modalTeacherEl.value;
  renderHtml(modalTeacherEl, optsHtml);
  if (curVal && (activeTeachers.includes(curVal) || extraArchived.includes(curVal))) {
    modalTeacherEl.value = curVal;
  } else {
    modalTeacherEl.value = '';
  }
}

function populateSelects() {
  recomputeDynamicMonths();
  const mSel = $('#globalMonthSelect');
  if (mSel) {
    renderHtml(mSel, html`${uiState.monthsList.map((m) => html`
      <option value="${m.key}" ${m.key === uiState.selectedMonth ? 'selected' : ''}>${m.label}</option>
    `)}`);
    mSel.value = uiState.selectedMonth;
  }

  const allTeachersSorted = getAllTeachersSorted();
  const activeTeachers = getActiveTeachers();

  const teacherOptsFilterWithUnassigned = html`
    <option value="">جميع المعلمات</option>
    ${allTeachersSorted.map((t) => html`<option value="${t}">${t}${isTeacherArchived(t) ? ' (مؤرشفة)' : ''}</option>`)}
    <option value="__unassigned__">بدون معلمة (مجموعات)</option>
  `;
  ['#stuFilterTeacher', '#statsTeacherFilter', '#collTeacherFilter', '#attTeacherFilter', '#memTeacherFilter'].forEach((sel) => {
    const el = $(sel);
    if (el) {
      const prevT = el.value;
      renderHtml(el, teacherOptsFilterWithUnassigned);
      if (prevT && (prevT === '__unassigned__' || state.teachers.includes(prevT))) {
        el.value = prevT;
      } else {
        el.value = '';
      }
    }
  });

  // Modal teacher select: active teachers only for new assignments (or preserved current teacher if editing)
  const editingSid = ($('#modalStudentId') && $('#modalStudentId').value) || '';
  const editingStu = editingSid ? state.students.find((s) => s.id === editingSid) : null;
  populateModalTeacherSelect(editingStu ? (editingStu.teacher || '') : '');

  // Salary payout select: active teachers + any archived teacher with students or payouts in selectedMonth
  const mk = uiState.selectedMonth;
  const fOpts = getFinanceOptions();
  const payableTeachers = allTeachersSorted.filter((t) => {
    if (isTeacherActive(t)) return true;
    const tStuds = state.students.filter((s) => s.teacher === t);
    const agg = FE.computeMonthAggregate(tStuds, mk, state.installments, state.teacherMeta, fOpts);
    const hasPayout = state.payouts.some((p) => p.teacher === t && p.month === mk);
    return agg.wages > 0 || tStuds.length > 0 || hasPayout;
  });
  const payTeacherEl = $('#payTeacherSelect');
  if (payTeacherEl) {
    const prevPayT = payTeacherEl.value;
    renderHtml(payTeacherEl, html`
      <option value="">اختر المعلمة...</option>
      ${payableTeachers.map((t) => html`<option value="${t}">${t}${isTeacherArchived(t) ? ' (مؤرشفة)' : ''}</option>`)}
    `);
    if (prevPayT && payableTeachers.includes(prevPayT)) {
      payTeacherEl.value = prevPayT;
    } else if (activeTeachers.length > 0) {
      payTeacherEl.value = activeTeachers[0];
    }
  }

  // HTML Export teacher select: all teachers (active + archived for historical reports)
  const htmlExpTeacherEl = $('#htmlExportTeacher');
  if (htmlExpTeacherEl) {
    const prevExpT = htmlExpTeacherEl.value;
    renderHtml(htmlExpTeacherEl, html`
      <option value="">جميع المعلمات</option>
      ${allTeachersSorted.map((t) => html`<option value="${t}">${t}${isTeacherArchived(t) ? ' (مؤرشفة)' : ''}</option>`)}
    `);
    if (prevExpT && state.teachers.includes(prevExpT)) {
      htmlExpTeacherEl.value = prevExpT;
    }
  }

  const typeOptsAll = html`
    <option value="">جميع الأنظمة</option>
    ${state.subTypes.map((tp) => html`<option value="${tp}">${tp}</option>`)}
  `;
  const stuFilterType = $('#stuFilterType');
  if (stuFilterType) {
    const prevTp = stuFilterType.value;
    renderHtml(stuFilterType, typeOptsAll);
    if (prevTp && state.subTypes.includes(prevTp)) {
      stuFilterType.value = prevTp;
    } else {
      stuFilterType.value = '';
    }
  }

  const modalType = $('#modalStudentType');
  if (modalType) {
    const prevModalTp = modalType.value;
    renderHtml(modalType, html`${state.subTypes.map((tp) => html`<option value="${tp}">${tp}</option>`)}`);
    if (prevModalTp && state.subTypes.includes(prevModalTp)) {
      modalType.value = prevModalTp;
    }
  }

  const stuOpts = html`
    ${state.students.map((s) => html`<option value="${s.id}">${s.name} (${s.teacher || 'بدون معلمة'})</option>`)}
  `;
  [
    '#profileStudentSelect', '#instStudentSelect',
    '#madStudentSelect', '#certStudentSelect', '#htmlExportStudent'
  ].forEach((sel) => {
    const el = $(sel);
    if (el) {
      const cur = el.value;
      renderHtml(el, stuOpts);
      if (cur && state.students.some((s) => s.id === cur)) {
        el.value = cur;
      }
    }
  });

  // Attendance & Memorization student selects respect their view teacher filters if selected
  const attTf = ($('#attTeacherFilter') && $('#attTeacherFilter').value) || '';
  const attStudents = attTf
    ? state.students.filter((s) => (attTf === '__unassigned__' ? !s.teacher : s.teacher === attTf))
    : state.students;
  const planStuEl = $('#planStudentSelect');
  if (planStuEl) {
    const curPlanStu = planStuEl.value;
    renderHtml(planStuEl, html`
      ${attStudents.map((s) => html`<option value="${s.id}">${s.name} (${s.teacher || 'بدون معلمة'})</option>`)}
    `);
    if (curPlanStu && attStudents.some((s) => s.id === curPlanStu)) {
      planStuEl.value = curPlanStu;
    }
  }

  const memTf = ($('#memTeacherFilter') && $('#memTeacherFilter').value) || '';
  const memStudents = memTf
    ? state.students.filter((s) => (memTf === '__unassigned__' ? !s.teacher : s.teacher === memTf))
    : state.students;
  const memStuEl = $('#memStudentSelect');
  if (memStuEl) {
    const curMemStu = memStuEl.value;
    renderHtml(memStuEl, html`
      ${memStudents.map((s) => html`<option value="${s.id}">${s.name} (${s.teacher || 'بدون معلمة'})</option>`)}
    `);
    if (curMemStu && memStudents.some((s) => s.id === curMemStu)) {
      memStuEl.value = curMemStu;
    }
  }

  if ($('#profileStudentSelect') && uiState.selectedProfileId) {
    $('#profileStudentSelect').value = uiState.selectedProfileId;
  }
  const bulkTeacherEl = $('#bulkTeacherSelect');
  if (bulkTeacherEl) {
    const prevBt = bulkTeacherEl.value;
    renderHtml(bulkTeacherEl, html`
      <option value="">بدون معلمة</option>
      ${activeTeachers.map((t) => html`<option value="${t}">${t}</option>`)}
    `);
    if (prevBt && activeTeachers.includes(prevBt)) {
      bulkTeacherEl.value = prevBt;
    } else if (activeTeachers[0]) {
      bulkTeacherEl.value = activeTeachers[0];
    }
  }

  const bulkPkgEl = $('#bulkPackageSelect');
  if (bulkPkgEl) {
    const prevBp = bulkPkgEl.value;
    renderHtml(bulkPkgEl, html`${state.subTypes.map((tp) => html`<option value="${tp}">${tp}</option>`)}`);
    if (prevBp && state.subTypes.includes(prevBp)) {
      bulkPkgEl.value = prevBp;
    }
  }

  const bulkStartEl = $('#bulkActualStartInput');
  if (bulkStartEl && !bulkStartEl.value) {
    bulkStartEl.value = uiState.referenceDate;
  }

  const badge = $('#navStudentsCount');
  if (badge) badge.textContent = String(state.students.length);
}

/* ==========================================================================
   1. RENDER EXECUTIVE DASHBOARD
   ========================================================================== */
function renderDashboard() {
  const mk = uiState.selectedMonth;
  const fOpts = getFinanceOptions();
  const mAgg = FE.computeMonthAggregate(state.students, mk, state.installments, state.teacherMeta, fOpts);
  const accAll = FE.computeAccountingSummary(state, '__all__', fOpts);

  const emptyBanner = $('#dashboardEmptyBanner');
  if (emptyBanner) {
    emptyBanner.style.display = state.students.length === 0 ? 'block' : 'none';
  }

  renderHtml('#dashboardKpis', html`
    <div class="kpi-card">
      <div class="kpi-label">إجمالي الطالبات المسجلات</div>
      <div class="kpi-value">${state.students.length} طالبة</div>
      <div class="kpi-sub">مشتركات ${monthLabel(mk)}: <b>${mAgg.count} طالبة</b></div>
    </div>
    <div class="kpi-card kpi-gold">
      <div class="kpi-label">صافي اشتراكات ${monthLabel(mk)}</div>
      <div class="kpi-value">${money(mAgg.value)}</div>
      <div class="kpi-sub">إجمالي العام (صافي): <b>${money(accAll.subscriptions)}</b></div>
    </div>
    <div class="kpi-card kpi-pay">
      <div class="kpi-label">المحصّل الفعلي (${monthLabel(mk)})</div>
      <div class="kpi-value">${money(mAgg.collected)}</div>
      <div class="kpi-sub">مسددات: <b>${mAgg.paidCount}</b> | متبقي: <b>${money(mAgg.uncollected)}</b></div>
    </div>
    <div class="kpi-card kpi-wage">
      <div class="kpi-label">رواتب المعلمات (${monthLabel(mk)})</div>
      <div class="kpi-value">${money(mAgg.wages)}</div>
      <div class="kpi-sub">إجمالي رواتب العام: <b>${money(accAll.wages)}</b></div>
    </div>
    <div class="kpi-card kpi-start">
      <div class="kpi-label">صافي الأكاديمية (${monthLabel(mk)})</div>
      <div class="kpi-value">${money(mAgg.net)}</div>
      <div class="kpi-sub">صافي الربح التشغيلي: <b>${money(accAll.netOperatingProfit)}</b></div>
    </div>
  `);

  renderComparativeTeachersTable('#dashTeachersTableWrap');

  const advList = [];
  const defList = [];
  const prevMk = getPrevMonthKey(mk);
  state.students.forEach((s) => {
    [prevMk, mk].forEach((mKey) => {
      const m = (s.months && s.months[mKey]) || {};
      const ev = FE.evaluateStudentMonthDates(m, uiState.referenceDate, fOpts.durationDays, fOpts.expiringSoonDays);
      if (ev.paidInAdvance) {
        advList.push({ s, mk: mKey, ev });
      } else if (ev.paidAfterStart) {
        defList.push({ s, mk: mKey, ev });
      }
    });
  });

  const sampleRows = advList.concat(defList).slice(0, 6);
  renderHtml('#dashDatesBreakdown', html`
    <div class="kpi-grid" style="margin-bottom:12px;">
      <div class="kpi-card kpi-pay" style="padding:12px;">
        <div class="kpi-label">دفع مسبق قبل تاريخ البداية الفعلي</div>
        <div class="kpi-value" style="font-size:19px;">${advList.length} اشتراك</div>
        <div class="kpi-sub">Payment Date &lt; Actual Start Date</div>
      </div>
      <div class="kpi-card kpi-start" style="padding:12px;">
        <div class="kpi-label">دفع لاحق بعد تاريخ البداية الفعلي</div>
        <div class="kpi-value" style="font-size:19px;">${defList.length} اشتراك</div>
        <div class="kpi-sub">Payment Date &gt; Actual Start Date</div>
      </div>
    </div>
    ${sampleRows.length ? html`
      <div class="table-wrap">
        <table class="data-table">
          <thead>
            <tr>
              <th>الطالبة</th>
              <th>الشهر</th>
              <th class="th-pay">تاريخ الدفع</th>
              <th class="th-start">تاريخ البداية الفعلي</th>
              <th class="th-end">تاريخ النهاية</th>
              <th>الحالة</th>
            </tr>
          </thead>
          <tbody>
            ${sampleRows.map((item) => html`
              <tr>
                <td><b>${item.s.name}</b></td>
                <td>${monthLabel(item.mk)}</td>
                <td><span class="badge badge-pay">${item.ev.paymentDate || '—'}</span></td>
                <td><span class="badge badge-start">${item.ev.actualStartDate || '—'}</span></td>
                <td><span class="badge badge-end">${item.ev.endDate || '—'}</span></td>
                <td>${item.ev.paidInAdvance
                  ? html`<span class="badge badge-pay">دفع مسبق</span>`
                  : html`<span class="badge badge-start">دفع بعد البدء</span>`}</td>
              </tr>
            `)}
          </tbody>
        </table>
      </div>
    ` : ''}
  `);
}

function renderComparativeTeachersTable(targetSel) {
  const container = $(targetSel);
  if (!container) return;
  const mk2 = uiState.selectedMonth;
  const mk1 = getPrevMonthKey(mk2);
  const fOpts = getFinanceOptions();

  const teacherList = getAllTeachersSorted().filter((t) => {
    if (isTeacherActive(t)) return true;
    const subStuds = state.students.filter((s) => s.teacher === t);
    if (subStuds.length > 0) return true;
    return state.payouts.some((p) => p.teacher === t && (p.month === mk1 || p.month === mk2));
  });
  if (state.students.some((s) => !String(s.teacher || '').trim())) {
    teacherList.push('__unassigned__');
  }

  let totM1N = 0, totM1S = 0, totM1W = 0;
  let totM2N = 0, totM2S = 0, totM2W = 0;

  const rowsHtml = teacherList.map((tKey, idx) => {
    const isUn = tKey === '__unassigned__';
    const tLabel = isUn ? 'بدون معلمة (مجموعات)' : tKey;
    const subStuds = state.students.filter((s) => (isUn ? !String(s.teacher || '').trim() : s.teacher === tKey));
    const m1Agg = FE.computeMonthAggregate(subStuds, mk1, state.installments, state.teacherMeta, fOpts);
    const m2Agg = FE.computeMonthAggregate(subStuds, mk2, state.installments, state.teacherMeta, fOpts);

    totM1N += m1Agg.count;
    totM1S += m1Agg.value;
    totM1W += m1Agg.wages;
    totM2N += m2Agg.count;
    totM2S += m2Agg.value;
    totM2W += m2Agg.wages;

    const totWage = m1Agg.wages + m2Agg.wages;
    const netAcad = (m1Agg.value + m2Agg.value) - totWage;
    const defPct = isUn ? '—' : `${FE.parsePctInt(state.teacherMeta[tKey] && state.teacherMeta[tKey].pct, 70)}%`;

    return html`
      <tr>
        <td>${isUn ? '—' : (idx + 1)}</td>
        <td style="text-align:right;font-weight:700;">
          ${tLabel}
          ${(!isUn && isTeacherArchived(tKey)) ? html`<span class="badge badge-warn" style="margin-right:4px;">مؤرشفة</span>` : ''}
        </td>
        <td>${defPct}</td>
        <td>${m1Agg.count}</td>
        <td>${money(m1Agg.value)}</td>
        <td><span class="badge badge-wage">${money(m1Agg.wages)}</span></td>
        <td>${m2Agg.count}</td>
        <td>${money(m2Agg.value)}</td>
        <td><span class="badge badge-wage">${money(m2Agg.wages)}</span></td>
        <td><b>${money(totWage)}</b></td>
        <td><b>${money(netAcad)}</b></td>
      </tr>
    `;
  });

  const grandWages = totM1W + totM2W;
  const grandNet = (totM1S + totM2S) - grandWages;

  renderHtml(container, html`
    <table class="data-table">
      <thead>
        <tr>
          <th rowspan="2">م</th>
          <th rowspan="2">اسم المعلمة</th>
          <th rowspan="2">النسبة</th>
          <th colspan="3" class="th-month-banner">${monthLabel(mk1)}</th>
          <th colspan="3" class="th-month-banner">${monthLabel(mk2)}</th>
          <th rowspan="2">إجمالي الرواتب</th>
          <th rowspan="2">صافي الأكاديمية</th>
        </tr>
        <tr>
          <th>الطالبات</th><th>صافي الاشتراكات</th><th>الراتب</th>
          <th>الطالبات</th><th>صافي الاشتراكات</th><th>الراتب</th>
        </tr>
      </thead>
      <tbody>${rowsHtml}</tbody>
      <tfoot>
        <tr>
          <td colspan="3">الإجمالي العام</td>
          <td>${totM1N}</td>
          <td>${money(totM1S)}</td>
          <td>${money(totM1W)}</td>
          <td>${totM2N}</td>
          <td>${money(totM2S)}</td>
          <td>${money(totM2W)}</td>
          <td>${money(grandWages)}</td>
          <td>${money(grandNet)}</td>
        </tr>
      </tfoot>
    </table>
  `);
}

/* ==========================================================================
   2. RENDER STUDENTS & SUBSCRIPTIONS MATRIX
   ========================================================================== */
function getFilteredStudents() {
  const q = String(($('#stuFilterSearch') && $('#stuFilterSearch').value) || '').trim().toLowerCase();
  const tFilter = ($('#stuFilterTeacher') && $('#stuFilterTeacher').value) || '';
  const tpFilter = ($('#stuFilterType') && $('#stuFilterType').value) || '';
  const payFilter = ($('#stuFilterPayStatus') && $('#stuFilterPayStatus').value) || '';
  const studyFilter = ($('#stuFilterStudyStatus') && $('#stuFilterStudyStatus').value) || '';
  const mk = uiState.selectedMonth;
  const fOpts = getFinanceOptions();
  const instMap = FE.indexInstallments(state.installments);

  return state.students.filter((s) => {
    if (q) {
      const hay = `${s.name} ${s.phone} ${s.code} ${s.teacher}`.toLowerCase();
      if (!hay.includes(q)) return false;
    }
    if (tFilter) {
      if (tFilter === '__unassigned__') {
        if (String(s.teacher || '').trim() !== '') return false;
      } else if (s.teacher !== tFilter) {
        return false;
      }
    }
    if (tpFilter && s.type !== tpFilter) return false;

    const m = (s.months && s.months[mk]) || {};
    const ev = FE.evaluateStudentMonthDates(m, uiState.referenceDate, fOpts.durationDays, fOpts.expiringSoonDays);
    const payStatus = FE.deriveMonthPaymentStatus(m, instMap[`${s.id}|${mk}`], uiState.referenceDate);

    if (payFilter === 'paid' && payStatus !== 'paid') return false;
    if (payFilter === 'partial' && payStatus !== 'partial') return false;
    if (payFilter === 'unpaid' && payStatus !== 'unpaid') return false;
    if (payFilter === 'advance' && !ev.paidInAdvance) return false;
    if (payFilter === 'deferred' && !ev.paidAfterStart) return false;

    if (studyFilter && ev.studyPeriod.status !== studyFilter) return false;
    return true;
  });
}

function renderStudents() {
  const list = getFilteredStudents();
  const mode = ($('#stuViewMode') && $('#stuViewMode').value) || 'selected';
  const mk = uiState.selectedMonth;
  const monthKeys = mode === 'two_months' ? [getPrevMonthKey(mk), mk] : [mk];
  const fOpts = getFinanceOptions();
  const instMap = FE.indexInstallments(state.installments);

  const headBanners = monthKeys.map((mKey) => html`<th colspan="6" class="th-month-banner">${monthLabel(mKey)}</th>`);
  const headSubCols = monthKeys.map(() => html`
    <th>الاشتراك</th>
    <th>المحصّل</th>
    <th class="th-pay">💳 تاريخ الدفع<br><small>(Payment Date)</small></th>
    <th class="th-start">📅 تاريخ البداية الفعلي<br><small>(Actual Start)</small></th>
    <th class="th-end">🏁 تاريخ النهاية<br><small>(End Date)</small></th>
    <th>أجر المعلمة</th>
  `);

  const bodyRows = list.map((s, idx) => {
    const pctDisp = `${FE.parsePctInt(s.pct, 70)}%`;
    const monthCells = monthKeys.map((mKey) => {
      const m = (s.months && s.months[mKey]) || {};
      const v = m.value !== null && m.value !== undefined && m.value !== '' ? m.value : '';
      const payDt = FE.resolvePaymentDate(m);
      const startDt = FE.resolveActualStartDate(m);
      const endDt = FE.resolveEndDate(m);
      const instList = instMap[`${s.id}|${mKey}`] || [];
      const coll = FE.effectiveMonthCollected(m, instList, uiState.referenceDate);
      const paySt = FE.deriveMonthPaymentStatus(m, instList, uiState.referenceDate);
      const wage = FE.effectiveMonthWage(s, m, state.teacherMeta, Object.assign({}, fOpts, { collectedAmount: coll }));
      const hasCustomWage = m.customWage !== null && m.customWage !== undefined && m.customWage !== '';
      const hasEntry = (Number(v) > 0) || wage > 0 || hasCustomWage;
      const isClosed = Boolean(state.closedMonths && state.closedMonths[mKey]);

      const statusBadge = paySt === 'paid'
        ? html`<span class="badge badge-end" title="مسدد بالكامل">${money(coll)}</span>`
        : (paySt === 'partial'
          ? html`<span class="badge badge-warn" title="مسدد جزئيًا">${money(coll)}</span>`
          : (paySt === 'unpaid' ? html`<span class="badge badge-unpaid">0</span>` : html`<span>—</span>`));

      return html`
        <td>
          <input type="number" class="cell-input stu-inline-input" data-stu-id="${s.id}" data-mk="${mKey}" data-field="value" value="${v}" placeholder="—" ${isClosed ? 'disabled' : ''}>
        </td>
        <td>${statusBadge}</td>
        <td>
          <input type="date" class="cell-input cell-pay stu-inline-input" title="تاريخ الدفع الفعلي (مالي مستقل)" data-stu-id="${s.id}" data-mk="${mKey}" data-field="paymentDate" value="${payDt}" ${isClosed ? 'disabled' : ''}>
        </td>
        <td>
          <input type="date" class="cell-input cell-start stu-inline-input" title="تاريخ البداية الفعلي للدراسة (دراسي مستقل)" data-stu-id="${s.id}" data-mk="${mKey}" data-field="actualStartDate" value="${startDt}" ${isClosed ? 'disabled' : ''}>
        </td>
        <td>
          <input type="date" class="cell-input cell-end stu-inline-input" title="تاريخ نهاية الاشتراك" data-stu-id="${s.id}" data-mk="${mKey}" data-field="endDate" value="${endDt}" ${isClosed ? 'disabled' : ''}>
        </td>
        <td>
          <div style="display:flex;align-items:center;gap:3px;justify-content:center;">
            <input type="number" class="cell-input stu-inline-input" data-stu-id="${s.id}" data-mk="${mKey}" data-field="customWage" value="${hasCustomWage ? m.customWage : (hasEntry ? wage : '')}" placeholder="تلقائي" min="0" step="1" style="width:72px;text-align:center;${hasCustomWage ? 'border-color:var(--brand-primary);font-weight:700;' : ''}" title="${hasCustomWage ? 'أجر مخصص يدويًا — يمكنك تعديله أو مسحه للعودة للحساب التلقائي' : 'أجر المعلمة (قابل للتعديل المباشر بأي مبلغ دون إجبار)'}" ${isClosed ? 'disabled' : ''}>
            ${hasCustomWage && !isClosed ? html`<button type="button" class="btn btn-sm stu-reset-wage-btn" data-stu-id="${s.id}" data-mk="${mKey}" title="إلغاء الأجر المخصص والعودة للحساب التلقائي من النسبة" style="padding:2px 5px;font-size:11px;">↺</button>` : ''}
          </div>
        </td>
      `;
    });

    const isPrimaryClosed = Boolean(state.closedMonths && state.closedMonths[mk]);
    return html`
      <tr data-student-row="${s.id}">
        <td>${idx + 1}</td>
        <td style="text-align:right;font-weight:700;">
          <a href="#/profile/${s.id}" class="stu-profile-link" data-stu-id="${s.id}" style="color:var(--brand-primary);text-decoration:none;">${s.name}</a>
        </td>
        <td dir="ltr">${s.phone || '—'}</td>
        <td>${s.teacher || 'بدون معلمة'}</td>
        <td>${s.type || '—'}</td>
        <td>
          <div style="display:flex;align-items:center;gap:2px;justify-content:center;">
            <input type="number" class="cell-input stu-inline-input" data-stu-id="${s.id}" data-mk="${mk}" data-field="pct" value="${FE.parsePctInt(s.pct, 0)}" min="0" max="100" step="1" style="width:58px;text-align:center;" title="نسبة المعلمة للطالبة (0..100% — قابلة للتعديل المباشر)" ${isPrimaryClosed ? 'disabled' : ''}>
            <span style="font-size:11px;color:var(--text-muted);">%</span>
          </div>
        </td>
        ${monthCells}
        <td>
          <div style="display:flex;gap:4px;justify-content:center;">
            <button type="button" class="btn btn-sm stu-edit-btn" data-stu-id="${s.id}" title="تعديل">✏️</button>
            <button type="button" class="btn btn-sm stu-view-btn" data-stu-id="${s.id}" title="ملف 360">🪪</button>
            <button type="button" class="btn btn-sm stu-del-btn" data-stu-id="${s.id}" title="حذف الطالبة">🗑️</button>
          </div>
        </td>
      </tr>
    `;
  });

  const footCols = monthKeys.map((mKey) => {
    const agg = FE.computeMonthAggregate(list, mKey, state.installments, state.teacherMeta, fOpts);
    return html`
      <td>${money(agg.value)}</td>
      <td>${money(agg.collected)}</td>
      <td>مسدد: ${agg.paidCount}</td>
      <td>بدأت: ${agg.activeStudyCount}</td>
      <td>—</td>
      <td>${money(agg.wages)}</td>
    `;
  });

  renderHtml('#studentsTableWrap', html`
    <table class="data-table">
      <thead>
        <tr>
          <th rowspan="2">م</th>
          <th rowspan="2">اسم الطالبة</th>
          <th rowspan="2">رقم التواصل</th>
          <th rowspan="2">المعلمة</th>
          <th rowspan="2">نوع الاشتراك</th>
          <th rowspan="2">النسبة</th>
          ${headBanners}
          <th rowspan="2">إجراءات</th>
        </tr>
        <tr>${headSubCols}</tr>
      </thead>
      <tbody>${bodyRows}</tbody>
      <tfoot>
        <tr>
          <td colspan="6">الإجمالي (${list.length} طالبة)</td>
          ${footCols}
          <td></td>
        </tr>
      </tfoot>
    </table>
  `);
}

/* ==========================================================================
   3. RENDER STUDENT 360° PROFILE
   ========================================================================== */
function renderProfile() {
  const sid = uiState.selectedProfileId || (state.students[0] && state.students[0].id);
  const stu = state.students.find((s) => s.id === sid) || state.students[0];
  const box = $('#studentProfileContainer');
  if (!box || !stu) {
    if (box) renderHtml(box, html`<div class="card">لا توجد طالبة محددة حاليًا.</div>`);
    return;
  }

  const mk = uiState.selectedMonth;
  const fOpts = getFinanceOptions();
  const m = (stu.months && stu.months[mk]) || {};
  const ev = FE.evaluateStudentMonthDates(m, uiState.referenceDate, fOpts.durationDays, fOpts.expiringSoonDays);
  const instMap = FE.indexInstallments(state.installments);
  const netSub = FE.effectiveMonthNetSubscription(m);
  const collVal = FE.effectiveMonthCollected(m, instMap[`${stu.id}|${mk}`], uiState.referenceDate);
  const wageVal = FE.effectiveMonthWage(stu, m, state.teacherMeta, Object.assign({}, fOpts, { collectedAmount: collVal }));

  let timingBadge = html`<span class="badge badge-unpaid">غير مسدد بعد</span>`;
  if (ev.paymentDate || collVal > 0) {
    if (ev.paidInAdvance) {
      timingBadge = html`<span class="badge badge-pay">✓ دفع مسبق قبل بدء الدراسة (${ev.paymentDate} ← ${ev.actualStartDate})</span>`;
    } else if (ev.paidAfterStart) {
      timingBadge = html`<span class="badge badge-start">✓ دفع لاحق بعد بدء الدراسة (${ev.actualStartDate} ← ${ev.paymentDate})</span>`;
    } else {
      timingBadge = html`<span class="badge badge-end">✓ مسدد بتاريخ ${ev.paymentDate || '—'}</span>`;
    }
  }

  const monthRows = Object.keys(stu.months || {}).sort().map((mKey) => {
    const mObj = stu.months[mKey] || {};
    const mEv = FE.evaluateStudentMonthDates(mObj, uiState.referenceDate, fOpts.durationDays, fOpts.expiringSoonDays);
    const v = FE.effectiveMonthNetSubscription(mObj);
    const c = FE.effectiveMonthCollected(mObj, instMap[`${stu.id}|${mKey}`], uiState.referenceDate);
    const w = FE.effectiveMonthWage(stu, mObj, state.teacherMeta, Object.assign({}, fOpts, { collectedAmount: c }));
    return html`
      <tr>
        <td><b>${monthLabel(mKey)}</b></td>
        <td>${money(v)}</td>
        <td>${money(c)}</td>
        <td><span class="badge badge-pay">${mEv.paymentDate || 'غير مسجل'}</span></td>
        <td><span class="badge badge-start">${mEv.actualStartDate || 'غير مسجل'}</span></td>
        <td><span class="badge badge-end">${mEv.endDate || '—'}</span></td>
        <td>${mEv.studyPeriod.durationDays ? `${mEv.studyPeriod.durationDays} يوم` : '—'}</td>
        <td>${money(w)}</td>
      </tr>
    `;
  });

  renderHtml(box, html`
    <div class="kpi-grid">
      <div class="kpi-card">
        <div class="kpi-label">الطالبة والكود</div>
        <div class="kpi-value" style="font-size:20px;">${stu.name}</div>
        <div class="kpi-sub">الكود: <b>${stu.code}</b> | الهاتف: <span dir="ltr">${stu.phone || '—'}</span></div>
      </div>
      <div class="kpi-card kpi-gold">
        <div class="kpi-label">المعلمة والباقة</div>
        <div class="kpi-value" style="font-size:19px;">${stu.teacher || 'بدون معلمة'}</div>
        <div class="kpi-sub">النظام: <b>${stu.type || '—'}</b> | أجر المعلمة: <b>${money(wageVal)}</b></div>
      </div>
      <div class="kpi-card kpi-pay">
        <div class="kpi-label">💳 تاريخ الدفع المالي (${monthLabel(mk)})</div>
        <div class="kpi-value" style="font-size:20px;color:var(--pay-text);">${ev.paymentDate || 'لم يُسدَّد بعد'}</div>
        <div class="kpi-sub">المحصّل: <b>${money(collVal)}</b> من صافي <b>${money(netSub)}</b></div>
      </div>
      <div class="kpi-card kpi-start">
        <div class="kpi-label">📅 تاريخ البداية الفعلي للدراسة (${monthLabel(mk)})</div>
        <div class="kpi-value" style="font-size:20px;color:var(--start-text);">${ev.actualStartDate || 'لم يُحدَّد بعد'}</div>
        <div class="kpi-sub">تاريخ النهاية: <b>${ev.endDate || '—'}</b></div>
      </div>
    </div>
    <div class="card">
      <div class="card-header">
        <h3>📊 السجل المالي والدراسي للطالبة عبر الشهور (فصل تام بين تاريخ الدفع وتاريخ البداية الفعلي)</h3>
        <div>${timingBadge}</div>
      </div>
      <div class="table-wrap">
        <table class="data-table">
          <thead>
            <tr>
              <th>الشهر</th><th>صافي الاشتراك</th><th>المحصّل الفعلي</th>
              <th class="th-pay">💳 تاريخ الدفع (Payment Date)</th>
              <th class="th-start">📅 تاريخ البداية الفعلي (Actual Start)</th>
              <th class="th-end">🏁 تاريخ نهاية الدورة</th>
              <th>مدة الدراسة المحسوبة</th><th>أجر المعلمة</th>
            </tr>
          </thead>
          <tbody>${monthRows.length ? monthRows : html`<tr><td colspan="8">لا توجد شهور مسجلة</td></tr>`}</tbody>
        </table>
      </div>
    </div>
  `);
}

/* ==========================================================================
   4. RENDER COLLECTIONS & DATES HUB
   ========================================================================== */
function renderCollections() {
  const mk = uiState.selectedMonth;
  const fromPay = ($('#collFromPayDate') && $('#collFromPayDate').value) || '';
  const toPay = ($('#collToPayDate') && $('#collToPayDate').value) || '';
  const timingFilter = ($('#collTimingFilter') && $('#collTimingFilter').value) || '';
  const collTeacher = ($('#collTeacherFilter') && $('#collTeacherFilter').value) || '';
  const basis = ($('#collBasisSelect') && $('#collBasisSelect').value) || 'accrual';
  const instMap = FE.indexInstallments(state.installments);

  const rows = [];
  let totalSub = 0, totalColl = 0, advCount = 0, defCount = 0;

  state.students.forEach((s) => {
    if (collTeacher) {
      if (collTeacher === '__unassigned__') {
        if (String(s.teacher || '').trim() !== '') return;
      } else if (s.teacher !== collTeacher) {
        return;
      }
    }
    const m = (s.months && s.months[mk]) || {};
    const netSub = FE.effectiveMonthNetSubscription(m);
    const payDt = FE.resolvePaymentDate(m);
    const startDt = FE.resolveActualStartDate(m);
    const endDt = FE.resolveEndDate(m);
    const instList = instMap[`${s.id}|${mk}`] || [];
    const coll = FE.effectiveMonthCollected(m, instList, uiState.referenceDate);
    if (netSub <= 0 && coll <= 0 && !payDt && !startDt) return;

    if (fromPay && (!payDt || payDt < fromPay)) return;
    if (toPay && (!payDt || payDt > toPay)) return;

    let timing = 'unpaid';
    if (coll > 0) {
      if (payDt && startDt) {
        if (payDt < startDt) timing = 'paid_in_advance';
        else if (payDt > startDt) timing = 'paid_after_start';
        else timing = 'paid_on_start';
      } else {
        timing = 'paid_on_start';
      }
    }

    if (timingFilter && timing !== timingFilter) return;

    if (timing === 'paid_in_advance') advCount++;
    if (timing === 'paid_after_start') defCount++;
    totalSub += netSub;
    totalColl += coll;

    rows.push({
      s,
      netSub,
      coll,
      rem: Math.max(0, netSub - coll),
      payDt,
      startDt,
      endDt,
      timing
    });
  });

  // Cash basis total for selected month
  let cashTotalForMonth = 0;
  state.installments.forEach((inst) => {
    const pd = String(inst.paymentDate || inst.date || '');
    if (pd && pd <= uiState.referenceDate && pd.slice(0, 7) === mk) {
      cashTotalForMonth += FE.roundHalfUp(inst.amount || 0);
    }
  });

  renderHtml('#collectionsKpis', html`
    <div class="kpi-card kpi-gold">
      <div class="kpi-label">إجمالي صافي المستحق (${monthLabel(mk)})</div>
      <div class="kpi-value">${money(totalSub)}</div>
    </div>
    <div class="kpi-card kpi-pay">
      <div class="kpi-label">المحصّل (${basis === 'cash' ? 'الأساس النقدي حسب تاريخ الإيصال' : 'أساس الاستحقاق للشهر'})</div>
      <div class="kpi-value">${money(basis === 'cash' ? cashTotalForMonth : totalColl)}</div>
      <div class="kpi-sub">استحقاق: <b>${money(totalColl)}</b> | نقدي بالشهر: <b>${money(cashTotalForMonth)}</b></div>
    </div>
    <div class="kpi-card kpi-danger">
      <div class="kpi-label">المتبقي للتحصيل</div>
      <div class="kpi-value">${money(Math.max(0, totalSub - totalColl))}</div>
    </div>
    <div class="kpi-card kpi-start">
      <div class="kpi-label">دفع مسبق / دفع بعد البدء</div>
      <div class="kpi-value">${advCount} مسبق / ${defCount} لاحق</div>
    </div>
  `);

  const timingLabels = {
    paid_in_advance: html`<span class="badge badge-pay">دفع مسبق قبل البدء</span>`,
    paid_on_start: html`<span class="badge badge-end">مسدد في الموعد</span>`,
    paid_after_start: html`<span class="badge badge-start">دفع لاحق بعد البدء</span>`,
    unpaid: html`<span class="badge badge-unpaid">غير مسدد</span>`
  };

  renderHtml('#collectionsTableWrap', html`
    <table class="data-table">
      <thead>
        <tr>
          <th>م</th><th>الطالبة</th><th>المعلمة</th><th>صافي الاشتراك</th><th>المحصّل</th><th>المتبقي</th>
          <th class="th-pay">💳 تاريخ الدفع (Payment Date)</th>
          <th class="th-start">📅 تاريخ البداية الفعلي (Actual Start)</th>
          <th class="th-end">🏁 تاريخ النهاية</th><th>تصنيف التوقيت</th>
        </tr>
      </thead>
      <tbody>
        ${rows.map((r, i) => html`
          <tr>
            <td>${i + 1}</td>
            <td style="text-align:right;font-weight:700;">${r.s.name}</td>
            <td>${r.s.teacher || '—'}</td>
            <td>${money(r.netSub)}</td>
            <td>${money(r.coll)}</td>
            <td>${money(r.rem)}</td>
            <td><span class="badge badge-pay">${r.payDt || '—'}</span></td>
            <td><span class="badge badge-start">${r.startDt || '—'}</span></td>
            <td><span class="badge badge-end">${r.endDt || '—'}</span></td>
            <td>${timingLabels[r.timing] || ''}</td>
          </tr>
        `)}
      </tbody>
    </table>
  `);

  const stuById = {};
  state.students.forEach((s) => { stuById[s.id] = s; });
  const instRows = state.installments.slice().reverse();
  renderHtml('#installmentsTableWrap', html`
    <table class="data-table">
      <thead>
        <tr>
          <th>رقم الإيصال</th><th>الطالبة</th><th>الشهر</th><th>المبلغ المسدد</th>
          <th class="th-pay">💳 تاريخ الدفع</th><th>وسيلة الدفع</th><th>ملاحظة</th><th>إجراءات</th>
        </tr>
      </thead>
      <tbody>
        ${instRows.length ? instRows.map((inst) => {
          const stObj = stuById[inst.studentId] || {};
          return html`
            <tr data-row-id="${inst.id}">
              <td><b>${inst.receiptNo}</b></td>
              <td style="text-align:right;font-weight:700;">${stObj.name || inst.studentId}</td>
              <td>${monthLabel(inst.month)}</td>
              <td>${money(inst.amount)}</td>
              <td><span class="badge badge-pay">${inst.paymentDate || inst.date || '—'}</span></td>
              <td>${inst.method || '—'}</td>
              <td>${inst.note || '—'}</td>
              <td>
                <div style="display:flex;gap:4px;justify-content:center;">
                  <button type="button" class="btn btn-sm btn-print-receipt" data-inst-id="${inst.id}" title="تحميل إيصال HTML للطباعة">🖨️ إيصال</button>
                  <button type="button" class="btn btn-sm btn-del-installment" data-inst-id="${inst.id}" title="حذف الإيصال">🗑️</button>
                </div>
              </td>
            </tr>
          `;
        }) : html`<tr><td colspan="8">لا توجد إيصالات مسجلة بعد.</td></tr>`}
      </tbody>
    </table>
  `);

  renderDatesSimulator();
}

function renderDatesSimulator() {
  const simPayEl = $('#simPayDate');
  const simStartEl = $('#simActualStart');
  if (simPayEl && !simPayEl.value) simPayEl.value = uiState.referenceDate;
  if (simStartEl && !simStartEl.value) simStartEl.value = FE.addDaysISO(uiState.referenceDate, 5);

  const payDt = (simPayEl && simPayEl.value) || uiState.referenceDate;
  const startDt = (simStartEl && simStartEl.value) || uiState.referenceDate;
  const dur = Number(($('#simDurationDays') && $('#simDurationDays').value) || 30);
  const box = $('#simDatesResult');
  if (!box) return;

  const study = FE.calculateStudyPeriod(startDt, '', dur, uiState.referenceDate);
  const diffPayStart = (payDt && startDt) ? FE.diffDaysISO(payDt, startDt) : 0;
  let relationText = '';
  if (payDt && startDt) {
    if (diffPayStart > 0) {
      relationText = `دفعت الطالبة الرسوم مسبقًا قبل بدء الدراسة الفعلي بـ ${diffPayStart} أيام (الدفع: ${payDt} | بدء الدراسة: ${startDt}).`;
    } else if (diffPayStart < 0) {
      relationText = `بدأت الطالبة الدراسة الفعلي قبل سداد الرسوم بـ ${Math.abs(diffPayStart)} أيام.`;
    } else {
      relationText = 'تاريخ الدفع وتاريخ البداية الفعلي في نفس اليوم ولكن يُحفظ كل حقل بشكل مستقل.';
    }
  }

  renderHtml(box, html`
    <div class="dates-rule-banner" style="margin:0;">
      <div>
        <div>💳 <b>تاريخ القيد المالي (Payment Date):</b> <span class="badge badge-pay">${payDt || 'غير مسدد'}</span></div>
        <div style="margin-top:4px;">📅 <b>فترة الدراسة الفعلية (Actual Start → End):</b> <span class="badge badge-start">${startDt}</span> إلى <span class="badge badge-end">${study.calculatedEndDate}</span> (${study.durationDays} يومًا)</div>
        <div style="margin-top:4px;">ℹ️ ${relationText}</div>
      </div>
    </div>
  `);
}

/* ==========================================================================
   5. RENDER PAYROLL, ACCOUNTING, ATTENDANCE, MEMORIZATION, MADRASA, CERTS
   ========================================================================== */
async function renderPayrollPreparation() {
  const wrap = $('#payrollPrepWrap');
  const banner = $('#payrollPrepPolicyBanner');
  if (!wrap) return;
  const mk = uiState.selectedMonth;
  try {
    const prep = await apiRequest(`/payroll/preparation?month=${encodeURIComponent(mk)}&reference_date=${encodeURIComponent(uiState.referenceDate)}`);
    if (!prep || !Array.isArray(prep.teachers)) return;
    const pol = prep.calculationPolicy || {};
    if (banner) {
      renderHtml(banner, html`
        <div class="dates-rule-banner" style="margin:0;">
          <div>
            <strong>⚙️ سياسة الاحتساب المعتمدة (${monthLabel(mk)}):</strong>
            أساس الأجر: <b>${pol.wageBasis === 'gross' ? 'إجمالي الاشتراك قبل الخصم (Gross)' : 'صافي الاشتراك بعد الخصم (Net)'}</b> |
            الاستحقاق: <b>${pol.wageAccrualMode === 'collected' ? 'من المحصّل فعليًا (Collected)' : 'بمجرد تسجيل الاشتراك (Accrued)'}</b> |
            النسبة الافتراضية: <b>${pol.defaultTeacherPct ?? 70}%</b>
            ${prep.unassignedSummary && prep.unassignedSummary.count > 0 ? html`
              <br><span style="color:#dc2626;font-weight:700;">⚠️ تنبيه: توجد ${prep.unassignedSummary.count} طالبة نشطة بدون معلمة معينة (صافي اشتراكات: ${money(prep.unassignedSummary.netSubscriptions)}).</span>
            ` : ''}
          </div>
        </div>
      `);
    }
    renderHtml(wrap, html`
      <table class="data-table">
        <thead>
          <tr>
            <th>المعلمة</th>
            <th>النسبة</th>
            <th>الطالبات</th>
            <th>صافي الاشتراكات</th>
            <th>المحصّل</th>
            <th>الأجر المستحق</th>
            <th>المصروف</th>
            <th>المتبقي للصرف</th>
            <th>التغير الشهري</th>
            <th>الحالة والملاحظات</th>
            <th>تجهيز نموذج الصرف</th>
          </tr>
        </thead>
        <tbody>
          ${prep.teachers.length ? prep.teachers.map((r) => {
            const chgText = r.momChangePct !== null && r.momChangePct !== undefined
              ? `${r.momChangePct > 0 ? '+' : ''}${r.momChangePct}%`
              : '—';
            const statusBadge = r.status === 'settled'
              ? html`<span class="badge badge-end">✓ مسوى بالكامل</span>`
              : (r.status === 'overpaid'
                ? html`<span class="badge badge-unpaid">⚠️ زيادة صرف</span>`
                : (r.status === 'needs_review'
                  ? html`<span class="badge badge-warn">🔍 يحتاج مراجعة</span>`
                  : html`<span class="badge badge-pay">جاهز للمراجعة</span>`));
            const warnNotes = [].concat(r.missingInputs || [], r.warnings || []);
            return html`
              <tr>
                <td style="text-align:right;font-weight:700;">${r.teacher}${r.archived ? ' (مؤرشفة)' : ''}</td>
                <td>${r.configuredPct}%</td>
                <td>${r.activeStudentCount}</td>
                <td>${money(r.netSubscriptions)}</td>
                <td>${money(r.collectedAmount)}</td>
                <td><span class="badge badge-wage">${money(r.accruedWage)}</span></td>
                <td>${money(r.paidAmount)}</td>
                <td><b>${money(r.remainingBalance)}</b></td>
                <td>${r.unusualChange ? html`<span class="badge badge-warn">${chgText}</span>` : chgText}</td>
                <td style="text-align:right;font-size:12px;">
                  ${statusBadge}
                  ${warnNotes.length ? html`<div style="color:var(--text-secondary);margin-top:2px;">${warnNotes.join(' • ')}</div>` : ''}
                </td>
                <td>
                  ${r.remainingBalance > 0 ? html`
                    <button type="button" class="btn btn-sm btn-primary btn-prefill-payout"
                      data-teacher="${r.teacher}"
                      data-amount="${r.remainingBalance}">
                      ⚡ تعبئة نموذج الصرف
                    </button>
                  ` : html`<span>—</span>`}
                </td>
              </tr>
            `;
          }) : html`<tr><td colspan="11">لا توجد بيانات رواتب لهذا الشهر.</td></tr>`}
        </tbody>
      </table>
    `);
  } catch (err) {}
}

function renderPayroll() {
  renderComparativeTeachersTable('#payrollComparativeWrap');
  renderPayrollPreparation();
  const list = state.payouts.slice().reverse();
  renderHtml('#payoutsHistoryWrap', html`
    <table class="data-table">
      <thead>
        <tr><th>التاريخ</th><th>المعلمة</th><th>الشهر</th><th>المبلغ</th><th>ملاحظة</th><th>إجراء</th></tr>
      </thead>
      <tbody>
        ${list.length ? list.map((p) => html`
          <tr>
            <td>${p.date}</td>
            <td><b>${p.teacher}</b></td>
            <td>${monthLabel(p.month)}</td>
            <td>${money(p.amount)}</td>
            <td>${p.note || '—'}</td>
            <td><button type="button" class="btn btn-sm btn-del-payout" data-payout-id="${p.id}" title="حذف">🗑️</button></td>
          </tr>
        `) : html`<tr><td colspan="6">لا توجد دفعات مسجلة بعد.</td></tr>`}
      </tbody>
    </table>
  `);
}

function renderAccounting() {
  const acc = FE.computeAccountingSummary(state, '__all__', getFinanceOptions());
  renderHtml('#accountingKpis', html`
    <div class="kpi-card kpi-pay">
      <div class="kpi-label">إجمالي التدفقات الداخلة (أساس نقدي)</div>
      <div class="kpi-value">${money(acc.totalCashIn)}</div>
      <div class="kpi-sub">تحصيل إيصالات: ${money(acc.collectedCash)} + إيرادات إضافية: ${money(acc.extraRevenue)}</div>
    </div>
    <div class="kpi-card kpi-danger">
      <div class="kpi-label">إجمالي التدفقات الخارجة (أساس نقدي)</div>
      <div class="kpi-value">${money(acc.totalCashOut)}</div>
      <div class="kpi-sub">رواتب مصروفة: ${money(acc.salaryPaid)} + مصروفات: ${money(acc.operatingExpense)}</div>
    </div>
    <div class="kpi-card kpi-wage">
      <div class="kpi-label">صافي رصيد الخزينة النقدي</div>
      <div class="kpi-value">${money(acc.netCashBalance)}</div>
      <div class="kpi-sub">متبقي للمعلمات غير مصروف: ${money(acc.teacherUnpaidBalance)}</div>
    </div>
    <div class="kpi-card kpi-gold">
      <div class="kpi-label">صافي الربح التشغيلي (أساس استحقاق)</div>
      <div class="kpi-value">${money(acc.netOperatingProfit)}</div>
      <div class="kpi-sub">صافي الاشتراكات: ${money(acc.subscriptions)} (الخصومات: ${money(acc.discounts)})</div>
    </div>
  `);

  const txs = state.transactions.slice().reverse();
  renderHtml('#transactionsTableWrap', html`
    <table class="data-table">
      <thead>
        <tr><th>التاريخ</th><th>النوع</th><th>البند</th><th>المبلغ</th><th>الوسيلة</th><th>ملاحظة</th><th>إجراء</th></tr>
      </thead>
      <tbody>
        ${txs.length ? txs.map((tx) => html`
          <tr>
            <td>${tx.date}</td>
            <td>${tx.type === 'revenue'
              ? html`<span class="badge badge-end">إيراد (+)</span>`
              : html`<span class="badge badge-unpaid">مصروف (-)</span>`}</td>
            <td><b>${tx.category}</b></td>
            <td>${money(tx.amount)}</td>
            <td>${tx.method || 'نقدي'}</td>
            <td>${tx.note || '—'}</td>
            <td><button type="button" class="btn btn-sm btn-del-tx" data-tx-id="${tx.id}" title="حذف القيد">🗑️</button></td>
          </tr>
        `) : html`<tr><td colspan="7">لا توجد قيود إضافية مسجلة بعد.</td></tr>`}
      </tbody>
    </table>
  `);
}

function renderAttendance() {
  const stuById = {};
  state.students.forEach((s) => { stuById[s.id] = s; });

  const planStuSel = $('#planStudentSelect');
  const planStartInput = $('#planActualStartInput');
  if (planStuSel && planStartInput && !planStartInput.value) {
    const selStu = stuById[planStuSel.value] || state.students[0];
    const mObj = selStu && selStu.months && selStu.months[uiState.selectedMonth];
    planStartInput.value = FE.resolveActualStartDate(mObj) || uiState.referenceDate;
  }

  const attTeacher = ($('#attTeacherFilter') && $('#attTeacherFilter').value) || '';
  const sessRows = state.sessions.filter((s) => {
    if (!attTeacher) return true;
    const tName = s.teacher || (stuById[s.studentId] && stuById[s.studentId].teacher) || '';
    if (attTeacher === '__unassigned__') return !String(tName).trim();
    return tName === attTeacher;
  }).sort((a, b) => String(a.date).localeCompare(String(b.date)));
  const compensatedSet = {};
  state.sessions.forEach((s) => {
    if (s.compensatedFor) compensatedSet[s.compensatedFor] = true;
  });

  renderHtml('#sessionsTableWrap', html`
    <table class="data-table">
      <thead>
        <tr><th>التاريخ</th><th>الوقت</th><th>الطالبة</th><th>المعلمة</th><th>الحالة</th><th>إجراءات</th></tr>
      </thead>
      <tbody>
        ${sessRows.length ? sessRows.map((s) => {
          const stName = (stuById[s.studentId] && stuById[s.studentId].name) || s.studentId;
          const curStatus = s.status || 'scheduled';
          const alreadyComp = Boolean(compensatedSet[s.id] || s.status === 'makeup');
          return html`
            <tr data-row-id="${s.id}">
              <td><b>${s.date}</b></td>
              <td>${s.time}</td>
              <td>${stName}</td>
              <td>${s.teacher || '—'}</td>
              <td>
                <select class="cell-input sess-status-select" data-sess-id="${s.id}" style="min-width:130px;">
                  ${SESSION_STATUS_OPTIONS.map((opt) => html`
                    <option value="${opt.val}" ${opt.val === curStatus ? 'selected' : ''}>${opt.label}</option>
                  `)}
                </select>
              </td>
              <td>
                <div style="display:flex;gap:4px;justify-content:center;">
                  <button type="button" class="btn btn-sm btn-compensate-session" data-sess-id="${s.id}" ${alreadyComp ? 'disabled' : ''}>🔄 إلغاء معلمة وتعويض تلقائي</button>
                  <button type="button" class="btn btn-sm btn-del-session" data-sess-id="${s.id}" title="حذف الحصة">🗑️</button>
                </div>
              </td>
            </tr>
          `;
        }) : html`<tr><td colspan="6">لا توجد حصص مجدولة بعد — استخدم نموذج توليد الخطة الذكية على اليمين.</td></tr>`}
      </tbody>
    </table>
  `);

  const mk = uiState.selectedMonth;
  const activeList = state.students.filter((s) => {
    if (attTeacher) {
      if (attTeacher === '__unassigned__') {
        if (String(s.teacher || '').trim() !== '') return false;
      } else if (s.teacher !== attTeacher) {
        return false;
      }
    }
    const m = s.months && s.months[mk];
    return m && (FE.effectiveMonthNetSubscription(m) > 0 || FE.resolveActualStartDate(m));
  });

  renderHtml('#weeklyAttendanceTableWrap', html`
    <table class="data-table">
      <thead>
        <tr>
          <th>م</th><th>الطالبة</th><th>المعلمة</th>
          <th class="th-start">تاريخ البداية الفعلي</th>
          <th class="th-end">تاريخ النهاية</th>
          <th>الأسبوع 1</th><th>الأسبوع 2</th><th>الأسبوع 3</th><th>الأسبوع 4</th>
        </tr>
      </thead>
      <tbody>
        ${activeList.map((s, idx) => {
          const m = (s.months && s.months[mk]) || {};
          const weeks = (m.att && Array.isArray(m.att.weeks)) ? m.att.weeks : [['', '', ''], ['', '', ''], ['', '', ''], ['', '', '']];
          return html`
            <tr>
              <td>${idx + 1}</td>
              <td style="text-align:right;font-weight:700;">${s.name}</td>
              <td>${s.teacher || '—'}</td>
              <td><span class="badge badge-start">${FE.resolveActualStartDate(m) || '—'}</span></td>
              <td><span class="badge badge-end">${FE.resolveEndDate(m) || '—'}</span></td>
              ${[0, 1, 2, 3].map((wIdx) => {
                const wArr = weeks[wIdx] || ['', '', ''];
                const doneCnt = wArr.filter((x) => x === '✓' || x === 'ح').length;
                return html`
                  <td>
                    <button type="button" class="btn btn-sm btn-toggle-week" data-stu-id="${s.id}" data-week="${wIdx}">
                      ${doneCnt > 0 ? `✓ حاضرتان (${doneCnt})` : 'تسجيل حضور'}
                    </button>
                  </td>
                `;
              })}
            </tr>
          `;
        })}
      </tbody>
    </table>
  `);
}

function renderMemorization() {
  const stuById = {};
  state.students.forEach((s) => { stuById[s.id] = s; });
  const memTeacher = ($('#memTeacherFilter') && $('#memTeacherFilter').value) || '';
  const list = state.memorization.filter((m) => {
    if (!memTeacher) return true;
    const stu = stuById[m.studentId] || {};
    const tName = m.teacher || stu.teacher || '';
    if (memTeacher === '__unassigned__') return !String(tName).trim();
    return tName === memTeacher;
  }).slice().reverse();
  renderHtml('#memorizationTableWrap', html`
    <table class="data-table">
      <thead>
        <tr><th>التاريخ</th><th>الطالبة</th><th>المعلمة</th><th>المسار</th><th>السورة</th><th>الأوجه</th><th>الدرجة</th><th>ملاحظات</th><th>إجراء</th></tr>
      </thead>
      <tbody>
        ${list.length ? list.map((m) => {
          const stu = stuById[m.studentId] || {};
          return html`
            <tr>
              <td>${m.date}</td>
              <td><b>${stu.name || m.studentId}</b></td>
              <td>${m.teacher || stu.teacher || '—'}</td>
              <td>${m.track}</td>
              <td>${m.fromSurah}</td>
              <td>${m.pages}</td>
              <td><span class="badge badge-end">${m.score}/100</span></td>
              <td>${m.notes || '—'}</td>
              <td><button type="button" class="btn btn-sm btn-del-mem" data-mem-id="${m.id}" title="حذف">🗑️</button></td>
            </tr>
          `;
        }) : html`<tr><td colspan="9">لا توجد سجلات تسميع مضافة بعد.</td></tr>`}
      </tbody>
    </table>
  `);
}

function renderMadrasa() {
  const stuById = {};
  state.students.forEach((s) => { stuById[s.id] = s; });
  const list = state.madrasaEnrollments;
  renderHtml('#madrasaTableWrap', html`
    <table class="data-table">
      <thead>
        <tr><th>الطالبة</th><th>الرسوم المسددة</th><th>🔥 أيام الحماسة</th><th>الأوسمة</th><th>إجراءات</th></tr>
      </thead>
      <tbody>
        ${list.length ? list.map((e) => {
          const stu = stuById[e.studentId] || {};
          return html`
            <tr>
              <td><b>${stu.name || e.studentId}</b></td>
              <td>${money(e.feePaid)}</td>
              <td>🔥 ${e.streak || 1} يوم</td>
              <td>${(e.badges || []).join(' ، ') || '🌟 نجمة المواظبة'}</td>
              <td>
                <div style="display:flex;gap:4px;justify-content:center;">
                  <button type="button" class="btn btn-sm btn-bump-streak" data-enr-id="${e.id}" title="تسجيل يوم حماسة جديد">+1 🔥</button>
                  <button type="button" class="btn btn-sm btn-del-madrasa" data-enr-id="${e.id}" title="إلغاء الانتساب">🗑️</button>
                </div>
              </td>
            </tr>
          `;
        }) : html`<tr><td colspan="5">لا توجد طالبات مسجلات في الدفعة بعد.</td></tr>`}
      </tbody>
    </table>
  `);
}

function drawCertificatePreview() {
  const canvas = $('#certCanvas');
  if (!canvas || !canvas.getContext) return;
  const ctx = canvas.getContext('2d');
  const stuSel = $('#certStudentSelect');
  const stu = state.students.find((s) => s.id === (stuSel && stuSel.value)) || state.students[0];
  const stuName = stu ? stu.name : 'اسم الطالبة';
  const title = ($('#certTitleInput') && $('#certTitleInput').value) || 'إتمام حفظ ومراجعة جزء عمّ';
  const grade = ($('#certGradeInput') && $('#certGradeInput').value) || 'ممتاز متقن';
  const dt = ($('#certDateInput') && $('#certDateInput').value) || uiState.referenceDate;
  const acadName = (state.settings && state.settings.academyName) || 'أكاديمية يوم جديد';

  ctx.fillStyle = '#fefcf6';
  ctx.fillRect(0, 0, canvas.width, canvas.height);

  ctx.strokeStyle = '#556a4f';
  ctx.lineWidth = 10;
  ctx.strokeRect(20, 20, canvas.width - 40, canvas.height - 40);

  ctx.strokeStyle = '#b08d3c';
  ctx.lineWidth = 3;
  ctx.strokeRect(32, 32, canvas.width - 64, canvas.height - 64);

  ctx.textAlign = 'center';
  ctx.fillStyle = '#556a4f';
  ctx.font = 'bold 25px Cairo, sans-serif';
  ctx.fillText(acadName, canvas.width / 2, 95);

  ctx.fillStyle = '#b45309';
  ctx.font = 'bold 34px Cairo, sans-serif';
  ctx.fillText('شهادة تقدير وإتقان', canvas.width / 2, 155);

  ctx.fillStyle = '#334155';
  ctx.font = '20px Cairo, sans-serif';
  ctx.fillText('تتشرف إدارة الأكاديمية بمنح هذه الشهادة للطالبة المتقنة:', canvas.width / 2, 215);

  ctx.fillStyle = '#0f172a';
  ctx.font = 'bold 32px Cairo, sans-serif';
  ctx.fillText(stuName, canvas.width / 2, 275);

  ctx.fillStyle = '#556a4f';
  ctx.font = 'bold 22px Cairo, sans-serif';
  ctx.fillText(title, canvas.width / 2, 340);

  ctx.fillStyle = '#b45309';
  ctx.font = 'bold 20px Cairo, sans-serif';
  ctx.fillText(`التقدير: ${grade}   |   التاريخ: ${dt}`, canvas.width / 2, 405);

  ctx.fillStyle = '#64748b';
  ctx.font = '16px Cairo, sans-serif';
  ctx.fillText(`المعلمة المشرفة: ${(stu && stu.teacher) || 'إدارة الأكاديمية'}`, canvas.width / 2, 465);
}

function renderCertificates() {
  drawCertificatePreview();
  const list = state.certificates.slice().reverse();
  renderHtml('#certsTableWrap', html`
    <table class="data-table">
      <thead>
        <tr><th>رقم الشهادة</th><th>الطالبة</th><th>الدورة / الإنجاز</th><th>التقدير</th><th>تاريخ الإصدار</th><th>إجراءات</th></tr>
      </thead>
      <tbody>
        ${list.length ? list.map((c) => html`
          <tr>
            <td><b>${c.certNo}</b></td>
            <td>${c.studentName}</td>
            <td>${c.courseTitle}</td>
            <td><span class="badge badge-end">${c.grade}</span></td>
            <td>${c.issueDate}</td>
            <td>
              <div style="display:flex;gap:4px;justify-content:center;">
                <button type="button" class="btn btn-sm btn-preview-cert" data-cert-id="${c.id}" title="معاينة الشهادة">🖼️</button>
                <button type="button" class="btn btn-sm btn-del-cert" data-cert-id="${c.id}" title="حذف الشهادة">🗑️</button>
              </div>
            </td>
          </tr>
        `) : html`<tr><td colspan="6">لم تصدر شهادات بعد.</td></tr>`}
      </tbody>
    </table>
  `);
}

function renderPackagesList() {
  const pkgWrap = $('#packagesListWrap');
  if (!pkgWrap) return;
  if (!state.subTypes || state.subTypes.length === 0) {
    renderHtml(pkgWrap, html`<span style="color:var(--text-muted);font-size:13px;">لا توجد باقات مضافة.</span>`);
    return;
  }
  renderHtml(pkgWrap, html`${state.subTypes.map((pkg) => {
    const usedCnt = state.students.filter((s) => s.type === pkg).length;
    return html`
      <span class="badge badge-pay" style="display:inline-flex;align-items:center;gap:6px;padding:6px 10px;font-size:12.5px;">
        <span>📦 ${pkg} (${usedCnt})</span>
        ${usedCnt === 0 ? html`
          <button type="button" class="btn-del-package" data-pkg="${pkg}" title="حذف الباقة من القوائم" style="border:0;background:transparent;cursor:pointer;color:inherit;font-weight:700;padding:0 2px;">✕</button>
        ` : ''}
      </span>
    `;
  })}`);
}

function renderTeachers() {
  renderPackagesList();
  const grid = $('#teachersCardsGrid');
  if (!grid) return;
  const mk2 = uiState.selectedMonth;
  const mk1 = getPrevMonthKey(mk2);
  const fOpts = getFinanceOptions();

  if (state.teachers.length === 0) {
    renderHtml(grid, html`<div class="card">لا توجد معلمات مضافات بعد. أضف معلمة من النموذج أعلاه.</div>`);
    return;
  }

  const q = String(($('#teacherSearchInput') && $('#teacherSearchInput').value) || '').trim().toLowerCase();
  const statusFilter = ($('#teacherStatusFilter') && $('#teacherStatusFilter').value) || 'all';

  const filteredTeachers = getAllTeachersSorted().filter((t) => {
    const archived = isTeacherArchived(t);
    if (statusFilter === 'active' && archived) return false;
    if (statusFilter === 'inactive' && !archived) return false;
    if (q && !String(t).toLowerCase().includes(q)) return false;
    return true;
  });

  if (filteredTeachers.length === 0) {
    renderHtml(grid, html`<div class="card">لا توجد معلمات مطابقة لمعايير البحث أو التصفية الحالية.</div>`);
    return;
  }

  renderHtml(grid, html`${filteredTeachers.map((t) => {
    const studs = state.students.filter((s) => s.teacher === t);
    const m1Agg = FE.computeMonthAggregate(studs, mk1, state.installments, state.teacherMeta, fOpts);
    const m2Agg = FE.computeMonthAggregate(studs, mk2, state.installments, state.teacherMeta, fOpts);
    const pct = FE.parsePctInt(state.teacherMeta[t] && state.teacherMeta[t].pct, 70);
    const archived = isTeacherArchived(t);
    return html`
      <div class="card" data-teacher-card="${t}">
        <div class="card-header">
          <h3>👩‍🏫 ${t}</h3>
          <div style="display:flex;gap:4px;flex-wrap:wrap;">
            ${archived
              ? html`<span class="badge badge-warn">غير نشطة / مؤرشفة</span>`
              : html`<span class="badge badge-end">نشطة</span>`}
            <span class="badge badge-wage">النسبة: ${pct}%</span>
          </div>
        </div>
        <p style="margin:6px 0;font-size:13px;">إجمالي الطالبات المسجلات: <b>${studs.length} طالبة</b></p>
        <p style="margin:6px 0;font-size:13px;">راتب ${monthLabel(mk1)}: <b>${money(m1Agg.wages)}</b> (${m1Agg.count} طالبة)</p>
        <p style="margin:6px 0;font-size:13px;">راتب ${monthLabel(mk2)}: <b>${money(m2Agg.wages)}</b> (${m2Agg.count} طالبة)</p>
        <div style="margin-top:12px;display:flex;gap:8px;flex-wrap:wrap;">
          <button type="button" class="btn btn-sm btn-primary btn-filter-by-teacher" data-teacher="${t}">عرض طالباتها</button>
          <button type="button" class="btn btn-sm btn-export-teacher" data-teacher="${t}">📥 إكسيل</button>
          <button type="button" class="btn btn-sm btn-export-teacher-html" data-teacher="${t}">🌐 HTML</button>
          <button type="button" class="btn btn-sm btn-edit-teacher" data-teacher="${t}" data-pct="${pct}" title="تعديل الاسم أو النسبة">✏️ تعديل</button>
          ${archived
            ? html`<button type="button" class="btn btn-sm btn-accent btn-restore-teacher" data-teacher="${t}" title="إعادة تفعيل المعلمة لإتاحتها في القوائم الجديدة">♻️ تفعيل</button>`
            : html`<button type="button" class="btn btn-sm btn-deactivate-teacher" data-teacher="${t}" title="إيقاف مؤقت / أرشفة المعلمة مع الحفاظ على سجلها التاريخي">⏸️ إيقاف</button>`}
          ${(studs.length === 0 && !archived) ? html`<button type="button" class="btn btn-sm btn-del-teacher" data-teacher="${t}" title="حذف المعلمة (تُؤرشف تلقائياً إن كان لها سجلات)">🗑️</button>` : ''}
        </div>
      </div>
    `;
  })}`);
}

export function renderAll() {
  populateSelects();
  applyBrandingAndSettingsToUI();
  renderDashboard();
  renderStudents();
  renderProfile();
  renderCollections();
  renderPayroll();
  renderAccounting();
  renderAttendance();
  renderMemorization();
  renderMadrasa();
  renderCertificates();
  renderTeachers();
  if (uiState.activeView === 'statistics') {
    renderStatisticsView();
  }
  if (uiState.activeView === 'system') {
    renderSystemHealthView();
  }
}

/* ==========================================================================
   STUDENT MODAL & EXPORT HELPERS
   ========================================================================== */
function openStudentModal(studentId) {
  const mk = uiState.selectedMonth;
  if ($('#modalMonthLabel')) $('#modalMonthLabel').textContent = monthLabel(mk);
  const stu = studentId ? state.students.find((s) => s.id === studentId) : null;
  const defPct = FE.parsePctInt(state.settings && state.settings.defaultTeacherPct, 70);

  if (stu) {
    const m = (stu.months && stu.months[mk]) || {};
    $('#studentModalTitle').textContent = `تعديل بيانات الطالبة: ${stu.name}`;
    $('#modalStudentId').value = stu.id;
    populateModalTeacherSelect(stu.teacher || '');
    $('#modalStudentName').value = stu.name;
    $('#modalStudentPhone').value = stu.phone || '';
    $('#modalStudentCode').value = stu.code || '';
    $('#modalStudentTeacher').value = stu.teacher || '';
    $('#modalStudentType').value = stu.type || state.subTypes[0] || 'خاص';
    $('#modalStudentPct').value = FE.parsePctInt(stu.pct, defPct);
    $('#modalMonthValue').value = m.value !== null && m.value !== undefined ? m.value : '';
    $('#modalMonthDiscount').value = m.discount !== null && m.discount !== undefined ? m.discount : '';
    $('#modalMonthCustomWage').value = m.customWage !== null && m.customWage !== undefined ? m.customWage : '';
    $('#modalMonthPaymentDate').value = FE.resolvePaymentDate(m);
    $('#modalMonthActualStartDate').value = FE.resolveActualStartDate(m);
    $('#modalMonthEndDate').value = FE.resolveEndDate(m);
    if ($('#modalCreateReceiptCheck')) $('#modalCreateReceiptCheck').checked = false;
    $('#btnModalDeleteStudent').style.display = 'inline-flex';
  } else {
    const nextNum = state.students.length + 1;
    const defaultActiveTeacher = getActiveTeachers()[0] || '';
    const teacherDefaultPct = (defaultActiveTeacher && state.teacherMeta[defaultActiveTeacher] && state.teacherMeta[defaultActiveTeacher].pct !== undefined)
      ? FE.parsePctInt(state.teacherMeta[defaultActiveTeacher].pct, defPct)
      : defPct;
    $('#studentModalTitle').textContent = 'إضافة طالبة جديدة';
    $('#modalStudentId').value = '';
    populateModalTeacherSelect('');
    $('#modalStudentName').value = '';
    $('#modalStudentPhone').value = '';
    $('#modalStudentCode').value = 'STU-' + String(nextNum).padStart(4, '0');
    $('#modalStudentTeacher').value = defaultActiveTeacher;
    $('#modalStudentType').value = state.subTypes[0] || 'خاص';
    $('#modalStudentPct').value = teacherDefaultPct;
    $('#modalMonthValue').value = '';
    $('#modalMonthDiscount').value = '';
    $('#modalMonthCustomWage').value = '';
    $('#modalMonthPaymentDate').value = '';
    $('#modalMonthActualStartDate').value = '';
    $('#modalMonthEndDate').value = '';
    if ($('#modalCreateReceiptCheck')) $('#modalCreateReceiptCheck').checked = true;
    $('#btnModalDeleteStudent').style.display = 'none';
  }
  $('#studentModal').classList.add('open');
}

function closeStudentModal() {
  $('#studentModal').classList.remove('open');
}

function triggerXlsxDownload(scope, teacher = '', month = '') {
  let q = `?scope=${encodeURIComponent(scope || 'full')}`;
  if (teacher) q += `&teacher=${encodeURIComponent(teacher)}`;
  if (month) q += `&month=${encodeURIComponent(month)}`;
  const defName = `YawmJadid_Academy_${scope || 'full'}_${uiState.referenceDate}.xlsx`;
  downloadBlobFromApi(`/export/xlsx${q}`, defName, `تم تحميل ملف الإكسيل (${scope || 'full'}) بنجاح.`);
}

function triggerHtmlDownload(scope, opts = {}) {
  const hideVal = opts.hidePersonal !== undefined ? String(opts.hidePersonal) : 'true';
  const params = new URLSearchParams({
    scope: scope || 'full',
    month: opts.month || uiState.selectedMonth,
    teacher: opts.teacher || '',
    student_id: opts.studentId || '',
    hide_personal_data: hideVal,
    hide_personal: hideVal,
    reference_date: uiState.referenceDate
  });
  const defName = `YawmJadid_Report_${scope || 'full'}_${uiState.referenceDate}.html`;
  downloadBlobFromApi(`/export/html?${params.toString()}`, defName, 'تم تحميل صفحة التقرير الثابتة (.html) بنجاح.');
}

function exportCsvFallback() {
  const mk = uiState.selectedMonth;
  const fOpts = getFinanceOptions();
  const instMap = FE.indexInstallmentsByStudentMonth(state.installments || []);
  const headers = [
    'م', 'كود الطالبة', 'اسم الطالبة', 'رقم التواصل', 'اسم المعلمة', 'نوع الاشتراك',
    'نسبة المعلمة', 'المواعيد', 'الشهر', 'قيمة الاشتراك', 'الخصم', 'صافي الاشتراك',
    'المبلغ المسدد', 'تاريخ الدفع', 'تاريخ البداية الفعلي', 'تاريخ النهاية', 'أجر المعلمة'
  ];
  const lines = [headers.join(',')];
  state.students.forEach((s, idx) => {
    const m = (s.months && s.months[mk]) || {};
    const grossVal = (m.value !== null && m.value !== undefined && m.value !== '') ? FE.roundHalfUp(m.value) : '';
    const discVal = (m.discount !== null && m.discount !== undefined && m.discount !== '') ? FE.roundHalfUp(m.discount) : 0;
    const netVal = FE.effectiveMonthNetSubscription(m);
    const stuInsts = instMap[`${s.id}__${mk}`] || [];
    const collVal = FE.effectiveMonthCollected(m, stuInsts, uiState.referenceDate);
    const row = [
      idx + 1,
      csvCell(s.code),
      csvCell(s.name),
      csvCell(s.phone),
      csvCell(s.teacher),
      csvCell(s.type),
      FE.parsePctInt(s.pct, 70),
      csvCell(s.schedule || ''),
      csvCell(mk),
      grossVal,
      discVal,
      netVal,
      collVal,
      csvCell(FE.resolvePaymentDate(m)),
      csvCell(FE.resolveActualStartDate(m)),
      csvCell(FE.resolveEndDate(m)),
      FE.effectiveMonthWage(s, m, state.teacherMeta, fOpts)
    ];
    lines.push(row.join(','));
  });
  const blob = new Blob(['\uFEFF' + lines.join('\n')], { type: 'text/csv;charset=utf-8' });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = `YawmJadid_Students_${mk}.csv`;
  a.click();
  showToast('تم تصدير جدول الطالبات للشهر المحدد (.csv) بنجاح.');
}

function wireMainEvents() {
  // Sidebar navigation
  $all('.nav-btn').forEach((btn) => {
    btn.addEventListener('click', () => {
      const v = btn.getAttribute('data-view');
      navigateTo(`#/${v}`);
    });
  });

  // Global month selector
  $('#globalMonthSelect').addEventListener('change', (e) => {
    uiState.selectedMonth = e.target.value;
    renderAll();
  });

  // Global search box
  $('#globalSearchInput').addEventListener('input', (e) => {
    const val = e.target.value;
    if ($('#stuFilterSearch')) $('#stuFilterSearch').value = val;
    if (uiState.activeView !== 'students' && val.trim().length > 0) {
      navigateTo('#/students');
    } else {
      renderStudents();
    }
  });

  // Theme toggle
  $('#btnToggleTheme').addEventListener('click', () => {
    const cur = document.documentElement.getAttribute('data-theme');
    const nextTheme = cur === 'dark' ? 'light' : 'dark';
    document.documentElement.setAttribute('data-theme', nextTheme);
    try { localStorage.setItem(THEME_KEY, nextTheme); } catch (e) {}
  });

  // Quick navigation & add buttons
  ['#btnQuickAddStudent', '#btnAddStudentMain', '#btnEmptyAddStudent'].forEach((sel) => {
    const btn = $(sel);
    if (btn) btn.addEventListener('click', () => openStudentModal(''));
  });
  ['#btnGoImportFromStudents', '#btnEmptyGoImport'].forEach((sel) => {
    const btn = $(sel);
    if (btn) btn.addEventListener('click', () => navigateTo('#/import'));
  });
  $('#btnDashGoStudents').addEventListener('click', () => navigateTo('#/students'));
  $('#btnDashInspectDates').addEventListener('click', () => navigateTo('#/collections'));
  $('#btnDashExportFull').addEventListener('click', () => triggerXlsxDownload('full'));
  $('#btnDashExportHtml').addEventListener('click', () => triggerHtmlDownload('full'));
  $('#btnDashExportTeachers').addEventListener('click', () => triggerXlsxDownload('teachers_stats'));
  $('#btnExportStudentsXlsx').addEventListener('click', () => triggerXlsxDownload('students'));
  $('#btnExportPayrollXlsx').addEventListener('click', () => triggerXlsxDownload('payroll'));
  $('#btnExportPayrollHtml').addEventListener('click', () => triggerHtmlDownload('payroll'));
  $('#btnExportCollectionsHtml').addEventListener('click', () => triggerHtmlDownload('collections'));
  $('#btnExportAccountingXlsx').addEventListener('click', () => triggerXlsxDownload('accounting'));
  $('#btnExportAttendanceXlsx').addEventListener('click', () => triggerXlsxDownload('attendance'));
  $('#btnExportMemXlsx').addEventListener('click', () => triggerXlsxDownload('memorization'));
  $('#btnExportMadrasaXlsx').addEventListener('click', () => triggerXlsxDownload('madrasa'));

  // Student filters
  ['#stuFilterSearch', '#stuFilterTeacher', '#stuFilterType', '#stuFilterPayStatus', '#stuFilterStudyStatus', '#stuViewMode'].forEach((sel) => {
    const el = $(sel);
    if (el) {
      el.addEventListener('input', renderStudents);
      el.addEventListener('change', renderStudents);
    }
  });

  // Granular Inline Editing in Students Matrix (Payment Date & Actual Start Date strictly independent)
  $('#studentsTableWrap').addEventListener('change', async (e) => {
    const input = e.target.closest('.stu-inline-input');
    if (!input) return;
    const sid = input.getAttribute('data-stu-id');
    const mk = input.getAttribute('data-mk');
    const field = input.getAttribute('data-field');
    const stu = state.students.find((s) => s.id === sid);
    if (!stu) return;

    stu.months = stu.months || {};
    const m = stu.months[mk] = stu.months[mk] || {
      value: null, paymentDate: '', actualStartDate: '', pay: '', startDate: '', end: '', endDate: '',
      att: { weeks: [['', '', ''], ['', '', ''], ['', '', ''], ['', '', '']] }
    };

    const patchPayload = {};
    if (field === 'value') {
      const newVal = input.value !== '' ? Math.max(0, FE.roundHalfUp(input.value)) : null;
      m.value = newVal;
      patchPayload.value = newVal;
    } else if (field === 'paymentDate') {
      // Strictly updates Payment Date ONLY — never touches actualStartDate!
      const pVal = input.value.trim();
      m.paymentDate = pVal;
      m.pay = pVal;
      stu.paymentDate = pVal;
      patchPayload.paymentDate = pVal;
    } else if (field === 'actualStartDate') {
      // Strictly updates Actual Start Date ONLY — never touches paymentDate!
      const sVal = input.value.trim();
      m.actualStartDate = sVal;
      m.startDate = sVal;
      stu.actualStartDate = sVal;
      patchPayload.actualStartDate = sVal;
      if (sVal && !m.endDate) {
        const durDays = Number((state.settings && state.settings.defaultDurationDays) || 30);
        const calcEnd = FE.addDaysISO(sVal, durDays);
        m.endDate = calcEnd;
        m.end = calcEnd;
        patchPayload.endDate = calcEnd;
      }
    } else if (field === 'endDate') {
      const eVal = input.value.trim();
      const curStart = FE.resolveActualStartDate(m);
      if (curStart && eVal && eVal < curStart) {
        showToast('تاريخ نهاية الدراسة لا يمكن أن يسبق تاريخ البداية الفعلي!', true);
        input.value = m.endDate || '';
        return;
      }
      m.endDate = eVal;
      m.end = eVal;
      patchPayload.endDate = eVal;
    } else if (field === 'customWage') {
      const cwVal = input.value.trim() !== '' ? Math.max(0, FE.roundHalfUp(input.value)) : null;
      m.customWage = cwVal;
      patchPayload.customWage = cwVal;
    } else if (field === 'pct') {
      const pctVal = input.value.trim() !== '' ? Math.max(0, Math.min(100, FE.roundHalfUp(input.value))) : 0;
      stu.pct = pctVal;
      patchPayload.pct = pctVal;
    }

    try {
      await apiRequest(`/students/${encodeURIComponent(sid)}/months/${encodeURIComponent(mk)}`, {
        method: 'PATCH',
        body: patchPayload
      });
      renderAll();
      await refreshAlertsAndBadges();
    } catch (err) {
      await reloadFullStateFromServer();
    }
  });

  // Student row action buttons
  $('#studentsTableWrap').addEventListener('click', async (e) => {
    const resetWageBtn = e.target.closest('.stu-reset-wage-btn');
    if (resetWageBtn) {
      const sid = resetWageBtn.getAttribute('data-stu-id');
      const mk = resetWageBtn.getAttribute('data-mk');
      const stu = state.students.find((s) => s.id === sid);
      if (stu && stu.months && stu.months[mk]) {
        stu.months[mk].customWage = null;
        try {
          await apiRequest(`/students/${encodeURIComponent(sid)}/months/${encodeURIComponent(mk)}`, {
            method: 'PATCH',
            body: { customWage: null }
          });
          renderAll();
          await refreshAlertsAndBadges();
          showToast('تمت استعادة حساب أجر المعلمة التلقائي من النسبة.');
        } catch (err) {
          await reloadFullStateFromServer();
        }
      }
      return;
    }
    const editBtn = e.target.closest('.stu-edit-btn');
    if (editBtn) {
      openStudentModal(editBtn.getAttribute('data-stu-id'));
      return;
    }
    const delBtn = e.target.closest('.stu-del-btn');
    if (delBtn) {
      const delId = delBtn.getAttribute('data-stu-id');
      try {
        await apiRequest(`/students/${encodeURIComponent(delId)}`, { method: 'DELETE' });
        state.students = state.students.filter((s) => s.id !== delId);
        renderAll();
        await refreshAlertsAndBadges();
        showToast('تم حذف سجل الطالبة.');
      } catch (err) {}
      return;
    }
    const viewBtn = e.target.closest('.stu-view-btn') || e.target.closest('.stu-profile-link');
    if (viewBtn) {
      e.preventDefault();
      const sid = viewBtn.getAttribute('data-stu-id');
      uiState.selectedProfileId = sid;
      navigateTo(`#/profile/${sid}`);
    }
  });

  // Profile controls
  $('#profileStudentSelect').addEventListener('change', (e) => {
    uiState.selectedProfileId = e.target.value;
    renderProfile();
  });
  $('#btnProfileEditStudent').addEventListener('click', () => {
    openStudentModal(uiState.selectedProfileId);
  });
  const btnProfileHtml = $('#btnProfileExportHtml');
  if (btnProfileHtml) {
    btnProfileHtml.addEventListener('click', () => {
      triggerHtmlDownload('student', { studentId: uiState.selectedProfileId });
    });
  }

  // Modal controls
  $('#btnCloseStudentModal').addEventListener('click', closeStudentModal);
  $('#btnCancelStudentModal').addEventListener('click', closeStudentModal);
  $('#btnModalCalcEndFromStart').addEventListener('click', () => {
    const stDt = $('#modalMonthActualStartDate').value.trim();
    if (!stDt) {
      showToast('أدخل تاريخ البداية الفعلي أولًا لحساب تاريخ النهاية.', true);
      return;
    }
    const durDays = Number((state.settings && state.settings.defaultDurationDays) || 30);
    $('#modalMonthEndDate').value = FE.addDaysISO(stDt, durDays);
    showToast(`تم حساب تاريخ النهاية (+${durDays} يومًا من تاريخ البداية الفعلي).`);
  });

  $('#formStudentModal').addEventListener('submit', async (e) => {
    e.preventDefault();
    const sid = $('#modalStudentId').value.trim();
    const name = $('#modalStudentName').value.trim();
    if (!name) return;
    const mk = uiState.selectedMonth;
    const pctInt = FE.parsePctInt($('#modalStudentPct').value, 70);

    const payDt = $('#modalMonthPaymentDate').value.trim();
    const startDt = $('#modalMonthActualStartDate').value.trim();
    const endDt = $('#modalMonthEndDate').value.trim();

    if (startDt && endDt && endDt < startDt) {
      showToast('تاريخ نهاية الدراسة لا يمكن أن يسبق تاريخ البداية الفعلي!', true);
      return;
    }

    const rawVal = $('#modalMonthValue').value;
    const rawDisc = $('#modalMonthDiscount').value;
    const rawCw = $('#modalMonthCustomWage').value;
    const valNum = rawVal !== '' ? Math.max(0, FE.roundHalfUp(rawVal)) : null;
    const discNum = rawDisc !== '' ? Math.max(0, FE.roundHalfUp(rawDisc)) : null;
    const cwNum = rawCw !== '' ? Math.max(0, FE.roundHalfUp(rawCw)) : null;

    if (discNum !== null && discNum > 0 && (valNum === null || discNum > valNum)) {
      showToast('قيمة الخصم لا يمكن أن تتجاوز قيمة الاشتراك الشهري!', true);
      return;
    }

    const phoneVal = $('#modalStudentPhone').value.trim();
    const existing = sid ? state.students.find((s) => s.id === sid) : null;
    const monthsCopy = existing && existing.months ? JSON.parse(JSON.stringify(existing.months)) : {};
    monthsCopy[mk] = Object.assign({}, monthsCopy[mk] || {}, {
      value: valNum,
      discount: discNum,
      customWage: cwNum,
      paymentDate: payDt,
      pay: payDt,
      actualStartDate: startDt,
      startDate: startDt,
      endDate: endDt,
      end: endDt
    });

    const stuPayload = {
      id: sid || ('stu' + String(Date.now()).slice(-6)),
      code: $('#modalStudentCode').value.trim() || ('STU-' + String(state.students.length + 1).padStart(4, '0')),
      name,
      phone: phoneVal,
      teacher: $('#modalStudentTeacher').value,
      type: $('#modalStudentType').value,
      pct: pctInt,
      paymentDate: payDt,
      actualStartDate: startDt,
      months: monthsCopy
    };

    try {
      const savedRes = sid
        ? await apiRequest(`/students/${encodeURIComponent(sid)}`, { method: 'PUT', body: stuPayload })
        : await apiRequest('/students', { method: 'POST', body: stuPayload });

      const finalStu = savedRes.student || stuPayload;
      const autoRcp = Boolean($('#modalCreateReceiptCheck') && $('#modalCreateReceiptCheck').checked);
      const netVal = Math.max(0, (valNum || 0) - (discNum || 0));
      const hasExistingRcp = state.installments.some((inst) => inst.studentId === finalStu.id && inst.month === mk);
      if (autoRcp && payDt && netVal > 0 && !hasExistingRcp) {
        await apiRequest('/installments', {
          method: 'POST',
          body: {
            studentId: finalStu.id,
            month: mk,
            amount: netVal,
            paymentDate: payDt,
            method: 'نقدي',
            note: `سداد اشتراك ${monthLabel(mk)}`
          }
        });
      }

      closeStudentModal();
      await reloadFullStateFromServer();
      showToast('تم حفظ بيانات الطالبة وتواريخ الدفع والبداية الفعلية بنجاح.');
    } catch (err) {}
  });

  $('#btnModalDeleteStudent').addEventListener('click', async () => {
    const sid = $('#modalStudentId').value.trim();
    if (!sid) return;
    try {
      await apiRequest(`/students/${encodeURIComponent(sid)}`, { method: 'DELETE' });
      closeStudentModal();
      await reloadFullStateFromServer();
      showToast('تم حذف سجل الطالبة.');
    } catch (err) {}
  });

  // Auto-sync teacher percentage when selecting a teacher in Student Modal
  const modalTeacherSelect = $('#modalStudentTeacher');
  if (modalTeacherSelect) {
    modalTeacherSelect.addEventListener('change', (e) => {
      const selectedT = e.target.value;
      const isNewStudent = !($('#modalStudentId') && $('#modalStudentId').value);
      const useTeacherPct = isNewStudent || (state.settings && state.settings.pctSource === 'teacher');
      if (selectedT && useTeacherPct && state.teacherMeta && state.teacherMeta[selectedT]) {
        const defPct = FE.parsePctInt(state.settings && state.settings.defaultTeacherPct, 70);
        $('#modalStudentPct').value = FE.parsePctInt(state.teacherMeta[selectedT].pct, defPct);
      }
    });
  }

  // Collections filters & simulator
  ['#collBasisSelect', '#collTeacherFilter', '#collFromPayDate', '#collToPayDate', '#collTimingFilter'].forEach((sel) => {
    const el = $(sel);
    if (el) el.addEventListener('change', renderCollections);
  });
  $('#btnResetCollFilters').addEventListener('click', () => {
    $('#collFromPayDate').value = '';
    $('#collToPayDate').value = '';
    $('#collTimingFilter').value = '';
    if ($('#collTeacherFilter')) $('#collTeacherFilter').value = '';
    renderCollections();
  });

  const attTeacherEl = $('#attTeacherFilter');
  if (attTeacherEl) {
    attTeacherEl.addEventListener('change', () => {
      populateSelects();
      renderAttendance();
    });
  }

  const memTeacherEl = $('#memTeacherFilter');
  if (memTeacherEl) {
    memTeacherEl.addEventListener('change', () => {
      populateSelects();
      renderMemorization();
    });
  }
  ['#simPayDate', '#simActualStart', '#simDurationDays'].forEach((sel) => {
    const el = $(sel);
    if (el) {
      el.addEventListener('input', renderDatesSimulator);
      el.addEventListener('change', renderDatesSimulator);
    }
  });

  // Add Installment Receipt
  $('#formAddInstallment').addEventListener('submit', async (e) => {
    e.preventDefault();
    const sid = $('#instStudentSelect').value;
    const amt = FE.roundHalfUp($('#instAmountInput').value);
    const payDt = $('#instPayDateInput').value;
    const method = $('#instMethodInput').value;
    const note = $('#instNoteInput').value.trim();
    if (!sid || amt <= 0 || !payDt) return;
    try {
      const res = await apiRequest('/installments', {
        method: 'POST',
        body: {
          studentId: sid,
          month: uiState.selectedMonth,
          amount: amt,
          paymentDate: payDt,
          date: payDt,
          method,
          note
        }
      });
      $('#instAmountInput').value = '';
      $('#instNoteInput').value = '';
      await reloadFullStateFromServer();
      showToast(`تم إصدار الإيصال ${res.installment ? res.installment.receiptNo : ''} بنجاح.`);
    } catch (err) {}
  });

  // Installments actions (Print Receipt HTML & Delete)
  $('#installmentsTableWrap').addEventListener('click', async (e) => {
    const printBtn = e.target.closest('.btn-print-receipt');
    if (printBtn) {
      const iid = printBtn.getAttribute('data-inst-id');
      downloadBlobFromApi(
        `/export/receipt-html?receipt_id=${encodeURIComponent(iid)}`,
        `YawmJadid_Receipt_${iid}.html`,
        'تم تحميل إيصال السداد الجاهز للطباعة (.html).'
      );
      return;
    }
    const delBtn = e.target.closest('.btn-del-installment');
    if (delBtn) {
      const iid = delBtn.getAttribute('data-inst-id');
      try {
        await apiRequest(`/installments/${encodeURIComponent(iid)}`, { method: 'DELETE' });
        await reloadFullStateFromServer();
        showToast('تم حذف إيصال الدفعة وتحديث رصيد التحصيل.');
      } catch (err) {}
    }
  });

  // Add & Delete Salary Payout
  $('#formAddPayout').addEventListener('submit', async (e) => {
    e.preventDefault();
    const t = $('#payTeacherSelect').value;
    const amt = FE.roundHalfUp($('#payAmountInput').value);
    const dt = $('#payDateInput').value;
    const note = $('#payNoteInput').value.trim();
    if (!t || amt <= 0 || !dt) return;
    try {
      await apiRequest('/payouts', {
        method: 'POST',
        body: {
          teacher: t,
          month: uiState.selectedMonth,
          amount: amt,
          date: dt,
          note
        }
      });
      $('#payAmountInput').value = '';
      $('#payNoteInput').value = '';
      await reloadFullStateFromServer();
      showToast(`تم تسجيل صرف الراتب للمعلمة ${t}`);
    } catch (err) {}
  });

  $('#payoutsHistoryWrap').addEventListener('click', async (e) => {
    const delBtn = e.target.closest('.btn-del-payout');
    if (!delBtn) return;
    const pid = delBtn.getAttribute('data-payout-id');
    try {
      await apiRequest(`/payouts/${encodeURIComponent(pid)}`, { method: 'DELETE' });
      await reloadFullStateFromServer();
      showToast('تم حذف سجل صرف الراتب.');
    } catch (err) {}
  });

  // Add & Delete Accounting Transaction
  $('#formAddTransaction').addEventListener('submit', async (e) => {
    e.preventDefault();
    const type = $('#txTypeSelect').value;
    const cat = $('#txCategoryInput').value.trim();
    const amt = FE.roundHalfUp($('#txAmountInput').value);
    const dt = $('#txDateInput').value;
    const method = $('#txMethodInput').value.trim();
    const note = $('#txNoteInput').value.trim();
    if (!cat || amt <= 0 || !dt) return;
    try {
      await apiRequest('/transactions', {
        method: 'POST',
        body: {
          type,
          category: cat,
          amount: amt,
          date: dt,
          month: dt.slice(0, 7),
          method,
          note
        }
      });
      $('#txCategoryInput').value = '';
      $('#txAmountInput').value = '';
      $('#txNoteInput').value = '';
      await reloadFullStateFromServer();
      showToast('تم حفظ القيد المحاسبي في الخزينة.');
    } catch (err) {}
  });

  $('#transactionsTableWrap').addEventListener('click', async (e) => {
    const delBtn = e.target.closest('.btn-del-tx');
    if (!delBtn) return;
    const tid = delBtn.getAttribute('data-tx-id');
    try {
      await apiRequest(`/transactions/${encodeURIComponent(tid)}`, { method: 'DELETE' });
      await reloadFullStateFromServer();
      showToast('تم حذف القيد المحاسبي.');
    } catch (err) {}
  });

  // Smart Attendance Plan Generator
  $('#planStudentSelect').addEventListener('change', (e) => {
    const stu = state.students.find((s) => s.id === e.target.value);
    const m = stu && stu.months && stu.months[uiState.selectedMonth];
    const actualStart = FE.resolveActualStartDate(m);
    if (actualStart) {
      $('#planActualStartInput').value = actualStart;
    }
  });

  $('#formCreatePlan').addEventListener('submit', async (e) => {
    e.preventDefault();
    const sid = $('#planStudentSelect').value;
    const actualStart = $('#planActualStartInput').value;
    const count = parseInt($('#planCountInput').value || '8', 10);
    const timeStr = $('#planTimeInput').value || '16:00';
    const days = $('#planDaysSelect').value.split(',').map((x) => parseInt(x, 10));
    const stu = state.students.find((s) => s.id === sid);
    if (!stu || !actualStart) return;

    const planId = 'pln_' + Date.now();
    let curIso = actualStart;
    const generatedSessions = [];
    let guard = 0;
    while (generatedSessions.length < count && guard < 400) {
      guard++;
      const parts = curIso.split('-').map(Number);
      const dt = new Date(Date.UTC(parts[0], parts[1] - 1, parts[2]));
      if (days.includes(dt.getUTCDay())) {
        generatedSessions.push({
          id: `sess_${Date.now()}_${generatedSessions.length + 1}`,
          planId,
          studentId: sid,
          teacher: stu.teacher || '',
          date: curIso,
          time: timeStr,
          dur: 40,
          status: 'scheduled',
          notes: `مجدولة من تاريخ البداية الفعلي ${actualStart}`
        });
      }
      curIso = FE.addDaysISO(curIso, 1);
    }

    try {
      await apiRequest('/plans', {
        method: 'POST',
        body: {
          plan: {
            id: planId,
            studentId: sid,
            studentName: stu.name,
            teacher: stu.teacher || '',
            count,
            days,
            time: timeStr,
            dur: 40,
            startDate: actualStart,
            actualStartDate: actualStart
          },
          sessions: generatedSessions
        }
      });
      await reloadFullStateFromServer();
      showToast(`تم توليد ${generatedSessions.length} حصص بدءًا من تاريخ البداية الفعلي (${actualStart}).`);
    } catch (err) {
      if (err && err.code === 'SCHEDULING_CONFLICT') {
        const proceed = window.confirm(`${err.message}\n\nهل ترغب في حفظ الخطة على الرغم من وجود تعارض المواعيد؟`);
        if (proceed) {
          try {
            await apiRequest('/plans', {
              method: 'POST',
              body: {
                plan: {
                  id: planId,
                  studentId: sid,
                  studentName: stu.name,
                  teacher: stu.teacher || '',
                  count,
                  days,
                  time: timeStr,
                  dur: 40,
                  startDate: actualStart,
                  actualStartDate: actualStart
                },
                sessions: generatedSessions,
                allowConflicts: true
              }
            });
            await reloadFullStateFromServer();
            showToast(`تم حفظ الخطة (${generatedSessions.length} حصص) بتأكيد المستخدم.`);
          } catch (e2) {}
        }
      }
    }
  });

  // Session inline status update, auto-compensate & delete
  $('#sessionsTableWrap').addEventListener('change', async (e) => {
    const sel = e.target.closest('.sess-status-select');
    if (!sel) return;
    const sessId = sel.getAttribute('data-sess-id');
    try {
      await apiRequest(`/sessions/${encodeURIComponent(sessId)}`, {
        method: 'PATCH',
        body: { status: sel.value }
      });
      await reloadFullStateFromServer();
      showToast('تم تحديث حالة الحصة.');
    } catch (err) {}
  });

  $('#sessionsTableWrap').addEventListener('click', async (e) => {
    const delBtn = e.target.closest('.btn-del-session');
    if (delBtn) {
      const delId = delBtn.getAttribute('data-sess-id');
      try {
        await apiRequest(`/sessions/${encodeURIComponent(delId)}`, { method: 'DELETE' });
        await reloadFullStateFromServer();
        showToast('تم حذف الحصة.');
      } catch (err) {}
      return;
    }

    const compBtn = e.target.closest('.btn-compensate-session');
    if (compBtn && !compBtn.disabled) {
      const sessId = compBtn.getAttribute('data-sess-id');
      try {
        const res = await apiRequest(`/sessions/${encodeURIComponent(sessId)}/compensate`, {
          method: 'POST',
          body: {}
        });
        await reloadFullStateFromServer();
        showToast(`تم جدولة حصة تعويضية تلقائية بتاريخ ${res.makeupSession ? res.makeupSession.date : ''} وتحديث تاريخ النهاية.`);
      } catch (err) {}
    }
  });

  // Weekly attendance quick toggle
  $('#weeklyAttendanceTableWrap').addEventListener('click', async (e) => {
    const btn = e.target.closest('.btn-toggle-week');
    if (!btn) return;
    const sid = btn.getAttribute('data-stu-id');
    const wIdx = parseInt(btn.getAttribute('data-week'), 10);
    const stu = state.students.find((s) => s.id === sid);
    if (!stu) return;
    const mk = uiState.selectedMonth;
    const m = (stu.months && stu.months[mk]) || {};
    const att = m.att && Array.isArray(m.att.weeks)
      ? JSON.parse(JSON.stringify(m.att))
      : { weeks: [['', '', ''], ['', '', ''], ['', '', ''], ['', '', '']], note: '' };
    const cur = att.weeks[wIdx] || ['', '', ''];
    const hasMark = cur[0] === '✓';
    att.weeks[wIdx] = hasMark ? ['', '', ''] : ['✓', '✓', ''];
    try {
      await apiRequest(`/students/${encodeURIComponent(sid)}/months/${encodeURIComponent(mk)}`, {
        method: 'PATCH',
        body: { att }
      });
      await reloadFullStateFromServer();
    } catch (err) {}
  });

  // Memorization
  $('#formAddMem').addEventListener('submit', async (e) => {
    e.preventDefault();
    const sid = $('#memStudentSelect').value;
    const stu = state.students.find((s) => s.id === sid);
    try {
      await apiRequest('/memorization', {
        method: 'POST',
        body: {
          studentId: sid,
          teacher: (stu && stu.teacher) || '',
          date: $('#memDateInput').value,
          track: $('#memTrackSelect').value,
          fromSurah: $('#memFromSurahInput').value.trim(),
          fromAyah: 1,
          toSurah: $('#memFromSurahInput').value.trim(),
          toAyah: 30,
          juz: 30,
          pages: Number($('#memPagesInput').value || 1),
          grade: 'ممتاز متقن',
          score: Number($('#memScoreInput').value || 98),
          notes: $('#memNotesInput').value.trim()
        }
      });
      await reloadFullStateFromServer();
      showToast('تم حفظ سجل التسميع والمراجعة.');
    } catch (err) {}
  });

  $('#memorizationTableWrap').addEventListener('click', async (e) => {
    const delBtn = e.target.closest('.btn-del-mem');
    if (!delBtn) return;
    const mid = delBtn.getAttribute('data-mem-id');
    try {
      await apiRequest(`/memorization/${encodeURIComponent(mid)}`, { method: 'DELETE' });
      await reloadFullStateFromServer();
      showToast('تم حذف سجل التسميع.');
    } catch (err) {}
  });

  // Madrasa
  $('#formEnrollMadrasa').addEventListener('submit', async (e) => {
    e.preventDefault();
    const bName = $('#madBatchNameInput').value.trim();
    const sid = $('#madStudentSelect').value;
    const fee = FE.roundHalfUp($('#madFeeInput').value || 60);
    try {
      await apiRequest('/madrasa/enrollments', {
        method: 'POST',
        body: {
          batchId: 'batch_1',
          batchName: bName,
          studentId: sid,
          enrolledAt: uiState.referenceDate,
          feePaid: fee,
          streak: 7,
          bestStreak: 7,
          badges: ['🌟 نجمة المواظبة', '🔥 شعلة الفجر والقرآن'],
          tracksProgress: { qurani: 100, taabudi: 95, ilmi: 90, tarbawi: 100 }
        }
      });
      await reloadFullStateFromServer();
      showToast('تم ضم الطالبة للدفعة الإيمانية.');
    } catch (err) {}
  });

  $('#madrasaTableWrap').addEventListener('click', async (e) => {
    const bumpBtn = e.target.closest('.btn-bump-streak');
    if (bumpBtn) {
      const eid = bumpBtn.getAttribute('data-enr-id');
      const enr = state.madrasaEnrollments.find((x) => x.id === eid);
      if (!enr) return;
      const nextStreak = (Number(enr.streak) || 0) + 1;
      try {
        await apiRequest(`/madrasa/enrollments/${encodeURIComponent(eid)}`, {
          method: 'PATCH',
          body: { streak: nextStreak }
        });
        await reloadFullStateFromServer();
        showToast(`🔥 تم تسجيل يوم حماسة إضافي (${nextStreak} يوم)!`);
      } catch (err) {}
      return;
    }
    const delBtn = e.target.closest('.btn-del-madrasa');
    if (delBtn) {
      const delId = delBtn.getAttribute('data-enr-id');
      try {
        await apiRequest(`/madrasa/enrollments/${encodeURIComponent(delId)}`, { method: 'DELETE' });
        await reloadFullStateFromServer();
        showToast('تم إلغاء انتساب الطالبة من الدفعة.');
      } catch (err) {}
    }
  });

  // Certificates
  ['#certStudentSelect', '#certTitleInput', '#certGradeInput', '#certDateInput'].forEach((sel) => {
    const el = $(sel);
    if (el) {
      el.addEventListener('input', drawCertificatePreview);
      el.addEventListener('change', drawCertificatePreview);
    }
  });

  $('#formIssueCert').addEventListener('submit', async (e) => {
    e.preventDefault();
    const sid = $('#certStudentSelect').value;
    const stu = state.students.find((s) => s.id === sid);
    if (!stu) return;
    try {
      await apiRequest('/certificates', {
        method: 'POST',
        body: {
          studentId: sid,
          studentName: stu.name,
          courseTitle: $('#certTitleInput').value.trim(),
          grade: $('#certGradeInput').value.trim(),
          issueDate: $('#certDateInput').value || uiState.referenceDate,
          teacherName: stu.teacher || ''
        }
      });
      await reloadFullStateFromServer();
      showToast('تم إصدار وحفظ الشهادة التقديرية.');
    } catch (err) {}
  });

  $('#btnDownloadCertPng').addEventListener('click', () => {
    const canvas = $('#certCanvas');
    if (!canvas) return;
    const a = document.createElement('a');
    a.href = canvas.toDataURL('image/png');
    a.download = 'YawmJadid_Certificate.png';
    a.click();
  });

  $('#certsTableWrap').addEventListener('click', async (e) => {
    const prevBtn = e.target.closest('.btn-preview-cert');
    if (prevBtn) {
      const cid = prevBtn.getAttribute('data-cert-id');
      const cert = state.certificates.find((c) => c.id === cid);
      if (cert) {
        if ($('#certStudentSelect')) $('#certStudentSelect').value = cert.studentId;
        if ($('#certTitleInput')) $('#certTitleInput').value = cert.courseTitle || '';
        if ($('#certGradeInput')) $('#certGradeInput').value = cert.grade || '';
        if ($('#certDateInput')) $('#certDateInput').value = cert.issueDate || uiState.referenceDate;
        drawCertificatePreview();
        showToast('تم عرض الشهادة في لوحة المعاينة.');
      }
      return;
    }
    const delBtn = e.target.closest('.btn-del-cert');
    if (delBtn) {
      const delCid = delBtn.getAttribute('data-cert-id');
      try {
        await apiRequest(`/certificates/${encodeURIComponent(delCid)}`, { method: 'DELETE' });
        await reloadFullStateFromServer();
        showToast('تم حذف الشهادة.');
      } catch (err) {}
    }
  });

  // Teachers View & Dynamic Teacher Lifecycle
  const resetTeacherEditForm = () => {
    if ($('#tOriginalNameInput')) $('#tOriginalNameInput').value = '';
    if ($('#tNameInput')) $('#tNameInput').value = '';
    if ($('#tPctInput')) $('#tPctInput').value = String((state.settings && state.settings.defaultTeacherPct) || 70);
    if ($('#teacherFormHeading')) $('#teacherFormHeading').textContent = '➕ إضافة أو تحديث معلمة';
    if ($('#btnSubmitTeacher')) $('#btnSubmitTeacher').textContent = '💾 حفظ المعلمة';
    if ($('#btnCancelTeacherEdit')) $('#btnCancelTeacherEdit').style.display = 'none';
  };

  const btnCancelTEdit = $('#btnCancelTeacherEdit');
  if (btnCancelTEdit) {
    btnCancelTEdit.addEventListener('click', () => {
      resetTeacherEditForm();
    });
  }

  ['#teacherSearchInput', '#teacherStatusFilter'].forEach((sel) => {
    const el = $(sel);
    if (el) {
      el.addEventListener('input', renderTeachers);
      el.addEventListener('change', renderTeachers);
    }
  });

  let _teacherSubmitInFlight = false;
  $('#formAddTeacher').addEventListener('submit', async (e) => {
    e.preventDefault();
    if (_teacherSubmitInFlight) return;
    const name = cleanTeacherName($('#tNameInput').value);
    const origName = cleanTeacherName(($('#tOriginalNameInput') && $('#tOriginalNameInput').value) || '');
    const pct = FE.parsePctInt($('#tPctInput').value, 70);
    if (!name) return;

    _teacherSubmitInFlight = true;
    try {
      if (origName) {
        const res = await apiRequest(`/teachers/${encodeURIComponent(origName)}`, {
          method: 'PUT',
          body: { oldName: origName, newName: name, name, pct }
        });
        const finalName = (res && res.name) || name;
        if (origName !== finalName) {
          ['#stuFilterTeacher', '#statsTeacherFilter', '#collTeacherFilter', '#attTeacherFilter', '#memTeacherFilter', '#htmlExportTeacher', '#payTeacherSelect'].forEach((sel) => {
            const el = $(sel);
            if (el && el.value === origName) {
              el.value = finalName;
            }
          });
        }
        resetTeacherEditForm();
        await reloadFullStateFromServer();
        if (origName !== finalName) {
          showToast(`تم تغيير اسم المعلمة من «${origName}» إلى «${finalName}» وتحديث جميع سجلاتها.`);
        } else {
          showToast(`تم تحديث بيانات المعلمة «${finalName}» بنجاح.`);
        }
      } else {
        const res = await apiRequest('/teachers', {
          method: 'POST',
          body: { name, pct, mode: 'create' }
        });
        const finalName = (res && res.name) || name;
        resetTeacherEditForm();
        await reloadFullStateFromServer();
        showToast(`تمت إضافة المعلمة «${finalName}» وإتاحتها في جميع القوائم.`);
      }
    } catch (err) {
      // Error toast is already displayed by apiRequest
    } finally {
      _teacherSubmitInFlight = false;
    }
  });

  $('#teachersCardsGrid').addEventListener('click', async (e) => {
    const fBtn = e.target.closest('.btn-filter-by-teacher');
    if (fBtn) {
      const t = fBtn.getAttribute('data-teacher');
      navigateTo(`#/students?teacher=${encodeURIComponent(t)}`);
      return;
    }
    const xBtn = e.target.closest('.btn-export-teacher');
    if (xBtn) {
      triggerXlsxDownload('teacher', xBtn.getAttribute('data-teacher'));
      return;
    }
    const hBtn = e.target.closest('.btn-export-teacher-html');
    if (hBtn) {
      triggerHtmlDownload('teacher', { teacher: hBtn.getAttribute('data-teacher') });
      return;
    }
    const eBtn = e.target.closest('.btn-edit-teacher');
    if (eBtn) {
      const t = eBtn.getAttribute('data-teacher') || '';
      const pct = eBtn.getAttribute('data-pct') || '70';
      if ($('#tOriginalNameInput')) $('#tOriginalNameInput').value = t;
      $('#tNameInput').value = t;
      $('#tPctInput').value = pct;
      if ($('#teacherFormHeading')) $('#teacherFormHeading').textContent = `✏️ تعديل بيانات المعلمة: ${t}`;
      if ($('#btnSubmitTeacher')) $('#btnSubmitTeacher').textContent = '💾 حفظ التعديل';
      if ($('#btnCancelTeacherEdit')) $('#btnCancelTeacherEdit').style.display = 'inline-flex';
      $('#tNameInput').focus();
      return;
    }
    const deactBtn = e.target.closest('.btn-deactivate-teacher');
    if (deactBtn) {
      const deactT = deactBtn.getAttribute('data-teacher');
      try {
        await apiRequest(`/teachers/${encodeURIComponent(deactT)}`, {
          method: 'PATCH',
          body: { name: deactT, active: false, archived: true }
        });
        await reloadFullStateFromServer();
        showToast(`تم إيقاف/أرشفة المعلمة «${deactT}» وإخفاؤها من التعيينات الجديدة مع حفظ كافة سجلاتها التاريخية.`);
      } catch (err) {}
      return;
    }
    const dBtn = e.target.closest('.btn-del-teacher');
    if (dBtn) {
      const delT = dBtn.getAttribute('data-teacher');
      try {
        const res = await apiRequest(`/teachers/${encodeURIComponent(delT)}`, { method: 'DELETE' });
        if ($('#tOriginalNameInput') && $('#tOriginalNameInput').value === delT) {
          resetTeacherEditForm();
        }
        await reloadFullStateFromServer();
        if (res && res.archived) {
          showToast(`المعلمة ${delT} لها سجلات مالية/تشغيلية، لذلك تمت أرشفتها بدل الحذف للحفاظ على البيانات.`);
        } else {
          showToast(`تم حذف المعلمة ${delT}`);
        }
      } catch (err) {}
      return;
    }
    const rBtn = e.target.closest('.btn-restore-teacher');
    if (rBtn) {
      const rT = rBtn.getAttribute('data-teacher');
      try {
        await apiRequest(`/teachers/${encodeURIComponent(rT)}`, {
          method: 'PATCH',
          body: { name: rT, active: true, archived: false }
        });
        await reloadFullStateFromServer();
        showToast(`تمت إعادة تفعيل المعلمة «${rT}» وإتاحتها في جميع القوائم.`);
      } catch (err) {}
    }
  });

  // Dynamic Subscription Packages (subTypes) Management
  const formPkg = $('#formAddPackage');
  if (formPkg) {
    formPkg.addEventListener('submit', async (e) => {
      e.preventDefault();
      const pkgInput = $('#pkgNameInput');
      const rawPkg = cleanTeacherName(pkgInput ? pkgInput.value : '');
      if (!rawPkg) return;
      if (state.subTypes.includes(rawPkg)) {
        showToast(`الباقة «${rawPkg}» موجودة بالفعل في القوائم.`, true);
        return;
      }
      const nextSubTypes = state.subTypes.concat([rawPkg]);
      try {
        await apiRequest('/kv/subTypes', {
          method: 'PUT',
          body: { value: nextSubTypes }
        });
        if (pkgInput) pkgInput.value = '';
        await reloadFullStateFromServer();
        showToast(`تمت إضافة الباقة «${rawPkg}» وتحديث جميع القوائم.`);
      } catch (err) {}
    });
  }

  const pkgWrap = $('#packagesListWrap');
  if (pkgWrap) {
    pkgWrap.addEventListener('click', async (e) => {
      const delPkgBtn = e.target.closest('.btn-del-package');
      if (!delPkgBtn) return;
      const pkg = delPkgBtn.getAttribute('data-pkg');
      if (!pkg) return;
      const nextSubTypes = state.subTypes.filter((p) => p !== pkg);
      if (nextSubTypes.length === 0) {
        showToast('لا يمكن حذف جميع الباقات؛ يجب بقاء باقة واحدة على الأقل.', true);
        return;
      }
      try {
        await apiRequest('/kv/subTypes', {
          method: 'PUT',
          body: { value: nextSubTypes }
        });
        await reloadFullStateFromServer();
        showToast(`تم حذف الباقة «${pkg}» من القوائم.`);
      } catch (err) {}
    });
  }

  // Export Center
  $all('[data-export-scope]').forEach((btn) => {
    btn.addEventListener('click', () => {
      triggerXlsxDownload(btn.getAttribute('data-export-scope'));
    });
  });

  const btnHtmlExport = $('#btnDownloadHtmlExport');
  if (btnHtmlExport) {
    btnHtmlExport.addEventListener('click', () => {
      const scope = ($('#htmlExportScope') && $('#htmlExportScope').value) || 'full';
      const teacher = ($('#htmlExportTeacher') && $('#htmlExportTeacher').value) || '';
      const studentId = ($('#htmlExportStudent') && $('#htmlExportStudent').value) || '';
      const hidePersonal = Boolean($('#htmlExportHidePrivacy') && $('#htmlExportHidePrivacy').checked);
      triggerHtmlDownload(scope, { teacher, studentId, hidePersonal });
    });
  }

  $('#btnExportJson').addEventListener('click', async () => {
    let payload = state;
    try {
      payload = await apiRequest('/sync');
    } catch (e) {
      payload = Object.assign({}, state, {
        subTypes: (state.subTypesMap && Object.keys(state.subTypesMap).length) ? state.subTypesMap : state.subTypes
      });
    }
    const blob = new Blob([JSON.stringify(payload, null, 2)], { type: 'application/json;charset=utf-8' });
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = `YawmJadid_Academy_Backup_${uiState.referenceDate}.json`;
    a.click();
    showToast('تم تصدير النسخة الاحتياطية الكاملة (.json) بنجاح.');
  });

  $('#btnExportCsv').addEventListener('click', exportCsvFallback);

  let _pendingJsonRestore = null;

  const closeJsonRestoreModal = () => {
    _pendingJsonRestore = null;
    const modal = $('#jsonRestoreModal');
    if (modal) modal.classList.remove('open');
  };

  ['#btnCloseJsonRestoreModal', '#btnCancelJsonRestore'].forEach((sel) => {
    const btn = $(sel);
    if (btn) btn.addEventListener('click', closeJsonRestoreModal);
  });

  const renderJsonRestorePreviewModal = (preview) => {
    const sum = (preview && preview.summary) || {};
    const rows = Array.isArray(preview && preview.datasetRows) ? preview.datasetRows : [];
    const warnBox = $('#jsonRestoreWarningBanner');
    if (warnBox) {
      renderHtml(warnBox, html`
        <div class="dates-rule-banner" style="margin:0;">
          <div>
            <strong>📄 الملف: ${preview.filename || 'backup.json'}</strong>
            <div style="font-size:12.5px;margin-top:4px;">
              سيقوم النظام تلقائيًا بإنشاء وفحص نسخة احتياطية فورية (<code>alahd_safety_pre_json_restore_*.sqlite3</code>) قبل تطبيق الاستعادة، مع التراجع التلقائي الكامل في حال حدوث أي خطأ.
              ${sum.willDeleteExistingData ? html`<br><span style="color:#dc2626;font-weight:700;">⚠️ تنبيه: ستؤدي هذه الاستعادة إلى حذف/استبدال ${sum.totalDeleted} سجل حالي غير موجود في الملف المرفوع.</span>` : ''}
            </div>
          </div>
        </div>
      `);
    }
    const kpiBox = $('#jsonRestoreSummaryKpis');
    if (kpiBox) {
      renderHtml(kpiBox, html`
        <div class="kpi-card">
          <div class="kpi-label">إجمالي السجلات الحالية</div>
          <div class="kpi-value">${sum.totalCurrentRecords ?? 0}</div>
        </div>
        <div class="kpi-card kpi-pay">
          <div class="kpi-label">سجلات جديدة ستُضاف (+)</div>
          <div class="kpi-value">${sum.totalAdded ?? 0}</div>
        </div>
        <div class="kpi-card kpi-gold">
          <div class="kpi-label">سجلات ستُحدَّث (~)</div>
          <div class="kpi-value">${sum.totalUpdated ?? 0}</div>
        </div>
        <div class="kpi-card kpi-danger">
          <div class="kpi-label">سجلات ستُحذف (-)</div>
          <div class="kpi-value">${sum.totalDeleted ?? 0}</div>
        </div>
      `);
    }
    const tblBox = $('#jsonRestoreTableWrap');
    if (tblBox) {
      renderHtml(tblBox, html`
        <table class="data-table">
          <thead>
            <tr>
              <th>القسم / الجدول</th>
              <th>العدد الحالي</th>
              <th>العدد في الملف</th>
              <th>إضافة (+)</th>
              <th>تحديث (~)</th>
              <th>حذف (-)</th>
              <th>دون تغيير</th>
            </tr>
          </thead>
          <tbody>
            ${rows.map((r) => html`
              <tr>
                <td style="text-align:right;font-weight:700;">${r.label || r.dataset}</td>
                <td>${r.currentCount ?? 0}</td>
                <td>${r.incomingCount ?? 0}</td>
                <td><span class="badge badge-end">+${r.added ?? 0}</span></td>
                <td><span class="badge badge-warn">~${r.updated ?? 0}</span></td>
                <td><span class="badge badge-unpaid">-${r.deleted ?? 0}</span></td>
                <td>${r.unchanged ?? 0}</td>
              </tr>
            `)}
          </tbody>
        </table>
      `);
    }
    const modal = $('#jsonRestoreModal');
    if (modal) modal.classList.add('open');
  };

  $('#inputImportJson').addEventListener('change', (e) => {
    const file = e.target.files && e.target.files[0];
    if (!file) return;
    const fname = String(file.name || 'backup.json');
    if (!fname.toLowerCase().endsWith('.json')) {
      showToast('صيغة الملف غير مدعومة؛ يرجى اختيار ملف بصيغة (.json) فقط.', true);
      e.target.value = '';
      return;
    }
    const reader = new FileReader();
    reader.onload = async () => {
      try {
        let parsed = null;
        try {
          parsed = JSON.parse(String(reader.result));
        } catch (syntaxErr) {
          throw new Error('ملف JSON غير صالح (خطأ في صياغة JSON)');
        }
        const preview = await apiRequest('/json-restore/preview?allow_legacy_dates=true', {
          method: 'POST',
          readOnlyPost: true,
          sendRev: false,
          body: {
            filename: fname,
            state: parsed
          }
        });
        _pendingJsonRestore = {
          filename: fname,
          state: parsed,
          payloadHash: preview.payloadHash,
          preview
        };
        renderJsonRestorePreviewModal(preview);
      } catch (err) {
        showToast(`تعذّر معاينة ملف JSON: ${err.message}`, true);
      } finally {
        e.target.value = '';
      }
    };
    reader.readAsText(file);
  });

  const btnConfirmJson = $('#btnConfirmJsonRestore');
  if (btnConfirmJson) {
    btnConfirmJson.addEventListener('click', async () => {
      if (!_pendingJsonRestore) return;
      const pending = _pendingJsonRestore;
      try {
        const res = await apiRequest('/json-restore/commit?allow_legacy_dates=true', {
          method: 'POST',
          body: {
            filename: pending.filename,
            state: pending.state,
            expectedPayloadHash: pending.payloadHash,
            confirmRestore: true,
            allowEmptyCollections: true
          }
        });
        closeJsonRestoreModal();
        await reloadFullStateFromServer();
        showToast(`تمت استعادة قاعدة البيانات بنجاح (نسخة الأمان قبل الاستعادة: ${res.snapshot || 'محفوظة'}).`);
      } catch (err) {}
    });
  }

  // Payroll preparation pre-fill button (never auto-executes payout)
  const payrollPrepWrap = $('#payrollPrepWrap');
  if (payrollPrepWrap) {
    payrollPrepWrap.addEventListener('click', (e) => {
      const btn = e.target.closest('.btn-prefill-payout');
      if (!btn) return;
      const tName = btn.getAttribute('data-teacher') || '';
      const amt = btn.getAttribute('data-amount') || '';
      if ($('#payTeacherSelect') && tName) $('#payTeacherSelect').value = tName;
      if ($('#payAmountInput') && amt) $('#payAmountInput').value = amt;
      if ($('#payDateInput') && !$('#payDateInput').value) $('#payDateInput').value = uiState.referenceDate;
      if ($('#payNoteInput')) $('#payNoteInput').value = `صرف مستحقات شهر ${monthLabel(uiState.selectedMonth)}`;
      if ($('#payAmountInput')) $('#payAmountInput').focus();
      showToast(`تم تعبئة نموذج الصرف للمعلمة «${tName}» بمبلغ ${money(amt)} — اضغط «تسجيل عملية الصرف» للاعتماد.`);
    });
  }

  // Safe Preview-First Bulk Operations (Phase 4 Task 10)
  let _pendingBulkOp = null;
  const bulkOpSel = $('#bulkOpSelect');
  if (bulkOpSel) {
    bulkOpSel.addEventListener('change', () => {
      const op = bulkOpSel.value;
      if ($('#bulkTeacherGroup')) $('#bulkTeacherGroup').style.display = op === 'assign_teacher' ? '' : 'none';
      if ($('#bulkPackageGroup')) $('#bulkPackageGroup').style.display = op === 'assign_package' ? '' : 'none';
      if ($('#bulkStartDateGroup')) $('#bulkStartDateGroup').style.display = op === 'set_actual_start_date' ? '' : 'none';
    });
  }

  const closeBulkPreviewModal = () => {
    _pendingBulkOp = null;
    const modal = $('#bulkPreviewModal');
    if (modal) modal.classList.remove('open');
  };

  ['#btnCloseBulkPreviewModal', '#btnCancelBulkPreview'].forEach((sel) => {
    const btn = $(sel);
    if (btn) btn.addEventListener('click', closeBulkPreviewModal);
  });

  const btnPreviewBulk = $('#btnPreviewBulkOp');
  if (btnPreviewBulk) {
    btnPreviewBulk.addEventListener('click', async () => {
      const op = ($('#bulkOpSelect') && $('#bulkOpSelect').value) || 'assign_teacher';
      const filteredStuds = getFilteredStudents();
      const studentIds = filteredStuds.map((s) => s.id);
      if (studentIds.length === 0) {
        showToast('لا توجد طالبات مطابقة للفلاتر الحالية لتطبيق العملية الجماعية.', true);
        return;
      }
      const params = {
        studentIds,
        month: uiState.selectedMonth,
        newTeacher: ($('#bulkTeacherSelect') && $('#bulkTeacherSelect').value) || '',
        newPackage: ($('#bulkPackageSelect') && $('#bulkPackageSelect').value) || '',
        actualStartDate: ($('#bulkActualStartInput') && $('#bulkActualStartInput').value) || uiState.referenceDate,
        autoCalculateEndDate: true
      };
      try {
        const preview = await apiRequest('/bulk/preview', {
          method: 'POST',
          readOnlyPost: true,
          sendRev: false,
          body: { operation: op, params }
        });
        _pendingBulkOp = { operation: op, params, previewHash: preview.previewHash, preview };
        renderHtml('#bulkPreviewWarningBanner', html`
          <div class="dates-rule-banner" style="margin:0;">
            <div>
              <strong>🛡️ معاينة العملية الجماعية:</strong> ${preview.summaryText || ''}
              <div style="font-size:12px;margin-top:4px;">سيتم حفظ نسخة أمان فورية (<code>alahd_safety_pre_bulk_*.sqlite3</code>) قبل تطبيق التغييرات في معاملة ذرية واحدة.</div>
            </div>
          </div>
        `);
        renderHtml('#bulkPreviewSummaryKpis', html`
          <div class="kpi-card">
            <div class="kpi-label">إجمالي السجلات المفحوصة</div>
            <div class="kpi-value">${preview.targetCount ?? studentIds.length}</div>
          </div>
          <div class="kpi-card kpi-pay">
            <div class="kpi-label">سجلات ستتغير فعليًا</div>
            <div class="kpi-value">${preview.affectedCount ?? 0}</div>
          </div>
          <div class="kpi-card">
            <div class="kpi-label">بدون تغيير</div>
            <div class="kpi-value">${preview.unchangedCount ?? 0}</div>
          </div>
        `);
        const rows = Array.isArray(preview.previewRows) ? preview.previewRows : [];
        renderHtml('#bulkPreviewTableWrap', html`
          <table class="data-table">
            <thead>
              <tr><th>الطالبة / السجل</th><th>قبل التعديل</th><th>بعد التعديل</th></tr>
            </thead>
            <tbody>
              ${rows.length ? rows.map((r) => html`
                <tr>
                  <td style="text-align:right;font-weight:700;">${r.name || r.id}</td>
                  <td>${r.before || '—'}</td>
                  <td><span class="badge badge-end">${r.after || '—'}</span></td>
                </tr>
              `) : html`<tr><td colspan="3">لا توجد تغييرات مطلوبة على السجلات المحددة.</td></tr>`}
            </tbody>
          </table>
        `);
        const modal = $('#bulkPreviewModal');
        if (modal) modal.classList.add('open');
      } catch (err) {}
    });
  }

  const btnConfirmBulk = $('#btnConfirmBulkOp');
  if (btnConfirmBulk) {
    btnConfirmBulk.addEventListener('click', async () => {
      if (!_pendingBulkOp) return;
      if ((_pendingBulkOp.preview && _pendingBulkOp.preview.affectedCount) === 0) {
        closeBulkPreviewModal();
        showToast('لا توجد سجلات تتطلب تعديلًا.');
        return;
      }
      const pending = _pendingBulkOp;
      try {
        const res = await apiRequest('/bulk/execute', {
          method: 'POST',
          body: {
            operation: pending.operation,
            params: pending.params,
            expectedPreviewHash: pending.previewHash,
            confirm: true
          }
        });
        closeBulkPreviewModal();
        await reloadFullStateFromServer();
        showToast(`تم تنفيذ العملية الجماعية على ${res.updatedCount || 0} سجل بنجاح (نسخة الأمان: ${res.snapshot || 'محفوظة'}).`);
      } catch (err) {}
    });
  }

  // Keyboard shortcut Ctrl+K
  document.addEventListener('keydown', (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'k') {
      e.preventDefault();
      const sBox = $('#globalSearchInput');
      if (sBox) sBox.focus();
    }
  });
}

function initDatesDefaults() {
  ['#instPayDateInput', '#payDateInput', '#txDateInput', '#planActualStartInput', '#memDateInput', '#certDateInput'].forEach((sel) => {
    const el = $(sel);
    if (el && !el.value) el.value = uiState.referenceDate;
  });
}

async function bootstrap() {
  try {
    const savedTheme = localStorage.getItem(THEME_KEY);
    if (savedTheme === 'dark' || savedTheme === 'light') {
      document.documentElement.setAttribute('data-theme', savedTheme);
    }
  } catch (e) {}

  initDatesDefaults();
  wireSaveGuardAndRetry(reloadFullStateFromServer);
  wireMainEvents();
  wireStatisticsEvents(triggerXlsxDownload, triggerHtmlDownload);
  wireAlertsEvents(reloadFullStateFromServer, triggerHtmlDownload);
  wireImportEvents(reloadFullStateFromServer);
  wireWizardAndSystemEvents(reloadFullStateFromServer);

  initRouter((viewName) => {
    renderAll();
    if (viewName === 'alerts') {
      refreshAlertsAndBadges();
    }
  });

  await reloadFullStateFromServer();
  handleCurrentHash();
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', bootstrap);
} else {
  bootstrap();
}
