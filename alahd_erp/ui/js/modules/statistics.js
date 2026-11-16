/* ============================================================================
   modules/statistics.js — Phase 4: General Statistics Dashboard & SVG Charts
   ============================================================================ */

import { $, html, renderHtml } from './dom.js';
import { FE, state, uiState, money, monthLabel } from './store.js';
import { apiRequest } from './api.js';
import { navigateTo } from './router.js';

export async function renderStatisticsView() {
  const container = $('#statisticsContainer');
  if (!container) return;

  const periodSel = $('#statsPeriodSelect');
  const period = (periodSel && periodSel.value) || 'all_time';
  const fromGrp = $('#statsCustomFromGroup');
  const toGrp = $('#statsCustomToGroup');
  if (fromGrp) fromGrp.style.display = period === 'custom' ? 'flex' : 'none';
  if (toGrp) toGrp.style.display = period === 'custom' ? 'flex' : 'none';

  const customFrom = ($('#statsCustomFrom') && $('#statsCustomFrom').value) || '';
  const customTo = ($('#statsCustomTo') && $('#statsCustomTo').value) || '';
  const teacherFilter = ($('#statsTeacherFilter') && $('#statsTeacherFilter').value) || '';

  let statsData = null;
  try {
    let q = `?period=${encodeURIComponent(period)}&selected_month=${encodeURIComponent(uiState.selectedMonth)}&reference_date=${encodeURIComponent(uiState.referenceDate)}`;
    if (period === 'custom') {
      if (customFrom) q += `&custom_from=${encodeURIComponent(customFrom)}`;
      if (customTo) q += `&custom_to=${encodeURIComponent(customTo)}`;
    }
    if (teacherFilter) {
      q += `&teacher=${encodeURIComponent(teacherFilter)}`;
    }
    statsData = await apiRequest(`/statistics${q}`);
  } catch (err) {
    statsData = computeLocalFallbackStatistics(period, teacherFilter);
  }

  if (!statsData) return;

  const kpis = statsData.kpis || {};
  const byMonth = Array.isArray(statsData.byMonth) ? statsData.byMonth : [];
  const byTeacher = Array.isArray(statsData.byTeacher) ? statsData.byTeacher : [];
  const byPackage = Array.isArray(statsData.byPackage) ? statsData.byPackage : [];
  const byMethod = Array.isArray(statsData.byPaymentMethod) ? statsData.byPaymentMethod : [];
  const attStats = statsData.attendance || {};
  const memStats = statsData.memorization || {};
  const madStats = statsData.madrasa || {};

  renderHtml(container, html`
    <div class="kpi-grid">
      <div class="kpi-card">
        <div class="kpi-label">الطالبات النشطات / الإجمالي</div>
        <div class="kpi-value">${kpis.activeStudents ?? 0} / ${kpis.totalStudents ?? state.students.length}</div>
        <div class="kpi-sub">جدد: <b>${kpis.newStudents ?? 0}</b> | منقطعات: <b>${kpis.churnedStudents ?? 0}</b></div>
      </div>
      <div class="kpi-card kpi-gold">
        <div class="kpi-label">صافي الاشتراكات (بعد الخصم)</div>
        <div class="kpi-value">${money(kpis.netSubscriptions ?? 0)}</div>
        <div class="kpi-sub">الإجمالي قبل الخصم: <b>${money(kpis.grossSubscriptions ?? 0)}</b> (خصومات: ${money(kpis.discounts ?? 0)})</div>
      </div>
      <div class="kpi-card kpi-pay">
        <div class="kpi-label">المحصّل الفعلي ونسبة التحصيل</div>
        <div class="kpi-value">${money(kpis.collectedTotal ?? 0)}</div>
        <div class="kpi-sub">نسبة التحصيل: <b>${kpis.collectionRatePct ?? 0}%</b> | متبقي: <b>${money(kpis.uncollectedTotal ?? 0)}</b></div>
      </div>
      <div class="kpi-card kpi-wage">
        <div class="kpi-label">أجور المعلمات المستحقة / المصروفة</div>
        <div class="kpi-value">${money(kpis.teacherWagesAccrued ?? 0)}</div>
        <div class="kpi-sub">مصروف: <b>${money(kpis.teacherWagesPaid ?? 0)}</b> | متبقي: <b>${money(kpis.teacherWagesUnpaid ?? 0)}</b></div>
      </div>
      <div class="kpi-card kpi-start">
        <div class="kpi-label">صافي ربح الأكاديمية ومعدل البقاء</div>
        <div class="kpi-value">${money(kpis.academyNetProfit ?? 0)}</div>
        <div class="kpi-sub">معدل بقاء الطالبات: <b>${kpis.retentionRatePct ?? 100}%</b> | متوسط الطالبة: <b>${money(kpis.avgRevenuePerStudent ?? 0)}</b></div>
      </div>
    </div>

    <div class="grid-2">
      <div class="card">
        <div class="card-header">
          <h3>📈 المنحنى الشهري للإيرادات والتحصيل والرواتب</h3>
        </div>
        <div class="svg-chart-box">
          ${buildMonthlyBarSvg(byMonth)}
        </div>
      </div>

      <div class="card">
        <div class="card-header">
          <h3>📦 توزيع الطالبات والإيرادات حسب الباقات ووسائل الدفع</h3>
        </div>
        <div class="table-wrap">
          <table class="data-table">
            <thead>
              <tr><th>الباقة / النظام</th><th>عدد الطالبات</th><th>صافي الإيراد</th><th>المحصّل</th><th>نسبة المساهمة</th><th>انتقال</th></tr>
            </thead>
            <tbody>
              ${byPackage.length ? byPackage.map((p) => html`
                <tr>
                  <td style="text-align:right;font-weight:700;">${p.package || 'غير محدد'}</td>
                  <td>${p.studentsCount ?? 0}</td>
                  <td>${money(p.netRevenue ?? 0)}</td>
                  <td>${money(p.collected ?? 0)}</td>
                  <td><span class="badge badge-pay">${p.sharePct ?? 0}%</span></td>
                  <td><button type="button" class="btn btn-sm btn-stat-drill-pkg" data-pkg="${p.package || ''}">عرض الطالبات</button></td>
                </tr>
              `) : html`<tr><td colspan="6">لا توجد بيانات للفترة المحددة</td></tr>`}
            </tbody>
          </table>
        </div>
        ${byMethod.length ? html`
          <div style="margin-top:10px;display:flex;gap:8px;flex-wrap:wrap;">
            ${byMethod.map((m) => html`
              <span class="badge badge-end">${m.method || 'نقدي'}: ${money(m.amount || 0)} (${m.count || 0} إيصال)</span>
            `)}
          </div>
        ` : ''}
      </div>
    </div>

    <div class="card">
      <div class="card-header">
        <h3>👩‍🏫 تحليل الأداء المالي والتشغيلي حسب المعلمات (مع انتقال تفصيلي)</h3>
      </div>
      <div class="table-wrap">
        <table class="data-table">
          <thead>
            <tr>
              <th>م</th><th>المعلمة</th><th>النسبة</th><th>عدد الطالبات</th>
              <th>صافي الاشتراكات</th><th>المحصّل</th><th>المتبقي</th>
              <th>الأجر المستحق</th><th>المصروف</th><th>المتبقي للمعلمة</th>
              <th>صافي الأكاديمية</th><th>انتقال</th>
            </tr>
          </thead>
          <tbody>
            ${byTeacher.length ? byTeacher.map((t, idx) => html`
              <tr>
                <td>${idx + 1}</td>
                <td style="text-align:right;font-weight:700;">
                  ${t.teacher}
                  ${(state.teacherMeta[t.teacher] && (state.teacherMeta[t.teacher].archived || state.teacherMeta[t.teacher].active === false))
                    ? html`<span class="badge badge-warn" style="margin-right:4px;">مؤرشفة</span>`
                    : ''}
                </td>
                <td>${t.pct ?? 70}%</td>
                <td>${t.studentsCount ?? 0}</td>
                <td>${money(t.netSubscriptions ?? 0)}</td>
                <td>${money(t.collected ?? 0)}</td>
                <td>${money(t.uncollected ?? 0)}</td>
                <td><span class="badge badge-wage">${money(t.wagesAccrued ?? 0)}</span></td>
                <td>${money(t.wagesPaid ?? 0)}</td>
                <td>${money(t.wagesUnpaid ?? 0)}</td>
                <td><span class="badge badge-start">${money(t.academyNet ?? 0)}</span></td>
                <td>
                  <button type="button" class="btn btn-sm btn-primary btn-stat-drill-teacher" data-stat-teacher="${t.teacher}">فتح طالباتها</button>
                </td>
              </tr>
            `) : html`<tr><td colspan="12">لا توجد بيانات معلمات في هذه الفترة</td></tr>`}
          </tbody>
        </table>
      </div>
    </div>

    <div class="grid-3">
      <div class="card">
        <h3>🗓️ إحصائيات الحضور والحصص والتعويض</h3>
        <p style="font-size:13px;margin:6px 0;">إجمالي الحصص المسجلة: <b>${attStats.totalSessions ?? 0}</b></p>
        <p style="font-size:13px;margin:6px 0;">حضور مكتمل: <span class="badge badge-end">${attStats.doneCount ?? 0}</span> | غياب: <span class="badge badge-unpaid">${attStats.absentCount ?? 0}</span></p>
        <p style="font-size:13px;margin:6px 0;">إلغاء معلمة: <span class="badge badge-warn">${attStats.cancelTeacherCount ?? 0}</span> | حصص تعويضية: <span class="badge badge-pay">${attStats.makeupCount ?? 0}</span></p>
        <p style="font-size:13px;margin:6px 0;">نسبة الالتزام بالحضور: <b>${attStats.attendanceRatePct ?? 100}%</b></p>
      </div>
      <div class="card">
        <h3>📖 إحصائيات الحفظ والمراجعة القرآنية</h3>
        <p style="font-size:13px;margin:6px 0;">إجمالي جلسات التسميع: <b>${memStats.totalLogs ?? 0}</b></p>
        <p style="font-size:13px;margin:6px 0;">إجمالي الأوجه المنجزة: <span class="badge badge-end">${memStats.totalPages ?? 0} وجه</span></p>
        <p style="font-size:13px;margin:6px 0;">متوسط درجات الإتقان: <span class="badge badge-wage">${memStats.avgScore ?? 0} / 100</span></p>
      </div>
      <div class="card">
        <h3>🕌 إحصائيات المدرسة الإيمانية</h3>
        <p style="font-size:13px;margin:6px 0;">عدد الدفعات النشطة: <b>${madStats.batchesCount ?? 0}</b></p>
        <p style="font-size:13px;margin:6px 0;">إجمالي المنتسبات: <span class="badge badge-pay">${madStats.enrollmentsCount ?? 0} طالبة</span></p>
        <p style="font-size:13px;margin:6px 0;">إجمالي رسوم الانتساب المحصّلة: <b>${money(madStats.totalFeesPaid ?? 0)}</b></p>
        <p style="font-size:13px;margin:6px 0;">أعلى سلسلة حماسة (Streak): <b>🔥 ${madStats.maxStreak ?? 0} يوم</b></p>
      </div>
    </div>
  `);
}

