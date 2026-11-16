import datetime
import unittest

from tests import _helpers  # noqa: F401  (sets sys.path)
from alahd_erp.domain import finance_engine as fe


class RoundingAndPercent(unittest.TestCase):
    def test_round_half_up_not_bankers(self):
        self.assertEqual(fe.round_half_up(0.5), 1)
        self.assertEqual(fe.round_half_up(1.5), 2)
        self.assertEqual(fe.round_half_up(2.5), 3)   # banker's rounding would give 2
        self.assertEqual(fe.round_half_up("2.675"), 3)

    def test_no_float_drift(self):
        # 0.1 + 0.2 style drift must not leak into money
        self.assertEqual(fe.round_half_up(fe.Decimal("1000") * fe.normalize_pct_decimal(70)), 700)
        self.assertEqual(fe.round_half_up(fe.Decimal("333") * fe.normalize_pct_decimal(70)), 233)

    def test_pct_bounds(self):
        self.assertEqual(fe.parse_pct_int(70), 70)
        self.assertEqual(fe.parse_pct_int(1), 1)      # 1 means 1 %, not 100 %
        self.assertEqual(fe.parse_pct_int(100), 100)
        with self.assertRaises(ValueError):
            fe.parse_pct_int(101)
        with self.assertRaises(ValueError):
            fe.parse_pct_int(-1)
        with self.assertRaises(ValueError):
            fe.parse_pct_int(True)


class Collection(unittest.TestCase):
    month = {"value": 500, "discount": 100}

    def test_net_is_value_minus_discount(self):
        self.assertEqual(fe.effective_month_net_subscription(self.month), 400)

    def test_discount_never_exceeds_value(self):
        self.assertEqual(fe.effective_month_net_subscription({"value": 100, "discount": 900}), 0)

    def test_collected_capped_at_net(self):
        insts = [{"amount": 300, "paymentDate": "2026-11-01"}, {"amount": 300, "paymentDate": "2026-11-02"}]
        self.assertEqual(fe.effective_month_collected(self.month, insts, "2026-11-30"), 400)

    def test_future_receipt_not_collected(self):
        insts = [{"amount": 400, "paymentDate": "2026-12-20"}]
        self.assertEqual(fe.effective_month_collected(self.month, insts, "2026-11-30"), 0)
        self.assertEqual(fe.derive_month_payment_status(self.month, insts, "2026-11-30"), "unpaid")
        self.assertEqual(fe.derive_month_payment_status(self.month, insts, "2026-12-31"), "paid")

    def test_status_matrix(self):
        d = "2026-11-30"
        self.assertEqual(fe.derive_month_payment_status({"value": 0}, [], d), "unsubscribed")
        self.assertEqual(fe.derive_month_payment_status({"value": 500, "discount": 500}, [], d), "over_discounted")
        self.assertEqual(fe.derive_month_payment_status({"value": 500}, [{"amount": 100, "date": "2026-11-01"}], d), "partial")
        self.assertEqual(fe.derive_month_payment_status({"value": 500}, [{"amount": 500, "date": "2026-11-01"}], d), "paid")

    def test_payment_date_alone_with_empty_receipts_is_not_paid(self):
        m = {"value": 500, "paymentDate": "2026-11-01", "installments": []}
        self.assertEqual(fe.effective_month_collected(m, None, "2026-11-30"), 0)

    def test_actual_start_date_never_marks_paid(self):
        m = {"value": 500, "actualStartDate": "2026-11-01"}
        self.assertEqual(fe.effective_month_collected(m, None, "2026-11-30"), 0)


class Wages(unittest.TestCase):
    stu = {"id": "s", "teacher": "T", "pct": 70}

    def test_net_vs_gross_basis(self):
        m = {"value": 1000, "discount": 100}
        self.assertEqual(fe.effective_month_wage(self.stu, m, wage_basis="net"), 630)
        self.assertEqual(fe.effective_month_wage(self.stu, m, wage_basis="gross"), 700)

    def test_collected_mode_pays_only_on_collection(self):
        m = {"value": 1000}
        self.assertEqual(fe.effective_month_wage(self.stu, m, wage_accrual_mode="collected", collected_amount=0), 0)
        self.assertEqual(fe.effective_month_wage(self.stu, m, wage_accrual_mode="collected", collected_amount=500), 350)

    def test_custom_wage_overrides(self):
        self.assertEqual(fe.effective_month_wage(self.stu, {"value": 1000, "customWage": 123}), 123)

    def test_teacher_pct_policy(self):
        meta = {"T": {"pct": 50}}
        m = {"value": 1000}
        self.assertEqual(fe.effective_month_wage(self.stu, m, teacher_meta=meta, pct_source="teacher"), 500)
        self.assertEqual(fe.effective_month_wage(self.stu, m, teacher_meta=meta, pct_source="student"), 700)


class Aggregates(unittest.TestCase):
    def students(self):
        return [
            {"id": "a", "teacher": "T1", "pct": 70, "months": {"2026-11": {"value": 500, "discount": 100}}},
            {"id": "b", "teacher": "T1", "pct": 70, "months": {"2026-11": {"value": 300}}},
        ]

    def test_month_stats_totals(self):
        insts = [{"id": "i", "studentId": "a", "month": "2026-11", "amount": 400, "paymentDate": "2026-11-05"}]
        st = fe.compute_month_stats(self.students(), "2026-11", installments=insts, reference_date="2026-11-30")
        t = st["total"]
        self.assertEqual((t["grossSubs"], t["discounts"], t["subs"]), (800, 100, 700))
        self.assertEqual((t["collected"], t["uncollected"]), (400, 300))
        self.assertEqual(t["wages"], 280 + 210)

    def test_accounting_cash_vs_accrual(self):
        insts = [{"id": "i", "studentId": "a", "month": "2026-10", "amount": 100, "paymentDate": "2026-11-05"}]
        acc = fe.compute_accounting_summary(self.students(), installments=insts, month_key="2026-11", reference_date="2026-11-30")
        # receipt is for October but paid in November: cash basis sees it in November, accrual does not
        self.assertEqual(acc["cashReceiptsInPeriod"], 100)
        self.assertEqual(acc["collected"], 0)


if __name__ == "__main__":
    unittest.main()
