"""Unit tests for the DLL-free parts of ghostty_engine / ghostty_vt.

Everything that needs libghostty-vt (GhosttyParser's FFI paths) is out of
scope here: the DLL is a built binary artifact that isn't in the repo. The
pure translation helpers around it are testable, so they are tested — cell
text/colour/style translation is where the wrong-colour bugs live.

Run from repo root:
    python -m unittest tests.test_ghostty_engine -v
"""
import ctypes
import unittest

from terminal import ghostty_vt as gvt
from terminal.colors import (
    ATTR_BG_MASK,
    ATTR_FG_MASK,
    BG_SHIFT,
    BOLD,
    FAINT,
    ITALIC,
    REVERSE,
    UNDERLINE,
    quantize256,
)
from terminal.ghostty_engine import (
    GhosttyParser,
    _AltScreenFilter,
    _color_id,
    _strip_alt_screen,
)


def _detached_parser():
    """A GhosttyParser that never loaded the DLL — pure helpers only."""
    return GhosttyParser.__new__(GhosttyParser)


class SynchronizedScrollbackTests(unittest.TestCase):
    class _Native:
        def terminal_vt_write(self, _term, _data, _length):
            pass

    def _parser(self):
        parser = _detached_parser()
        parser.force_main_screen = False
        parser._sync_open = False
        parser._replace_scroll = False
        parser._replace_origin = None
        parser._g = self._Native()
        parser._term = None
        parser.s = type("ScreenStub", (), {"sync_output": False})()
        return parser

    def test_open_frame_defers_all_python_synchronization(self):
        parser = self._parser()
        parser._sync = lambda: self.fail("open synchronized frame must not sync")
        parser.feed("\x1b[?2026hpartial frame")
        self.assertTrue(parser.s.sync_output)

    def test_broker_bootstrap_advances_native_terminal_without_sync(self):
        parser = self._parser()
        writes = []
        parser._g.terminal_vt_write = (
            lambda _term, data, length: writes.append(bytes(data[:length]))
        )
        parser._sync = lambda: self.fail("broker bootstrap must not sync")

        parser.feed_bootstrap("restored replay")

        self.assertEqual(writes, [b"restored replay"])

    def test_closing_frame_synchronizes_once(self):
        parser = self._parser()
        parser._sync_open = True
        calls = []
        parser._sync = lambda: calls.append("sync")
        parser.feed("rest of frame\x1b[?2026l")
        self.assertEqual(calls, ["sync"])

    def test_main_screen_home_replay_clears_stale_active_grid(self):
        parser = self._parser()
        parser.force_main_screen = True
        parser._alt_screen_filter = type(
            "PassThrough", (), {"feed": staticmethod(lambda text: text)}
        )()
        writes = []
        parser._g.terminal_vt_write = (
            lambda _term, data, length: writes.append(bytes(data[:length]))
        )
        parser._sync = lambda: None

        parser.feed("\x1b[?2026h\x1b[Hshort repaint\x1b[?2026l")

        self.assertEqual(
            writes,
            [b"\x1b[?2026h\x1b[H\x1b[2Jshort repaint\x1b[?2026l"],
        )

    def test_alt_screen_home_replay_is_not_modified(self):
        parser = self._parser()
        writes = []
        parser._g.terminal_vt_write = (
            lambda _term, data, length: writes.append(bytes(data[:length]))
        )
        parser._sync = lambda: None

        parser.feed("\x1b[?2026h\x1b[Hpartial TUI repaint\x1b[?2026l")

        self.assertEqual(
            writes,
            [b"\x1b[?2026h\x1b[Hpartial TUI repaint\x1b[?2026l"],
        )

    def test_open_home_dump_defers_native_scrollback_walk(self):
        parser = _detached_parser()
        parser._sync_open = True
        parser._replace_scroll = True

        def unexpected_native_read(_kind):
            self.fail("open synchronized replay must not read scrollback")

        parser._get_size = unexpected_native_read
        parser._sync_scrollback()

    def test_resize_forces_full_scrollback_rebuild_on_next_sync(self):
        # Screen.resize() only clips/pads cells -- it does not reflow (no
        # per-row "was this a wrapped continuation" bookkeeping to reflow
        # from). Forcing _last_scrollback_rows back to -1 is what makes
        # the *next* _sync_scrollback() fall through to a full rebuild
        # from the native terminal (which does reflow) instead of an
        # incremental append or a shortcut that trusted Screen.resize()'s
        # own shallow adjustment. Deliberately reverted 2026-09-02 -- see
        # GhosttyParser.resize()'s own comment for the live-verified bug
        # the removed shortcut used to leave in place.
        from terminal.screen import Screen

        parser = _detached_parser()
        parser._g = type(
            "Native", (), {"terminal_resize": staticmethod(lambda *a: gvt.SUCCESS)}
        )()
        parser._term = None
        parser.s = Screen(80, 24)
        parser._last_scrollback_rows = 12345

        parser.resize(100, 30)

        self.assertEqual(parser._last_scrollback_rows, -1)


