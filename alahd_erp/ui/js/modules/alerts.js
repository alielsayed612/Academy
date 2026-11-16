/* ============================================================================
   modules/alerts.js — Phase 5 & Phase 4 Automation: Alerts Center, Read State,
   Topbar Badge, Scheduling Conflict Alerts & Quick Actions
   ============================================================================ */

import { $, html, renderHtml, showToast } from './dom.js';
import { uiState, money } from './store.js';
import { apiRequest } from './api.js';
import { navigateTo } from './router.js';

let _cachedAlertsResult = null;

export async function refreshAlertsAndBadges() {
  try {
    const showDismissed = ($('#alertsShowDismissedToggle') && $('#alertsShowDismissedToggle').value === '1') ? 'true' : 'false';
    const data = await apiRequest(`/alerts?reference_date=${encodeURIComponent(uiState.referenceDate)}&include_dismissed=${showDismissed}`);
    _cachedAlertsResult = data;
    updateAlertsBadgesAndStrip(data);
    if (uiState.activeView === 'alerts') {
      renderAlertsViewFromData(data);
    }
    return data;
  } catch (err) {
    return null;
  }
}

export function updateAlertsBadgesAndStrip(data) {
  const counts = (data && data.counts) || { total: 0, critical: 0, warning: 0, info: 0, unread: 0 };
  const total = Number(counts.total || 0);

  ['#topbarAlertsBadge', '#navAlertsBadge'].forEach((sel) => {
    const el = $(sel);
    if (!el) return;
    el.textContent = String(total);
    el.classList.toggle('zero', total === 0);
  });

  const strip = $('#dashboardAlertsStrip');
  if (strip) {
    if (total === 0) {
      strip.style.display = 'none';
      strip.innerHTML = '';
    } else {
      strip.style.display = 'flex';
      renderHtml(strip, html`
        <div>
          <strong>🔔 التنبيهات الرقابية النشطة (${total}):</strong>
          <span class="badge badge-unpaid" style="margin-right:6px;">🔴 حرجة: ${counts.critical}</span>
          <span class="badge badge-warn" style="margin-right:6px;">🟠 تحذيرية: ${counts.warning}</span>
          <span class="badge badge-pay" style="margin-right:6px;">🔵 معلوماتية: ${counts.info}</span>
          ${counts.unread !== undefined ? html`<span class="badge badge-start" style="margin-right:6px;">غير مقروء: ${counts.unread}</span>` : ''}
        </div>
        <button type="button" class="btn btn-sm btn-primary" id="btnStripOpenAlerts">فتح مركز التنبيهات والانتقال المباشر ←</button>
      `);
      const openBtn = $('#btnStripOpenAlerts');
      if (openBtn) {
        openBtn.addEventListener('click', () => navigateTo('#/alerts'));
      }
    }
  }
}

export function renderAlertsViewFromData(data) {
  const kpiBox = $('#alertsSummaryKpis');
  const groupsBox = $('#alertsGroupsContainer');
  if (!kpiBox || !groupsBox) return;

  const counts = (data && data.counts) || { total: 0, critical: 0, warning: 0, info: 0, dismissed: 0, unread: 0 };
  renderHtml(kpiBox, html`
    <div class="kpi-card kpi-danger">
      <div class="kpi-label">🔴 تنبيهات حرجة عاجلة</div>
      <div class="kpi-value">${counts.critical}</div>
    </div>
    <div class="kpi-card kpi-gold">
      <div class="kpi-label">🟠 تنبيهات تحذيرية</div>
      <div class="kpi-value">${counts.warning}</div>
    </div>
    <div class="kpi-card kpi-pay">
      <div class="kpi-label">🔵 ملاحظات معلوماتية</div>
      <div class="kpi-value">${counts.info}</div>
    </div>
    <div class="kpi-card">
      <div class="kpi-label">النشط / غير المقروء / المؤجل</div>
      <div class="kpi-value">${counts.total} نشط (${counts.unread ?? counts.total} جديد) / ${counts.dismissed || 0} مؤجل</div>
    </div>
  `);

  const sevFilter = ($('#alertsSeverityFilter') && $('#alertsSeverityFilter').value) || '';
  const catFilter = ($('#alertsCategoryFilter') && $('#alertsCategoryFilter').value) || '';
  const showDismissed = ($('#alertsShowDismissedToggle') && $('#alertsShowDismissedToggle').value === '1');

  const allItems = Array.isArray(data && data.items) ? data.items : [];
  const filtered = allItems.filter((a) => {
    if (!showDismissed && a.dismissed) return false;
    if (sevFilter && a.severity !== sevFilter) return false;
    if (catFilter && a.category !== catFilter) return false;
    return true;
  });

  if (filtered.length === 0) {
    renderHtml(groupsBox, html`
      <div class="card">
        <h3>✅ لا توجد تنبيهات مطابقة للفلاتر الحالية</h3>
        <p style="font-size:13px;color:var(--text-secondary);">جميع السجلات المالية والدراسية والنسخ الاحتياطية في حالة سليمة.</p>
      </div>
    `);
    return;
  }

  const groupMeta = [
    { key: 'critical', label: '🔴 التنبيهات الحرجة (تتطلب إجراءً فوريًا)' },
    { key: 'warning', label: '🟠 التنبيهات التحذيرية (تستوجب المتابعة)' },
    { key: 'info', label: '🔵 التنبيهات والملاحظات المعلوماتية' }
  ];

  const groupsHtml = groupMeta.map((gm) => {
    const items = filtered.filter((a) => a.severity === gm.key);
    if (!items.length) return '';
    return html`
      <div class="alert-group" data-group-sev="${gm.key}">
        <button type="button" class="alert-group-header">
          <span>${gm.label}</span>
          <span class="badge">${items.length} تنبيه ▾</span>
        </button>
        <div class="alert-group-body">
          ${items.map((a) => renderSingleAlertCard(a))}
        </div>
      </div>
    `;
  });

  renderHtml(groupsBox, html`${groupsHtml}`);
}

