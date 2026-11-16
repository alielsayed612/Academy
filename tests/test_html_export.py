import unittest

from tests import _helpers  # noqa: F401
from tests._helpers import StoreTestCase
from alahd_erp.services.html_export_service import (
    generate_readonly_html,
    mask_phone,
    sanitize_export_filename,
)


class Helpers(unittest.TestCase):
    def test_mask_phone(self):
        self.assertEqual(mask_phone("01012345678"), "010•••••678")
        self.assertEqual(mask_phone("01012345678", hide=False), "01012345678")
        self.assertEqual(mask_phone(""), "—")
        self.assertEqual(mask_phone("1234"), "•••••")

    def test_filename_cannot_traverse_directories(self):
        out = sanitize_export_filename("../../etc/passwd")
        self.assertNotIn("/", out)
        self.assertNotIn(".", out)

    def test_filename_defaults_and_length(self):
        self.assertEqual(sanitize_export_filename(""), "YawmJadid_Report")
        self.assertLessEqual(len(sanitize_export_filename("a" * 300)), 80)
        self.assertEqual(sanitize_export_filename("تقرير: نوفمبر?"), "تقرير_نوفمبر")


class ReadonlyReport(StoreTestCase):
    def test_phone_hidden_by_default_and_visible_on_request(self):
        self.seed()
        st = self.store.export_state()
        hidden = generate_readonly_html(st, scope="full", month="2026-11")
        shown = generate_readonly_html(st, scope="full", month="2026-11", hide_personal_data=False)
        self.assertNotIn("01000000001", hidden)
        self.assertIn("01000000001", shown)

    def test_student_name_is_html_escaped(self):
        self.seed()
        self.store.upsert_student({
            "id": "s2", "name": "<script>alert(1)</script>", "phone": "", "teacher": "معلمة أ",
            "type": "8 حلق", "pct": 70,
            "months": {"2026-11": {"value": 100, "discount": 0}},
        })
        out = generate_readonly_html(self.store.export_state(), scope="full", month="2026-11")
        self.assertNotIn("<script>alert(1)</script>", out)
        self.assertIn("&lt;script&gt;", out)

    def test_unknown_scope_falls_back_instead_of_crashing(self):
        self.seed()
        out = generate_readonly_html(self.store.export_state(), scope="nonsense", month="2026-11")
        self.assertIn("<html", out.lower())


if __name__ == "__main__":
    unittest.main()