def _style(**kwargs):
    style = gvt.GhosttyStyle().init()
    for name, value in kwargs.items():
        setattr(style, name, value)
    return style


def _style_color(tag, palette=0, rgb=(0, 0, 0)):
    color = gvt.GhosttyStyleColor()
    color.tag = tag
    if tag == gvt.STYLE_COLOR_PALETTE:
        color.value.palette = palette
    elif tag == gvt.STYLE_COLOR_RGB:
        color.value.rgb = gvt.GhosttyColorRgb(*rgb)
    return color


class StripAltScreenTests(unittest.TestCase):
    def test_alt_screen_enter_and_exit_are_removed(self):
        self.assertEqual(
            _strip_alt_screen("a\x1b[?1049hb\x1b[?1049lc"), "abc"
        )
        self.assertEqual(_strip_alt_screen("\x1b[?47h\x1b[?1047l"), "")

    def test_other_private_modes_survive(self):
        text = "\x1b[?1000h\x1b[?2004h"
        self.assertEqual(_strip_alt_screen(text), text)

    def test_alt_screen_combined_with_another_mode_is_still_stripped(self):
        # A prior regex only matched an isolated "?1049h" and silently let
        # combined-parameter forms through -- confirmed live 2026-08-25.
        self.assertEqual(_strip_alt_screen("\x1b[?1049;2004h"), "\x1b[?2004h")
        self.assertEqual(_strip_alt_screen("\x1b[?2004;1049h"), "\x1b[?2004h")
        self.assertEqual(_strip_alt_screen("\x1b[?1;47h"), "\x1b[?1h")

    def test_text_without_private_modes_is_returned_unchanged(self):
        text = "plain \x1b[31mred\x1b[0m"
        self.assertIs(_strip_alt_screen(text), text)


class AltScreenStreamFilterTests(unittest.TestCase):
    def _assert_every_split(self, text, expected):
        for split in range(len(text) + 1):
            stream_filter = _AltScreenFilter()
            actual = stream_filter.feed(text[:split])
            actual += stream_filter.feed(text[split:])
            actual += stream_filter.flush()
            self.assertEqual(actual, expected, "split at byte %d" % split)

    def test_enter_and_exit_work_at_every_read_boundary(self):
        self._assert_every_split("a\x1b[?1049hb\x1b[?1049lc", "abc")
        self._assert_every_split("a\x1b[?47hb\x1b[?1047lc", "abc")

    def test_combined_modes_work_at_every_read_boundary(self):
        self._assert_every_split(
            "a\x1b[?1049;2004hb\x1b[?2004;1049lc",
            "a\x1b[?2004hb\x1b[?2004lc",
        )

    def test_one_character_reads_preserve_text_and_unrelated_csi(self):
        text = "plain \x1b[31mred\x1b[0m \x1b[?1049hinside\x1b[?1049l done"
        stream_filter = _AltScreenFilter()
        actual = "".join(stream_filter.feed(ch) for ch in text)
        actual += stream_filter.flush()
        self.assertEqual(actual, "plain \x1b[31mred\x1b[0m inside done")

    def test_malformed_parameter_run_is_bounded_and_flushed_unchanged(self):
        stream_filter = _AltScreenFilter(pending_max=8)
        malformed = "\x1b[?123456789"
        self.assertEqual(stream_filter.feed(malformed), malformed)
        self.assertEqual(stream_filter.pending, "")

    def test_reset_discards_an_incomplete_sequence(self):
        stream_filter = _AltScreenFilter()
        self.assertEqual(stream_filter.feed("before\x1b[?104"), "before")
        self.assertEqual(stream_filter.pending, "\x1b[?104")
        stream_filter.reset()
        self.assertEqual(stream_filter.feed("9hafter"), "9hafter")
        self.assertEqual(stream_filter.pending, "")


