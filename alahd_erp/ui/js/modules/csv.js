/* ============================================================================
   modules/csv.js — safe CSV cell quoting (Phase 1, finding F-05: CSV/formula injection)
   ============================================================================ */

// Phone-like values ("+20 100 123 4567", "-", "(02) 123") must stay untouched; anything else that starts with
// + or - and then continues like a formula is neutralised.
const PHONE_LIKE = /^[+-]?[\d\s().-]*$/;

export function csvCell(val) {
  let s = String(val === null || val === undefined ? '' : val);
  const first = s.charAt(0);
  const dangerous =
    first === '=' || first === '@' || first === '\t' || first === '\r' ||
    ((first === '+' || first === '-') && !PHONE_LIKE.test(s));
  if (dangerous) s = "'" + s;
  return '"' + s.replace(/"/g, '""') + '"';
}
