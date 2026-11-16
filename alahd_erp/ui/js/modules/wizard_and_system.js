/* ============================================================================
   modules/wizard_and_system.js — Onboarding Wizard, PIN Lock, Month Lock,
   13-Category Settings & Control Center, Windows Auto-Start, Demo Quarantine
   Cleaner, Comprehensive Data Quality Audit & System Diagnostics (v1.2.0)
   ============================================================================ */

import { $, $all, html, renderHtml, showToast } from './dom.js';
import { FE, state, uiState, monthLabel } from './store.js';
import { apiRequest, downloadBlobFromApi } from './api.js';
import { navigateTo } from './router.js';

let _wizLogoDataUri = '';
let _idleTimerId = null;
let _activeSettingsCat = 'all';

const CATEGORY_LABELS = {
  all: '📋 جميع الأقسام (13)',
  general: '🏛️ 1. الإعدادات العامة والتشغيل التلقائي',
  appearance: '🎨 2. المظهر واللغة والعرض',
  students: '👩‍🎓 3. إعدادات الطالبات والتسجيل',
  teachers: '👩‍🏫 4. إعدادات المعلمات والأقسام',
  packages: '📦 5. إعدادات الباقات والاشتراكات',
  attendance: '🗓️ 6. إعدادات الحضور والجلسات',
  financial: '⚖️ 7. إعدادات وقواعد النظام المالي',
  reports: '📊 8. إعدادات التقارير والطباعة',
  import_export: '🔄 9. إعدادات الاستيراد والتصدير',
  backup_restore: '💾 10. إعدادات النسخ الاحتياطي والاستعادة',
  notifications: '🔔 11. إعدادات التنبيهات والإشعارات',
  system_info: '🖥️ 12. معلومات النظام والتشخيص',
  maintenance: '🛠️ 13. أدوات الصيانة والإدارة المتقدمة'
};

export function applyBrandingAndSettingsToUI() {
  const s = state.settings || {};
  const titleEl = $('#brandTitle');
  if (titleEl) {
    titleEl.textContent = s.academyName || 'أكاديمية يوم جديد';
  }
  const subEl = $('#brandSubtitle');
  if (subEl) {
    const subText = s.academySubtitle || 'يوم جديد هو عهد جديد';
    subEl.textContent = `${subText} • v1.2.0`;
  }
  const logoEl = $('#sidebarLogo');
  if (logoEl) {
    if (s.logoDataUri && String(s.logoDataUri).startsWith('data:image/')) {
      renderHtml(logoEl, html`<img src="${s.logoDataUri}" alt="شعار" style="width:100%;height:100%;object-fit:cover;border-radius:50%;">`);
    } else {
      renderHtml(logoEl, html`<img src="assets/logo.png" alt="شعار أكاديمية يوم جديد" style="width:100%;height:100%;object-fit:cover;border-radius:50%;">`);
    }
  }

  // Apply appearance & layout settings directly to documentElement
  if (typeof document !== 'undefined' && document.documentElement) {
    const themeVal = s.theme || (typeof localStorage !== 'undefined' && localStorage.getItem('alahd_erp_theme_v1')) || 'light';
    document.documentElement.setAttribute('data-theme', themeVal);
    document.documentElement.setAttribute('data-font-scale', s.fontScale || 'md');
    document.documentElement.setAttribute('data-table-density', s.tableDensity || 'comfortable');
    document.documentElement.setAttribute('data-zebra', s.tableZebraStripes !== false ? 'true' : 'false');
    document.documentElement.setAttribute('data-sidebar-compact', s.sidebarCompact ? 'true' : 'false');
  }

  // Update month lock badge in topbar
  const lockBadge = $('#monthLockBadge');
  if (lockBadge) {
    const isClosed = Boolean(state.closedMonths && state.closedMonths[uiState.selectedMonth]);
    lockBadge.style.display = isClosed ? 'inline-flex' : 'none';
  }

  // Populate System settings form across all categories
  if ($('#setAcademyName')) $('#setAcademyName').value = s.academyName || 'أكاديمية يوم جديد';
  if ($('#setAcademySubtitle')) $('#setAcademySubtitle').value = s.academySubtitle || 'نظام إدارة الأكاديمية الموحد';
  if ($('#setCurrency')) $('#setCurrency').value = s.currency || 'ج.م';
  if ($('#setDefaultLandingView')) $('#setDefaultLandingView').value = s.defaultLandingView || 'dashboard';
  if ($('#setDefaultPageSize')) $('#setDefaultPageSize').value = s.defaultPageSize || 50;
  if ($('#setStartMinimized')) $('#setStartMinimized').checked = Boolean(s.startMinimized);

  if ($('#setTheme')) $('#setTheme').value = s.theme || 'light';
  if ($('#setFontScale')) $('#setFontScale').value = s.fontScale || 'md';
  if ($('#setTableDensity')) $('#setTableDensity').value = s.tableDensity || 'comfortable';
  if ($('#setUseArabicDigits')) $('#setUseArabicDigits').checked = Boolean(s.useArabicDigits);
  if ($('#setTableZebraStripes')) $('#setTableZebraStripes').checked = s.tableZebraStripes !== false;
  if ($('#setSidebarCompact')) $('#setSidebarCompact').checked = Boolean(s.sidebarCompact);

  if ($('#setStudentCodePrefix')) $('#setStudentCodePrefix').value = s.studentCodePrefix !== undefined ? s.studentCodePrefix : 'STD-';
  if ($('#setDefaultStudentViewMode')) $('#setDefaultStudentViewMode').value = s.defaultStudentViewMode || 'table';
  if ($('#setRequireStudentPhone')) $('#setRequireStudentPhone').checked = Boolean(s.requireStudentPhone);
  if ($('#setStudentStatusesInput') && Array.isArray(s.studentStatuses)) {
    $('#setStudentStatusesInput').value = s.studentStatuses.join(', ');
  }

  if ($('#setDefaultTeacherPct')) $('#setDefaultTeacherPct').value = s.defaultTeacherPct !== undefined ? s.defaultTeacherPct : 70;
  if ($('#setPctSource')) $('#setPctSource').value = s.pctSource || 'student';

  if ($('#setDurationDays')) $('#setDurationDays').value = s.defaultDurationDays || 30;
  if ($('#setExpiringDays')) $('#setExpiringDays').value = s.expiringSoonDays !== undefined ? s.expiringSoonDays : 5;
  if ($('#setMonthsRangePast')) $('#setMonthsRangePast').value = s.monthsRangePast || 6;
  if ($('#setMonthsRangeFuture')) $('#setMonthsRangeFuture').value = s.monthsRangeFuture || 6;

  if ($('#setDefaultSessionDurationMin')) $('#setDefaultSessionDurationMin').value = s.defaultSessionDurationMin || 40;
  if ($('#setDefaultPlanSessionCount')) $('#setDefaultPlanSessionCount').value = s.defaultPlanSessionCount || 8;
  if ($('#setDefaultSessionStartTime')) $('#setDefaultSessionStartTime').value = s.defaultSessionStartTime || '16:00';
  if ($('#setConsecutiveAbsenceThreshold')) $('#setConsecutiveAbsenceThreshold').value = s.consecutiveAbsenceThreshold || 2;

  if ($('#setWageBasis')) $('#setWageBasis').value = s.wageBasis || 'net';
  if ($('#setWageAccrualMode')) $('#setWageAccrualMode').value = s.wageAccrualMode || 'accrued';

  if ($('#setDefaultStatsPeriod')) $('#setDefaultStatsPeriod').value = s.defaultStatsPeriod || 'all_time';
  if ($('#setDefaultCollectionBasis')) $('#setDefaultCollectionBasis').value = s.defaultCollectionBasis || 'cash';
  if ($('#setHidePersonalDataByDefault')) $('#setHidePersonalDataByDefault').checked = s.hidePersonalDataByDefault !== false;
  if ($('#htmlExportHidePrivacy')) $('#htmlExportHidePrivacy').checked = s.hidePersonalDataByDefault !== false;

  if ($('#setDefaultImportMode')) $('#setDefaultImportMode').value = s.defaultImportMode || 'update_existing';
  if ($('#setDefaultAutoCreateTeachers')) $('#setDefaultAutoCreateTeachers').checked = s.defaultAutoCreateTeachers !== false;
  if ($('#setDefaultAutoCreatePackages')) $('#setDefaultAutoCreatePackages').checked = s.defaultAutoCreatePackages !== false;

  if ($('#setExtraBackupFolder')) $('#setExtraBackupFolder').value = s.extraBackupFolder || '';
  if ($('#setBackupWarnDays')) $('#setBackupWarnDays').value = s.backupWarnDays || 3;
  if ($('#setIdleMinutes')) $('#setIdleMinutes').value = s.idleLockMinutes !== undefined ? s.idleLockMinutes : 15;
  if ($('#setShowDashAlertsStrip')) $('#setShowDashAlertsStrip').checked = s.showDashAlertsStrip !== false;
}