class ColorIdTests(unittest.TestCase):
    def test_failed_lookup_is_default(self):
        self.assertEqual(_color_id(gvt.INVALID_VALUE, gvt.GhosttyColorRgb(255, 0, 0)), 0)

    def test_rgb_is_quantized_to_a_one_based_palette_id(self):
        self.assertEqual(
            _color_id(gvt.SUCCESS, gvt.GhosttyColorRgb(255, 0, 0)),
            quantize256(255, 0, 0) + 1,
        )


class StyleFlagTests(unittest.TestCase):
    def setUp(self):
        self.parser = _detached_parser()

    def test_no_styles(self):
        self.assertEqual(self.parser._style_flags(_style()), 0)

    def test_each_rendered_style_maps_to_its_bit(self):
        self.assertEqual(self.parser._style_flags(_style(bold=True)), BOLD)
        self.assertEqual(self.parser._style_flags(_style(inverse=True)), REVERSE)
        self.assertEqual(self.parser._style_flags(_style(faint=True)), FAINT)
        self.assertEqual(self.parser._style_flags(_style(italic=True)), ITALIC)

    def test_underline_is_an_enum_not_a_bool(self):
        self.assertEqual(self.parser._style_flags(_style(underline=0)), 0)
        for value in (1, 2, 3):  # single / double / curly all mean "underlined"
            self.assertEqual(self.parser._style_flags(_style(underline=value)), UNDERLINE)

    def test_flags_combine(self):
        flags = self.parser._style_flags(_style(bold=True, inverse=True, italic=True))
        self.assertEqual(flags, BOLD | REVERSE | ITALIC)

    def test_unrendered_styles_are_ignored(self):
        self.assertEqual(
            self.parser._style_flags(_style(strikethrough=True, overline=True, blink=True)),
            0,
        )


class FinishCellTests(unittest.TestCase):
    def setUp(self):
        self.parser = _detached_parser()

    def test_empty_text_becomes_a_blank_cell(self):
        text, attr = self.parser._finish_cell("", 5, 0, 0)
        self.assertEqual(text, " ")
        self.assertEqual(attr, 0)  # fg dropped so rstrip can trim the cell

    def test_blank_keeps_its_background(self):
        _text, attr = self.parser._finish_cell(" ", 5, 3, 0)
        self.assertEqual(attr & ATTR_FG_MASK, 5)
        self.assertEqual((attr & ATTR_BG_MASK) >> BG_SHIFT, 3)

    def test_reversed_blank_keeps_its_foreground(self):
        _text, attr = self.parser._finish_cell(" ", 5, 0, REVERSE)
        self.assertEqual(attr & ATTR_FG_MASK, 5)

    def test_real_glyph_keeps_its_foreground(self):
        text, attr = self.parser._finish_cell("x", 5, 0, BOLD)
        self.assertEqual(text, "x")
        self.assertEqual(attr & ATTR_FG_MASK, 5)
        self.assertTrue(attr & BOLD)


class ResolveStyleColorTests(unittest.TestCase):
    def setUp(self):
        self.parser = _detached_parser()
        self.palette = (gvt.GhosttyColorRgb * 256)(
            *[gvt.GhosttyColorRgb(0, 0, 0) for _ in range(256)]
        )
        self.palette[9] = gvt.GhosttyColorRgb(255, 0, 0)

    def test_none_is_default(self):
        color = _style_color(gvt.STYLE_COLOR_NONE)
        self.assertEqual(self.parser._resolve_style_color(color, self.palette), 0)

    def test_palette_entry_is_resolved_through_the_palette(self):
        color = _style_color(gvt.STYLE_COLOR_PALETTE, palette=9)
        self.assertEqual(
            self.parser._resolve_style_color(color, self.palette),
            quantize256(255, 0, 0) + 1,
        )

    def test_rgb_is_quantized(self):
        color = _style_color(gvt.STYLE_COLOR_RGB, rgb=(0, 0, 255))
        self.assertEqual(
            self.parser._resolve_style_color(color, self.palette),
            quantize256(0, 0, 255) + 1,
        )


