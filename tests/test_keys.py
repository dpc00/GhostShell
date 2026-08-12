"""Unit tests for ai/terminal/keys.py (key name -> byte sequences).

Run from repo root:
    python -m unittest tests.test_keys -v
"""
import unittest

from ai.terminal.keys import (
    ENHANCED_KEY,
    LEFT_ALT_PRESSED,
    LEFT_CTRL_PRESSED,
    SHIFT_PRESSED,
    encode_win32_key,
    get_alt_key_code,
    get_ctrl_key_code,
    get_key_code,
    get_shift_key_code,
    translate_key,
)


class GetKeyCodeTests(unittest.TestCase):
    def test_named_keys(self):
        self.assertEqual(get_key_code("home"), "\x1b[1~")
        self.assertEqual(get_key_code("pagedown"), "\x1b[6~")
        self.assertEqual(get_key_code("f5"), "\x1b[15~")

    def test_application_mode_only_covers_arrows(self):
        self.assertEqual(get_key_code("left", application_mode=True), "\x1bOD")
        # No app-mode variant: falls through to the normal map.
        self.assertEqual(get_key_code("home", application_mode=True), "\x1b[1~")

    def test_unknown_key_passes_through(self):
        self.assertEqual(get_key_code("\u00e9"), "\u00e9")


class CtrlKeyTests(unittest.TestCase):
    def test_letters_become_control_chars(self):
        self.assertEqual(get_ctrl_key_code("a"), "\x01")
        self.assertEqual(get_ctrl_key_code("Z"), "\x1a")

    def test_punctuation_table(self):
        self.assertEqual(get_ctrl_key_code("@"), "\x00")
        self.assertEqual(get_ctrl_key_code("["), "\x1b")
        self.assertEqual(get_ctrl_key_code("\\"), "\x1c")
        self.assertEqual(get_ctrl_key_code("?"), "\x7f")

    def test_navigation_keys_use_modifier_params(self):
        self.assertEqual(get_ctrl_key_code("up"), "\x1b[1;5A")
        self.assertEqual(get_ctrl_key_code("delete"), "\x1b[3;5~")

    def test_unmapped_falls_back_to_plain_code(self):
        self.assertEqual(get_ctrl_key_code("f5"), "\x1b[15~")
        self.assertEqual(get_ctrl_key_code("1"), "1")


class AltKeyTests(unittest.TestCase):
    def test_arrows_use_modifier_params(self):
        self.assertEqual(get_alt_key_code("Right"), "\x1b[1;3C")

    def test_other_keys_get_esc_prefix(self):
        self.assertEqual(get_alt_key_code("b"), "\x1bb")
        self.assertEqual(get_alt_key_code("enter"), "\x1b\r")


class ShiftKeyTests(unittest.TestCase):
    def test_shift_table(self):
        self.assertEqual(get_shift_key_code("tab"), "\x1b[Z")
        self.assertEqual(get_shift_key_code("Up"), "\x1b[1;2A")

    def test_named_key_without_shift_variant(self):
        self.assertEqual(get_shift_key_code("f5"), "\x1b[15~")

    def test_printable_uppercases(self):
        self.assertEqual(get_shift_key_code("a"), "A")


class TranslateKeyTests(unittest.TestCase):
    def test_modifier_precedence_is_ctrl_alt_shift(self):
        self.assertEqual(translate_key("a", ctrl=True, alt=True, shift=True), "\x01")
        self.assertEqual(translate_key("a", alt=True, shift=True), "\x1ba")
        self.assertEqual(translate_key("a", shift=True), "A")

    def test_plain_key_honours_application_mode(self):
        self.assertEqual(translate_key("up", application_mode=True), "\x1bOA")


class Win32InputModeTests(unittest.TestCase):
    """CSI Vk;Sc;Uc;Kd;Cs;Rc _ — DEC private mode 9001 (ConPTY spec)."""

    def _fields(self, seq):
        self.assertTrue(seq.startswith("\x1b["))
        self.assertTrue(seq.endswith("_"))
        return [int(part) for part in seq[2:-1].split(";")]

    def test_named_key_carries_vk_sc_and_unicode(self):
        vk, sc, uc, kd, cs, rc = self._fields(encode_win32_key("enter"))
        self.assertEqual((vk, sc, uc), (0x0D, 28, 13))
        self.assertEqual((kd, cs, rc), (1, 0, 1))

    def test_navigation_keys_set_enhanced_bit(self):
        _vk, _sc, _uc, _kd, cs, _rc = self._fields(encode_win32_key("left"))
        self.assertEqual(cs, ENHANCED_KEY)

    def test_modifier_bits(self):
        _vk, _sc, _uc, _kd, cs, _rc = self._fields(
            encode_win32_key("f1", ctrl=True, alt=True, shift=True)
        )
        self.assertEqual(cs, LEFT_CTRL_PRESSED | LEFT_ALT_PRESSED | SHIFT_PRESSED)

    def test_ctrl_backspace_sends_del(self):
        _vk, _sc, uc, _kd, cs, _rc = self._fields(
            encode_win32_key("backspace", ctrl=True)
        )
        self.assertEqual(uc, 127)
        self.assertEqual(cs, LEFT_CTRL_PRESSED)

    def test_letter_uses_uppercase_vk_and_layout_scan_code(self):
        vk, sc, uc, _kd, cs, _rc = self._fields(encode_win32_key("a"))
        self.assertEqual((vk, sc, uc), (ord("A"), 30, ord("a")))
        self.assertEqual(cs, 0)

    def test_letter_shift_uppercases_unicode(self):
        _vk, _sc, uc, _kd, _cs, _rc = self._fields(encode_win32_key("a", shift=True))
        self.assertEqual(uc, ord("A"))

    def test_letter_ctrl_sends_control_char(self):
        _vk, _sc, uc, _kd, _cs, _rc = self._fields(encode_win32_key("C", ctrl=True))
        self.assertEqual(uc, 3)

    def test_digit(self):
        vk, sc, uc, _kd, _cs, _rc = self._fields(encode_win32_key("7"))
        self.assertEqual((vk, sc, uc), (ord("7"), 8, ord("7")))

    def test_us_layout_punctuation(self):
        vk, sc, uc, _kd, _cs, _rc = self._fields(encode_win32_key("/"))
        self.assertEqual((vk, sc, uc), (0xBF, 53, ord("/")))

    def test_unknown_punctuation_keeps_unicode_only(self):
        vk, sc, uc, _kd, _cs, _rc = self._fields(encode_win32_key("\u20ac"))
        self.assertEqual((vk, sc), (0, 0))
        self.assertEqual(uc, ord("\u20ac"))

    def test_unencodable_key_name_is_empty(self):
        self.assertEqual(encode_win32_key("mouse1"), "")


if __name__ == "__main__":
    unittest.main()
