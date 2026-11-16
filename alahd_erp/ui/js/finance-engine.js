/* ============================================================================
   finance-engine.js — Isomorphic Financial & Study Period Calculation Engine
   Yawm Jadid Academy Suite (v1.1.0)
   - Works in both Browser (`window.FinanceEngine`) and Node.js (`module.exports`).
   - Uses Scaled Integer / BigInt Decimal arithmetic with Round Half Up to nearest EGP.
   - Enforces strict independence between:
     1) Payment Date (`paymentDate` / `pay`): used for payment tracking, collection status,
        collection reports, and cash-in records.
     2) Actual Start Date (`actualStartDate` / `startDate`): used for calculating subscription
        duration, study periods, elapsed/remaining days, and attendance session projections.
   - Enforces NET subscription (value - discount) everywhere, receipt-based payment status,
     integer percentage (0..100), and dynamic months.
   ============================================================================ */
(function (root, factory) {
  if (typeof module === 'object' && module.exports) {
    module.exports = factory();
  } else {
    root.FinanceEngine = factory();
  }
})(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  var SCALE_DIGITS = 6;
  var SCALE = 1000000;
  var DATE_RE = /^\d{4}-\d{2}-\d{2}$/;
  var MONTH_RE = /^\d{4}-(0[1-9]|1[0-2])$/;

  var ARABIC_MONTH_NAMES = {
    1: 'يناير', 2: 'فبراير', 3: 'مارس', 4: 'أبريل',
    5: 'مايو', 6: 'يونيو', 7: 'يوليو', 8: 'أغسطس',
    9: 'سبتمبر', 10: 'أكتوبر', 11: 'نوفمبر', 12: 'ديسمبر'
  };

  function formatMonthLabel(monthKey) {
    var mk = String(monthKey || '').trim();
    if (!MONTH_RE.test(mk)) return mk;
    var parts = mk.split('-');
    var y = parts[0];
    var m = parseInt(parts[1], 10);
    return (ARABIC_MONTH_NAMES[m] || parts[1]) + ' ' + y;
  }

  function generateDynamicMonths(referenceDate, pastMonths, futureMonths, extraMonths) {
    if (Array.isArray(pastMonths)) {
      var _arr = pastMonths;
      pastMonths = futureMonths;
      futureMonths = extraMonths;
      extraMonths = _arr;
    }
    var refStr = isValidCalendarDate(referenceDate) ? referenceDate.trim() : new Date().toISOString().slice(0, 10);
    var parts = refStr.split('-');
    var baseY = parseInt(parts[0], 10);
    var baseM = parseInt(parts[1], 10);
    var past = Math.max(0, parseInt(pastMonths !== undefined ? pastMonths : 6, 10));
    var future = Math.max(0, parseInt(futureMonths !== undefined ? futureMonths : 6, 10));
    var seen = Object.create(null);
    var keys = [];

    for (var offset = -past; offset <= future; offset++) {
      var totalM = (baseY * 12 + (baseM - 1)) + offset;
      var y = Math.floor(totalM / 12);
      var m = (totalM % 12) + 1;
      if (m <= 0) { m += 12; y -= 1; }
      var k = String(y).padStart(4, '0') + '-' + String(m).padStart(2, '0');
      if (!seen[k]) {
        seen[k] = true;
        keys.push(k);
      }
    }
    if (Array.isArray(extraMonths)) {
      for (var i = 0; i < extraMonths.length; i++) {
        var ex = String(extraMonths[i] || '').trim();
        if (MONTH_RE.test(ex) && !seen[ex]) {
          seen[ex] = true;
          keys.push(ex);
        }
      }
    }
    keys.sort();
    return keys.map(function (k) {
      return { key: k, label: formatMonthLabel(k) };
    });
  }

  function isValidCalendarDate(isoStr) {
    if (!isoStr || typeof isoStr !== 'string' || !DATE_RE.test(isoStr.trim())) return false;
    var parts = isoStr.trim().split('-');
    var y = parseInt(parts[0], 10);
    var m = parseInt(parts[1], 10);
    var d = parseInt(parts[2], 10);
    if (m < 1 || m > 12 || d < 1 || d > 31) return false;
    var dt = new Date(Date.UTC(y, m - 1, d));
    return dt.getUTCFullYear() === y && (dt.getUTCMonth() + 1) === m && dt.getUTCDate() === d;
  }

  function addDaysISO(isoStr, days) {
    if (!isValidCalendarDate(isoStr)) return '';
    var parts = isoStr.trim().split('-');
    var dt = new Date(Date.UTC(parseInt(parts[0], 10), parseInt(parts[1], 10) - 1, parseInt(parts[2], 10)));
    dt.setUTCDate(dt.getUTCDate() + Number(days || 0));
    return dt.toISOString().slice(0, 10);
  }

  function diffDaysISO(fromIso, toIso) {
    if (!isValidCalendarDate(fromIso) || !isValidCalendarDate(toIso)) return 0;
    var p1 = fromIso.trim().split('-');
    var p2 = toIso.trim().split('-');
    var d1 = Date.UTC(parseInt(p1[0], 10), parseInt(p1[1], 10) - 1, parseInt(p1[2], 10));
    var d2 = Date.UTC(parseInt(p2[0], 10), parseInt(p2[1], 10) - 1, parseInt(p2[2], 10));
    return Math.round((d2 - d1) / 86400000);
  }

  function toScaledInt(val) {
    if (val === null || val === undefined || val === '' || typeof val === 'boolean') {
      return 0;
    }
    var s = String(val).trim();
    if (!s) return 0;
    var numCheck = Number(s);
    if (!Number.isFinite(numCheck)) {
      throw new Error('Invalid non-finite number: ' + val);
    }
    if (s.indexOf('e') !== -1 || s.indexOf('E') !== -1) {
      s = numCheck.toFixed(SCALE_DIGITS);
    }
    var neg = false;
    if (s.charAt(0) === '-') {
      neg = true;
      s = s.slice(1);
    } else if (s.charAt(0) === '+') {
      s = s.slice(1);
    }
    var parts = s.split('.');
    var intPart = parseInt(parts[0] || '0', 10);
    var fracStr = (parts[1] || '').slice(0, SCALE_DIGITS + 1);
    while (fracStr.length < SCALE_DIGITS + 1) {
      fracStr += '0';
    }
    var fracScaled = parseInt(fracStr.slice(0, SCALE_DIGITS), 10);
    var seventhDigit = parseInt(fracStr.charAt(SCALE_DIGITS), 10);
    if (seventhDigit >= 5) {
      fracScaled += 1;
    }
    var total = intPart * SCALE + fracScaled;
    return neg ? -total : total;
  }

  function roundHalfUp(val) {
    var scaled = toScaledInt(val);
    if (scaled >= 0) {
      return Math.floor((scaled + (SCALE / 2)) / SCALE);
    }
    return -Math.floor(((-scaled) + (SCALE / 2)) / SCALE);
  }

  function parsePctInt(pctVal, defaultPct) {
    var def = (defaultPct !== undefined && defaultPct !== null) ? defaultPct : 0;
    if (pctVal === null || pctVal === undefined || pctVal === '') return def;
    if (typeof pctVal === 'boolean') {
      throw new Error('Percentage cannot be boolean');
    }
    var num = Number(pctVal);
    if (!isNaN(num) && num > 0 && num < 1) {
      pctVal = num * 100;
    }
    var scaled = toScaledInt(pctVal);
    if (scaled < 0 || scaled > 100 * SCALE) {
      throw new Error('Percentage must be between 0 and 100, got ' + pctVal);
    }
    return roundHalfUp(pctVal);
  }

  function normalizePctScaled(pctVal) {
    var pctInt = parsePctInt(pctVal, 0);
    return Math.round((pctInt * SCALE) / 100);
  }

  function normalizePctFraction(pctVal) {
    return parsePctInt(pctVal, 0) / 100;
  }

  function multiplyAndRoundHalfUp(amount, pctIntOrVal) {
    var amtScaled = toScaledInt(amount);
    var pctScaled = normalizePctScaled(pctIntOrVal);
    if (amtScaled <= 0 || pctScaled <= 0) return 0;
    var prod = BigInt(amtScaled) * BigInt(pctScaled);
    var denom = BigInt(SCALE) * BigInt(SCALE);
    var half = denom / BigInt(2);
    return Number((prod + half) / denom);
  }

  function resolvePaymentDate(monthEntry, studentMonthInstallments, referenceDate) {
    if (Array.isArray(studentMonthInstallments) && studentMonthInstallments.length > 0) {
      var maxDt = '';
      for (var i = 0; i < studentMonthInstallments.length; i++) {
        var inst = studentMonthInstallments[i];
        if (!inst || typeof inst !== 'object') continue;
        var dt = String(inst.paymentDate || inst.date || '').trim();
        if (dt && (!referenceDate || dt <= referenceDate)) {
          if (!maxDt || dt > maxDt) maxDt = dt;
        }
      }
      if (maxDt) return maxDt;
    }
    if (!monthEntry || typeof monthEntry !== 'object') return '';
    if (monthEntry.paymentDate !== undefined && monthEntry.paymentDate !== null) {
      return String(monthEntry.paymentDate).trim();
    }
    if (monthEntry.pay !== undefined && monthEntry.pay !== null) {
      return String(monthEntry.pay).trim();
    }
    return '';
  }

  function resolveActualStartDate(monthEntry) {
    if (!monthEntry || typeof monthEntry !== 'object') return '';
    if (monthEntry.actualStartDate !== undefined && monthEntry.actualStartDate !== null) {
      return String(monthEntry.actualStartDate).trim();
    }
    if (monthEntry.startDate !== undefined && monthEntry.startDate !== null) {
      return String(monthEntry.startDate).trim();
    }
    return '';
  }

  function resolveEndDate(monthEntry) {
    if (!monthEntry || typeof monthEntry !== 'object') return '';
    if (monthEntry.endDate !== undefined && monthEntry.endDate !== null && String(monthEntry.endDate).trim() !== '') {
      return String(monthEntry.endDate).trim();
    }
    if (monthEntry.end !== undefined && monthEntry.end !== null) {
      return String(monthEntry.end).trim();
    }
    return '';
  }

  function calculateStudyPeriod(actualStartDate, endDate, durationDays, referenceDate, expiringSoonDays) {
    var startStr = isValidCalendarDate(actualStartDate) ? actualStartDate.trim() : '';
    var endStr = isValidCalendarDate(endDate) ? endDate.trim() : '';
    var refStr = isValidCalendarDate(referenceDate) ? referenceDate.trim() : new Date().toISOString().slice(0, 10);
    var dur = Math.max(1, parseInt(durationDays || 30, 10));
    var soonThreshold = (expiringSoonDays !== undefined && expiringSoonDays !== null) ? Math.max(0, parseInt(expiringSoonDays, 10)) : 5;

    if (!startStr && !endStr) {
      return {
        actualStartDate: '',
        calculatedEndDate: '',
        durationDays: 0,
        elapsedDays: 0,
        daysUntilStart: 0,
        remainingDays: null,
        status: 'unscheduled'
      };
    }

    if (startStr && !endStr) {
      endStr = addDaysISO(startStr, dur);
    }

    var totalDays = (startStr && endStr) ? Math.max(0, diffDaysISO(startStr, endStr)) : dur;

    if (startStr && refStr < startStr) {
      var daysUntilStart = diffDaysISO(refStr, startStr);
      var remBeforeStart = endStr ? diffDaysISO(refStr, endStr) : totalDays;
      return {
        actualStartDate: startStr,
        calculatedEndDate: endStr,
        durationDays: totalDays,
        elapsedDays: 0,
        daysUntilStart: daysUntilStart,
        remainingDays: remBeforeStart,
        status: 'not_started'
      };
    }

    var elapsed = startStr ? Math.max(0, diffDaysISO(startStr, refStr)) : 0;
    var remaining = endStr ? diffDaysISO(refStr, endStr) : 0;
    var status = 'active';
    if (remaining < 0) status = 'expired';
    else if (remaining <= soonThreshold) status = 'expiring_soon';

    return {
      actualStartDate: startStr,
      calculatedEndDate: endStr,
      durationDays: totalDays,
      elapsedDays: elapsed,
      daysUntilStart: 0,
      remainingDays: remaining,
      status: status
    };
  }

  function evaluateStudentMonthDates(monthEntry, referenceDate, defaultDurationDays, expiringSoonDays, studentMonthInstallments) {
    var payDate = resolvePaymentDate(monthEntry, studentMonthInstallments, referenceDate);
    var actualStart = resolveActualStartDate(monthEntry);
    var explicitEnd = resolveEndDate(monthEntry);
    var studyPeriod = calculateStudyPeriod(actualStart, explicitEnd, defaultDurationDays || 30, referenceDate, expiringSoonDays);
    return {
      paymentDate: payDate,
      actualStartDate: actualStart,
      endDate: explicitEnd || studyPeriod.calculatedEndDate,
      isPaidByDate: Boolean(payDate),
      paidInAdvance: Boolean(payDate && actualStart && payDate < actualStart),
      paidAfterStart: Boolean(payDate && actualStart && payDate > actualStart),
      studyPeriod: studyPeriod
    };
  }

  function resolveWagePctInt(student, teacherMeta, pctSource, defaultPct) {
    var stu = student || {};
    var tname = String(stu.teacher || '').trim();
    var tPctRaw = null;
    if (teacherMeta && tname && teacherMeta[tname] && teacherMeta[tname].pct !== undefined && teacherMeta[tname].pct !== null && teacherMeta[tname].pct !== '') {
      tPctRaw = teacherMeta[tname].pct;
    }
    var sPctRaw = stu.pct;
    var defVal = (defaultPct !== undefined && defaultPct !== null) ? defaultPct : 70;

    if (pctSource === 'teacher' && tPctRaw !== null) {
      return parsePctInt(tPctRaw, defVal);
    }
    if (sPctRaw !== undefined && sPctRaw !== null && sPctRaw !== '') {
      return parsePctInt(sPctRaw, defVal);
    }
    if (tPctRaw !== null) {
      return parsePctInt(tPctRaw, defVal);
    }
    return parsePctInt(defVal, 70);
  }

  function resolveWagePctFraction(student, teacherMeta, pctSource, defaultPct) {
    return resolveWagePctInt(student, teacherMeta, pctSource, defaultPct) / 100;
  }

  function effectiveMonthSubscription(monthEntry) {
    if (!monthEntry || typeof monthEntry !== 'object') return 0;
    var v = monthEntry.value;
    if (v === null || v === undefined || v === '' || isNaN(Number(v))) return 0;
    return Math.max(0, roundHalfUp(v));
  }

  function rawMonthDiscount(monthEntry) {
    if (!monthEntry || typeof monthEntry !== 'object') return 0;
    var d = monthEntry.discount;
    if (d === null || d === undefined || d === '' || isNaN(Number(d))) return 0;
    return Math.max(0, roundHalfUp(d));
  }

  function effectiveMonthDiscount(monthEntry) {
    var sub = effectiveMonthSubscription(monthEntry);
    var disc = rawMonthDiscount(monthEntry);
    return Math.min(sub, disc);
  }

  function effectiveMonthNetSubscription(monthEntry) {
    var sub = effectiveMonthSubscription(monthEntry);
    if (sub <= 0) return 0;
    var disc = effectiveMonthDiscount(monthEntry);
    return Math.max(0, sub - disc);
  }

  /**
   * Calculates actual collected fee for the month strictly from non-future receipts (`installments`).
   * - A non-empty `paymentDate` alone on `monthEntry` does NOT mark a month as paid/collected.
   * - A future-dated receipt (`paymentDate > referenceDate`) does NOT count as collected.
   * - NEVER uses `actualStartDate`!
   */
  function effectiveMonthCollected(monthEntry, studentMonthInstallments, referenceDate) {
    if (!monthEntry || typeof monthEntry !== 'object') return 0;
    var netSub = effectiveMonthNetSubscription(monthEntry);
    if (netSub <= 0) return 0;

    var refDt = isValidCalendarDate(referenceDate) ? referenceDate.trim() : null;

    var insts = studentMonthInstallments;
    if (insts === undefined && Array.isArray(monthEntry.installments)) {
      insts = monthEntry.installments;
    }

    if (Array.isArray(insts)) {
      var instSum = 0;
      for (var i = 0; i < insts.length; i++) {
        var inst = insts[i];
        if (!inst || typeof inst !== 'object') continue;
        var payDt = String(inst.paymentDate || inst.date || '').trim();
        if (payDt && refDt && payDt > refDt) continue;
        if (inst.amount !== undefined && inst.amount !== null && inst.amount !== '' && !isNaN(Number(inst.amount))) {
          instSum += Math.max(0, roundHalfUp(inst.amount));
        }
      }
      return Math.min(netSub, instSum);
    }

    // Fallback when no installments array is provided at all, but paymentDate is explicitly set
    var pDt = resolvePaymentDate(monthEntry, null, refDt);
    if (pDt && (!refDt || pDt <= refDt)) {
      return netSub;
    }

    return 0;
  }

  function deriveMonthPaymentStatus(monthEntry, studentMonthInstallments, referenceDate) {
    if (!monthEntry || typeof monthEntry !== 'object') return 'unsubscribed';
    var sub = effectiveMonthSubscription(monthEntry);
    if (sub <= 0) return 'unsubscribed';
    var rawDisc = rawMonthDiscount(monthEntry);
    if (rawDisc >= sub) return 'over_discounted';
    var netSub = Math.max(0, sub - rawDisc);
    var coll = effectiveMonthCollected(monthEntry, studentMonthInstallments, referenceDate);
    if (coll >= netSub) return 'paid';
    if (coll > 0) return 'partial';
    return 'unpaid';
  }

  // Call contract: every wage/aggregate function accepts EITHER the positional arguments below OR a single options
  // object in the `pctSource` slot: { pctSource, wageBasis, wageAccrualMode, referenceDate, defaultPct,
  // collectedAmount }. app.js uses the options form (Phase 2 fix: it used to be silently ignored, so the UI
  // always computed with accrued/net/no-reference-date regardless of settings).
  function isOptionsObject(v) {
    return v !== null && typeof v === 'object' && !Array.isArray(v);
  }

  function effectiveMonthWage(student, monthEntry, teacherMeta, pctSource, wageBasis, wageAccrualMode, studentMonthInstallments, referenceDate, defaultPct) {
    var collectedOverride = null;
    if (isOptionsObject(pctSource)) {
      var wo = pctSource;
      pctSource = wo.pctSource;
      wageBasis = wo.wageBasis;
      wageAccrualMode = wo.wageAccrualMode;
      referenceDate = wo.referenceDate;
      defaultPct = wo.defaultPct;
      if (wo.collectedAmount !== undefined && wo.collectedAmount !== null) collectedOverride = wo.collectedAmount;
    }
    if (!monthEntry || typeof monthEntry !== 'object') return 0;
    var cw = monthEntry.customWage;
    if (cw !== undefined && cw !== null && cw !== '' && !isNaN(Number(cw))) {
      var cwInt = Math.max(0, roundHalfUp(cw));
      if (wageAccrualMode === 'collected') {
        var collCw = collectedOverride !== null ? collectedOverride : effectiveMonthCollected(monthEntry, studentMonthInstallments, referenceDate);
        return collCw > 0 ? cwInt : 0;
      }
      return cwInt;
    }
    var grossSub = effectiveMonthSubscription(monthEntry);
    if (grossSub <= 0) return 0;
    var netSub = effectiveMonthNetSubscription(monthEntry);
    var baseAmount;
    if (wageAccrualMode === 'collected') {
      baseAmount = collectedOverride !== null ? collectedOverride : effectiveMonthCollected(monthEntry, studentMonthInstallments, referenceDate);
    } else {
      baseAmount = (wageBasis === 'gross') ? grossSub : netSub;
    }
    if (baseAmount <= 0) return 0;
    var pctInt = resolveWagePctInt(student, teacherMeta, pctSource || 'student', defaultPct);
    return multiplyAndRoundHalfUp(baseAmount, pctInt);
  }

  function indexInstallments(installments) {
    var map = Object.create(null);
    if (!Array.isArray(installments)) return map;
    for (var i = 0; i < installments.length; i++) {
      var inst = installments[i];
      if (!inst || typeof inst !== 'object') continue;
      var sid = String(inst.studentId || '').trim();
      var mk = String(inst.month || '').trim();
      if (!sid || !mk) continue;
      var k = sid + '|' + mk;
      if (!map[k]) map[k] = [];
      map[k].push(inst);
    }
    return map;
  }

  function migrateLegacyPaymentsToReceipts(stateObj) {
    var st = stateObj || {};
    var students = Array.isArray(st.students) ? st.students : [];
    var existingInsts = Array.isArray(st.installments) ? st.installments.slice() : [];
    var instMap = indexInstallments(existingInsts);
    var existingIds = Object.create(null);
    for (var i = 0; i < existingInsts.length; i++) {
      if (existingInsts[i] && existingInsts[i].id) existingIds[existingInsts[i].id] = true;
    }

    for (var s = 0; s < students.length; s++) {
      var stu = students[s];
      if (!stu || typeof stu !== 'object') continue;
      var sid = String(stu.id || '').trim();
      if (!sid) continue;
      var months = stu.months || {};
      var mKeys = Object.keys(months).sort();
      for (var k = 0; k < mKeys.length; k++) {
        var mk = mKeys[k];
        var m = months[mk];
        if (!m || typeof m !== 'object') continue;
        if (instMap[sid + '|' + mk] && instMap[sid + '|' + mk].length > 0) continue;
        var netSub = effectiveMonthNetSubscription(m);
        if (netSub <= 0) continue;

        var legacyAmt = 0;
        var pp = m.partialPaid;
        var payDt = String(m.paymentDate || m.pay || '').trim();
        var paidSt = String(m.paidStatus || '').trim();

        if (pp !== undefined && pp !== null && pp !== '' && !isNaN(Number(pp))) {
          legacyAmt = Math.min(netSub, Math.max(0, roundHalfUp(pp)));
        } else if (payDt || paidSt === 'paid') {
          legacyAmt = netSub;
        }

        if (legacyAmt > 0) {
          var rcpDate = payDt.length === 10 ? payDt : (mk + '-01');
          var instId = 'inst_mig_' + sid + '_' + mk;
          var suffix = 1;
          while (existingIds[instId]) {
            suffix++;
            instId = 'inst_mig_' + sid + '_' + mk + '_' + suffix;
          }
          existingIds[instId] = true;
          var newInst = {
            id: instId,
            receiptNo: 'RCP-MIG-' + sid + '-' + mk.replace('-', ''),
            studentId: sid,
            month: mk,
            amount: legacyAmt,
            date: rcpDate,
            paymentDate: rcpDate,
            method: 'نقدي / محوّل',
            note: 'إيصال مرحّل تلقائيًا من السجل السابق',
            createdAt: rcpDate
          };
          existingInsts.push(newInst);
          instMap[sid + '|' + mk] = [newInst];
        }
      }
    }
    st.installments = existingInsts;
    return existingInsts;
  }

  function computeMonthAggregate(students, monthKey, installments, teacherMeta, pctSource, wageBasis, wageAccrualMode, referenceDate, defaultPct) {
    if (isOptionsObject(pctSource)) {
      var ao = pctSource;
      pctSource = ao.pctSource;
      wageBasis = ao.wageBasis;
      wageAccrualMode = ao.wageAccrualMode;
      referenceDate = ao.referenceDate;
      defaultPct = ao.defaultPct;
    }
    var hasInstArray = Array.isArray(installments);
    var instMap = hasInstArray ? indexInstallments(installments) : null;
    var grossValue = 0, discounts = 0, value = 0, collected = 0, wages = 0, count = 0, paidCount = 0;
    var advancePaidCount = 0, deferredPaidCount = 0, activeStudyCount = 0;
    var list = Array.isArray(students) ? students : [];
    for (var i = 0; i < list.length; i++) {
      var s = list[i];
      if (!s || !(s.name || '').trim()) continue;
      var m = (s.months && s.months[monthKey]) || {};
      var grossV = effectiveMonthSubscription(m);
      var discV = effectiveMonthDiscount(m);
      var netV = effectiveMonthNetSubscription(m);
      var stuInsts = hasInstArray ? (instMap[String(s.id || '') + '|' + monthKey] || []) : undefined;
      var w = effectiveMonthWage(s, m, teacherMeta, pctSource, wageBasis, wageAccrualMode, stuInsts, referenceDate, defaultPct);
      var c = effectiveMonthCollected(m, stuInsts, referenceDate);
      var payDt = resolvePaymentDate(m, stuInsts, referenceDate);
      var startDt = resolveActualStartDate(m);
      if (grossV > 0) {
        count++;
        if (c > 0) paidCount++;
        if (payDt && startDt) {
          if (payDt < startDt) advancePaidCount++;
          else if (payDt > startDt) deferredPaidCount++;
        }
        if (startDt) activeStudyCount++;
      }
      grossValue += grossV;
      discounts += discV;
      value += netV;
      collected += c;
      wages += w;
    }
    var uncollected = Math.max(0, value - collected);
    var net = value - wages;
    return {
      grossValue: grossValue,
      discounts: discounts,
      value: value,
      collected: collected,
      uncollected: uncollected,
      wages: wages,
      net: net,
      count: count,
      paidCount: paidCount,
      advancePaidCount: advancePaidCount,
      deferredPaidCount: deferredPaidCount,
      activeStudyCount: activeStudyCount
    };
  }

  function computeAccountingSummary(stateObj, mkFilter, pctSource, wageBasis, wageAccrualMode, referenceDate) {
    var st = stateObj || {};
    var settings = st.settings || {};
    var optionsForm = isOptionsObject(pctSource);
    if (optionsForm) {
      var so = pctSource;
      pctSource = so.pctSource || 'student';   // options form: do not fall back to settings.pctSource (matches Python reports)
      wageBasis = so.wageBasis;
      wageAccrualMode = so.wageAccrualMode;
      referenceDate = so.referenceDate;
    }
    var effPctSource = pctSource || settings.pctSource || 'student';
    var effWageBasis = wageBasis || settings.wageBasis || 'net';
    var effWageAccrual = wageAccrualMode || settings.wageAccrualMode || 'accrued';
    var defaultPct = (settings.defaultTeacherPct !== undefined && settings.defaultTeacherPct !== null) ? settings.defaultTeacherPct : 70;
    var refDt = isValidCalendarDate(referenceDate) ? referenceDate.trim() : null;

    var students = Array.isArray(st.students) ? st.students.filter(function (s) { return s && (s.name || '').trim(); }) : [];
    var installments = Array.isArray(st.installments) ? st.installments : null;
    var payouts = Array.isArray(st.payouts) ? st.payouts : [];
    var transactions = Array.isArray(st.transactions) ? st.transactions : [];
    var teacherMeta = st.teacherMeta || {};

    var seenMk = Object.create(null);
    var monthKeys = [];
    for (var si = 0; si < students.length; si++) {
      var smObj = students[si].months || {};
      var smKeys = Object.keys(smObj);
      for (var ki = 0; ki < smKeys.length; ki++) {
        var k = smKeys[ki];
        if (k && !seenMk[k]) {
          seenMk[k] = true;
          monthKeys.push(k);
        }
      }
    }
    monthKeys.sort();

    var isAll = !mkFilter || mkFilter === '__all__' || mkFilter === 'all';
    var targetMonths = isAll ? monthKeys : [mkFilter];

    var grossSubs = 0, discountsTotal = 0, subsValue = 0, subsCollected = 0, wagesTotal = 0;
    for (var i = 0; i < targetMonths.length; i++) {
      var agg = computeMonthAggregate(students, targetMonths[i], installments, teacherMeta, effPctSource, effWageBasis, effWageAccrual, refDt, defaultPct);
      grossSubs += agg.grossValue;
      discountsTotal += agg.discounts;
      subsValue += agg.value;
      subsCollected += agg.collected;
      wagesTotal += agg.wages;
    }

    var cashReceiptsIn = 0;
    var instList = Array.isArray(installments) ? installments : [];
    for (var ci = 0; ci < instList.length; ci++) {
      var inst = instList[ci];
      if (!inst || typeof inst !== 'object') continue;
      var pDt = String(inst.paymentDate || inst.date || '').trim();
      if (!pDt || pDt > refDt) continue;
      if (!isAll && pDt.slice(0, 7) !== mkFilter) continue;
      cashReceiptsIn += Math.max(0, roundHalfUp(inst.amount || 0));
    }

    var salaryPaid = 0;
    for (var p = 0; p < payouts.length; p++) {
      var po = payouts[p];
      if (!po) continue;
      if (isAll || po.month === mkFilter) {
        salaryPaid += Math.max(0, roundHalfUp(po.amount || 0));
      }
    }

    var extraRevenue = 0, operatingExpense = 0;
    var txList = [];
    for (var t = 0; t < transactions.length; t++) {
      var tx = transactions[t];
      if (!tx) continue;
      var txMonth = tx.month || String(tx.date || '').slice(0, 7);
      if (isAll || txMonth === mkFilter) {
        txList.push(tx);
        var amt = Math.max(0, roundHalfUp(tx.amount || 0));
        if (tx.type === 'revenue') extraRevenue += amt;
        else if (tx.type === 'expense') operatingExpense += amt;
      }
    }

    var totalCashIn = cashReceiptsIn + extraRevenue;
    var totalCashOut = salaryPaid + operatingExpense;
    var netCashBalance = totalCashIn - totalCashOut;
    var netOperatingProfit = (subsValue - wagesTotal) + extraRevenue - operatingExpense;

    return {
      grossSubscriptions: grossSubs,
      discounts: discountsTotal,
      subscriptions: subsValue,
      collected: subsCollected,
      uncollected: Math.max(0, subsValue - subsCollected),
      cashReceiptsInPeriod: cashReceiptsIn,
      wages: wagesTotal,
      salaryPaid: salaryPaid,
      salaryRemaining: Math.max(0, wagesTotal - salaryPaid),
      extraRevenue: extraRevenue,
      operatingExpense: operatingExpense,
      totalCashIn: totalCashIn,
      totalCashOut: totalCashOut,
      netCashBalance: netCashBalance,
      netOperatingProfit: netOperatingProfit,
      txList: txList
    };
  }

  function normalizeDigitsToAscii(str) {
    return String(str || '').replace(/[٠-٩]/g, function (d) {
      return String(d.charCodeAt(0) - 0x0660);
    }).replace(/[۰-۹]/g, function (d) {
      return String(d.charCodeAt(0) - 0x06F0);
    });
  }

  function normalizeArabicForComparison(str) {
    var s = normalizeDigitsToAscii(str).trim();
    s = s.replace(/[\u064B-\u065F\u0670\u0640]/g, '');
    s = s.replace(/[أإآٱ]/g, 'ا').replace(/ى/g, 'ي').replace(/ة/g, 'ه').replace(/ؤ/g, 'و').replace(/ئ/g, 'ي');
    return s.replace(/\s+/g, ' ').trim().toLowerCase();
  }

  function normalizePhoneDigits(phone) {
    var s = normalizeDigitsToAscii(phone).trim();
    if (!s) return '';
    var digits = s.replace(/\D/g, '');
    if (digits.indexOf('00201') === 0 && digits.length === 14) {
      digits = '0' + digits.slice(4);
    } else if (digits.indexOf('201') === 0 && digits.length === 12) {
      digits = '0' + digits.slice(2);
    } else if (digits.charAt(0) === '1' && digits.length === 10 && '0125'.indexOf(digits.charAt(1)) !== -1) {
      digits = '0' + digits;
    }
    return digits || s;
  }

  function detectDuplicateStudents(students) {
    var byKey = Object.create(null);
    var list = Array.isArray(students) ? students : [];
    for (var i = 0; i < list.length; i++) {
      var s = list[i];
      if (!s || typeof s !== 'object') continue;
      var rawName = String(s.name || '').replace(/\s+/g, ' ').trim();
      if (!rawName) continue;
      var normName = normalizeArabicForComparison(rawName);
      var phone = normalizePhoneDigits(s.phone || '');
      var k = normName + '||' + phone;
      if (!byKey[k]) byKey[k] = [];
      byKey[k].push({
        index: i,
        id: s.id || '',
        code: s.code || '',
        name: rawName,
        phone: s.phone || '',
        teacher: s.teacher || ''
      });
    }
    var out = [];
    var keys = Object.keys(byKey);
    for (var j = 0; j < keys.length; j++) {
      var items = byKey[keys[j]];
      if (items.length > 1) {
        out.push({
          name: items[0].name,
          phone: items[0].phone,
          normalizedPhone: normalizePhoneDigits(items[0].phone),
          count: items.length,
          records: items
        });
      }
    }
    return out;
  }

  return {
    isValidCalendarDate: isValidCalendarDate,
    addDaysISO: addDaysISO,
    diffDaysISO: diffDaysISO,
    formatMonthLabel: formatMonthLabel,
    generateDynamicMonths: generateDynamicMonths,
    toScaledInt: toScaledInt,
    roundHalfUp: roundHalfUp,
    parsePctInt: parsePctInt,
    normalizePctScaled: normalizePctScaled,
    normalizePctFraction: normalizePctFraction,
    multiplyAndRoundHalfUp: multiplyAndRoundHalfUp,
    resolvePaymentDate: resolvePaymentDate,
    resolveActualStartDate: resolveActualStartDate,
    resolveEndDate: resolveEndDate,
    calculateStudyPeriod: calculateStudyPeriod,
    evaluateStudentMonthDates: evaluateStudentMonthDates,
    resolveWagePctInt: resolveWagePctInt,
    resolveWagePctFraction: resolveWagePctFraction,
    effectiveMonthSubscription: effectiveMonthSubscription,
    rawMonthDiscount: rawMonthDiscount,
    effectiveMonthDiscount: effectiveMonthDiscount,
    effectiveMonthNetSubscription: effectiveMonthNetSubscription,
    effectiveMonthCollected: effectiveMonthCollected,
    deriveMonthPaymentStatus: deriveMonthPaymentStatus,
    effectiveMonthWage: effectiveMonthWage,
    indexInstallments: indexInstallments,
    migrateLegacyPaymentsToReceipts: migrateLegacyPaymentsToReceipts,
    computeMonthAggregate: computeMonthAggregate,
    computeAccountingSummary: computeAccountingSummary,
    normalizeDigitsToAscii: normalizeDigitsToAscii,
    normalizeArabicForComparison: normalizeArabicForComparison,
    normalizePhoneDigits: normalizePhoneDigits,
    detectDuplicateStudents: detectDuplicateStudents
  };
});