class GhosttyVtBindingTests(unittest.TestCase):
    def test_point_packs_tag_and_coordinates(self):
        pt = gvt.point(gvt.POINT_TAG_SCREEN, 4, 9)
        self.assertEqual(pt.tag, gvt.POINT_TAG_SCREEN)
        self.assertEqual((pt.value.coordinate.x, pt.value.coordinate.y), (4, 9))

    def test_struct_init_records_its_own_size(self):
        self.assertEqual(gvt.GhosttyStyle().init().size, ctypes.sizeof(gvt.GhosttyStyle))
        self.assertEqual(
            gvt.GhosttyGridRef().init().size, ctypes.sizeof(gvt.GhosttyGridRef)
        )

    def test_modifier_bitmask(self):
        self.assertEqual(gvt.st_mods_to_ghostty(), 0)
        self.assertEqual(
            gvt.st_mods_to_ghostty(ctrl=True, alt=True, shift=True),
            gvt.MODS_CTRL | gvt.MODS_ALT | gvt.MODS_SHIFT,
        )
        self.assertEqual(gvt.st_mods_to_ghostty(shift=True), gvt.MODS_SHIFT)

    def test_key_name_map_covers_named_and_printable_keys(self):
        self.assertEqual(gvt.ST_KEY_TO_GHOSTTY["a"], gvt.KEY_A)
        self.assertEqual(gvt.ST_KEY_TO_GHOSTTY["keypad_enter"], gvt.KEY_NUMPAD_ENTER)
        self.assertNotIn("mouse1", gvt.ST_KEY_TO_GHOSTTY)

    def test_missing_library_raises(self):
        with self.assertRaises(OSError):
            gvt.load_library("/nonexistent/ghostty-vt.dll")


def _dll_available():
    try:
        gvt.load_library()
        return True
    except OSError:
        return False


@unittest.skipUnless(_dll_available(), "ghostty-vt.dll not present")
class BuildInfoTests(unittest.TestCase):
    def test_loaded_library_reports_recorded_version(self):
        self.assertEqual(gvt.libghostty_version(gvt.load_library()), "0.1.0-dev")


# The gitignored DLL (terminal/bin/ghostty-vt.dll, built from the ~/tools
# ghostty checkout) isn't in the repo, so these tests skip cleanly wherever
# it's absent rather than failing the suite.
@unittest.skipUnless(_dll_available(), "ghostty-vt.dll not present")
class WritePtyCallbackTests(unittest.TestCase):
    """Covers the write_pty/size wiring that fixes a real hang: a startup
    capability probe (DA/kitty-flags/XTVERSION/size query) that
    libghostty-vt parses internally but, without these callbacks, has
    nowhere to send a response -- so the child blocks forever waiting for
    an answer that never comes. See ghostty_engine.py's GhosttyParser
    docstrings on bind_write_pty/_on_size_query."""

    def setUp(self):
        from terminal.screen import Screen
        self.responses = []
        self.parser = GhosttyParser(Screen(80, 24), force_main_screen=False)
        self.parser.bind_write_pty(self.responses.append)

    def tearDown(self):
        self.parser._g.terminal_free(self.parser._term)

    def test_unbound_sink_is_a_noop_not_a_crash(self):
        from terminal.screen import Screen
        parser = GhosttyParser(Screen(80, 24), force_main_screen=False)
        try:
            parser.feed("\x1b[c")  # DA1 query, sink never bound
        finally:
            parser._g.terminal_free(parser._term)
        # No exception is the assertion; nothing else to check.

    def test_kitty_flags_query_gets_a_response(self):
        # This is the exact query Grok's client uses to detect Kitty
        # keyboard protocol support (grok doctor: "keyboard protocol is
        # unavailable" when it goes unanswered).
        self.parser.feed("\x1b[?u")
        self.assertEqual(self.responses, [b"\x1b[?0u"])

    def test_device_attributes_query_gets_a_response(self):
        self.parser.feed("\x1b[c")
        self.assertEqual(len(self.responses), 1)
        self.assertTrue(self.responses[0].startswith(b"\x1b[?"))

    def test_size_query_reports_real_screen_dimensions(self):
        self.parser.feed("\x1b[18t")
        self.assertEqual(self.responses, [b"\x1b[8;24;80t"])

    def test_size_query_after_resize_reports_new_dimensions(self):
        self.parser.resize(100, 40)
        self.parser.feed("\x1b[18t")
        self.assertEqual(self.responses, [b"\x1b[8;40;100t"])