export function openOnboardingWizard() {
  const s = state.settings || {};
  if ($('#wizAcademyName')) $('#wizAcademyName').value = s.academyName || 'أكاديمية يوم جديد';
  if ($('#wizCurrency')) $('#wizCurrency').value = s.currency || 'ج.م';
  if ($('#wizDurationDays')) $('#wizDurationDays').value = s.defaultDurationDays || 30;
  if ($('#wizExpiringDays')) $('#wizExpiringDays').value = s.expiringSoonDays || 5;
  if ($('#wizTeacherPct')) $('#wizTeacherPct').value = s.defaultTeacherPct !== undefined ? s.defaultTeacherPct : 70;
  if ($('#wizWageBasis')) $('#wizWageBasis').value = s.wageBasis || 'net';
  if ($('#wizWageAccrual')) $('#wizWageAccrual').value = s.wageAccrualMode || 'accrued';
  if ($('#wizTeachersList') && state.teachers.length > 0) {
    $('#wizTeachersList').value = state.teachers.map((t) => {
      const pct = (state.teacherMeta[t] && state.teacherMeta[t].pct) || 70;
      return `${t} | ${pct}`;
    }).join('\n');
  }
  if ($('#wizPackagesList') && state.subTypes.length > 0) {
    $('#wizPackagesList').value = state.subTypes.join('\n');
  }
  const modal = $('#onboardingModal');
  if (modal) modal.classList.add('open');
}

export function closeOnboardingWizard() {
  const modal = $('#onboardingModal');
  if (modal) modal.classList.remove('open');
}

export function checkPinAndIdleLock() {
  if (state.pinEnabled && !uiState.pinUnlocked) {
    const pinModal = $('#pinLockModal');
    if (pinModal) pinModal.classList.add('open');
  }
  resetIdleLockTimer();
}

function resetIdleLockTimer() {
  if (_idleTimerId) {
    clearTimeout(_idleTimerId);
    _idleTimerId = null;
  }
  const mins = Number((state.settings && state.settings.idleLockMinutes) || 0);
  if (state.pinEnabled && mins > 0) {
    _idleTimerId = setTimeout(() => {
      uiState.pinUnlocked = false;
      const pinModal = $('#pinLockModal');
      if (pinModal) pinModal.classList.add('open');
    }, mins * 60 * 1000);
  }
}

function filterSettingsByCategoryAndQuery() {
  const q = (($('#settingsSearchInput') && $('#settingsSearchInput').value) || '').trim().toLowerCase();
  const cat = _activeSettingsCat || 'all';
  const badge = $('#settingsActiveCategoryBadge');
  if (badge) {
    badge.textContent = CATEGORY_LABELS[cat] || CATEGORY_LABELS.all;
  }

  $all('.setting-item').forEach((item) => {
    const itemCats = (item.getAttribute('data-settings-cat') || '').split(/\s+/);
    const kw = (item.getAttribute('data-setting-keywords') || '').toLowerCase();
    const labelTxt = (item.textContent || '').toLowerCase();
    const matchCat = cat === 'all' || itemCats.includes(cat);
    const matchQ = !q || kw.includes(q) || labelTxt.includes(q);
    item.style.display = (matchCat && matchQ) ? '' : 'none';
  });

  $all('.settings-section-card').forEach((card) => {
    const cardCats = (card.getAttribute('data-settings-cat') || '').split(/\s+/);
    const kw = (card.getAttribute('data-setting-keywords') || '').toLowerCase();
    const matchCat = cat === 'all' || cardCats.includes(cat);
    if (card.id === 'mainSettingsFormCard') {
      const visibleItems = Array.from(card.querySelectorAll('.setting-item')).some((el) => el.style.display !== 'none');
      card.style.display = (matchCat && visibleItems) ? '' : 'none';
    } else {
      const matchQ = !q || kw.includes(q) || (card.textContent || '').toLowerCase().includes(q);
      card.style.display = (matchCat && matchQ) ? '' : 'none';
    }
  });
}