function renderSingleAlertCard(a) {
  const quickActionBtn = buildQuickActionButton(a);
  const waBtn = a.whatsappUrl ? html`
    <a href="${a.whatsappUrl}" target="_blank" rel="noopener noreferrer" class="btn btn-sm" title="مراسلة واتساب">💬 واتساب</a>
  ` : '';
  const readBtn = a.read
    ? html`<span class="badge badge-end" style="font-size:11px;">✓ مقروء</span>`
    : html`<button type="button" class="btn btn-sm btn-alert-read" data-alert-id="${a.id}" title="تعليم التنبيه كمقروء">✓ مقروء</button>`;
  const dismissOrRestoreBtn = a.dismissed
    ? html`<button type="button" class="btn btn-sm btn-alert-restore" data-alert-id="${a.id}">♻️ استعادة التنبيه</button>`
    : html`<button type="button" class="btn btn-sm btn-alert-dismiss" data-alert-id="${a.id}">🔕 تأجيل 7 أيام</button>`;

  return html`
    <div class="alert-card severity-${a.severity}" data-alert-card="${a.id}" style="${a.read ? 'opacity:0.88;' : ''}">
      <div>
        <div class="alert-card-title">
          ${!a.read ? html`<span class="badge badge-pay" style="font-size:10px;margin-left:6px;">جديد</span>` : ''}
          ${a.title}
        </div>
        <div class="alert-card-detail">${a.detail}</div>
      </div>
      <div class="alert-card-actions">
        <button type="button" class="btn btn-sm btn-primary btn-alert-deeplink" data-deeplink="${a.deepLink || '#/dashboard'}">🎯 فتح السجل مباشرة</button>
        ${quickActionBtn}
        ${waBtn}
        ${readBtn}
        ${dismissOrRestoreBtn}
      </div>
    </div>
  `;
}

function buildQuickActionButton(a) {
  if (a.quickAction === 'record_payment' && a.studentId) {
    return html`
      <button type="button" class="btn btn-sm btn-accent btn-alert-quick-pay"
        data-stu-id="${a.studentId}"
        data-month="${a.month || uiState.selectedMonth}"
        data-amount="${a.amountDue || 0}">
        💳 تحصيل سريع (${money(a.amountDue || 0)})
      </button>
    `;
  }
  if (a.quickAction === 'compensate_session' && a.sessionId) {
    return html`
      <button type="button" class="btn btn-sm btn-accent btn-alert-quick-comp" data-sess-id="${a.sessionId}">
        🔄 تعويض الحصة تلقائيًا
      </button>
    `;
  }
  if (a.quickAction === 'record_payout' && a.teacherName) {
    return html`
      <button type="button" class="btn btn-sm btn-accent btn-alert-quick-payout"
        data-teacher="${a.teacherName}"
        data-month="${a.month || uiState.selectedMonth}"
        data-amount="${a.amountDue || 0}">
        💰 إعداد صرف الراتب (${money(a.amountDue || 0)})
      </button>
    `;
  }
  return '';
}