@unittest.skipUnless(_dll_available(), "ghostty-vt.dll not present")
class SyncOutputModeTests(unittest.TestCase):
    """screen.sync_output (DEC mode 2026) is queried fresh from the native
    terminal after every feed (ghostty_terminal_mode_get), replacing what
    used to be a separate Python regex tracker in ai_terminal.py. Native
    modes are inherently a boolean level, not a stack, so a repeated "h"
    while already open (confirmed live: Grok sends exactly this, h twice
    per l) can never become a nested/counted open the way a naive counter
    could."""

    def setUp(self):
        from terminal.screen import Screen
        self.screen = Screen(80, 24)
        self.parser = GhosttyParser(self.screen, force_main_screen=False)

    def tearDown(self):
        self.parser._g.terminal_free(self.parser._term)

    def test_closed_by_default(self):
        self.assertFalse(self.screen.sync_output)

    def test_opens_on_h_and_closes_on_l(self):
        self.parser.feed("\x1b[?2026h")
        self.assertTrue(self.screen.sync_output)
        self.parser.feed("some content")
        self.assertTrue(self.screen.sync_output, "stays open until the matching l")
        self.parser.feed("\x1b[?2026l")
        self.assertFalse(self.screen.sync_output)

    def test_repeated_h_is_a_level_not_a_stack(self):
        self.parser.feed("\x1b[?2026h\x1b[?2026h")
        self.assertTrue(self.screen.sync_output)
        # A single closing l must fully close it -- if this were a stack
        # (incrementing on each h), it would incorrectly still read open
        # after only one l.
        self.parser.feed("\x1b[?2026l")
        self.assertFalse(self.screen.sync_output)


@unittest.skipUnless(_dll_available(), "ghostty-vt.dll not present")
class ParserCloseTests(unittest.TestCase):
    """GhosttyParser.close() frees the terminal, render state, and (if
    ever created) the key encoder/event -- previously nothing did, so a
    closed tab leaked all of it until Sublime restarted. No tearDown here:
    each test is responsible for its own single close() call, since a
    second real free of an already-freed native handle (not exercised by
    these tests, which test the idempotency guard, not double-freeing
    past it) would be the actual bug this class exists to catch."""

    def test_close_is_safe_with_no_keys_ever_encoded(self):
        from terminal.screen import Screen
        parser = GhosttyParser(Screen(80, 24), force_main_screen=False)
        parser.close()  # no exception is the assertion

    def test_close_frees_the_lazily_created_key_encoder_too(self):
        from terminal.screen import Screen
        parser = GhosttyParser(Screen(80, 24), force_main_screen=False)
        # Allocates _key_encoder/_key_event on first use -- see encode_key.
        parser.encode_key("a")
        self.assertTrue(hasattr(parser, "_key_encoder"))
        parser.close()  # no exception is the assertion

    def test_close_is_idempotent(self):
        from terminal.screen import Screen
        parser = GhosttyParser(Screen(80, 24), force_main_screen=False)
        parser.close()
        parser.close()  # must not double-free; no exception is the assertion


def _cells(text):
    return [(ch, 0) for ch in text]


def _row_texts(rows):
    return ["".join(ch for ch, _ in row).rstrip() for row in rows]


def _history_lines(screen):
    return ["".join(ch for ch, _ in row).rstrip() for row in screen.history]


def _visible_text(screen):
    hist = "\n".join(_history_lines(screen))
    live = "\n".join("".join(screen.grid[r]).rstrip() for r in range(screen.rows))
    return hist + "\n" + live