function buildMonthlyBarSvg(byMonth) {
  if (!byMonth || byMonth.length === 0) {
    return html`<div style="padding:20px;color:var(--text-muted);">لا توجد بيانات شهرية لعرض الرسم البياني.</div>`;
  }
  const items = byMonth.slice(-8);
  const maxVal = Math.max(100, ...items.map((m) => Math.max(m.netSubscriptions || 0, m.collected || 0, m.wages || 0)));
  const svgW = 680;
  const svgH = 220;
  const chartBottom = 175;
  const chartTop = 25;
  const chartH = chartBottom - chartTop;
  const colW = Math.floor((svgW - 60) / Math.max(1, items.length));

  const barsSvg = items.map((m, i) => {
    const xBase = 40 + i * colW;
    const hSub = Math.round(((m.netSubscriptions || 0) / maxVal) * chartH);
    const hColl = Math.round(((m.collected || 0) / maxVal) * chartH);
    const hWage = Math.round(((m.wages || 0) / maxVal) * chartH);
    const shortLbl = String(m.month || '').slice(2);
    return html`
      <g>
        <rect x="${xBase}" y="${chartBottom - hSub}" width="14" height="${hSub}" rx="3" fill="#556a4f">
          <title>${m.label || m.month} — صافي الاشتراكات: ${m.netSubscriptions || 0}</title>
        </rect>
        <rect x="${xBase + 16}" y="${chartBottom - hColl}" width="14" height="${hColl}" rx="3" fill="#0284c7">
          <title>${m.label || m.month} — المحصّل: ${m.collected || 0}</title>
        </rect>
        <rect x="${xBase + 32}" y="${chartBottom - hWage}" width="14" height="${hWage}" rx="3" fill="#b45309">
          <title>${m.label || m.month} — رواتب المعلمات: ${m.wages || 0}</title>
        </rect>
        <text x="${xBase + 23}" y="${chartBottom + 18}" text-anchor="middle" font-size="11" fill="#475569" font-family="Cairo, sans-serif">${shortLbl}</text>
      </g>
    `;
  });

  return html`
    <svg viewBox="0 0 ${svgW} ${svgH}" role="img" aria-label="رسم بياني شهري للإيرادات والتحصيل والرواتب">
      <line x1="30" y1="${chartBottom}" x2="${svgW - 15}" y2="${chartBottom}" stroke="#cbd5e1" stroke-width="1.5" />
      ${barsSvg}
      <g transform="translate(40, 14)" font-size="11" font-family="Cairo, sans-serif" fill="#334155">
        <rect x="0" y="-9" width="10" height="10" rx="2" fill="#556a4f" />
        <text x="15" y="0" text-anchor="start">صافي الاشتراكات</text>
        <rect x="120" y="-9" width="10" height="10" rx="2" fill="#0284c7" />
        <text x="135" y="0" text-anchor="start">المحصّل الفعلي</text>
        <rect x="240" y="-9" width="10" height="10" rx="2" fill="#b45309" />
        <text x="255" y="0" text-anchor="start">رواتب المعلمات</text>
      </g>
    </svg>
  `;
}

