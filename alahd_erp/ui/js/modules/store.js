/* ============================================================================
   modules/store.js — Central Application State & Dynamic Months (v1.2.0)
   ============================================================================ */

export const FE = window.FinanceEngine;

export const DEFAULT_SUB_TYPES = [
  'خاص', '8 حلقات-ساعة', '8 حلقات-40 دقيقة', '8 حلقات-30 دقيقة',
  '8 حلقات-20 دقيقة', '4 حلقات - نصف ساعة', '12 حصة - 40 دقيقة',
  'خاص حلقين 40', 'خاص 20 دقيقة', 'الإجازة', 'تجويد نظري',
  'مجموعة تامة الأولى', 'مجموعة تامة الثانية', 'المجموعة الأولى',
  'مجموعة', 'شهري', 'أخرى'
];

export function todayISO() {
  const d = new Date();
  const y = d.getFullYear();
  const m = String(d.getMonth() + 1).padStart(2, '0');
  const day = String(d.getDate()).padStart(2, '0');
  return `${y}-${m}-${day}`;
}

export const state = {
  rev: 1,
  onboarded: false,
  pinEnabled: false,
  closedMonths: {},
  settings: {
    academyName: 'أكاديمية يوم جديد',
    logoDataUri: '',
    currency: 'ج.م',
    defaultDurationDays: 30,
    expiringSoonDays: 5,
    defaultTeacherPct: 70,
    pctSource: 'student',
    wageBasis: 'net',
    wageAccrualMode: 'accrued',
    monthsPastRange: 6,
    monthsFutureRange: 6,
    extraBackupFolder: '',
    backupWarnDays: 3,
    idleLockMinutes: 0
  },
  teachers: [],
  teacherMeta: {},
  subTypes: DEFAULT_SUB_TYPES.slice(),
  subTypesMap: {},
  columnMappings: {},
  dashConfig: {
    title: 'أكاديمية يوم جديد',
    currency: 'ج.م'
  },
  students: [],
  payouts: [],
  transactions: [],
  installments: [],
  groups: [],
  plans: [],
  sessions: [],
  memorization: [],
  madrasaBatches: [],
  madrasaEnrollments: [],
  madrasaLogs: [],
  certificates: []
};

const initialToday = todayISO();

export const uiState = {
  activeView: 'dashboard',
  selectedMonth: initialToday.slice(0, 7),
  selectedProfileId: '',
  backendOnline: false,
  referenceDate: initialToday,
  monthsList: [],
  pendingSaves: 0,
  saveFailed: false,
  lastSaveError: '',
  lastFailedRetryFn: null,
  pinUnlocked: false
};

export function cleanTeacherName(name) {
  return String(name || '').trim().replace(/\s+/g, ' ');
}

export function normalizeTeacherKey(name) {
  let s = cleanTeacherName(name);
  if (!s) return '';
  s = s.replace(/[\u064B-\u065F\u0670\u0640]/g, '').trim();
  const stripped = s.replace(/^(?:أ\s*\/\s*|أ\s*\.\s*|ا\s*\/\s*|ا\s*\.\s*|أستاذة\s+|استاذة\s+|أستاذه\s+|استاذه\s+|المعلمة\s+|معلمة\s+|معلمه\s+|الشيخة\s+|شيخة\s+|مس\s+)+/u, '').trim();
  if (stripped) s = stripped;
  s = s.replace(/[أإآٱ]/g, 'ا')
       .replace(/ى/g, 'ي')
       .replace(/ة/g, 'ه')
       .replace(/ؤ/g, 'و')
       .replace(/ئ/g, 'ي')
       .replace(/\bعبد\s+ال/g, 'عبدال')
       .replace(/[/._\-]+/g, ' ')
       .replace(/\s+/g, ' ')
       .trim()
       .toLowerCase();
  return s;
}

export function isTeacherArchived(teacherName) {
  const m = state.teacherMeta && state.teacherMeta[teacherName];
  if (!m || typeof m !== 'object') return false;
  return Boolean(m.archived === true || m.active === false);
}

export function isTeacherActive(teacherName) {
  return !isTeacherArchived(teacherName);
}

export function getActiveTeachers() {
  return (state.teachers || []).filter((t) => isTeacherActive(t));
}

export function getAllTeachersSorted() {
  const active = [];
  const inactive = [];
  (state.teachers || []).forEach((t) => {
    if (isTeacherActive(t)) active.push(t);
    else inactive.push(t);
  });
  return active.concat(inactive);
}