@unittest.skipUnless(_dll_available(), "ghostty-vt.dll not present")
class HomeReplaceScrollTests(unittest.TestCase):
    """Codex (and similar) home+dump a full transcript inside CSI ?2026.

    Overflow must replace Screen.history, not append another copy. A
    production change that went back to always-append would make LINE-00
    appear twice after the second paint.
    """

    def setUp(self):
        from terminal.screen import Screen

        self.screen = Screen(20, 4, history_cap=200)
        self.parser = GhosttyParser(self.screen, force_main_screen=True)

    def tearDown(self):
        self.parser._g.terminal_free(self.parser._term)

    def test_2026_home_dump_does_not_duplicate_transcript_in_history(self):
        first = "\n".join("LINE-%02d" % i for i in range(12)) + "\n"
        self.parser.feed(first)
        replay = "\x1b[?2026h\x1b[H" + first + "LINE-99\n" + "\x1b[?2026l"
        self.parser.feed(replay)
        self.assertEqual(_visible_text(self.screen).count("LINE-00"), 1)

    def test_split_2026_home_dump_does_not_duplicate(self):
        first = "\n".join("LINE-%02d" % i for i in range(12)) + "\n"
        self.parser.feed(first)
        self.parser.feed("\x1b[?2026h\x1b[H" + first[:40])
        self.parser.feed(first[40:] + "LINE-99\n\x1b[?2026l")
        self.assertEqual(_visible_text(self.screen).count("LINE-00"), 1)

    def test_fifty_testing_agent_dumps_keep_one_copy(self):
        from terminal.screen import Screen
        from tests.mock_agent_cli import encode_replay_frame, make_turn_lines

        self.parser._g.terminal_free(self.parser._term)
        self.screen = Screen(80, 24, history_cap=2000)
        self.parser = GhosttyParser(self.screen, force_main_screen=True)
        lines = []
        for n in range(50):
            lines.extend(make_turn_lines(n, body_lines=1))
            self.parser.feed(encode_replay_frame(lines))
        vis = _visible_text(self.screen)
        self.assertEqual(vis.count("TURN-00"), 2)
        self.assertEqual(vis.count("TURN-49"), 2)

    def test_plain_scroll_without_home_still_appends(self):
        self.parser.feed("AAA\nBBB\nCCC\nDDD\nEEE\n")
        n1 = len(self.screen.history)
        self.parser.feed("FFF\nGGG\n")
        self.assertGreater(len(self.screen.history), n1)
        joined = "\n".join(_history_lines(self.screen))
        self.assertIn("AAA", joined)
        self.assertIn("EEE", joined)

    def test_growing_replay_dumps_keep_earliest_turn_once(self):
        from terminal.screen import Screen
        from tests.mock_agent_cli import encode_replay_frame, make_turn_lines

        self.parser._g.terminal_free(self.parser._term)
        self.screen = Screen(80, 24, history_cap=2000)
        self.parser = GhosttyParser(self.screen, force_main_screen=True)
        lines = []
        for n in range(3):
            lines.extend(make_turn_lines(n))
        self.parser.feed(encode_replay_frame(lines))
        for n in range(3, 5):
            lines.extend(make_turn_lines(n))
            self.parser.feed(encode_replay_frame(lines))
        present = set(_history_lines(self.screen)) | {
            "".join(self.screen.grid[r]).rstrip() for r in range(self.screen.rows)
        }
        missing = [line for line in lines if line not in present]
        self.assertEqual(missing, [], "earliest-turn lines dropped from history+grid")
        vis = _visible_text(self.screen)
        self.assertEqual(vis.count("› user prompt TURN-00"), 1)
        self.assertEqual(vis.count("TURN-00"), 29)

    def test_home_dump_preserves_unrelated_prior_scrollback(self):
        from terminal.screen import Screen

        self.parser._g.terminal_free(self.parser._term)
        self.screen = Screen(80, 4, history_cap=200)
        self.parser = GhosttyParser(self.screen, force_main_screen=True)
        prior = "\n".join("UNIQUE-%02d" % i for i in range(8)) + "\n"
        self.parser.feed(prior)
        first = "\n".join("LINE-%02d" % i for i in range(12)) + "\n"
        self.parser.feed("\x1b[?2026h\x1b[H" + first + "\x1b[?2026l")
        vis = _visible_text(self.screen)
        self.assertIn("UNIQUE-00", vis)
        self.assertEqual(vis.count("LINE-00"), 1)

    def test_resize_then_home_dump_reflows_old_scrollback(self):
        # The actual live-verified bug (2026-09-02): resizing alone changes
        # nothing about already-retired scrollback -- old rows keep
        # whatever wrapping they were originally drawn at unless a later
        # synchronized home+full-transcript repaint (real resizing apps
        # send one) is genuinely re-extracted from the native terminal,
        # which does reflow, rather than trusted-and-skipped via a
        # shortcut that assumed Screen.resize() had already reflowed it
        # (it never did -- see GhosttyParser.resize()'s comment).
        long_line = "REWRAP-ME-" + "".join(str(i % 10) for i in range(25))
        self.assertEqual(len(long_line), 35)
        # Screen is 20 cols (setUp): this auto-wraps across 2 physical rows
        # with no explicit newline, so the 35-char run is never contiguous
        # in _visible_text (a "\n" from the row join falls inside it).
        self.parser.feed(long_line + "\n")
        for i in range(6):
            self.parser.feed("filler-%d\n" % i)
        self.assertNotIn(long_line, _visible_text(self.screen))

        self.parser.resize(40, 4)
        self.parser.feed("\x1b[?2026h\x1b[H" + long_line + "\n\x1b[?2026l")

        # At 40 cols the same 35 characters fit on one row -- contiguous
        # in _visible_text only if the resize's synchronized repaint was
        # actually re-extracted from native (reflowed) history, not
        # skipped.
        self.assertIn(long_line, _visible_text(self.screen))


