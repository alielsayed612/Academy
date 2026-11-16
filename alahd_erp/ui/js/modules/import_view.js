/* ============================================================================
   modules/import_view.js — Phase 1 & 6: Safe Excel/CSV Import, Column Mapping & Dry-Run Preview
   ============================================================================ */

import { $, $all, html, renderHtml, showToast } from './dom.js';
import { uiState } from './store.js';
import { apiRequest, downloadBlobFromApi } from './api.js';

let _currentFileBase64 = '';
let _currentFilename = '';
let _currentFileMeta = '';
let _currentColumnOverrides = {};
let _lastPreviewData = null;
let _lastPreviewSignature = '';
let _activeStatusFilter = 'all';
let _previewInFlight = null;
let _selectedPreviewRows = new Set();
let _userModifiedSelection = false;
let _forceImportAll = false;
let _rowFieldOverrides = {};

const FIELD_AR_LABELS = {
  code: 'كود الطالبة',
  name: 'الاسم',
  studentCode: 'كود الطالبة',
  studentName: 'اسم الطالبة',
  phone: 'رقم الهاتف',
  teacher: 'المعلمة',
  type: 'الباقة / النوع',
  specialty: 'التخصص',
  defaultPct: 'النسبة الافتراضية',
  pct: 'نسبة المعلمة',
  price: 'السعر',
  schedule: 'المواعيد',
  month: 'الشهر',
  value: 'قيمة الاشتراك',
  discount: 'الخصم',
  customWage: 'أجر المعلمة',
  adminStatus: 'حالة الطالبة',
  paidAmount: 'المبلغ المسدد',
  amount: 'المبلغ',
  paymentDate: 'تاريخ الدفع',
  actualStartDate: 'تاريخ البداية الفعلي',
  endDate: 'تاريخ النهاية',
  date: 'التاريخ',
  category: 'التصنيف',
  receiptNo: 'رقم الإيصال',
  method: 'طريقة الدفع',
  note: 'ملاحظة',
  notes: 'ملاحظات'
};

function readFileAsBase64(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => {
      const res = String(reader.result || '');
      const idx = res.indexOf(',');
      resolve(idx !== -1 ? res.slice(idx + 1) : res);
    };
    reader.onerror = () => reject(new Error('تعذّر قراءة الملف'));
    reader.readAsDataURL(file);
  });
}

function getFileMetaSignature(file) {
  if (!file) return _currentFileMeta;
  return `${file.name || ''}:${file.size || 0}:${file.lastModified || 0}`;
}

export function getImportControlState() {
  const dsEl = $('#importDatasetSelect');
  const modeEl = $('#importModeSelect');
  const sheetEl = $('#importSheetSelect');
  const autoAllEl = $('#importAutoCreateDeps');
  const autoTeachersEl = $('#importAutoCreateTeachers');
  const autoPackagesEl = $('#importAutoCreatePackages');

  const dataset = (dsEl && dsEl.value) || 'students';
  const mode = (modeEl && modeEl.value) || 'update_existing';
  const sheetName = (sheetEl && sheetEl.value) || '';
  const masterAuto = autoAllEl ? Boolean(autoAllEl.checked) : true;
  const autoCreateTeachers = autoTeachersEl ? Boolean(autoTeachersEl.checked) : masterAuto;
  const autoCreatePackages = autoPackagesEl ? Boolean(autoPackagesEl.checked) : masterAuto;
  const autoCreateRefs = autoCreateTeachers && autoCreatePackages;

  return {
    dataset,
    mode,
    sheetName,
    autoCreateRefs,
    autoCreateTeachers,
    autoCreatePackages,
    defaultMonth: uiState.selectedMonth || '',
    columnMapping: Object.assign({}, _currentColumnOverrides)
  };
}

export function computePreviewSignature(opts, fileBase64, fileMeta) {
  const mapKeys = Object.keys(opts.columnMapping || {}).sort();
  const mapPairs = mapKeys.map((k) => `${k}=${opts.columnMapping[k]}`).join('&');
  return [
    fileMeta || '',
    (fileBase64 || '').length,
    (fileBase64 || '').slice(0, 64),
    (fileBase64 || '').slice(-64),
    opts.dataset,
    opts.mode,
    opts.sheetName || '',
    String(opts.autoCreateTeachers),
    String(opts.autoCreatePackages),
    opts.defaultMonth || '',
    mapPairs
  ].join('|');
}

export function isPreviewStale() {
  if (!_lastPreviewData || !_lastPreviewSignature) return true;
  const fileInput = $('#importFileInput');
  const file = fileInput && fileInput.files && fileInput.files[0];
  const curMeta = file ? getFileMetaSignature(file) : _currentFileMeta;
  const opts = getImportControlState();
  const effectiveOpts = Object.assign({}, opts, {
    sheetName: opts.sheetName || _lastPreviewData.selectedSheet || _lastPreviewData.detectedSheet || ''
  });
  const curSig = computePreviewSignature(effectiveOpts, _currentFileBase64, curMeta);
  return curSig !== _lastPreviewSignature;
}

