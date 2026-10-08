import unittest

import susi_gen
from understand import parse_screen_control_selected


def _spec(selected, brightness=False, backlight=False):
    return {
        "features": {"brightness": brightness, "backlight": backlight},
        "screen_control": {
            "selected": selected,
            "brightness": {"enabled": brightness, "items": []},
            "backlight": {"enabled": backlight, "items": []},
        },
    }


def _gate(spec):
    return {
        sec: susi_gen.evaluate_section_applicability(sec, spec, None)
        for sec in ("VGA.Brightness", "VGA.Backlight")
    }


class ScreenControlRequestTests(unittest.TestCase):
    def test_nothing_ticked_means_neither_is_queried(self):
        out = _gate(_spec(False))
        for sec in out:
            self.assertFalse(out[sec]["applicable"])
            self.assertEqual(out[sec]["reason_code"], "REQUEST_NOT_SELECTED")
            self.assertNotIn("warning", out[sec])

    def test_parent_only_means_both_wanted(self):
        out = _gate(_spec(True))
        self.assertTrue(out["VGA.Brightness"]["applicable"])
        self.assertTrue(out["VGA.Backlight"]["applicable"])

    def test_parent_with_one_child_means_only_that_child(self):
        out = _gate(_spec(True, brightness=True))
        self.assertTrue(out["VGA.Brightness"]["applicable"])
        self.assertFalse(out["VGA.Backlight"]["applicable"])

    def test_both_children_and_parent(self):
        out = _gate(_spec(True, brightness=True, backlight=True))
        self.assertTrue(out["VGA.Brightness"]["applicable"])
        self.assertTrue(out["VGA.Backlight"]["applicable"])

    def test_child_without_parent_is_wanted_with_warning(self):
        out = _gate(_spec(False, backlight=True))
        self.assertFalse(out["VGA.Brightness"]["applicable"])
        self.assertTrue(out["VGA.Backlight"]["applicable"])
        self.assertEqual(out["VGA.Backlight"]["warning"], "SCREEN_CONTROL_FORM_INCONSISTENT")

    def test_unknown_parent_state_keeps_legacy_behaviour(self):
        spec = {"features": {}, "screen_control": {"brightness": {"enabled": False}}}
        out = _gate(spec)
        self.assertTrue(out["VGA.Brightness"]["applicable"])


class ParentCheckboxParsingTests(unittest.TestCase):
    def _form(self, text):
        return {"pages": [{"page": 1, "text": text}]}

    def test_checked_and_unchecked(self):
        self.assertIs(parse_screen_control_selected(self._form("x\n■ Screen control\n□ eDP Brightness")), True)
        self.assertIs(parse_screen_control_selected(self._form("x\n□ Screen control\n□ LVDS Brightness")), False)

    def test_missing_line_is_unknown(self):
        self.assertIsNone(parse_screen_control_selected(self._form("no such item")))


if __name__ == "__main__":
    unittest.main()