export function recomputeDynamicMonths() {
  const dataMonths = [];
  state.students.forEach((s) => {
    Object.keys(s.months || {}).forEach((mk) => dataMonths.push(mk));
  });
  state.installments.forEach((inst) => {
    if (inst.month) dataMonths.push(inst.month);
  });
  state.payouts.forEach((p) => {
    if (p.month) dataMonths.push(p.month);
  });
  state.transactions.forEach((tx) => {
    if (tx.month) dataMonths.push(tx.month);
  });
  Object.keys(state.closedMonths || {}).forEach((mk) => dataMonths.push(mk));

  const past = Number(state.settings.monthsPastRange || state.settings.monthsRangePast || 6);
  const future = Number(state.settings.monthsFutureRange || state.settings.monthsRangeFuture || 6);
  uiState.monthsList = FE.generateDynamicMonths(uiState.referenceDate, past, future, dataMonths);

  if (!uiState.monthsList.some((m) => m.key === uiState.selectedMonth)) {
    const curMk = uiState.referenceDate.slice(0, 7);
    if (uiState.monthsList.some((m) => m.key === curMk)) {
      uiState.selectedMonth = curMk;
    } else if (uiState.monthsList.length > 0) {
      uiState.selectedMonth = uiState.monthsList[0].key;
    }
  }
}

export function money(val) {
  const n = FE.roundHalfUp(val || 0);
  const curr = (state.settings && state.settings.currency) || 'ج.م';
  return `${n.toLocaleString('en-US')} ${curr}`;
}

export function monthLabel(mk) {
  const found = uiState.monthsList.find((m) => m.key === mk);
  if (found) return found.label;
  return FE.formatMonthLabel ? FE.formatMonthLabel(mk) : mk;
}

export function normalizeStudentMonths(monthsObj) {
  const out = {};
  const src = monthsObj && typeof monthsObj === 'object' ? monthsObj : {};
  Object.keys(src).forEach((mk) => {
    const m = src[mk] || {};
    const payDt = FE.resolvePaymentDate(m);
    const startDt = FE.resolveActualStartDate(m);
    const endDt = FE.resolveEndDate(m);
    out[mk] = {
      value: (m.value !== null && m.value !== undefined && m.value !== '') ? FE.roundHalfUp(m.value) : null,
      paymentDate: payDt,
      actualStartDate: startDt,
      pay: payDt,
      startDate: startDt,
      end: endDt,
      endDate: endDt,
      paidStatus: m.paidStatus || '',
      partialPaid: (m.partialPaid !== null && m.partialPaid !== undefined && m.partialPaid !== '') ? FE.roundHalfUp(m.partialPaid) : null,
      customWage: (m.customWage !== null && m.customWage !== undefined && m.customWage !== '') ? FE.roundHalfUp(m.customWage) : null,
      discount: (m.discount !== null && m.discount !== undefined && m.discount !== '') ? FE.roundHalfUp(m.discount) : null,
      invoiceNote: m.invoiceNote || '',
      att: m.att || { weeks: [['', '', ''], ['', '', ''], ['', '', ''], ['', '', '']], note: '' }
    };
  });
  return out;
}

