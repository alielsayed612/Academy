"""Regression: the server tells the UI whether a PIN is set via `pinRequired`; store.js must map it to state.pinEnabled,
otherwise the PIN lock screen never appears after a restart (found by the browser scenario run, session 13)."""
from __future__ import annotations

import pathlib
import unittest

from tests._helpers import ROOT, StoreTestCase


class PinStateContract(StoreTestCase):
    def test_export_state_reports_pin(self) -> None:
        self.assertFalse(self.store.export_state()["pinRequired"])
        self.store.set_pin("2468")
        st = self.store.export_state()
        self.assertTrue(st["pinRequired"])
        self.assertTrue(st["settings"]["pinEnabled"])
        self.store.clear_pin()
        self.assertFalse(self.store.export_state()["pinRequired"])

    def test_frontend_maps_pin_required(self) -> None:
        js = (pathlib.Path(ROOT) / "alahd_erp" / "ui" / "js" / "modules" / "store.js").read_text(encoding="utf-8")
        self.assertIn("rawData.pinRequired", js)


if __name__ == "__main__":
    unittest.main()
