"""TSP view: shows a TSP document in a Sublime view as rich panels (phantoms).

This is the DISPLAY block of GhostShell's native TSP view.  A TspHost (the
protocol side) keeps the document up to date; this class draws it:

  * tsp_html.render_regions() turns the document into HTML for each entry,
  * each entry becomes one Sublime "phantom" (an HTML panel inside the view),
  * only phantoms whose HTML changed are replaced (PhantomSet compares them),
  * the view's own text is reduced to one empty line, because the panels are
    the display; the terminal's text rows are not used while native view is on,
  * while the program has an overlay open (a picker, /usage, settings...) ONLY
    the topmost overlay is shown, as a screen of its own: no old transcript
    underneath to confuse what is current,
  * if the reader was at the bottom, the view follows new content,
  * a click on a panel's link sends the matching event back to the program, and
    the status bar says what was sent,
  * the status bar also says the view is live (with the time of the last
    redraw), so a frozen display can be told from a quiet one,
  * spinners and running-time counters are clocked here (the program expects the
    terminal to do that): a single timer runs only while something on screen
    is animated, and stops by itself.  Profile key tsp_animation_ms sets its
    period; 0 turns animation off.

Needs Sublime (it uses the sublime module), so it runs inside Sublime only.
"""

import time
import webbrowser

import sublime

from . import tsp_html

PHANTOM_KEY = "tsp-native"
VIEW_SETTINGS = (("line_numbers", False), ("gutter", False), ("highlight_line", False),
                 ("word_wrap", True), ("draw_white_space", "none"), ("wrap_width", 0),
                 ("ai_tsp_native", True))     # the last tells ai_terminal's scroll code to keep out
STATUS_KEY = "ai_tsp"
MARK_KEY = "ai-tsp-mark-"          # prefix of the region sets that draw the minimap marks
MARK_KINDS = ("user", "error", "tool", "assistant", "note", "other", "input", "sheet")
# An earlier version drew coloured minimap marks; these names are kept only to erase them. This view adds no
# colour of its own: colours are the TUI's and GhostShell's own scheme's.
DEFAULT_ANIMATION_MS = 250


class _Clock:
    """Wall time for the drawers: running-time counters, toast lifetimes and the spinner frame."""

    def __init__(self):
        self.now = time.monotonic()
        self._seen = {}         # node id -> (age the program sent, when we first saw that age)
        self._first = {}        # node id -> when we first saw the node at all

    def tick(self):
        self.now = time.monotonic()

    def elapsed_ms(self, node_id, age_ms):
        """The time a running node shows: the age the program sent plus time since we got it."""
        age_ms = age_ms or 0
        seen = self._seen.get(node_id)
        if seen is None or seen[0] != age_ms:
            seen = self._seen[node_id] = (age_ms, self.now)
        return age_ms + (self.now - seen[1]) * 1000.0

    def visible_ms(self, node_id):
        """How long ``node_id`` has been on screen (for a toast's lifetime)."""
        first = self._first.setdefault(node_id, self.now)
        return (self.now - first) * 1000.0

    def spinner_index(self, frame_count):
        return int(self.now * 8) % frame_count


def describe_event(event):
    """A short plain-words label for an event, for the status bar."""
    kind = event.get("ev")
    if kind == "action":
        return "%s %s" % (event.get("act", ""), event.get("value") or "")
    if kind in ("select", "activate"):
        return "%s %s" % (kind, event.get("item", ""))
    if kind == "toggle":
        return "%s %s" % ("collapse" if event.get("collapsed") else "expand", event.get("id", ""))
    return str(kind)