export function applyLoadedState(rawData) {
  if (!rawData || typeof rawData !== 'object') return;
  if (typeof rawData.rev === 'number') {
    state.rev = rawData.rev;
  }
  if (typeof rawData.onboarded === 'boolean') {
    state.onboarded = rawData.onboarded;
  }
  if (typeof rawData.pinRequired === 'boolean') {
    state.pinEnabled = rawData.pinRequired;
  } else if (typeof rawData.pinEnabled === 'boolean') {
    state.pinEnabled = rawData.pinEnabled;
  } else if (rawData.settings && typeof rawData.settings.pinEnabled === 'boolean') {
    state.pinEnabled = rawData.settings.pinEnabled;
  }
  if (Array.isArray(rawData.closedMonths)) {
    const cmMap = {};
    rawData.closedMonths.forEach((entry) => {
      if (entry && entry.month) cmMap[entry.month] = entry;
    });
    state.closedMonths = cmMap;
  } else {
    state.closedMonths = (rawData.closedMonths && typeof rawData.closedMonths === 'object') ? rawData.closedMonths : {};
  }
  if (rawData.settings && typeof rawData.settings === 'object') {
    state.settings = Object.assign({}, state.settings, rawData.settings);
  }
  state.teachers = Array.isArray(rawData.teachers) ? rawData.teachers.slice() : [];
  state.teacherMeta = rawData.teacherMeta && typeof rawData.teacherMeta === 'object' ? rawData.teacherMeta : {};
  state.teachers.forEach((t) => {
    const m = state.teacherMeta[t] = Object.assign({}, state.teacherMeta[t] || {});
    if (m.archived === true || m.active === false) {
      m.archived = true;
      m.active = false;
    } else {
      m.active = true;
    }
  });

  if (Array.isArray(rawData.subTypes) && rawData.subTypes.length) {
    state.subTypes = rawData.subTypes.map((k) => String(k || '').trim()).filter(Boolean);
    state.subTypesMap = {};
    state.subTypes.forEach((k) => {
      state.subTypesMap[k] = 0;
    });
  } else if (rawData.subTypes && typeof rawData.subTypes === 'object' && !Array.isArray(rawData.subTypes)) {
    const keys = Object.keys(rawData.subTypes).map((k) => String(k || '').trim()).filter(Boolean);
    state.subTypes = keys.length ? keys : DEFAULT_SUB_TYPES.slice();
    state.subTypesMap = Object.assign({}, rawData.subTypes);
  } else {
    state.subTypes = DEFAULT_SUB_TYPES.slice();
    state.subTypesMap = {};
  }
  state.columnMappings = (rawData.columnMappings && typeof rawData.columnMappings === 'object') ? rawData.columnMappings : {};
  state.dashConfig = rawData.dashConfig || {
    title: state.settings.academyName || 'أكاديمية يوم جديد',
    currency: state.settings.currency || 'ج.م'
  };

  const defPct = FE.parsePctInt(state.settings.defaultTeacherPct, 70);
  state.students = (Array.isArray(rawData.students) ? rawData.students : []).map((s, i) => ({
    id: s.id || ('stu' + String(i + 1).padStart(4, '0')),
    code: s.code || ('STU-' + String(i + 1).padStart(4, '0')),
    name: String(s.name || '').trim(),
    phone: String(s.phone || '').trim(),
    teacher: String(s.teacher || '').trim(),
    type: String(s.type || '').trim(),
    pct: FE.parsePctInt(s.pct, defPct),
    schedule: String(s.schedule || ''),
    adminStatus: String(s.adminStatus || ''),
    paymentDate: String(s.paymentDate || ''),
    actualStartDate: String(s.actualStartDate || ''),
    transferHistory: Array.isArray(s.transferHistory) ? s.transferHistory : [],
    months: normalizeStudentMonths(s.months)
  }));

  // Ensure any package assigned to a student is also represented in state.subTypes (Single Source of Truth)
  state.students.forEach((s) => {
    if (s.type && !state.subTypes.includes(s.type)) {
      state.subTypes.push(s.type);
    }
    if (s.teacher && !state.teachers.includes(s.teacher)) {
      state.teachers.push(s.teacher);
    }
  });

  state.payouts = Array.isArray(rawData.payouts) ? rawData.payouts : [];
  state.transactions = Array.isArray(rawData.transactions) ? rawData.transactions : [];
  state.installments = Array.isArray(rawData.installments) ? rawData.installments : [];
  state.groups = Array.isArray(rawData.groups) ? rawData.groups : [];
  state.plans = Array.isArray(rawData.plans) ? rawData.plans : [];
  state.sessions = Array.isArray(rawData.sessions) ? rawData.sessions : [];
  state.memorization = Array.isArray(rawData.memorization) ? rawData.memorization : [];
  state.madrasaBatches = Array.isArray(rawData.madrasaBatches) ? rawData.madrasaBatches : [];
  state.madrasaEnrollments = Array.isArray(rawData.madrasaEnrollments) ? rawData.madrasaEnrollments : [];
  state.madrasaLogs = Array.isArray(rawData.madrasaLogs) ? rawData.madrasaLogs : [];
  state.certificates = Array.isArray(rawData.certificates) ? rawData.certificates : [];

  recomputeDynamicMonths();

  if (!uiState.selectedProfileId && state.students.length > 0) {
    uiState.selectedProfileId = state.students[0].id;
  } else if (uiState.selectedProfileId && !state.students.some((s) => s.id === uiState.selectedProfileId)) {
    uiState.selectedProfileId = state.students[0] ? state.students[0].id : '';
  }
}