function computeLocalFallbackStatistics(period, teacherFilter = '') {
  const mk = uiState.selectedMonth;
  const fOpts = {
    pctSource: (state.settings && state.settings.pctSource) || 'student',
    wageBasis: (state.settings && state.settings.wageBasis) || 'net',
    wageAccrualMode: (state.settings && state.settings.wageAccrualMode) || 'accrued',
    referenceDate: uiState.referenceDate,
    defaultPct: Number((state.settings && state.settings.defaultTeacherPct) || 70)
  };
  const studs = teacherFilter
    ? state.students.filter((s) => (teacherFilter === '__unassigned__' ? !s.teacher : s.teacher === teacherFilter))
    : state.students;
  const mAgg = FE.computeMonthAggregate(studs, mk, state.installments, state.teacherMeta, fOpts);
  return {
    kpis: {
      totalStudents: studs.length,
      activeStudents: mAgg.count,
      newStudents: mAgg.count,
      churnedStudents: 0,
      grossSubscriptions: mAgg.grossValue,
      discounts: mAgg.discount,
      netSubscriptions: mAgg.value,
      collectedTotal: mAgg.collected,
      uncollectedTotal: mAgg.uncollected,
      teacherWagesAccrued: mAgg.wages,
      teacherWagesPaid: 0,
      teacherWagesUnpaid: mAgg.wages,
      academyNetProfit: mAgg.net,
      collectionRatePct: mAgg.value > 0 ? Math.round((mAgg.collected * 100) / mAgg.value) : 0,
      retentionRatePct: 100,
      avgRevenuePerStudent: mAgg.count > 0 ? Math.round(mAgg.value / mAgg.count) : 0
    },
    byMonth: [
      {
        month: mk,
        label: monthLabel(mk),
        netSubscriptions: mAgg.value,
        collected: mAgg.collected,
        wages: mAgg.wages
      }
    ],
    byTeacher: [],
    byPackage: [],
    byPaymentMethod: [],
    attendance: { totalSessions: state.sessions.length, doneCount: 0, absentCount: 0, cancelTeacherCount: 0, makeupCount: 0, attendanceRatePct: 100 },
    memorization: { totalLogs: state.memorization.length, totalPages: 0, avgScore: 0 },
    madrasa: { batchesCount: state.madrasaBatches.length, enrollmentsCount: state.madrasaEnrollments.length, totalFeesPaid: 0, maxStreak: 0 }
  };
}