class TspView:
    """Draws the newest TSP surface of a host into ``view``."""

    def __init__(self, view, animation_ms=DEFAULT_ANIMATION_MS):
        self.view = view
        self._phantoms = sublime.PhantomSet(view, PHANTOM_KEY)
        self._shown = None          # list of (key, html) last put on screen
        self._prepared = False      # view text already reduced to one line
        self._host = None           # the TspHost clicks are sent to
        self._cols = 100
        self._clock = _Clock()
        self._animation_ms = animation_ms
        self._timer_armed = False
        self._local = {}            # view-only state (expanded sections, shown-row counts)
        self._overlay_open = False
        self._pin_bottom = False    # the reader typed: the next drawing must end at the input box
        self._surface_id = None     # the surface drawn last (a new one: show its end, e.g. after a reattach)
        self._text = None           # the tab text last written (one caption line per panel)
        self._outline = None        # (kind, caption) of each panel last drawn: what the text and minimap marks show

    def navigate(self, href):
        """A link in a panel was clicked: send the event it stands for back to the program."""
        event = tsp_html.href_event(href)
        if event is None:
            if href.startswith(("http://", "https://")):
                webbrowser.open(href)
            return
        local = event.get("local")
        if local == "set":
            self._local[event.get("key")] = event.get("value")
            if self._host is not None:
                self.render(self._host, self._cols)
        elif local == "copy":
            sublime.set_clipboard(event.get("text", ""))
            sublime.status_message("Copied")
        elif local == "open":
            target = event.get("href") or ""
            if target.startswith(("http://", "https://")):
                webbrowser.open(target)
        elif self._host is not None:
            self._host.send_event(event)
            sublime.status_message("Sent to omp: " + describe_event(event))

    def pin_to_bottom(self):
        """The reader pressed a key: the input box is at the bottom, so show it, now and after the echo.

        Typing while scrolled up used to land in an input box that was off screen.  The pin
        stays until a drawing has put the view at the bottom, so the echo of the keystroke cannot
        leave the box out of sight either.
        """
        if not self._prepared:
            return
        self._pin_bottom = True
        self._to_bottom_soon()

    def _to_bottom_soon(self):
        """Scroll to the bottom AFTER Sublime has laid the new panels out.

        PhantomSet.update() changes the layout a moment later; asking for the layout height right
        away gets the old height and scrolls to the wrong place.  Five one-shot tries (now, 40, 120, 300
        and 700 ms) because the layout settles within one of them (a transcript of over a hundred panels,
        as after a reattach, needs the later ones); there is no event for it.
        """
        for delay in (0, 40, 120, 300, 700):
            sublime.set_timeout(self._to_bottom_now, delay)

    def _to_bottom_now(self):
        view = self.view
        if not view.is_valid():
            return
        view.set_viewport_position((0, max(0, view.layout_extent()[1] - view.viewport_extent()[1])), False)

    def _prepare_view(self):
        """Set the tab up for panels: no line numbers and so on (once)."""
        view = self.view
        settings = view.settings()
        for name, value in VIEW_SETTINGS:
            settings.set(name, value)
        for kind in MARK_KINDS:
            view.erase_regions(MARK_KEY + kind)     # marks an earlier version drew
        self._text = None
        self._outline = None
        self._prepared = True

    def _set_text(self, text):
        """Make the tab's text exactly ``text`` (True when it had to change).

        The text is one line per panel, so the minimap (which draws the tab's text lines, spaced by the
        panels' heights) is an outline of the whole conversation, search finds the captions, and a click
        in the minimap lands on the right panel.  GhostShell's own command replaces the whole text and
        marks the edit as its own, so its key and selection handlers do not treat it as typing.
        """
        view = self.view
        if self._text == text and view.size() == len(text):
            return False
        view.run_command("ai_terminal_render", {
            "text": text, "cursor": [0, 0], "cursor_offset": -1, "regions": [], "fast_caret": False,
        })
        view.sel().clear()
        view.sel().add(sublime.Region(view.size(), view.size()))      # an API change: does not scroll
        self._text = text
        return True

    def _parts(self, host, cols):
        """([(key, html)], palette, animated, overlay_open, [(kind, caption)]) for the newest surface, or Nones."""
        surface_id = host.active_surface()
        if surface_id is None:
            return None, None, False, False, []
        palette = host.palettes.get(surface_id)
        regions = tsp_html.render_regions(host.documents[surface_id].snapshot(), palette, cols, self._clock,
                                          getattr(host, "blobs", None), self._local)
        if regions["layer"]:
            # An overlay is open: it is the whole screen.  Only the topmost one is drawn.
            parts = [("layer", regions["layer"][0])]
            outline = [("sheet", "")]
            if regions.get("under"):
                parts.append(("under", tsp_html.overlay_note(regions["under"], palette)))
                outline.append(("", ""))
            if regions["dock"]:
                parts.append(("dock", regions["dock"]))     # the command line stays in sight under the overlay
                outline.append(("input", ""))
            return parts, palette, regions["animated"], True, outline
        parts = [("main%d" % i, html) for i, html in enumerate(regions["main"]) if html]
        outline = [item for item, html in zip(regions["main_outline"], regions["main"]) if html]
        if regions["dock"]:
            parts.append(("dock", regions["dock"]))
            outline.append(("input", ""))
        return parts, palette, regions["animated"], False, outline

    def render(self, host, cols=100):
        """Bring the view up to date.  Returns False when there is nothing to draw yet."""
        self._host = host
        self._cols = cols
        self._clock.tick()
        parts, palette, animated, overlay_open, outline = self._parts(host, cols)
        if parts is None:
            return False
        if animated:
            self._arm_timer()
        view = self.view
        settings = view.settings()
        reset = False
        for name, value in VIEW_SETTINGS:      # GhostShell's own view setup can switch these back
            if settings.get(name) != value:
                settings.set(name, value)
                reset = True
        if reset and self._shown is not None:
            # The view's settings were reset by something else (a plugin reload sets the tab up afresh and the
            # panels go with it): draw everything again rather than trust what we think is on screen.
            self._shown = None
            self._prepared = False
        if parts == self._shown and outline == self._outline:
            return True
        if not self._prepared:
            self._prepare_view()
        line = view.line_height() or 20
        at_bottom = view.viewport_position()[1] + view.viewport_extent()[1] >= view.layout_extent()[1] - 2 * line
        # One text line per panel (spaces only: GhostShell's scheme has no way to hide text, and a panel's
        # own text is already shown by the panel), the panel hanging under its line; line 0 is empty (GhostShell's
        # toolbar hangs under it) and the last line is empty too (the hidden caret sits there, below everything:
        # anchored higher, every click or keystroke that reveals the caret threw the view to the top).
        captions = [" " for _item in outline]
        starts, offset = [], 1
        for caption in captions:
            starts.append(offset)
            offset += len(caption) + 1
        text_changed = self._set_text("\n" + "".join(caption + "\n" for caption in captions))
        if text_changed:
            self._phantoms.update([])           # the old anchors are gone with the old text
            self._shown = None
        phantoms = [
            sublime.Phantom(sublime.Region(start, start), tsp_html.page(html, palette), sublime.LAYOUT_BLOCK, self.navigate)
            for start, (_key, html) in zip(starts, parts)
        ]
        # Phantoms that share one anchor are shown in the order they were created, and PhantomSet puts a
        # re-drawn one at the end.  So when an entry above the last one changes (a section opened, a spinner
        # moved), take down that entry and everything after it first, then add them again in order.
        old = self._shown or []
        first_changed = 0
        while first_changed < min(len(old), len(parts)) and old[first_changed] == parts[first_changed]:
            first_changed += 1
        if first_changed < len(old):
            self._phantoms.update(phantoms[:first_changed])
        self._phantoms.update(phantoms)
        self._shown = parts
        self._outline = outline
        view.set_status(STATUS_KEY, "omp native view: live " + time.strftime("%H:%M:%S"))
        surface_id = host.active_surface()
        if surface_id != self._surface_id:
            self._surface_id = surface_id
            at_bottom = True
        if overlay_open != self._overlay_open:
            # An overlay opened or closed.  Either way the input box (at the bottom) must stay in sight,
            # as omp lays it out: the overlay above, the input box under it.
            self._overlay_open = overlay_open
            at_bottom = True
        if at_bottom or self._pin_bottom:
            self._pin_bottom = False
            self._to_bottom_soon()
        return True

    # ---- animation heartbeat ---------------------------------------------------------

    def _arm_timer(self):
        """Ask for one tick, unless animation is off or a tick is already waiting."""
        if self._animation_ms <= 0 or self._timer_armed:
            return
        self._timer_armed = True
        sublime.set_timeout(self._tick, self._animation_ms)

    def _tick(self):
        """Redraw with the new time; render() asks for the next tick only if something still moves."""
        self._timer_armed = False
        if self._host is None or not self.view.is_valid():
            return
        try:
            self.render(self._host, self._cols)
        except Exception as error:      # a drawing fault must not keep a timer running
            print("[tsp_view] animation stopped: %s" % error)

    def clear(self):
        """Take every panel down (native view turned off, or a drawing fault)."""
        self.view.erase_phantoms(PHANTOM_KEY)
        self._phantoms = sublime.PhantomSet(self.view, PHANTOM_KEY)
        self._shown = None
        self._prepared = False
        for kind in MARK_KINDS:
            self.view.erase_regions(MARK_KEY + kind)
        self._outline = None
        self._text = None
        self.view.settings().set("ai_tsp_native", False)    # text-row scrolling may act again