class ReplaceScrollMergeTests(unittest.TestCase):
    """Home+dump history merge: keep unique old rows, drop reproduced ones.

    A production change that kept the whole prior history would duplicate
    LINE-00; one that cleared it would drop UNIQUE-A and spliced TURN-00.
    """

    def test_full_replay_keeps_one_copy(self):
        from terminal.ghostty_engine import merge_replace_scroll_history

        old = [_cells("LINE-%02d" % i) for i in range(8)]
        new = [_cells("LINE-%02d" % i) for i in range(9)]
        merged = merge_replace_scroll_history(old, new, splice_window=4)
        self.assertEqual(_row_texts(merged), ["LINE-%02d" % i for i in range(9)])

    def test_spliced_overflow_prefers_clean_old_row(self):
        from terminal.ghostty_engine import merge_replace_scroll_history

        old = [_cells("user prompt TURN-00"), _cells("TURN-00 L00"), _cells("TURN-00 L01")]
        new = [
            _cells("user prompt TURN-00ent reply line 2005"),
            _cells("TURN-00 L00"),
            _cells("TURN-00 L01"),
            _cells("TURN-00 L02"),
        ]
        merged = merge_replace_scroll_history(old, new, splice_window=24)
        texts = _row_texts(merged)
        self.assertEqual(texts[0], "user prompt TURN-00")
        self.assertNotIn("ent reply line 2005", texts[0])
        self.assertEqual(texts.count("user prompt TURN-00"), 1)
        self.assertEqual(texts[1:], ["TURN-00 L00", "TURN-00 L01", "TURN-00 L02"])

    def test_unrelated_prior_rows_are_kept(self):
        from terminal.ghostty_engine import merge_replace_scroll_history

        old = [_cells("UNIQUE-A"), _cells("UNIQUE-B"), _cells("LINE-00"), _cells("LINE-01")]
        new = [_cells("LINE-00"), _cells("LINE-01"), _cells("LINE-02")]
        merged = merge_replace_scroll_history(old, new, splice_window=4)
        self.assertEqual(
            _row_texts(merged),
            ["UNIQUE-A", "UNIQUE-B", "LINE-00", "LINE-01", "LINE-02"],
        )

    def test_ambiguous_repeated_match_prefers_most_recent_occurrence(self):
        """Codex-style tools redraw from turn 0 on every single dump, so
        "TURN-00"-shaped text is not unique across accumulated history --
        it recurs once per prior replay cycle. Aligning to the FIRST
        (leftmost/earliest) matching old row instead of the LAST (most
        recent) one shifts every subsequent row comparison onto unrelated
        old rows, which can both let real splice corruption through
        uncorrected AND drop genuinely unique older content -- this is the
        2026-08-27 live-reproduced root cause (ai/TODO.md), traced to a
        `break` on the first match in the old_rows scan below.
        """
        from terminal.ghostty_engine import merge_replace_scroll_history

        old = [
            _cells("user prompt TURN-00"),  # 0: stale, from an earlier cycle
            _cells("STALE-ONLY-HERE"),      # 1: unique marker after the stale copy
            _cells("user prompt TURN-00"),  # 2: the correct, most-recent copy
            _cells("FRESH-ONLY-HERE"),      # 3: unique marker after the fresh copy
        ]
        # This dump only continues the FRESH copy (its own next row is the
        # fresh marker, not the stale one).
        new = [
            _cells("user prompt TURN-00"),
            _cells("FRESH-ONLY-HERE"),
            _cells("user prompt TURN-01"),
        ]
        merged = merge_replace_scroll_history(old, new, splice_window=4)
        texts = _row_texts(merged)
        self.assertEqual(
            texts,
            [
                "user prompt TURN-00",
                "STALE-ONLY-HERE",
                "user prompt TURN-00",
                "FRESH-ONLY-HERE",
                "user prompt TURN-01",
            ],
        )

    def test_mid_stream_start_finds_alignment_via_later_rows(self):
        """A dump chunk boundary can land such that new_rows[0] is a
        corrupted/unmatched row (real splice artifact, or simply mid-
        transcript content with no prior counterpart) while later rows in
        the SAME chunk clearly continue a run already in old_rows. Row-0-
        only alignment (the original design) finds nothing, falls back to
        keep=len(old_rows), and then BOTH duplicates the shared rows AND
        never gets a chance to protect anything -- this is the real
        2026-08-27 live-reproduced defect (ai/TODO.md), distinct from the
        ambiguous-repeat case above: here there is no candidate match for
        new_rows[0] AT ALL, so the fix must search evidence across
        multiple rows, not just retry the same row-0 anchor differently.
        """
        from terminal.ghostty_engine import merge_replace_scroll_history

        old = [
            _cells("PRE-A"),
            _cells("PRE-B"),
            _cells("SHARED-01"),
            _cells("SHARED-02"),
            _cells("SHARED-03"),
        ]
        new = [
            _cells("CORRUPT-ROW-0-no-match-anywhere"),
            _cells("SHARED-01"),
            _cells("SHARED-02"),
            _cells("SHARED-03"),
            _cells("SHARED-04"),
        ]
        merged = merge_replace_scroll_history(old, new, splice_window=4)
        texts = _row_texts(merged)
        # SHARED-01/02/03 must appear exactly once each -- the defining
        # failure mode of row-0-only alignment is duplicating them (kept
        # in old_rows' tail AND re-appended fresh from new_rows).
        for shared in ("SHARED-01", "SHARED-02", "SHARED-03"):
            self.assertEqual(
                texts.count(shared), 1, f"{shared} must appear exactly once, got {texts}"
            )
        # The genuinely new tail row must be present.
        self.assertIn("SHARED-04", texts)
        # SHARED-03 must be immediately followed by SHARED-04 (the real
        # continuation), not duplicated content in between.
        self.assertEqual(texts[texts.index("SHARED-03") + 1], "SHARED-04")

    def test_wrapped_replay_aligns_on_first_overflow_line(self):
        from terminal.ghostty_engine import merge_replace_scroll_history

        old = [
            _cells("LINE-00"),
            _cells("       LINE-01"),
            _cells("              LINE-0"),
            _cells("2"),
        ]
        new = [
            _cells("LINE-00   LINE-10"),
            _cells("       LINE-01   LIN"),
            _cells("E-11          LINE-0"),
            _cells("2"),
            _cells(" LINE-03"),
        ]
        merged = merge_replace_scroll_history(old, new, splice_window=4)
        texts = _row_texts(merged)
        self.assertEqual(texts[0], "LINE-00")
        self.assertEqual(sum(1 for t in texts if "LINE-00" in t), 1)


class ReplaceScrollStateTests(unittest.TestCase):
    def test_2026_then_home_marks_replace(self):
        from terminal.ghostty_engine import update_replace_scroll

        open_, replace = update_replace_scroll(False, False, "\x1b[?2026h\x1b[Hhello")
        self.assertTrue(open_)
        self.assertTrue(replace)

    def test_home_after_open_batch_from_prior_feed(self):
        from terminal.ghostty_engine import update_replace_scroll

        open_, replace = update_replace_scroll(True, False, "\x1b[Hmore")
        self.assertTrue(open_)
        self.assertTrue(replace)

    def test_replace_latches_across_chunks_until_2026_closes(self):
        from terminal.ghostty_engine import update_replace_scroll

        open_, replace = update_replace_scroll(True, True, "LINE-00\nLINE-01\n")
        self.assertTrue(open_)
        self.assertTrue(replace)

    def test_2026_without_home_does_not_replace(self):
        from terminal.ghostty_engine import update_replace_scroll

        open_, replace = update_replace_scroll(False, False, "\x1b[?2026hspin\x1b[?2026l")
        self.assertFalse(open_)
        self.assertFalse(replace)


if __name__ == "__main__":
    unittest.main()