function renderAutostartBox(auto) {
  if (!auto) return;
  const badge = $('#autostartStatusBadge');
  const repairBtn = $('#btnRepairAutoStart');
  const toggleBtn = $('#btnToggleAutoStart');
  if (badge) {
    if (auto.status === 'enabled') {
      badge.className = 'badge badge-end';
      badge.textContent = '✓ مفعّل في سجل ويندوز';
    } else if (auto.status === 'needs_repair') {
      badge.className = 'badge badge-warn';
      badge.textContent = '⚠️ يحتاج إصلاح المسار';
    } else if (auto.status === 'unavailable_source_mode') {
      badge.className = 'badge badge-pay';
      badge.textContent = 'ℹ️ وضع المصدر (يتفعل في نسخة EXE)';
    } else {
      badge.className = 'badge';
      badge.textContent = 'معطّل حاليًا';
    }
  }
  if (repairBtn) {
    repairBtn.style.display = auto.status === 'needs_repair' ? 'inline-flex' : 'none';
  }
  if (toggleBtn) {
    const isEn = auto.status === 'enabled' || Boolean(state.settings && state.settings.autoStartEnabled);
    toggleBtn.textContent = isEn ? '⏸️ إيقاف التشغيل التلقائي' : '🚀 تفعيل التشغيل التلقائي';
  }
  renderHtml('#autostartDetailsBox', html`
    <div><strong>الحالة الفعلية:</strong> ${auto.messageAr || '—'}</div>
    <div style="margin-top:4px;font-size:12px;color:var(--text-secondary);">
      <strong>مسار التسجيل في ويندوز:</strong> <code dir="ltr">HKCU\\${auto.registryKeyPath || 'Software\\Microsoft\\Windows\\CurrentVersion\\Run'}</code> (الاسم: <code dir="ltr">${auto.valueName || 'YawmJadidAcademy'}</code>)
    </div>
    ${auto.registeredCommand ? html`
      <div style="margin-top:4px;font-size:12px;">
        <strong>الأمر المسجّل حاليًا:</strong> <code dir="ltr">${auto.registeredCommand}</code>
      </div>
    ` : html`
      <div style="margin-top:4px;font-size:12px;color:var(--text-muted);">
        <strong>الأمر المتوقع عند التفعيل (EXE):</strong> <code dir="ltr">${auto.expectedCommand || '"YawmJadidAcademy.exe" --autostart'}</code>
      </div>
    `}
  `);
}

function renderHardcodedPoliciesBox(policies) {
  if (!Array.isArray(policies) || !policies.length) return;
  renderHtml('#hardcodedFinancialPoliciesBox', html`
    <div style="display:flex;flex-direction:column;gap:6px;">
      ${policies.map((p) => html`
        <div style="padding:7px 10px;border:1px solid var(--border-color);border-radius:8px;background:var(--bg-subtle);">
          <div style="font-weight:700;color:var(--brand-primary);">🔒 ${p.labelAr}: <span style="color:var(--text-main);">${p.valueAr}</span></div>
          <div style="font-size:11.5px;color:var(--text-muted);">${p.reasonAr}</div>
        </div>
      `)}
    </div>
  `);
}

function renderDemoInventoryBox(inv) {
  if (!inv || !inv.summary) return;
  const s = inv.summary;
  renderHtml('#demoInventorySummaryBox', html`
    <div style="padding:8px 12px;border-radius:8px;border:1px solid var(--border-color);background:var(--bg-subtle);">
      <div>
        <strong>📊 فحص قاعدة البيانات الحالية:</strong>
        سجلات تجريبية مؤكدة (Class A): <b>${s.classAConfirmedDemoStudents || 0}</b> طالبة / <b>${s.classAConfirmedDemoTeachers || 0}</b> معلمة |
        سجلات تجريبية مرجحة (Class B): <b>${s.classBProbableDemoStudents || 0}</b> |
        سجلات حقيقية محمية (Class C): <span class="badge badge-end">${s.classCProtectedStudents || 0} طالبة</span>
      </div>
      <div style="font-size:11.5px;color:var(--text-muted);margin-top:3px;">
        عند الضغط على «فحص وعزل البيانات التجريبية»، يحفظ النظام نسخة احتياطية كاملة أولًا ثم يصدّر السجلات المعزولة إلى مجلد <code>quarantine/</code> بصيغتي JSON و CSV قبل حذفها.
      </div>
    </div>
  `);
}

function renderSystemDiagnosticsBox(diag, logLines) {
  if (!diag) return;
  const tc = diag.tableCounts || {};
  renderHtml('#systemDiagnosticsBox', html`
    <div class="kpi-grid" style="margin-bottom:12px;">
      <div class="kpi-card">
        <div class="kpi-label">إصدار التطبيق والمخطط</div>
        <div class="kpi-value">v${diag.appVersion || '1.2.0'} (Schema v${diag.schemaVersion || 3})</div>
        <div class="kpi-sub">${diag.pythonTier || ''}</div>
      </div>
      <div class="kpi-card kpi-end">
        <div class="kpi-label">سلامة قاعدة البيانات (SQLite)</div>
        <div class="kpi-value">${diag.integrityOk ? '✓ سليمة (OK)' : '⚠️ خطأ'}</div>
        <div class="kpi-sub">وضع: ${String(diag.journalMode || 'wal').toUpperCase()} | انتهاكات FK: ${diag.foreignKeyViolations || 0}</div>
      </div>
      <div class="kpi-card kpi-pay">
        <div class="kpi-label">حجم قاعدة البيانات والسجل</div>
        <div class="kpi-value">${Math.max(1, Math.round((diag.dbSizeBytes || 0) / 1024))} KB</div>
        <div class="kpi-sub">WAL: ${Math.round((diag.walSizeBytes || 0) / 1024)} KB | Log: ${Math.round((diag.logSizeBytes || 0) / 1024)} KB</div>
      </div>
    </div>

    ${diag.isSyncedAppHome ? html`
      <div class="alerts-strip" style="margin-bottom:10px;">
        <div><strong>⚠️ تحذير مسار التخزين السحابي:</strong> مجلد بيانات التطبيق يقع داخل مجلد متزامن سحابيًا (OneDrive/Dropbox). يُنصح بإبقاء قاعدة البيانات النشطة في <code>%LOCALAPPDATA%\\AlAhdAcademySuite</code> لتجنب تعارض أقفال WAL.</div>
      </div>
    ` : ''}

    <div style="font-size:12.5px;display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:8px;margin-bottom:10px;">
      <div><strong>📁 مسار قاعدة البيانات:</strong> <code dir="ltr">${diag.dbPath || ''}</code></div>
      <div><strong>📂 مجلد النسخ الاحتياطية:</strong> <code dir="ltr">${diag.backupsDir || ''}</code></div>
      <div><strong>🛡️ مجلد العزل (Quarantine):</strong> <code dir="ltr">${diag.quarantineDir || ''}</code></div>
      <div><strong>🐍 مفسر بايثون:</strong> <code dir="ltr">Python ${diag.pythonVersion || ''} (${diag.platform || ''})</code></div>
    </div>

    <div style="font-size:12px;color:var(--text-secondary);margin-bottom:10px;">
      <strong>إحصاء الجداول:</strong>
      الطالبات: <b>${tc.students || 0}</b> |
      المعلمات: <b>${tc.teachers || 0}</b> |
      الاشتراكات الشهرية: <b>${tc.months || 0}</b> |
      الإيصالات: <b>${tc.installments || 0}</b> |
      الرواتب: <b>${tc.salaries || 0}</b> |
      الخزينة: <b>${tc.transactions || 0}</b> |
      الحصص: <b>${tc.sessions || 0}</b> |
      التسميع: <b>${tc.memorization_logs || 0}</b>
    </div>

    ${Array.isArray(logLines) && logLines.length ? html`
      <details style="font-size:12px;">
        <summary style="cursor:pointer;font-weight:700;color:var(--brand-primary);">📄 عرض آخر سطور سجل النظام (alahd_erp.log — ${logLines.length} سطر)</summary>
        <pre dir="ltr" style="max-height:180px;overflow-y:auto;background:var(--bg-subtle);padding:8px;border-radius:8px;border:1px solid var(--border-color);font-size:11px;margin-top:6px;">${logLines.join('\n')}</pre>
      </details>
    ` : ''}
  `);
}