function updateStalePreviewUI() {
  const banner = $('#importStaleBanner');
  const commitBtn = $('#btnCommitImport');
  if (!banner) return;
  if (!_lastPreviewData) {
    banner.style.display = 'none';
    return;
  }
  const stale = isPreviewStale();
  banner.style.display = stale ? 'block' : 'none';
  if (commitBtn) {
    commitBtn.disabled = stale;
  }
}

export function runImportPreview() {
  const p = _runImportPreviewImpl();
  _previewInFlight = p;
  p.finally(() => {
    if (_previewInFlight === p) _previewInFlight = null;
  });
  return p;
}

async function _runImportPreviewImpl() {
  const fileInput = $('#importFileInput');
  const file = fileInput && fileInput.files && fileInput.files[0];
  if (!file && !_currentFileBase64) {
    showToast('يرجى اختيار ملف إكسيل (.xlsx) أو (.csv) أولًا.', true);
    return null;
  }

  if (file) {
    if (file.size > 10 * 1024 * 1024) {
      showToast('حجم الملف يتجاوز الحد الأقصى المسموح به (10 ميجابايت).', true);
      return null;
    }
    _currentFilename = file.name;
    _currentFileMeta = getFileMetaSignature(file);
    _currentFileBase64 = await readFileAsBase64(file);
  }

  const opts = getImportControlState();

  try {
    const preview = await apiRequest('/import/preview', {
      method: 'POST',
      readOnlyPost: true,
      sendRev: false,
      body: {
        dataset: opts.dataset,
        mode: opts.mode,
        filename: _currentFilename,
        fileBase64: _currentFileBase64,
        sheetName: opts.sheetName,
        defaultMonth: opts.defaultMonth,
        autoCreateRefs: opts.autoCreateRefs,
        autoCreateTeachers: opts.autoCreateTeachers,
        autoCreatePackages: opts.autoCreatePackages,
        columnMapping: opts.columnMapping
      }
    });
    _lastPreviewData = preview;
    const selectedSheet = preview.selectedSheet || preview.detectedSheet || opts.sheetName || '';
    const resolvedOpts = Object.assign({}, opts, { sheetName: selectedSheet });
    _lastPreviewSignature = computePreviewSignature(resolvedOpts, _currentFileBase64, _currentFileMeta);
    renderImportPreview(preview);
    updateStalePreviewUI();
    return preview;
  } catch (err) {
    _lastPreviewData = null;
    _lastPreviewSignature = '';
    updateStalePreviewUI();
    showToast(`خطأ في معاينة الملف: ${err.message}`, true);
    return null;
  }
}

function formatNormalizedCell(normObj) {
  const entries = Object.entries(normObj || {}).filter(
    ([k, v]) => k !== 'matchedId' && k !== 'id' && v !== '' && v !== null && v !== undefined
  );
  if (!entries.length) return '—';
  return entries
    .map(([k, v]) => `${FIELD_AR_LABELS[k] || k}: ${v}`)
    .join(' | ');
}