export function wireAlertsEvents(onReloadFullState, triggerHtmlDownload) {
  const topbarBell = $('#btnTopbarAlerts');
  if (topbarBell) {
    topbarBell.addEventListener('click', () => navigateTo('#/alerts'));
  }

  ['#alertsSeverityFilter', '#alertsCategoryFilter', '#alertsShowDismissedToggle'].forEach((sel) => {
    const el = $(sel);
    if (el) {
      el.addEventListener('change', () => {
        if (sel === '#alertsShowDismissedToggle') {
          refreshAlertsAndBadges();
        } else if (_cachedAlertsResult) {
          renderAlertsViewFromData(_cachedAlertsResult);
        }
      });
    }
  });

  const btnRefresh = $('#btnRefreshAlerts');
  if (btnRefresh) {
    btnRefresh.addEventListener('click', () => refreshAlertsAndBadges());
  }

  const btnMarkAllRead = $('#btnMarkAllAlertsRead');
  if (btnMarkAllRead) {
    btnMarkAllRead.addEventListener('click', async () => {
      try {
        await apiRequest('/alerts/read', {
          method: 'POST',
          body: { alertId: '__all__', referenceDate: uiState.referenceDate }
        });
        showToast('تم تعليم جميع التنبيهات الحالية كمقروءة.');
        await refreshAlertsAndBadges();
      } catch (err) {}
    });
  }

  const btnExportHtml = $('#btnExportAlertsHtml');
  if (btnExportHtml) {
    btnExportHtml.addEventListener('click', () => triggerHtmlDownload('alerts'));
  }

  const groupsBox = $('#alertsGroupsContainer');
  if (groupsBox) {
    groupsBox.addEventListener('click', async (e) => {
      const hdr = e.target.closest('.alert-group-header');
      if (hdr) {
        const grp = hdr.closest('.alert-group');
        if (grp) grp.classList.toggle('collapsed');
        return;
      }

      const deepBtn = e.target.closest('.btn-alert-deeplink');
      if (deepBtn) {
        navigateTo(deepBtn.getAttribute('data-deeplink'));
        return;
      }

      const readBtn = e.target.closest('.btn-alert-read');
      if (readBtn) {
        const aid = readBtn.getAttribute('data-alert-id');
        try {
          await apiRequest('/alerts/read', {
            method: 'POST',
            body: { alertId: aid, referenceDate: uiState.referenceDate }
          });
          await refreshAlertsAndBadges();
        } catch (err) {}
        return;
      }

      const dismissBtn = e.target.closest('.btn-alert-dismiss');
      if (dismissBtn) {
        const aid = dismissBtn.getAttribute('data-alert-id');
        try {
          await apiRequest('/alerts/dismiss', {
            method: 'POST',
            body: { alertId: aid, reason: 'تأجيل من مركز التنبيهات', snoozeDays: 7 }
          });
          showToast('تم تأجيل التنبيه لمدة 7 أيام.');
          await refreshAlertsAndBadges();
        } catch (err) {}
        return;
      }

      const restoreBtn = e.target.closest('.btn-alert-restore');
      if (restoreBtn) {
        const aid = restoreBtn.getAttribute('data-alert-id');
        try {
          await apiRequest('/alerts/restore', {
            method: 'POST',
            body: { alertId: aid }
          });
          showToast('تمت استعادة التنبيه.');
          await refreshAlertsAndBadges();
        } catch (err) {}
        return;
      }

      const quickPayBtn = e.target.closest('.btn-alert-quick-pay');
      if (quickPayBtn) {
        const sid = quickPayBtn.getAttribute('data-stu-id');
        const mk = quickPayBtn.getAttribute('data-month') || uiState.selectedMonth;
        const amt = Number(quickPayBtn.getAttribute('data-amount') || 0);
        if (!sid || amt <= 0) {
          navigateTo(`#/students/${sid}?month=${mk}`);
          return;
        }
        try {
          await apiRequest('/installments', {
            method: 'POST',
            body: {
              studentId: sid,
              month: mk,
              amount: amt,
              paymentDate: uiState.referenceDate,
              method: 'نقدي',
              note: 'تحصيل سريع من مركز التنبيهات'
            }
          });
          showToast('تم تسجيل إيصال السداد بنجاح.');
          if (typeof onReloadFullState === 'function') await onReloadFullState();
        } catch (err) {}
        return;
      }

      const quickPayoutBtn = e.target.closest('.btn-alert-quick-payout');
      if (quickPayoutBtn) {
        const tName = quickPayoutBtn.getAttribute('data-teacher') || '';
        const amt = Number(quickPayoutBtn.getAttribute('data-amount') || 0);
        navigateTo('#/payroll');
        setTimeout(() => {
          if ($('#payTeacherSelect') && tName) $('#payTeacherSelect').value = tName;
          if ($('#payAmountInput') && amt > 0) $('#payAmountInput').value = String(amt);
          if ($('#payNoteInput')) $('#payNoteInput').value = `صرف مستحقات شهر ${uiState.selectedMonth} (بمراجعة المستخدم)`;
          if ($('#payAmountInput')) $('#payAmountInput').focus();
          showToast(`تم تجهيز بيانات الصرف للمعلمة «${tName}»؛ راجع المبلغ واضغط حفظ لاعتماد الصرف.`);
        }, 80);
        return;
      }

      const quickCompBtn = e.target.closest('.btn-alert-quick-comp');
      if (quickCompBtn) {
        const sessId = quickCompBtn.getAttribute('data-sess-id');
        navigateTo(`#/attendance?highlight=${encodeURIComponent(sessId)}`);
      }
    });
  }
}