export async function loadSettingsAuditLogs() {
  const wrap = $('#settingsAuditTableWrap');
  if (!wrap) return;
  try {
    const res = await apiRequest('/settings/audit?limit=30');
    const items = (res && Array.isArray(res.items)) ? res.items : [];
    renderHtml(wrap, html`
      <table class="data-table">
        <thead>
          <tr><th>الوقت</th><th>الإعداد</th><th>القيمة السابقة</th><th>القيمة الجديدة</th></tr>
        </thead>
        <tbody>
          ${items.length ? items.map((it) => html`
            <tr>
              <td dir="ltr" style="font-size:11.5px;">${it.tsIso || '—'}</td>
              <td style="text-align:right;font-weight:700;">${it.labelAr || it.key}</td>
              <td dir="ltr" style="font-size:12px;">${JSON.stringify(it.oldValue)}</td>
              <td dir="ltr" style="font-size:12px;"><span class="badge badge-end">${JSON.stringify(it.newValue)}</span></td>
            </tr>
          `) : html`<tr><td colspan="4">لا توجد تغييرات مسجلة على الإعدادات بعد.</td></tr>`}
        </tbody>
      </table>
    `);
  } catch (err) {}
}

export async function renderSystemHealthView() {
  applyBrandingAndSettingsToUI();
  filterSettingsByCategoryAndQuery();

  // Closed months list
  const closedKeys = Object.keys(state.closedMonths || {}).sort();
  renderHtml('#closedMonthsList', closedKeys.length
    ? html`<strong>الشهور المقفلة حاليًا:</strong> ${closedKeys.map((mk) => html`<span class="badge badge-warn" style="margin:2px;">🔒 ${monthLabel(mk)} (${mk})</span>`)}`
    : html`<span style="color:var(--text-muted);">لا توجد شهور مقفلة حاليًا.</span>`
  );

  // Load Settings Catalog, Auto-Start Status, Demo Inventory, and System Diagnostics
  try {
    const [catalogRes, demoInv, diagRes, logsRes] = await Promise.all([
      apiRequest('/settings/catalog').catch(() => null),
      apiRequest('/demo/inventory').catch(() => null),
      apiRequest('/system/diagnostics').catch(() => null),
      apiRequest('/system/logs?lines=40').catch(() => null)
    ]);
    if (catalogRes) {
      renderAutostartBox(catalogRes.autostart);
      renderHardcodedPoliciesBox(catalogRes.hardcodedFinancialPolicies);
    }
    if (demoInv) {
      renderDemoInventoryBox(demoInv);
    }
    if (diagRes) {
      renderSystemDiagnosticsBox(diagRes, logsRes && logsRes.lines ? logsRes.lines : []);
    }
  } catch (err) {}

  // Duplicate & Dates check + Comprehensive Data Quality Audit
  const dups = FE.detectDuplicateStudents ? FE.detectDuplicateStudents(state.students) : [];
  let independentDatesCount = 0;
  state.students.forEach((s) => {
    Object.keys(s.months || {}).forEach((mk) => {
      const m = s.months[mk] || {};
      if (FE.resolvePaymentDate(m) || FE.resolveActualStartDate(m)) independentDatesCount++;
    });
  });

  let dqReport = null;
  try {
    dqReport = await apiRequest(`/data-quality?reference_date=${encodeURIComponent(uiState.referenceDate)}`);
  } catch (err) {
    dqReport = null;
  }

  const dqSummary = (dqReport && dqReport.summary) || { totalIssues: dups.length, critical: 0, warning: dups.length, info: 0 };
  const dqIssues = Array.isArray(dqReport && dqReport.issues) ? dqReport.issues : [];

  renderHtml('#dataHealthReport', html`
    <div class="kpi-grid">
      <div class="kpi-card kpi-wage">
        <div class="kpi-label">إجمالي الطالبات المسجلات</div>
        <div class="kpi-value">${state.students.length} طالبة</div>
      </div>
      <div class="kpi-card kpi-pay">
        <div class="kpi-label">سجلات التواريخ المفصولة</div>
        <div class="kpi-value">${independentDatesCount} سجل</div>
        <div class="kpi-sub">Payment Date ⊥ Actual Start Date</div>
      </div>
      <div class="kpi-card ${dqSummary.totalIssues > 0 ? 'kpi-gold' : ''}">
        <div class="kpi-label">ملاحظات جودة البيانات الشاملة</div>
        <div class="kpi-value">${dqSummary.totalIssues}</div>
        <div class="kpi-sub">🔴 حرجة: ${dqSummary.critical || 0} | 🟠 تحذير: ${dqSummary.warning || 0} | 🔵 معلومة: ${dqSummary.info || 0}</div>
      </div>
    </div>

    <div style="display:flex;gap:8px;flex-wrap:wrap;margin:10px 0;">
      <button type="button" class="btn btn-sm btn-primary" id="btnRunDataQualityNormalize">✨ معاينة وتوحيد الأكواد وأرقام الهواتف تلقائيًا</button>
    </div>

    ${dqIssues.length > 0 ? html`
      <div class="table-wrap" style="margin-top:10px;max-height:320px;overflow-y:auto;">
        <table class="data-table">
          <thead>
            <tr>
              <th>الأهمية</th>
              <th>المشكلة المكتشفة</th>
              <th>التفسير والإجراء الآمن المقترح</th>
              <th>انتقال</th>
            </tr>
          </thead>
          <tbody>
            ${dqIssues.slice(0, 40).map((iss) => {
              const sevBadge = iss.severity === 'critical'
                ? html`<span class="badge badge-unpaid">🔴 حرجة</span>`
                : (iss.severity === 'warning'
                  ? html`<span class="badge badge-warn">🟠 تحذير</span>`
                  : html`<span class="badge badge-pay">🔵 معلومة</span>`);
              return html`
                <tr>
                  <td>${sevBadge}</td>
                  <td style="text-align:right;font-weight:700;">${iss.title}</td>
                  <td style="text-align:right;font-size:12px;">
                    <div>${iss.detail}</div>
                    ${iss.suggestion ? html`<div style="color:var(--brand-primary);margin-top:2px;">💡 <b>المقترح:</b> ${iss.suggestion}</div>` : ''}
                  </td>
                  <td>
                    <button type="button" class="btn btn-sm btn-dq-deeplink" data-deeplink="${iss.deepLink || '#/students'}">🎯 فتح</button>
                  </td>
                </tr>
              `;
            })}
          </tbody>
        </table>
      </div>
    ` : html`
      <p style="font-size:12.5px;color:var(--end-text);font-weight:700;margin-top:8px;">
        ✅ لم يتم رصد أي تعارضات أو نواقص أو تكرارات في فحص جودة البيانات الشامل (10 محاور رقابية).
      </p>
    `}
    <p style="font-size:12.5px;color:var(--text-secondary);margin-top:8px;">
      ✅ قاعدة البيانات تعمل بوضع <code>WAL + synchronous=FULL</code> مع فحص <code>PRAGMA integrity_check</code> و <code>PRAGMA foreign_key_check</code> لكل نسخة احتياطية.
    </p>
  `);

  // Backups & Backup Health
  try {
    const [backups, health] = await Promise.all([
      apiRequest('/backups'),
      apiRequest('/backups/health')
    ]);
    if (health) {
      renderHtml('#backupHealthBanner', health.staleWarning || health.missingExpectedBackup || (health.extraFolderConfigured && !health.extraFolderReachable)
        ? html`
          <div class="alerts-strip">
            <div>
              <strong>⚠️ تنبيه النسخ الاحتياطي:</strong>
              ${health.missingExpectedBackup ? 'لا توجد أي نسخة احتياطية محفوظة حتى الآن رغم وجود بيانات طالبات مسجلة! ' : ''}
              ${(!health.missingExpectedBackup && health.staleWarning) ? `آخر نسخة احتياطية منذ ${health.ageDays ?? '—'} يوم. ` : ''}
              ${(health.extraFolderConfigured && !health.extraFolderReachable) ? `مجلد النسخ الإضافي (${health.extraFolder}) غير متصل حاليًا.` : ''}
            </div>
          </div>
        `
        : ''
      );
    }

    if (Array.isArray(backups)) {
      renderHtml('#backupsTableWrap', html`
        <table class="data-table">
          <thead>
            <tr><th>اسم النسخة</th><th>الحجم</th><th>سلامة الفحص</th><th>إجراء</th></tr>
          </thead>
          <tbody>
            ${backups.length ? backups.slice(0, 15).map((b) => html`
              <tr>
                <td dir="ltr">${b.name}</td>
                <td>${Math.max(1, Math.round((b.size || 0) / 1024))} KB</td>
                <td><span class="badge badge-end">✓ مفحوصة</span></td>
                <td>
                  <button type="button" class="btn btn-sm btn-restore-backup" data-name="${b.name}">♻️ استعادة</button>
                </td>
              </tr>
            `) : html`<tr><td colspan="4">لا توجد نسخ احتياطية بعد.</td></tr>`}
          </tbody>
        </table>
      `);
    }
  } catch (err) {}

  await Promise.all([
    loadSettingsAuditLogs(),
    loadAuditLogs()
  ]);
}