export function renderImportPreview(preview) {
  const container = $('#importPreviewContainer');
  if (!container) return;
  container.style.display = 'block';

  // Worksheet selector (shown whenever sheets exist so the user can inspect or switch sheets)
  const sheetGrp = $('#importSheetGroup');
  const sheetSel = $('#importSheetSelect');
  const sheetNames = Array.isArray(preview.sheetNames)
    ? preview.sheetNames
    : (Array.isArray(preview.sheets) ? preview.sheets : []);
  const selectedSheet = preview.selectedSheet || preview.detectedSheet || (sheetNames[0] || '');

  if (sheetGrp && sheetSel && sheetNames.length >= 1) {
    sheetGrp.style.display = 'flex';
    renderHtml(sheetSel, html`${sheetNames.map((sn) => html`
      <option value="${sn}" ${sn === selectedSheet ? 'selected' : ''}>${sn}</option>
    `)}`);
    sheetSel.value = selectedSheet;
  } else if (sheetGrp) {
    sheetGrp.style.display = 'none';
  }

  const rawSum = preview.summary || {};
  const totalCount = rawSum.total !== undefined ? rawSum.total : (rawSum.totalRows || 0);
  const newCount = rawSum.new !== undefined ? rawSum.new : (rawSum.newCount || 0);
  const updateCount = rawSum.will_update !== undefined ? rawSum.will_update : (rawSum.updateCount || 0);
  const dupCount = rawSum.duplicate !== undefined ? rawSum.duplicate : (rawSum.duplicateCount || 0);
  const invalidCount = rawSum.invalid !== undefined ? rawSum.invalid : (rawSum.invalidCount || 0);
  const validCount = rawSum.valid !== undefined ? rawSum.valid : (rawSum.validCount !== undefined ? rawSum.validCount : (newCount + updateCount));

  const errBtn = $('#btnDownloadImportErrorReport');
  if (errBtn) {
    const hasErrs = invalidCount > 0 || (Array.isArray(preview.errors) && preview.errors.length > 0);
    errBtn.style.display = hasErrs ? 'inline-flex' : 'none';
  }

  renderHtml('#importSummaryKpis', html`
    <div class="kpi-card">
      <div class="kpi-label">إجمالي الصفوف المقروءة</div>
      <div class="kpi-value">${totalCount}</div>
    </div>
    <div class="kpi-card kpi-end">
      <div class="kpi-label">✓ سجلات صالحة للتنفيذ</div>
      <div class="kpi-value">${validCount}</div>
    </div>
    <div class="kpi-card kpi-wage">
      <div class="kpi-label">🟢 سجلات جديدة (New)</div>
      <div class="kpi-value">${newCount}</div>
    </div>
    <div class="kpi-card kpi-pay">
      <div class="kpi-label">🔵 ستُحدَّث (Will Update)</div>
      <div class="kpi-value">${updateCount}</div>
    </div>
    <div class="kpi-card kpi-gold">
      <div class="kpi-label">🟠 مكررة / متخطاة (Duplicate)</div>
      <div class="kpi-value">${dupCount}</div>
    </div>
    <div class="kpi-card kpi-danger">
      <div class="kpi-label">🔴 غير صالحة وبها أخطاء</div>
      <div class="kpi-value">${invalidCount}</div>
    </div>
  `);

  // Automatic creation warning banner
  const autoWarnBanner = $('#importAutoCreateWarningBanner');
  if (autoWarnBanner) {
    const newEnt = preview.newEntities || {};
    const newT = Array.isArray(newEnt.teachers) ? newEnt.teachers : [];
    const newP = Array.isArray(newEnt.packages) ? newEnt.packages : [];
    if (newT.length > 0 || newP.length > 0) {
      autoWarnBanner.style.display = 'block';
      renderHtml(autoWarnBanner, html`
        <div style="background:var(--warn-bg);color:var(--warn-text);border:1px solid var(--warn-border, #f59e0b);padding:10px 14px;border-radius:8px;margin-bottom:12px;font-size:13px;">
          <strong>⚠️ تنبيه إنشاء مراجع جديدة تلقائيًا عند الاعتماد:</strong>
          ${newT.length > 0 ? html`<div>• معلمات جديدة (${newT.length}): <b>${newT.join(' ، ')}</b></div>` : ''}
          ${newP.length > 0 ? html`<div>• باقات اشتراك جديدة (${newP.length}): <b>${newP.join(' ، ')}</b></div>` : ''}
        </div>
      `);
    } else {
      autoWarnBanner.style.display = 'none';
      autoWarnBanner.innerHTML = '';
    }
  }

  // Interactive Column Mapping UI
  const headers = Array.isArray(preview.headers) ? preview.headers : [];
  const mapping = preview.columnMapping || {};
  const headerToField = preview.headerToFieldMapping || {};
  const datasetFields = Array.isArray(preview.datasetFields) ? preview.datasetFields : [];
  const missingReq = Array.isArray(preview.missingRequiredFields) ? preview.missingRequiredFields : [];
  const mapErrors = Array.isArray(preview.mappingErrors) ? preview.mappingErrors : [];

  renderHtml('#importMappingWrap', html`
    <div class="card" style="background:var(--bg-surface);padding:12px 14px;margin-bottom:12px;border:1px solid var(--border-color);">
      <div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:8px;margin-bottom:10px;">
        <div>
          <strong>🧭 مطابقة أعمدة الملف مع حقول النظام (ورقة: ${selectedSheet || 'الافتراضية'} — ${headers.length} عمود):</strong>
          <div style="font-size:12px;color:var(--text-muted);">يمكنك تعديل ربط أي حقل يدويًا وسيتم تحديث المعاينة فورًا قبل الحفظ</div>
        </div>
        <div style="display:flex;gap:6px;flex-wrap:wrap;">
          <button type="button" class="btn btn-sm" id="btnResetColumnMapping">↺ استعادة المطابقة التلقائية</button>
          <button type="button" class="btn btn-sm btn-primary" id="btnApplyColumnMapping">🔄 تحديث المعاينة بالمطابقة الحالية</button>
        </div>
      </div>

      ${missingReq.length > 0 ? html`
        <div style="background:var(--unpaid-bg);color:var(--unpaid-text);padding:8px 12px;border-radius:6px;margin-bottom:10px;font-size:12.5px;font-weight:700;">
          ❌ حقول إلزامية غير مربوطة: ${missingReq.map((m) => m.label || m.field).join(' ، ')} — يرجى اختيار العمود المقابل لها من القائمة أدناه.
        </div>
      ` : ''}

      ${mapErrors.length > 0 ? html`
        <div style="background:var(--unpaid-bg);color:var(--unpaid-text);padding:8px 12px;border-radius:6px;margin-bottom:10px;font-size:12.5px;font-weight:700;">
          ❌ أخطاء في مطابقة الأعمدة: ${mapErrors.join(' | ')}
        </div>
      ` : ''}

      ${datasetFields.length > 0 ? html`
        <div class="grid-3" style="gap:10px;margin-bottom:10px;">
          ${datasetFields.map((spec) => {
            const curCol = mapping[spec.field] || '';
            return html`
              <div class="filter-group" style="margin:0;">
                <label style="font-size:12px;display:flex;justify-content:space-between;">
                  <span>${spec.label}</span>
                  ${spec.required
                    ? html`<span class="badge badge-unpaid" style="font-size:10px;">إلزامي *</span>`
                    : html`<span style="color:var(--text-muted);font-size:10px;">اختياري</span>`}
                </label>
                <select class="import-col-map-select" data-field="${spec.field}">
                  <option value="__none__" ${!curCol ? 'selected' : ''}>— غير مربوط (تجاهل) —</option>
                  ${headers.map((h) => html`
                    <option value="${h}" ${curCol === h ? 'selected' : ''}>${h}</option>
                  `)}
                </select>
              </div>
            `;
          })}
        </div>
      ` : ''}

      <div style="font-size:12px;color:var(--text-secondary);padding-top:6px;border-top:1px dashed var(--border-color);">
        <strong>الأعمدة المكتشفة في الملف (${headers.length}):</strong>
        ${headers.map((h, idx) => {
          const mappedFieldKey = headerToField[h] || Object.keys(mapping).find((k) => mapping[k] === h) || '';
          const mappedLabel = mappedFieldKey ? (FIELD_AR_LABELS[mappedFieldKey] || mappedFieldKey) : 'غير مرتبط';
          return html`<span class="badge ${mappedFieldKey ? 'badge-start' : ''}" style="margin:2px 4px;">${h || ('عمود ' + (idx + 1))} ← ${mappedLabel}</span>`;
        })}
      </div>
    </div>
  `);

  const allRowsInit = Array.isArray(preview.rows) ? preview.rows : [];
  _selectedPreviewRows = new Set();
  _userModifiedSelection = false;
  _rowFieldOverrides = {};
  allRowsInit.forEach((r, idx) => {
    const pIdx = r.previewIndex !== undefined ? r.previewIndex : idx;
    if (_forceImportAll || r.status === 'new' || r.status === 'will_update') {
      _selectedPreviewRows.add(pIdx);
    }
  });

  wireDynamicMappingControls();
  renderPreviewRowsTable(preview);
}