export function wireStatisticsEvents(triggerXlsxDownload, triggerHtmlDownload) {
  ['#statsPeriodSelect', '#statsCustomFrom', '#statsCustomTo', '#statsTeacherFilter'].forEach((sel) => {
    const el = $(sel);
    if (el) {
      el.addEventListener('change', () => renderStatisticsView());
    }
  });

  const btnRefresh = $('#btnRefreshStatistics');
  if (btnRefresh) {
    btnRefresh.addEventListener('click', () => renderStatisticsView());
  }

  const btnXlsx = $('#btnStatsExportXlsx');
  if (btnXlsx) {
    btnXlsx.addEventListener('click', () => {
      const tf = ($('#statsTeacherFilter') && $('#statsTeacherFilter').value) || '';
      triggerXlsxDownload('teachers_stats', tf);
    });
  }

  const btnHtml = $('#btnStatsExportHtml');
  if (btnHtml) {
    btnHtml.addEventListener('click', () => {
      const tf = ($('#statsTeacherFilter') && $('#statsTeacherFilter').value) || '';
      triggerHtmlDownload('statistics', { teacher: tf });
    });
  }

  const container = $('#statisticsContainer');
  if (container) {
    container.addEventListener('click', (e) => {
      const tBtn = e.target.closest('.btn-stat-drill-teacher');
      if (tBtn) {
        const t = tBtn.getAttribute('data-stat-teacher') || '';
        navigateTo(`#/students?teacher=${encodeURIComponent(t)}`);
        return;
      }
      const pBtn = e.target.closest('.btn-stat-drill-pkg');
      if (pBtn) {
        const p = pBtn.getAttribute('data-pkg') || '';
        navigateTo(`#/students?type=${encodeURIComponent(p)}`);
      }
    });
  }
}