export async function loadAuditLogs() {
  const auditWrap = $('#auditTableWrap');
  if (!auditWrap) return;
  const act = ($('#auditFilterAction') && $('#auditFilterAction').value) || '';
  const q = ($('#auditFilterQuery') && $('#auditFilterQuery').value) || '';
  try {
    const logs = await apiRequest(`/audit?limit=40&action=${encodeURIComponent(act)}&q=${encodeURIComponent(q)}`);
    if (!Array.isArray(logs)) return;
    renderHtml(auditWrap, html`
      <table class="data-table">
        <thead>
          <tr><th>الوقت</th><th>المستخدم</th><th>العملية</th><th>التفاصيل</th></tr>
        </thead>
        <tbody>
          ${logs.length ? logs.map((lg) => {
            const dtStr = lg.ts ? new Date(lg.ts * 1000).toLocaleString('ar-EG') : '—';
            return html`
              <tr>
                <td dir="ltr">${dtStr}</td>
                <td>${lg.actor || 'admin'}</td>
                <td><span class="badge badge-start">${lg.action || ''}</span></td>
                <td style="text-align:right;">${lg.detail || '—'}</td>
              </tr>
            `;
          }) : html`<tr><td colspan="4">لا توجد عمليات مطابقة.</td></tr>`}
        </tbody>
      </table>
    `);
  } catch (err) {}
}