function renderPreviewRowsTable(preview) {
  const allRows = Array.isArray(preview.rows) ? preview.rows : [];
  const filteredRows = allRows.filter((r) => {
    if (_activeStatusFilter === 'all') return true;
    if (_activeStatusFilter === 'valid') return r.status === 'new' || r.status === 'will_update';
    return r.status === _activeStatusFilter;
  });

  const statusBadges = {
    new: html`<span class="badge badge-end">🟢 جديد</span>`,
    will_update: html`<span class="badge badge-pay">🔵 تحديث موجود</span>`,
    duplicate: html`<span class="badge badge-warn">🟠 مكرر / متخطى</span>`,
    invalid: html`<span class="badge badge-unpaid">🔴 غير متناسق / تنبيه</span>`
  };

  const validTotal = allRows.filter((r) => r.status === 'new' || r.status === 'will_update').length;
  const filterButtons = [
    { id: 'all', label: `الكل (${allRows.length})` },
    { id: 'valid', label: `الصالح للتنفيذ (${validTotal})` },
    { id: 'new', label: `جديد (${allRows.filter((r) => r.status === 'new').length})` },
    { id: 'will_update', label: `سيُحدَّث (${allRows.filter((r) => r.status === 'will_update').length})` },
    { id: 'duplicate', label: `مكرر (${allRows.filter((r) => r.status === 'duplicate').length})` },
    { id: 'invalid', label: `غير متناسق / أخطاء (${allRows.filter((r) => r.status === 'invalid').length})` }
  ];

  const visibleIdxs = filteredRows.map((r, i) => (r.previewIndex !== undefined ? r.previewIndex : allRows.indexOf(r)));
  const allVisibleChecked = visibleIdxs.length > 0 && visibleIdxs.every((idx) => _selectedPreviewRows.has(idx));

  renderHtml('#importPreviewTableWrap', html`
    <div style="background:var(--bg-surface);border:1px solid var(--border-color);border-radius:8px;padding:10px 12px;margin-bottom:10px;display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:8px;">
      <div style="display:flex;gap:6px;flex-wrap:wrap;align-items:center;">
        <strong style="font-size:12.5px;">🎯 قرار الاستيراد بيدك (إضافة كلية أو جزئية):</strong>
        <button type="button" class="btn btn-sm btn-primary" id="btnImportSelectAll" title="تحديد جميع الصفوف في الملف بالكامل بما فيها الصفوف غير المتناسقة أو المكررة">☑️ إضافة بالكلية (${allRows.length})</button>
        <button type="button" class="btn btn-sm" id="btnImportSelectValid" title="تحديد الصفوف السليمة فقط">${'✓ السليمة فقط (' + validTotal + ')'}</button>
        <button type="button" class="btn btn-sm" id="btnImportDeselectAll" title="إلغاء تحديد الكل لاختيار صفوف محددة يدويًا">☐ إلغاء التحديد (اختيار جزئي)</button>
        <span class="badge badge-pay" id="importSelectedCountBadge" style="font-size:12px;">المحدد للإضافة: ${_selectedPreviewRows.size} من ${allRows.length}</span>
      </div>
      <label style="display:flex;align-items:center;gap:6px;font-size:12px;font-weight:700;cursor:pointer;color:var(--brand-primary);">
        <input type="checkbox" id="importForceAllCheck" ${_forceImportAll ? 'checked' : ''}>
        <span>⚡ السماح بإضافة الصفوف المحددة حتى لو غير متناسقة أو بها أخطاء/تكرار</span>
      </label>
    </div>

    <div style="display:flex;gap:6px;flex-wrap:wrap;margin-bottom:10px;align-items:center;">
      <strong style="font-size:12.5px;">تصفية الصفوف المعروضة:</strong>
      ${filterButtons.map((fb) => html`
        <button type="button" class="btn btn-sm import-filter-btn ${_activeStatusFilter === fb.id ? 'btn-primary' : ''}" data-status-filter="${fb.id}">
          ${fb.label}
        </button>
      `)}
    </div>
    <table class="data-table">
      <thead>
        <tr>
          <th style="width:42px;">
            <input type="checkbox" id="importCheckAllVisible" ${allVisibleChecked ? 'checked' : ''} title="تحديد أو إلغاء تحديد جميع الصفوف المعروضة">
          </th>
          <th>رقم الصف</th>
          <th>حالة الفحص</th>
          <th>البيانات المستخرجة بعد التطبيع</th>
          <th>الأخطاء / تحذيرات المطابقة</th>
        </tr>
      </thead>
      <tbody>
        ${filteredRows.length ? filteredRows.slice(0, 1000).map((r) => {
          const pIdx = r.previewIndex !== undefined ? r.previewIndex : allRows.indexOf(r);
          const isChecked = _selectedPreviewRows.has(pIdx);
          const rowNum = r.rowIndex !== undefined ? r.rowIndex : r.row;
          const normStr = formatNormalizedCell(r.normalized || r.data || {});
          const errList = Array.isArray(r.errors) ? r.errors : [];
          const warnList = Array.isArray(r.warnings) ? r.warnings : [];
          return html`
            <tr style="${isChecked ? '' : 'opacity:0.65;'}">
              <td>
                <input type="checkbox" class="import-row-checkbox" data-preview-idx="${pIdx}" data-row="${rowNum}" ${isChecked ? 'checked' : ''} title="حدد أو ألغِ تحديد هذا الصف للاستيراد الجزئي أو الكلي">
              </td>
              <td><b>${rowNum}</b></td>
              <td>${statusBadges[r.status] || r.status}</td>
              <td style="text-align:right;font-size:12px;">${normStr}</td>
              <td style="text-align:right;font-size:12px;">
                ${errList.length ? html`<div style="color:var(--unpaid-text);font-weight:700;">❌ ${errList.join(' — ')} ${isChecked ? '(سيتم استيراده بناءً على اختيارك)' : ''}</div>` : ''}
                ${warnList.length ? html`<div style="color:var(--warn-text);font-weight:600;">⚠️ ${warnList.join(' — ')}</div>` : ''}
                ${(!errList.length && !warnList.length) ? html`<span style="color:var(--end-text);font-weight:700;">✓ سليم</span>` : ''}
              </td>
            </tr>
          `;
        }) : html`<tr><td colspan="5">لا توجد صفوف مطابقة لفلتر العرض المحدد.</td></tr>`}
      </tbody>
    </table>
  `);

  $all('.import-filter-btn').forEach((btn) => {
    btn.addEventListener('click', () => {
      _activeStatusFilter = btn.getAttribute('data-status-filter') || 'all';
      if (_lastPreviewData) renderPreviewRowsTable(_lastPreviewData);
    });
  });

  const btnSelAll = $('#btnImportSelectAll');
  if (btnSelAll) {
    btnSelAll.addEventListener('click', () => {
      _userModifiedSelection = true;
      _forceImportAll = true;
      allRows.forEach((r, idx) => {
        const pIdx = r.previewIndex !== undefined ? r.previewIndex : idx;
        _selectedPreviewRows.add(pIdx);
      });
      renderPreviewRowsTable(preview);
    });
  }

  const btnSelValid = $('#btnImportSelectValid');
  if (btnSelValid) {
    btnSelValid.addEventListener('click', () => {
      _userModifiedSelection = true;
      _forceImportAll = false;
      _selectedPreviewRows = new Set();
      allRows.forEach((r, idx) => {
        const pIdx = r.previewIndex !== undefined ? r.previewIndex : idx;
        if (r.status === 'new' || r.status === 'will_update') {
          _selectedPreviewRows.add(pIdx);
        }
      });
      renderPreviewRowsTable(preview);
    });
  }

  const btnDeselAll = $('#btnImportDeselectAll');
  if (btnDeselAll) {
    btnDeselAll.addEventListener('click', () => {
      _userModifiedSelection = true;
      _selectedPreviewRows = new Set();
      renderPreviewRowsTable(preview);
    });
  }

  const chkForce = $('#importForceAllCheck');
  if (chkForce) {
    chkForce.addEventListener('change', () => {
      _userModifiedSelection = true;
      _forceImportAll = Boolean(chkForce.checked);
      if (_forceImportAll) {
        allRows.forEach((r, idx) => {
          const pIdx = r.previewIndex !== undefined ? r.previewIndex : idx;
          _selectedPreviewRows.add(pIdx);
        });
      }
      renderPreviewRowsTable(preview);
    });
  }

  const chkAllVis = $('#importCheckAllVisible');
  if (chkAllVis) {
    chkAllVis.addEventListener('change', () => {
      _userModifiedSelection = true;
      const shouldCheck = Boolean(chkAllVis.checked);
      visibleIdxs.forEach((idx) => {
        if (shouldCheck) _selectedPreviewRows.add(idx);
        else _selectedPreviewRows.delete(idx);
      });
      renderPreviewRowsTable(preview);
    });
  }

  $all('.import-row-checkbox').forEach((cb) => {
    cb.addEventListener('change', () => {
      _userModifiedSelection = true;
      const pIdx = Number(cb.getAttribute('data-preview-idx'));
      if (cb.checked) {
        _selectedPreviewRows.add(pIdx);
      } else {
        _selectedPreviewRows.delete(pIdx);
      }
      const badge = $('#importSelectedCountBadge');
      if (badge) badge.textContent = `المحدد للإضافة: ${_selectedPreviewRows.size} من ${allRows.length}`;
    });
  });
}

function wireDynamicMappingControls() {
  $all('.import-col-map-select').forEach((sel) => {
    sel.addEventListener('change', () => {
      const field = sel.getAttribute('data-field');
      if (!field) return null;
      _currentColumnOverrides[field] = sel.value || '__none__';
      updateStalePreviewUI();
      return runImportPreview();
    });
  });

  const btnReset = $('#btnResetColumnMapping');
  if (btnReset) {
    btnReset.addEventListener('click', () => {
      _currentColumnOverrides = {};
      updateStalePreviewUI();
      return runImportPreview();
    });
  }

  const btnApply = $('#btnApplyColumnMapping');
  if (btnApply) {
    btnApply.addEventListener('click', () => runImportPreview());
  }
}

function renderCommitResultBanner(res) {
  const banner = $('#importCommitResultBanner');
  if (!banner) return;
  const added = res.added !== undefined ? res.added : ((res.summary && res.summary.addedCount) || 0);
  const updated = res.updated !== undefined ? res.updated : ((res.summary && res.summary.updatedCount) || 0);
  const skipped = res.skipped !== undefined ? res.skipped : ((res.summary && res.summary.skippedCount) || 0);
  const dup = res.duplicate !== undefined ? res.duplicate : ((res.summary && res.summary.duplicateCount) || 0);
  const inv = res.invalid !== undefined ? res.invalid : ((res.summary && res.summary.invalidCount) || 0);
  const committed = res.committed !== undefined ? res.committed : (added + updated);
  const createdT = Array.isArray(res.createdTeachers) ? res.createdTeachers : [];
  const createdP = Array.isArray(res.createdPackages) ? res.createdPackages : [];

  banner.style.display = 'block';
  renderHtml(banner, html`
    <div style="background:var(--end-bg, #dcfce7);color:var(--end-text, #166534);border:1px solid #16a34a;padding:12px 16px;border-radius:8px;margin-bottom:14px;font-size:13px;">
      <strong>✅ نتائج تنفيذ واعتماد الاستيراد الفعلي:</strong>
      <div style="margin-top:4px;">
        إجمالي السجلات المنفذة: <b>${committed}</b> |
        سجلات جديدة أُضيفت: <b>${added}</b> |
        سجلات حُدِّثت: <b>${updated}</b> |
        تم تخطيها: <b>${skipped}</b> (مكرر: ${dup} ، غير صالح: ${inv})
        ${res.snapshotName ? html` | النسخة الاحتياطية الآمنة: <code dir="ltr">${res.snapshotName}</code>` : ''}
      </div>
      ${createdT.length ? html`<div style="margin-top:4px;">👩‍🏫 معلمات جديدة تم إنشاؤها: <b>${createdT.join(' ، ')}</b></div>` : ''}
      ${createdP.length ? html`<div style="margin-top:4px;">📦 باقات جديدة تم إنشاؤها: <b>${createdP.join(' ، ')}</b></div>` : ''}
    </div>
  `);
}