export function wireWizardAndSystemEvents(onReloadFullState) {
  ['mousemove', 'keydown', 'click'].forEach((ev) => {
    window.addEventListener(ev, () => resetIdleLockTimer(), { passive: true });
  });

  // Category tabs & live in-settings search
  const tabsBar = $('#settingsCategoryTabs');
  if (tabsBar) {
    tabsBar.addEventListener('click', (e) => {
      const btn = e.target.closest('.settings-cat-tab');
      if (!btn) return;
      _activeSettingsCat = btn.getAttribute('data-cat') || 'all';
      $all('#settingsCategoryTabs .settings-cat-tab').forEach((b) => {
        b.classList.toggle('active', b === btn);
      });
      filterSettingsByCategoryAndQuery();
    });
  }

  const searchInp = $('#settingsSearchInput');
  if (searchInp) {
    searchInp.addEventListener('input', () => filterSettingsByCategoryAndQuery());
  }

  // Windows Auto-Start toggle & self-repair
  const btnToggleAuto = $('#btnToggleAutoStart');
  if (btnToggleAuto) {
    btnToggleAuto.addEventListener('click', async () => {
      try {
        const curEn = Boolean(state.settings && state.settings.autoStartEnabled);
        const startMin = Boolean($('#setStartMinimized') && $('#setStartMinimized').checked);
        const res = await apiRequest('/autostart/toggle', {
          method: 'POST',
          body: { enabled: !curEn, minimized: startMin }
        });
        showToast(res.messageAr || 'تم تحديث حالة التشغيل التلقائي.');
        if (typeof onReloadFullState === 'function') await onReloadFullState();
      } catch (err) {}
    });
  }

  const btnRepairAuto = $('#btnRepairAutoStart');
  if (btnRepairAuto) {
    btnRepairAuto.addEventListener('click', async () => {
      try {
        const res = await apiRequest('/autostart/repair', {
          method: 'POST',
          sendRev: false,
          body: {}
        });
        showToast((res.autostart && res.autostart.messageAr) || 'تم فحص وإصلاح مسار التشغيل التلقائي.');
        await renderSystemHealthView();
      } catch (err) {}
    });
  }

  // Single-setting reset, category reset, and reset all settings
  const mainFormCard = $('#mainSettingsFormCard');
  if (mainFormCard) {
    mainFormCard.addEventListener('click', async (e) => {
      const singleBtn = e.target.closest('.btn-reset-single-setting');
      if (!singleBtn) return;
      const key = singleBtn.getAttribute('data-reset-key');
      if (!key) return;
      try {
        await apiRequest('/settings/reset', {
          method: 'POST',
          body: { key }
        });
        showToast(`تمت استعادة القيمة الافتراضية للإعداد (${key}).`);
        if (typeof onReloadFullState === 'function') await onReloadFullState();
      } catch (err) {}
    });
  }

  const btnResetCat = $('#btnResetCurrentCategory');
  if (btnResetCat) {
    btnResetCat.addEventListener('click', async () => {
      const cat = _activeSettingsCat && _activeSettingsCat !== 'all' ? _activeSettingsCat : 'general';
      try {
        await apiRequest('/settings/reset', {
          method: 'POST',
          body: { category: cat }
        });
        showToast(`تمت استعادة الإعدادات الافتراضية لقسم (${CATEGORY_LABELS[cat] || cat}) دون المساس ببيانات الطالبات.`);
        if (typeof onReloadFullState === 'function') await onReloadFullState();
      } catch (err) {}
    });
  }

  const btnResetAllSet = $('#btnResetAllSettings');
  if (btnResetAllSet) {
    btnResetAllSet.addEventListener('click', async () => {
      if (typeof window !== 'undefined' && typeof window.confirm === 'function') {
        const ok = window.confirm('هل تريد استعادة جميع الإعدادات إلى الوضع الافتراضي؟ (لن يتم حذف أي بيانات طالبات أو حسابات مالية)');
        if (!ok) return;
      }
      try {
        await apiRequest('/settings/reset', {
          method: 'POST',
          body: { resetAll: true, confirm: true }
        });
        showToast('تمت استعادة جميع الإعدادات الافتراضية بنجاح (بيانات الأكاديمية محفوظة بالكامل).');
        if (typeof onReloadFullState === 'function') await onReloadFullState();
      } catch (err) {}
    });
  }

  // Export & Import Settings JSON
  const btnExportSet = $('#btnExportSettingsJson');
  if (btnExportSet) {
    btnExportSet.addEventListener('click', async () => {
      try {
        const bundle = await apiRequest('/settings/export');
        const blob = new Blob([JSON.stringify(bundle, null, 2)], { type: 'application/json;charset=utf-8' });
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = `YawmJadid_Settings_${uiState.referenceDate}.json`;
        document.body.appendChild(a);
        a.click();
        a.remove();
        URL.revokeObjectURL(url);
        showToast('تم تصدير ملف الإعدادات والباقات بنجاح.');
      } catch (err) {}
    });
  }

  const inpImportSet = $('#inputImportSettingsJson');
  if (inpImportSet) {
    inpImportSet.addEventListener('change', async (e) => {
      const f = e.target.files && e.target.files[0];
      if (!f) return;
      try {
        const txt = await f.text();
        const parsed = JSON.parse(txt);
        const preview = await apiRequest('/settings/import/preview', {
          method: 'POST',
          readOnlyPost: true,
          sendRev: false,
          body: parsed
        });
        let proceed = true;
        if (typeof window !== 'undefined' && typeof window.confirm === 'function') {
          proceed = window.confirm(
            `معاينة استيراد الإعدادات:\nعدد الإعدادات المتغيرة: ${preview.changesCount || 0}\nتغييرات عالية الأثر المالي: ${preview.highImpactCount || 0}\n\nهل تريد اعتماد استيراد الإعدادات؟`
          );
        }
        if (proceed) {
          await apiRequest('/settings/import/commit', {
            method: 'POST',
            body: { ...parsed, confirm: true }
          });
          showToast('تم استيراد وتطبيق ملف الإعدادات بنجاح.');
          if (typeof onReloadFullState === 'function') await onReloadFullState();
        }
      } catch (err) {
        showToast(`تعذّر استيراد ملف الإعدادات: ${err.message || err}`, true);
      } finally {
        inpImportSet.value = '';
      }
    });
  }

  // Master data quick controls
  const btnSaveStatuses = $('#btnSaveStudentStatuses');
  if (btnSaveStatuses) {
    btnSaveStatuses.addEventListener('click', async () => {
      const raw = ($('#setStudentStatusesInput') && $('#setStudentStatusesInput').value) || '';
      const list = raw.split(/[،,]/).map((x) => x.trim()).filter(Boolean);
      try {
        await apiRequest('/settings', {
          method: 'PUT',
          body: { studentStatuses: list }
        });
        showToast('تم حفظ حالات الطالبات المعتمدة.');
        if (typeof onReloadFullState === 'function') await onReloadFullState();
      } catch (err) {}
    });
  }

  const btnGoTeachers = $('#btnGoManageTeachers');
  if (btnGoTeachers) btnGoTeachers.addEventListener('click', () => navigateTo('#/teachers'));
  const btnGoPackages = $('#btnGoManagePackages');
  if (btnGoPackages) btnGoPackages.addEventListener('click', () => navigateTo('#/teachers'));

  const btnRefreshDiag = $('#btnRefreshSystemDiagnostics');
  if (btnRefreshDiag) btnRefreshDiag.addEventListener('click', () => renderSystemHealthView());

  // Safe Demo Quarantine Cleaner
  const btnCleanQuarantine = $('#btnCleanQuarantineDemo');
  if (btnCleanQuarantine) {
    btnCleanQuarantine.addEventListener('click', async () => {
      try {
        const res = await apiRequest('/demo/clean-quarantine', {
          method: 'POST',
          body: { includeClassB: true }
        });
        if (res.cleaned) {
          showToast(`تم عزل وحذف ${res.totalDeleted || 0} سجل تجريبي بأمان وحفظ نسخة احتياطية (${res.snapshot}).`);
        } else {
          showToast(res.message || 'لا توجد بيانات تجريبية في قاعدة البيانات.');
        }
        if (typeof onReloadFullState === 'function') await onReloadFullState();
      } catch (err) {}
    });
  }

  const dqReportBox = $('#dataHealthReport');
  if (dqReportBox) {
    dqReportBox.addEventListener('click', (e) => {
      const deepBtn = e.target.closest('.btn-dq-deeplink');
      if (deepBtn) {
        navigateTo(deepBtn.getAttribute('data-deeplink') || '#/students');
        return;
      }
      const normBtn = e.target.closest('#btnRunDataQualityNormalize');
      if (normBtn) {
        navigateTo('#/students');
        setTimeout(() => {
          const opSel = $('#bulkOpSelect');
          if (opSel) {
            opSel.value = 'normalize_phones_and_codes';
            opSel.dispatchEvent(new Event('change'));
          }
          const prevBtn = $('#btnPreviewBulkOp');
          if (prevBtn) prevBtn.click();
        }, 80);
      }
    });
  }

  const wizLogoInp = $('#wizLogoFile');
  if (wizLogoInp) {
    wizLogoInp.addEventListener('change', (e) => {
      const f = e.target.files && e.target.files[0];
      if (!f) return;
      const reader = new FileReader();
      reader.onload = () => {
        _wizLogoDataUri = String(reader.result || '');
      };
      reader.readAsDataURL(f);
    });
  }

  const btnCloseWiz = $('#btnCloseOnboardingModal');
  if (btnCloseWiz) btnCloseWiz.addEventListener('click', closeOnboardingWizard);

  const btnOpenWiz = $('#btnOpenOnboardingWizard');
  if (btnOpenWiz) btnOpenWiz.addEventListener('click', openOnboardingWizard);

  const btnEmptyWiz = $('#btnEmptyOpenWizard');
  if (btnEmptyWiz) btnEmptyWiz.addEventListener('click', openOnboardingWizard);

  const formWiz = $('#formOnboardingWizard');
  if (formWiz) {
    formWiz.addEventListener('submit', async (e) => {
      e.preventDefault();
      const defPct = FE.parsePctInt($('#wizTeacherPct').value, 70);
      const rawTeachers = ($('#wizTeachersList').value || '').split(/\r?\n/).map((s) => s.trim()).filter(Boolean);
      const teachers = rawTeachers.map((line) => {
        const parts = line.split('|').map((x) => x.trim());
        return {
          name: parts[0],
          pct: parts[1] !== undefined ? FE.parsePctInt(parts[1], defPct) : defPct
        };
      });
      const subTypes = ($('#wizPackagesList').value || '').split(/\r?\n/).map((s) => s.trim()).filter(Boolean);

      try {
        await apiRequest('/onboarding/complete', {
          method: 'POST',
          sendRev: false,
          body: {
            settings: {
              academyName: $('#wizAcademyName').value.trim(),
              currency: $('#wizCurrency').value.trim() || 'ج.م',
              logoDataUri: _wizLogoDataUri || state.settings.logoDataUri || '',
              defaultDurationDays: Number($('#wizDurationDays').value || 30),
              expiringSoonDays: Number($('#wizExpiringDays').value || 5),
              defaultTeacherPct: defPct,
              wageBasis: $('#wizWageBasis').value || 'net',
              wageAccrualMode: $('#wizWageAccrual').value || 'accrued'
            },
            teachers,
            subTypes
          }
        });
        closeOnboardingWizard();
        showToast('تم حفظ إعدادات الأكاديمية بنجاح.');
        if (typeof onReloadFullState === 'function') await onReloadFullState();
      } catch (err) {}
    });
  }

  const triggerLoadDemo = async () => {
    try {
      await apiRequest('/demo/load', {
        method: 'POST',
        body: { referenceDate: uiState.referenceDate }
      });
      closeOnboardingWizard();
      showToast('تم تحميل البيانات التجريبية الوهمية بنجاح.');
      if (typeof onReloadFullState === 'function') await onReloadFullState();
    } catch (err) {}
  };

  ['#btnWizardLoadDemo', '#btnLoadDemoData', '#btnEmptyLoadDemo'].forEach((sel) => {
    const btn = $(sel);
    if (btn) btn.addEventListener('click', triggerLoadDemo);
  });

  const btnClearDemo = $('#btnClearDemoData');
  if (btnClearDemo) {
    btnClearDemo.addEventListener('click', async () => {
      try {
        await apiRequest('/demo/clear', { method: 'POST', body: {} });
        showToast('تم مسح جميع البيانات التجريبية.');
        if (typeof onReloadFullState === 'function') await onReloadFullState();
      } catch (err) {}
    });
  }

  // PIN unlock form
  const formPin = $('#formPinUnlock');
  if (formPin) {
    formPin.addEventListener('submit', async (e) => {
      e.preventDefault();
      const pinVal = ($('#pinUnlockInput') && $('#pinUnlockInput').value) || '';
      try {
        await apiRequest('/pin/verify', {
          method: 'POST',
          readOnlyPost: true,
          sendRev: false,
          body: { pin: pinVal }
        });
        uiState.pinUnlocked = true;
        if ($('#pinUnlockInput')) $('#pinUnlockInput').value = '';
        const modal = $('#pinLockModal');
        if (modal) modal.classList.remove('open');
        resetIdleLockTimer();
        showToast('تم فتح قفل المنظومة.');
      } catch (err) {
        showToast('رمز PIN غير صحيح!', true);
      }
    });
  }

  // Unified System settings form (all 13 categories)
  const formSys = $('#formSystemSettings');
  if (formSys) {
    formSys.addEventListener('submit', async (e) => {
      e.preventDefault();
      const curS = state.settings || {};
      const nextWageBasis = ($('#setWageBasis') && $('#setWageBasis').value) || 'net';
      const nextWageAccrual = ($('#setWageAccrualMode') && $('#setWageAccrualMode').value) || 'accrued';
      const nextPctSource = ($('#setPctSource') && $('#setPctSource').value) || 'student';
      const nextDefPct = FE.parsePctInt($('#setDefaultTeacherPct') ? $('#setDefaultTeacherPct').value : 70, 70);

      const highImpactChanged = (
        (curS.wageBasis && curS.wageBasis !== nextWageBasis) ||
        (curS.wageAccrualMode && curS.wageAccrualMode !== nextWageAccrual) ||
        (curS.pctSource && curS.pctSource !== nextPctSource)
      );
      if (highImpactChanged && typeof window !== 'undefined' && typeof window.confirm === 'function') {
        const confirmed = window.confirm(
          '⚠️ تنبيه: لقد قمت بتغيير إحدى السياسات المالية لحساب أجور المعلمات (أساس الحساب أو الاستحقاق أو مصدر النسبة).\nسيتم تطبيق السياسة الجديدة على الشهور المفتوحة بينما تظل الشهور المقفلة محمية.\n\nهل تريد تأكيد الحفظ؟'
        );
        if (!confirmed) return;
      }

      const nextTheme = ($('#setTheme') && $('#setTheme').value) || 'light';
      try {
        if (typeof localStorage !== 'undefined') {
          localStorage.setItem('alahd_erp_theme_v1', nextTheme);
        }
      } catch (err) {}

      const payload = {
        academyName: ($('#setAcademyName') && $('#setAcademyName').value.trim()) || 'أكاديمية يوم جديد',
        academySubtitle: ($('#setAcademySubtitle') && $('#setAcademySubtitle').value.trim()) || 'نظام إدارة الأكاديمية الموحد',
        currency: ($('#setCurrency') && $('#setCurrency').value.trim()) || 'ج.م',
        defaultLandingView: ($('#setDefaultLandingView') && $('#setDefaultLandingView').value) || 'dashboard',
        defaultPageSize: Number(($('#setDefaultPageSize') && $('#setDefaultPageSize').value) || 50),
        startMinimized: Boolean($('#setStartMinimized') && $('#setStartMinimized').checked),
        theme: nextTheme,
        fontScale: ($('#setFontScale') && $('#setFontScale').value) || 'md',
        tableDensity: ($('#setTableDensity') && $('#setTableDensity').value) || 'comfortable',
        useArabicDigits: Boolean($('#setUseArabicDigits') && $('#setUseArabicDigits').checked),
        tableZebraStripes: Boolean($('#setTableZebraStripes') ? $('#setTableZebraStripes').checked : true),
        sidebarCompact: Boolean($('#setSidebarCompact') && $('#setSidebarCompact').checked),
        studentCodePrefix: ($('#setStudentCodePrefix') && $('#setStudentCodePrefix').value.trim()) || 'STD-',
        defaultStudentViewMode: ($('#setDefaultStudentViewMode') && $('#setDefaultStudentViewMode').value) || 'table',
        requireStudentPhone: Boolean($('#setRequireStudentPhone') && $('#setRequireStudentPhone').checked),
        defaultTeacherPct: nextDefPct,
        pctSource: nextPctSource,
        defaultDurationDays: Number(($('#setDurationDays') && $('#setDurationDays').value) || 30),
        expiringSoonDays: Number(($('#setExpiringDays') && $('#setExpiringDays').value) || 5),
        monthsRangePast: Number(($('#setMonthsRangePast') && $('#setMonthsRangePast').value) || 6),
        monthsRangeFuture: Number(($('#setMonthsRangeFuture') && $('#setMonthsRangeFuture').value) || 6),
        defaultSessionDurationMin: Number(($('#setDefaultSessionDurationMin') && $('#setDefaultSessionDurationMin').value) || 40),
        defaultPlanSessionCount: Number(($('#setDefaultPlanSessionCount') && $('#setDefaultPlanSessionCount').value) || 8),
        defaultSessionStartTime: ($('#setDefaultSessionStartTime') && $('#setDefaultSessionStartTime').value) || '16:00',
        consecutiveAbsenceThreshold: Number(($('#setConsecutiveAbsenceThreshold') && $('#setConsecutiveAbsenceThreshold').value) || 2),
        wageBasis: nextWageBasis,
        wageAccrualMode: nextWageAccrual,
        defaultStatsPeriod: ($('#setDefaultStatsPeriod') && $('#setDefaultStatsPeriod').value) || 'all_time',
        defaultCollectionBasis: ($('#setDefaultCollectionBasis') && $('#setDefaultCollectionBasis').value) || 'cash',
        hidePersonalDataByDefault: Boolean($('#setHidePersonalDataByDefault') ? $('#setHidePersonalDataByDefault').checked : true),
        defaultImportMode: ($('#setDefaultImportMode') && $('#setDefaultImportMode').value) || 'update_existing',
        defaultAutoCreateTeachers: Boolean($('#setDefaultAutoCreateTeachers') ? $('#setDefaultAutoCreateTeachers').checked : true),
        defaultAutoCreatePackages: Boolean($('#setDefaultAutoCreatePackages') ? $('#setDefaultAutoCreatePackages').checked : true),
        extraBackupFolder: ($('#setExtraBackupFolder') && $('#setExtraBackupFolder').value.trim()) || '',
        backupWarnDays: Number(($('#setBackupWarnDays') && $('#setBackupWarnDays').value) || 3),
        idleLockMinutes: Number(($('#setIdleMinutes') && $('#setIdleMinutes').value) || 0),
        showDashAlertsStrip: Boolean($('#setShowDashAlertsStrip') ? $('#setShowDashAlertsStrip').checked : true)
      };

      try {
        await apiRequest('/settings', {
          method: 'PUT',
          body: payload
        });
        const newPin = ($('#setPinCodeInput') && $('#setPinCodeInput').value) || '';
        if (newPin.trim()) {
          await apiRequest('/pin/set', {
            method: 'POST',
            body: { pin: newPin.trim() }
          });
          $('#setPinCodeInput').value = '';
          uiState.pinUnlocked = true;
        }
        showToast('تم حفظ وتطبيق إعدادات الأكاديمية والمظهر والسياسات بنجاح.');
        if (typeof onReloadFullState === 'function') await onReloadFullState();
      } catch (err) {}
    });
  }

  // Month close & reopen
  const btnCloseMonth = $('#btnCloseSelectedMonth');
  if (btnCloseMonth) {
    btnCloseMonth.addEventListener('click', async () => {
      const mk = uiState.selectedMonth;
      try {
        await apiRequest(`/months/${encodeURIComponent(mk)}/close`, {
          method: 'POST',
          body: { note: `إقفال شهر ${monthLabel(mk)}` }
        });
        showToast(`تم إقفال شهر ${monthLabel(mk)} وأخذ نسخة احتياطية.`);
        if (typeof onReloadFullState === 'function') await onReloadFullState();
      } catch (err) {}
    });
  }

  const btnReopenMonth = $('#btnReopenSelectedMonth');
  if (btnReopenMonth) {
    btnReopenMonth.addEventListener('click', async () => {
      const mk = uiState.selectedMonth;
      try {
        await apiRequest(`/months/${encodeURIComponent(mk)}/reopen`, {
          method: 'POST',
          body: { reason: `إعادة فتح شهر ${monthLabel(mk)}` }
        });
        showToast(`تمت إعادة فتح شهر ${monthLabel(mk)} للتعديل.`);
        if (typeof onReloadFullState === 'function') await onReloadFullState();
      } catch (err) {}
    });
  }

  // Reset Application
  const btnResetApp = $('#btnResetApplication');
  if (btnResetApp) {
    btnResetApp.addEventListener('click', async () => {
      const word = ($('#resetConfirmWordInput') && $('#resetConfirmWordInput').value.trim()) || '';
      if (word !== 'إعادة ضبط') {
        showToast('يرجى كتابة عبارة التأكيد (إعادة ضبط) في الحقل المخصص أولًا.', true);
        return;
      }
      try {
        const res = await apiRequest('/system/reset', {
          method: 'POST',
          sendRev: false,
          body: { confirmWord: word }
        });
        if ($('#resetConfirmWordInput')) $('#resetConfirmWordInput').value = '';
        showToast(`تمت إعادة ضبط التطبيق بالكامل (تم حفظ نسخة الأمان: ${res.snapshot}).`);
        if (typeof onReloadFullState === 'function') await onReloadFullState();
        openOnboardingWizard();
      } catch (err) {}
    });
  }

  // Manual backup & restore
  const btnManualBackup = $('#btnCreateManualBackup');
  if (btnManualBackup) {
    btnManualBackup.addEventListener('click', async () => {
      try {
        await downloadBlobFromApi(
          '/backup',
          `alahd_manual_${uiState.referenceDate}.sqlite3`,
          'تم إنشاء وفحص وتحميل النسخة الاحتياطية بنجاح.'
        );
        await renderSystemHealthView();
      } catch (err) {}
    });
  }

  const backupsWrap = $('#backupsTableWrap');
  if (backupsWrap) {
    backupsWrap.addEventListener('click', async (e) => {
      const rBtn = e.target.closest('.btn-restore-backup');
      if (!rBtn) return;
      const bName = rBtn.getAttribute('data-name');
      if (!bName) return;
      try {
        await apiRequest('/restore', {
          method: 'POST',
          sendRev: false,
          body: { name: bName }
        });
        showToast(`تمت استعادة النسخة الاحتياطية (${bName}) بنجاح.`);
        if (typeof onReloadFullState === 'function') await onReloadFullState();
      } catch (err) {}
    });
  }

  const btnFilterAudit = $('#btnFilterAudit');
  if (btnFilterAudit) {
    btnFilterAudit.addEventListener('click', () => loadAuditLogs());
  }
}