export function wireImportEvents(onReloadFullState) {
  const btnTpl = $('#btnDownloadImportTemplate');
  if (btnTpl) {
    btnTpl.addEventListener('click', () => {
      const ds = ($('#importDatasetSelect') && $('#importDatasetSelect').value) || 'students';
      downloadBlobFromApi(
        `/import/template?dataset=${encodeURIComponent(ds)}`,
        `YawmJadid_Import_Template_${ds}.xlsx`,
        'تم تحميل قالب الإكسيل الجاهز.'
      );
    });
  }

  const fileInp = $('#importFileInput');
  if (fileInp) {
    fileInp.addEventListener('change', () => {
      _currentFileBase64 = '';
      _currentFileMeta = '';
      _currentColumnOverrides = {};
      _lastPreviewData = null;
      _lastPreviewSignature = '';
      _activeStatusFilter = 'all';
      const sheetSel = $('#importSheetSelect');
      if (sheetSel) sheetSel.innerHTML = '';
      const commitBanner = $('#importCommitResultBanner');
      if (commitBanner) {
        commitBanner.style.display = 'none';
        commitBanner.innerHTML = '';
      }
      runImportPreview();
    });
  }

  const dsSel = $('#importDatasetSelect');
  if (dsSel) {
    dsSel.addEventListener('change', () => {
      _currentColumnOverrides = {};
      _activeStatusFilter = 'all';
      updateStalePreviewUI();
      if (_currentFileBase64 || (fileInp && fileInp.files && fileInp.files[0])) {
        runImportPreview();
      }
    });
  }

  const modeSel = $('#importModeSelect');
  if (modeSel) {
    modeSel.addEventListener('change', () => {
      updateStalePreviewUI();
      if (_currentFileBase64 || (fileInp && fileInp.files && fileInp.files[0])) {
        runImportPreview();
      }
    });
  }

  const sheetSel = $('#importSheetSelect');
  if (sheetSel) {
    sheetSel.addEventListener('change', () => {
      _currentColumnOverrides = {};
      updateStalePreviewUI();
      runImportPreview();
    });
  }

  const autoAllEl = $('#importAutoCreateDeps');
  const autoTeachersEl = $('#importAutoCreateTeachers');
  const autoPackagesEl = $('#importAutoCreatePackages');

  if (autoAllEl) {
    autoAllEl.addEventListener('change', () => {
      const checked = Boolean(autoAllEl.checked);
      if (autoTeachersEl) autoTeachersEl.checked = checked;
      if (autoPackagesEl) autoPackagesEl.checked = checked;
      updateStalePreviewUI();
      if (_currentFileBase64) runImportPreview();
    });
  }

  [autoTeachersEl, autoPackagesEl].forEach((el) => {
    if (!el) return;
    el.addEventListener('change', () => {
      if (autoAllEl && autoTeachersEl && autoPackagesEl) {
        autoAllEl.checked = Boolean(autoTeachersEl.checked && autoPackagesEl.checked);
      }
      updateStalePreviewUI();
      if (_currentFileBase64) runImportPreview();
    });
  });

  const btnPreview = $('#btnPreviewImport');
  if (btnPreview) {
    btnPreview.addEventListener('click', () => runImportPreview());
  }

  const btnErrReport = $('#btnDownloadImportErrorReport');
  if (btnErrReport) {
    btnErrReport.addEventListener('click', () => {
      if (!_currentFileBase64 && !_lastPreviewData) return;
      const opts = getImportControlState();
      downloadBlobFromApi(
        '/import/error-report',
        `YawmJadid_Import_Errors_${opts.dataset}.xlsx`,
        'تم تحميل تقرير أخطاء الاستيراد.',
        {
          dataset: opts.dataset,
          mode: opts.mode,
          filename: _currentFilename,
          fileBase64: _currentFileBase64,
          sheetName: opts.sheetName,
          defaultMonth: opts.defaultMonth,
          autoCreateRefs: opts.autoCreateRefs,
          autoCreateTeachers: opts.autoCreateTeachers,
          autoCreatePackages: opts.autoCreatePackages,
          columnMapping: opts.columnMapping,
          errors: (_lastPreviewData && Array.isArray(_lastPreviewData.errors)) ? _lastPreviewData.errors : undefined
        }
      );
    });
  }

  const btnCommit = $('#btnCommitImport');
  if (btnCommit) {
    btnCommit.addEventListener('click', async () => {
      if (_previewInFlight) {
        try { await _previewInFlight; } catch (e) {}
      }
      if (!_currentFileBase64 || !_lastPreviewData) {
        showToast('يرجى معاينة الملف أولًا قبل الحفظ.', true);
        return;
      }

      // Check if source file on disk changed since preview
      const liveFile = fileInp && fileInp.files && fileInp.files[0];
      if (liveFile) {
        const liveMeta = getFileMetaSignature(liveFile);
        const liveBase64 = await readFileAsBase64(liveFile);
        if (liveMeta !== _currentFileMeta || liveBase64 !== _currentFileBase64) {
          _lastPreviewSignature = '';
          updateStalePreviewUI();
          showToast('تم تعديل الملف المصدري بعد آخر معاينة؛ يرجى إعادة المعاينة أولًا قبل الاعتماد.', true);
          return;
        }
      }

      if (isPreviewStale()) {
        updateStalePreviewUI();
        showToast('تغيرت خيارات الاستيراد أو مطابقة الأعمدة بعد آخر معاينة؛ يرجى إعادة المعاينة أولًا.', true);
        return;
      }

      if (
        !_forceImportAll &&
        ((Array.isArray(_lastPreviewData.missingRequiredFields) && _lastPreviewData.missingRequiredFields.length > 0) ||
        (Array.isArray(_lastPreviewData.mappingErrors) && _lastPreviewData.mappingErrors.length > 0))
      ) {
        showToast('لا يمكن اعتماد الاستيراد لوجود حقول إلزامية غير مربوطة أو أخطاء في مطابقة الأعمدة (أو فعّل خيار فرض الاستيراد).', true);
        return;
      }

      if (_userModifiedSelection && _selectedPreviewRows.size === 0 && !_forceImportAll) {
        showToast('لم تقم بتحديد أي صفوف للاستيراد؛ حدد صفًا واحدًا على الأقل أو اضغط «إضافة بالكلية».', true);
        return;
      }

      const opts = getImportControlState();
      const allPrevRows = Array.isArray(_lastPreviewData.rows) ? _lastPreviewData.rows : [];
      const hasSelectedNonStandard = allPrevRows.some((r, idx) => {
        const pIdx = r.previewIndex !== undefined ? r.previewIndex : idx;
        return _selectedPreviewRows.has(pIdx) && (r.status === 'invalid' || r.status === 'duplicate');
      });

      const commitBody = {
        dataset: opts.dataset,
        filename: _currentFilename,
        fileBase64: _currentFileBase64,
        expectedFileHash: _lastPreviewData.fileHash || '',
        sheetName: opts.sheetName || _lastPreviewData.selectedSheet || '',
        defaultMonth: opts.defaultMonth,
        mode: opts.mode,
        autoCreateRefs: opts.autoCreateRefs,
        autoCreateTeachers: opts.autoCreateTeachers,
        autoCreatePackages: opts.autoCreatePackages,
        columnMapping: opts.columnMapping
      };
      if (_userModifiedSelection || _forceImportAll || hasSelectedNonStandard) {
        commitBody.selectedRows = Array.from(_selectedPreviewRows).map((idx) => 'p:' + idx);
        commitBody.forceImport = Boolean(_forceImportAll || hasSelectedNonStandard);
      }
      if (_rowFieldOverrides && Object.keys(_rowFieldOverrides).length > 0) {
        commitBody.rowOverrides = _rowFieldOverrides;
      }

      try {
        const res = await apiRequest('/import/commit', {
          method: 'POST',
          body: commitBody
        });
        const added = res.added !== undefined ? res.added : ((res.summary && res.summary.addedCount) || 0);
        const updated = res.updated !== undefined ? res.updated : ((res.summary && res.summary.updatedCount) || 0);
        const skipped = res.skipped !== undefined ? res.skipped : ((res.summary && res.summary.skippedCount) || 0);
        renderCommitResultBanner(res);
        showToast(`تم الاستيراد بنجاح (إضافة: ${added} | تحديث: ${updated} | تخطي: ${skipped}).`);
        if (typeof onReloadFullState === 'function') await onReloadFullState();
      } catch (err) {}
    });
  }

  const btnBundled = $('#btnLoadBundledWorkbooks');
  if (btnBundled) {
    btnBundled.addEventListener('click', async () => {
      try {
        const res = await apiRequest('/import/commit', {
          method: 'POST',
          body: { loadBundledWorkbooks: true, force: true }
        });
        const s = res.summary || {};
        showToast(`تم تحميل بيانات ملفي الإكسيل بنجاح (إجمالي الطالبات: ${s.studentsCount || 110} | المعلمات: ${s.teachersCount || 8}).`);
        if (typeof onReloadFullState === 'function') await onReloadFullState();
      } catch (err) {}
    });
  }

  const btnUndo = $('#btnUndoLastImport');
  if (btnUndo) {
    btnUndo.addEventListener('click', async () => {
      try {
        const res = await apiRequest('/import/undo', {
          method: 'POST',
          body: {}
        });
        if (res && res.warning) {
          showToast(res.message || 'توجد تعديلات لاحقة بعد الاستيراد.', true);
          return;
        }
        showToast(`تم التراجع عن آخر استيراد واستعادة النسخة الاحتياطية (${res.restoredSnapshot || ''}).`);
        if (typeof onReloadFullState === 'function') await onReloadFullState();
      } catch (err) {}
    });
  }
}
