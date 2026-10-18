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

import base64
import os
import tempfile
import re
import time
import urllib.parse
import urllib.request
import webbrowser

import sublime

from . import tsp_html
from . import tsp_draw_data

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


_LINE_SUFFIX = re.compile(r"(?:#L(\d+)(?:-L?\d+)?|:(\d+)(?:-\d+)?(?::(\d+))?)$")


def parse_link(target):
    """What a link text points at: ("web", url), ("mail", url) or ("file", path, line, column); None if unusable.

    A file target may be a file:// URL, ~/x, C:\\x, /x or a relative path, with a :line, :a-b, :line:col,
    #L3 or #L10-L20 suffix (the first line is used)."""
    target = target.strip()
    if target.startswith(("http://", "https://")):
        return ("web", target)
    if target.startswith("mailto:"):
        return ("mail", target)
    if target.startswith("file://"):
        target = urllib.request.url2pathname(urllib.parse.urlparse(target).path)
        if re.match(r"^[/\\][A-Za-z]:", target):
            target = target[1:]
    line = column = None
    found = _LINE_SUFFIX.search(target)
    if found and found.start() > 1:
        line = int(found.group(1) or found.group(2))
        column = int(found.group(3)) if found.group(3) else None
        target = target[:found.start()]
    if not target:
        return None
    return ("file", os.path.expanduser(target), line, column)


ASIDE_COLS = 44             # the settings sheet's width in cells; at most ASIDE_MAX_FRACTION of the pane
ASIDE_MAX_FRACTION = 0.6


def split_layout(layout, group, fraction):
    """``layout`` with a new group to the right of ``group``, taking ``fraction`` of its width.

    The new group gets the last index, so the existing groups keep theirs."""
    cols = list(layout["cols"])
    cells = [list(cell) for cell in layout["cells"]]
    left, top, right, bottom = cells[group]
    x = cols[left] + (cols[right] - cols[left]) * (1.0 - fraction)
    index = left + 1
    while index < right and cols[index] < x:
        index += 1
    cols.insert(index, x)
    for cell in cells:
        if cell[0] >= index:
            cell[0] += 1
        if cell[2] >= index:
            cell[2] += 1
    cells[group][2] = index
    cells.append([index, top, right + 1, bottom])
    return {"cols": cols, "rows": list(layout["rows"]), "cells": cells}


class _Aside:
    """The aside sheet: a view in a new column right of the pane's group, removed again when it closes."""

    def __init__(self, main_view):
        self._main = main_view
        self._view = None
        self._saved = None          # the window layout from before the column was added
        self._window = None
        self._phantoms = None
        self._html = None

    def dismissed(self):
        """True when the sheet was open and its tab has been closed by the reader."""
        return self._view is not None and not self._view.is_valid()

    def show(self, html, fraction, navigate):
        if self._view is None and not self._open(fraction):
            return
        if html == self._html:
            return
        self._html = html
        self._phantoms.update([sublime.Phantom(sublime.Region(0, 0), html, sublime.LAYOUT_BLOCK, navigate)])

    def _open(self, fraction):
        window = self._main.window()
        if window is None:
            return False
        group = window.get_view_index(self._main)[0]
        self._saved = window.layout()
        window.set_layout(split_layout(self._saved, group, fraction))
        view = window.new_file()
        view.set_scratch(True)
        view.set_name("TSP aside")
        for name, value in VIEW_SETTINGS:
            if name != "ai_tsp_native":
                view.settings().set(name, value)
        view.run_command("append", {"characters": "\n"})
        view.set_read_only(True)
        window.set_view_index(view, len(self._saved["cells"]), 0)
        window.focus_view(self._main)
        self._window, self._view = window, view
        self._phantoms = sublime.PhantomSet(view, PHANTOM_KEY)
        self._html = None
        return True

    def close(self):
        """Remove the sheet and give the column back (the previous layout exactly)."""
        if self._view is None and self._saved is None:
            return
        view, window, saved = self._view, self._window, self._saved
        self._view = self._window = self._saved = self._phantoms = self._html = None
        if view is not None and view.is_valid():
            view.close()
        if window is not None and saved is not None:
            sublime.set_timeout(lambda: self._restore(window, saved), 50)

    def _restore(self, window, saved):
        if window.is_valid():
            window.set_layout(saved)
            if self._main.is_valid():
                window.focus_view(self._main)


_phantom_sets = {}      # view id -> the PhantomSet drawn there; the convention sublime-mcp's get_view_phantoms reads


class TspView:
    """Draws the newest TSP surface of a host into ``view``."""

    def __init__(self, view, animation_ms=DEFAULT_ANIMATION_MS, aside=False, on_state=None):
        self.view = view
        self._on_state = on_state   # called as on_state(animated, now) after each draw; None = nobody listens
        self._phantoms = sublime.PhantomSet(view, PHANTOM_KEY)
        _phantom_sets[view.id()] = self._phantoms     # editor tools that list a view's phantoms (sublime-mcp get_view_phantoms) look here
        self._shown = None          # list of (key, html) last put on screen
        self._prepared = False      # view text already reduced to one line
        self._host = None           # the TspHost clicks are sent to
        self._cols = 100
        self._clock = _Clock()
        self._animation_ms = animation_ms
        self._hold_id = 0           # bumped to cancel pending "keep the scrolled-up position" restores
        self._pin_id = 0            # bumped to cancel the pending tries of the last scroll to the bottom
        self._pin_set = None        # where the last try put the view
        self._timer_armed = False
        self._local = {}            # view-only state (expanded sections, shown-row counts)
        self._overlay_open = False
        self._prefs_page = False        # a settings (prefs) page is what the program shows, in main or as an overlay
        self._prefs_seen = False        # ... as of the last drawing
        self._pin_bottom = False    # the reader typed: the next drawing must end at the input box
        self._surface_id = None     # the surface drawn last (a new one: show its end, e.g. after a reattach)
        self._text = None           # the tab text last written (one caption line per panel)
        self._outline = None        # (kind, caption) of each panel last drawn: what the text and minimap marks show
        self._anchors = []          # [(part key, text offset of its caption line)] of the last drawing
        self._part_ids = {}         # part key "mainN" -> the id of that main entry
        self._reveal_doc = None     # the document whose reveal op was last taken up
        self._reveal_seen = 0       # sequence number of that reveal
        self._reveal_entry = None   # (main entry id, where) waiting for the drawing to end
        self._reveal_id = 0         # bumped to cancel the pending tries of the last reveal
        self._reveal_set = None     # where the last reveal try put the view
        self._eff_cols = 100        # width the program is told: the pane's own width (an open aside sheet is a column of its own)
        self._reported = None       # (surface, cols, cell w, cell h) last sent in a resize event
        self._dark = True           # appearance last told to the program (the hello says dark)
        self._aside_enabled = aside     # may a layer prefs sheet open in a column of its own (off: it is an overlay in the tab)
        self._aside = _Aside(view)
        self._aside_active = False  # the aside sheet is open
        self._aside_seen = False    # ... as of the last time the view was pinned for a change
        self._clamp_id = 0          # bumped to cancel the pending look-agains of the last aside change
        self._aside_cols = 0
        self._aside_html = ""
        self._aside_id = None
        self._last_click = None     # (href, time) of the last click on a row that has a double-click event
        self._aside_dismissed = None    # id of an aside node whose sheet tab the reader closed

    def _open_link(self, target):
        """Open a link's target: a web or mail link in the system's handler, a file in Sublime at its line."""
        parsed = parse_link(target)
        if parsed is None:
            return
        if parsed[0] != "file":
            webbrowser.open(parsed[1])
            return
        _kind, path, line, column = parsed
        window = self.view.window()
        if not os.path.isabs(path) and window is not None:
            base = next((os.path.join(folder, path) for folder in window.folders()
                         if os.path.exists(os.path.join(folder, path))), None)
            path = base or path
        if not os.path.exists(path):
            sublime.status_message("Not found: " + path)
            return
        if os.path.isdir(path):
            webbrowser.open(path)
            return
        if window is None:
            return
        if line:
            path = "%s:%d:%d" % (path, line, column or 1)
        window.open_file(path, sublime.ENCODED_POSITION)

    def _open_image(self, blob_id):
        """Write a picture the program sent to a temp file with the right extension and open it in the system viewer."""
        text = self._host.blobs.get(blob_id) if self._host is not None else None
        info = tsp_draw_data.image_info(text) if text else None
        if not info:
            sublime.status_message("Image not available")
            return
        ext = {"image/png": "png", "image/jpeg": "jpg", "image/gif": "gif", "image/webp": "webp",
               "image/svg+xml": "svg"}.get(info[0], "png")
        folder = os.path.join(tempfile.gettempdir(), "ghostshell-images")
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, "%s.%s" % (str(blob_id)[:16], ext))
        if not os.path.exists(path):
            with open(path, "wb") as handle:
                handle.write(base64.b64decode(text))
        if hasattr(os, "startfile"):
            os.startfile(path)
        else:
            webbrowser.open("file://" + path)

    def navigate(self, href):
        """A link in a panel was clicked: send the event it stands for back to the program."""
        event = tsp_html.href_event(href)
        if event is None:
            if href.startswith(("http://", "https://")):
                webbrowser.open(href)
            return
        event = dict(event)
        also = event.pop("also", None)      # a local change that goes with the event: applied first
        dbl = event.pop("dbl", None)        # the event a quick second click on the same row stands for
        if isinstance(dbl, dict) and self._host is not None:
            now, last = time.monotonic(), self._last_click
            self._last_click = (href, now)
            if last is not None and last[0] == href and now - last[1] < 0.4:
                self._last_click = None
                self._host.send_event(dbl)
                sublime.status_message("Sent to omp: " + describe_event(dbl))
                return
        if isinstance(also, dict) and also.get("local") == "set":
            self._local[also.get("key")] = also.get("value")
        local = event.get("local")
        if local == "set":
            self._local[event.get("key")] = event.get("value")
            if self._host is not None:
                send = event.get("send")
                if isinstance(send, dict):
                    self._host.send_event(send)
                self.render(self._host, self._cols)
        elif local == "copy":
            sublime.set_clipboard(event.get("text", ""))
            sublime.status_message("Copied")
        elif local == "open":
            self._open_link(event.get("href") or "")
        elif local == "image":
            self._open_image(event.get("blob"))
            self.render(self._host, self._cols)
        elif isinstance(local, str):
            return          # a local event this view does not handle (e.g. "none"): never sent to the program
        elif self._host is not None:
            self._host.send_event(event)
            sublime.status_message("Sent to omp: " + describe_event(event))
            if also:
                self.render(self._host, self._cols)

    def pin_to_bottom(self):
        """The reader pressed a key: the input box is at the bottom, so show it, now and after the echo.

        Typing while scrolled up used to land in an input box that was off screen.  The pin
        stays until a drawing has put the view at the bottom, so the echo of the keystroke cannot
        leave the box out of sight either.
        """
        if not self._prepared or self._dock_in_view() or self._prefs_page:
            return          # the reader can see the input box where it is: leave the view alone
        self._pin_bottom = True
        self._to_bottom_soon()

    def _dock_in_view(self):
        """True when the input box (the dock) is completely on screen: its anchor line is in view and nothing is below it."""
        anchor = getattr(self, "_dock_anchor", None)
        view = self.view
        if anchor is None or not view.is_valid():
            return False
        top = view.viewport_position()[1]
        return (view.text_to_layout(anchor)[1] >= top
                and view.layout_extent()[1] <= top + view.viewport_extent()[1] + 2)

    def _to_bottom_soon(self):
        """Scroll to the bottom AFTER Sublime has laid the new panels out.

        PhantomSet.update() changes the layout a moment later; asking for the layout height right
        away gets the old height and scrolls to the wrong place.  Five one-shot tries (now, 40, 120, 300
        and 700 ms) because the layout settles within one of them (a transcript of over a hundred panels,
        as after a reattach, needs the later ones); there is no event for it.
        """
        self._pin_id += 1
        self._pin_set = None
        pin_id = self._pin_id
        for delay in (0, 40, 120, 300, 700):
            sublime.set_timeout(lambda: self._to_bottom_now(pin_id), delay)

    def _to_bottom_now(self, pin_id):
        view = self.view
        if pin_id != self._pin_id or not view.is_valid():
            return
        y = view.viewport_position()[1]
        if self._pin_set is not None and abs(y - self._pin_set) > 3:
            self._pin_id += 1       # the reader scrolled since our last move: stop fighting them
            return
        self._hold_id += 1          # an explicit scroll to the bottom beats any pending hold
        self._pin_set = max(0, view.layout_extent()[1] - view.viewport_extent()[1])
        view.set_viewport_position((0, self._pin_set), False)

    def _to_top_soon(self):
        """Show the top of the view once Sublime has laid the new panels out (same tries as the scroll to the
        bottom), cancelling any pending pin to the bottom."""
        self._pin_id += 1
        self._hold_id += 1
        pin_id = self._pin_id
        for delay in (0, 40, 120, 300, 700):
            sublime.set_timeout(lambda: self._to_top_now(pin_id), delay)

    def _to_top_now(self, pin_id):
        if pin_id == self._pin_id and self.view.is_valid():
            self.view.set_viewport_position((0, 0), False)

    def _hold_position(self, position):
        """Put the view back at ``position`` once the layout has settled, unless a pin to the bottom came since."""
        self._hold_id += 1
        hold_id = self._hold_id

        def restore():
            view = self.view
            if hold_id != self._hold_id or not view.is_valid():
                return
            y = view.viewport_position()[1]
            bottom = view.layout_extent()[1] - view.viewport_extent()[1]
            pulled_down = y > position[1] and y + view.viewport_extent()[1] >= view.layout_extent()[1] - 2
            # A reader who pushed the view past the last line (input box mid-screen) was thrown back to the
            # ordinary bottom by a redraw: put them back where they were.
            clamped = position[1] > bottom + 4 and abs(y - bottom) <= 4
            if pulled_down or clamped:      # only a throw to the bottom is undone; the reader's own scrolling is left alone
                view.set_viewport_position(position, False)
        for delay in (0, 40, 120):
            sublime.set_timeout(restore, delay)

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

    def _regions(self, host, snapshot, palette, cols):
        """The document's HTML regions.  A layer ``prefs`` sheet (the terminal feature ``aside``, always on:
        the hello reply announces it) is taken out of the overlays and drawn narrower beside the pane: the
        rest is laid out at the narrower ``cols``."""
        blobs = getattr(host, "blobs", None)
        if getattr(self, "_entry_cache", None) is None:
            self._entry_cache = {}      # html of the entries that did not change (see tsp_html._draw_main_entries)

        def draw(width, aside):
            return tsp_html.render_regions(snapshot, palette, width, self._clock, blobs, self._local, aside=aside,
                                           reduce_motion=self._animation_ms <= 0, toast_region=True,
                                           entry_cache=self._entry_cache)

        if not self._aside_enabled:
            # The sheet stays an overlay inside this tab: the window's groups and layout are never touched.
            self._eff_cols = cols
            self._aside_active = False
            return draw(cols, None)
        # The sheet is a column of its own, so once it is open the pane is already that much narrower and
        # ``cols`` (measured from the view) is the width main is drawn at; the sheet keeps the width it opened with.
        if self._aside_active and self._aside_cols:
            width = self._aside_cols
        else:
            width = max(10, min(ASIDE_COLS, int(cols * ASIDE_MAX_FRACTION)))
            self._aside_cols = width
        regions = draw(cols, width)
        aside_id = regions.get("aside_id")
        if aside_id is None:
            self._aside_dismissed = None
        self._aside_active = bool(regions.get("aside")) and aside_id != self._aside_dismissed
        self._eff_cols = cols
        return regions

    def _pin_on_aside_change(self):
        """The sheet opened or closed, so the pane is about to get narrower or wider and the text reflows: a view
        that is at the end now must still end at the input box afterwards, not stay where the old layout put it
        (past the new end, after a close).  Measured before the column changes."""
        if self._aside_active == self._aside_seen:
            return
        self._aside_seen = self._aside_active
        line = self.view.line_height() or 20
        reach = self.view.viewport_position()[1] + self.view.viewport_extent()[1]
        if reach >= self.view.layout_extent()[1] - 2 * line:
            self._pin_bottom = True
            self._clamp_soon()

    def _clamp_soon(self):
        """The text reflows over the next second or two (the pane changes width, the phantoms wrap again) and
        Sublime does not pull a view back that ends up past the new end.  Look again a few times: a view that
        is past the end is put at the end; one that is not is left alone."""
        self._clamp_id += 1
        clamp_id = self._clamp_id
        for delay in (100, 300, 600, 1000, 1600, 2500):
            sublime.set_timeout(lambda: self._clamp_now(clamp_id), delay)

    def _clamp_now(self, clamp_id):
        view = self.view
        if clamp_id != self._clamp_id or not view.is_valid():
            return
        bottom = max(0, view.layout_extent()[1] - view.viewport_extent()[1])
        if view.viewport_position()[1] > bottom + (view.line_height() or 20):
            view.set_viewport_position((0, bottom), False)

    def _update_aside(self, host, palette):
        """Keep the sheet view beside the pane in step: open, redraw or close it."""
        if not self._aside_active:
            self._aside.close()
            return
        if self._aside.dismissed():
            self._aside_dismissed = self._aside_id
            self._aside_active = False
            self._aside.close()
            self._shown = None      # redraw main at the full width
            self._pin_on_aside_change()
            sublime.set_timeout(lambda: self._host is not None and self.render(self._host, self._cols), 0)
            return
        fraction = self._aside_cols / float(max(self._cols, self._aside_cols + 1))
        self._aside.show(tsp_html.page(self._aside_html, palette), fraction, self.navigate)

    def _parts(self, host, cols):
        """([(key, html)], palette, animated, overlay_open, [(kind, caption)]) for the newest surface, or Nones."""
        surface_id = host.active_surface()
        if surface_id is None:
            return None, None, False, False, []
        palette = host.palettes.get(surface_id)
        document = host.documents[surface_id]
        self._local["focus"] = document.focus
        self._handle_ops(host, document, surface_id)
        snapshot = document.snapshot()
        regions = self._regions(host, snapshot, palette, cols)
        self._aside_html = regions.get("aside") or "" if self._aside_active else ""
        self._aside_id = regions.get("aside_id")
        main = next((r for r in snapshot.get("c") or [] if r.get("id") == "main"), None) or {}
        self._prefs_page = (any(child.get("k") == "prefs" for child in main.get("c") or [])
                            or (not self._aside_active and any(o.get("prefs") for o in regions.get("overlays") or [])))
        self._part_ids = {"main%d" % i: child.get("id") for i, child in enumerate(main.get("c") or [])}
        layers = [html for html in regions["layer"] if html]
        toast = regions.get("toast") or ""
        self._moving = set()        # keys of the panels that change with time (an overlay is drawn whole, nothing is kept)
        if layers:
            # An overlay is open: it is the whole screen.  omp's stack shows only the topmost one; a layer of
            # plain overlays lists them all, in order.
            parts = [("layer%d" % i, html) for i, html in enumerate(layers)]
            outline = [("sheet", "") for _html in layers]
            if regions.get("under"):
                parts.append(("under", tsp_html.overlay_note(regions["under"], palette)))
                outline.append(("", ""))
            if regions["dock"]:
                parts.append(("dock", regions["dock"]))     # the command line stays in sight under the overlay
                outline.append(("input", ""))
            if toast:
                parts.append(("toast", toast))
                outline.append(("", ""))
            return parts, palette, regions["animated"], True, outline
        self._moving = {"main%d" % i for i in regions.get("main_moving") or ()}
        parts = [("main%d" % i, html) for i, html in enumerate(regions["main"]) if html]
        outline = [item for item, html in zip(regions["main_outline"], regions["main"]) if html]
        if regions["dock"]:
            parts.append(("dock", regions["dock"]))
            outline.append(("input", ""))
        if toast:
            parts.append(("toast", toast))      # the notice sits at the bottom of the view, under the input box
            outline.append(("", ""))
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
        if self._on_state is not None:
            self._on_state(bool(animated), self._clock.now)
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
        self._pin_on_aside_change()
        self._update_aside(host, palette)
        if parts == self._shown and outline == self._outline:
            self._after_draw(host)
            return True
        if not self._prepared:
            self._prepare_view()
        line = view.line_height() or 20
        reach = view.viewport_position()[1] + view.viewport_extent()[1]
        end = view.layout_extent()[1]
        # Following the end = at the end, not pushed past it: a reader who scrolled beyond the last line (to put the
        # input box mid-screen) has chosen that place and keeps it.
        at_bottom = end - 2 * line <= reach <= end + line
        if self._prefs_page and self._prefs_seen:
            # A settings page already on screen changes in place (a picker opens under its row, a switch flips):
            # the reader stays where they are.  Following the end threw the view past the row they had clicked.
            at_bottom = False
        # One text line per panel (spaces only: GhostShell's scheme has no way to hide text, and a panel's
        # own text is already shown by the panel), the panel hanging under its line; line 0 is empty (GhostShell's
        # toolbar hangs under it) and the last line is empty too (the hidden caret sits there, below everything:
        # anchored higher, every click or keystroke that reveals the caret threw the view to the top).
        captions = [" " for _item in outline]
        starts, offset = [], 1
        for caption in captions:
            starts.append(offset)
            offset += len(caption) + 1
        self._anchors = [(key, start) for start, (key, _html) in zip(starts, parts)]
        self._dock_anchor = next((start for key, start in self._anchors if key == "dock"), None)
        text_changed = self._set_text("\n" + "".join(caption + "\n" for caption in captions))
        if text_changed:
            self._phantoms.update([])           # the old anchors are gone with the old text
            self._shown = None
        phantoms = [
            sublime.Phantom(sublime.Region(start, start), tsp_html.page(html, palette), sublime.LAYOUT_BLOCK, self.navigate)
            for start, (_key, html) in zip(starts, parts)
        ]
        # Each panel hangs under its own caption line, so a re-drawn panel keeps its place without help.  The
        # take-down below is kept only for a view at the bottom, where it was proven; for a reader scrolled up it
        # shortens the layout, Sublime throws the view to the bottom and the drawing that follows pins it there.
        # A panel that moved with time in this drawing and in the last one (a spinner, a running-time counter)
        # is the same size as before, so it does not count as a change here: without that, one old tool call
        # still marked running made every tick take down and rebuild every panel after it.
        old = self._shown or []
        still_moving = self._moving & getattr(self, "_moving_shown", set())
        first_changed = 0
        while first_changed < min(len(old), len(parts)) and (
                old[first_changed] == parts[first_changed]
                or (old[first_changed][0] == parts[first_changed][0] and parts[first_changed][0] in still_moving)):
            first_changed += 1
        scrolled_to = None if at_bottom else view.viewport_position()
        if at_bottom and first_changed < len(old):
            self._phantoms.update(phantoms[:first_changed])
        self._phantoms.update(phantoms)
        if scrolled_to is not None:
            # The reader is looking at older output.  While the later panels were down the layout was
            # shorter and Sublime pulled the view toward the bottom, so the next drawing (a spinner tick)
            # would find the view "at the bottom" and pin it there: scrolling back never held.  Put the
            # view back where it was, after the layout has settled.
            self._hold_position(scrolled_to)
        self._shown = parts
        self._moving_shown = set(self._moving)
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
        if self._prefs_page != self._prefs_seen:
            self._prefs_seen = self._prefs_page
            if self._prefs_page:
                # A settings page is long, its title, pages and search are at the top, and its keys go to its own
                # search box, not to the input box: open it at the top.
                at_bottom = False
                self._pin_bottom = False
                self._to_top_soon()
            else:
                at_bottom = True        # the page closed: the transcript and the input box are back
        if at_bottom or self._pin_bottom:
            self._pin_bottom = False
            self._to_bottom_soon()
        self._after_draw(host)
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

    def close_aside(self):
        """Remove the aside sheet and give its column back (the surface ended, or the tab is closing)."""
        self._aside.close()
        self._aside_active = False

    def clear(self):
        """Take every panel down (native view turned off, or a drawing fault)."""
        self._aside.close()
        self._aside_active = False
        self.view.erase_phantoms(PHANTOM_KEY)
        self._phantoms = sublime.PhantomSet(self.view, PHANTOM_KEY)
        _phantom_sets[self.view.id()] = self._phantoms
        self._shown = None
        self._prepared = False
        for kind in MARK_KINDS:
            self.view.erase_regions(MARK_KEY + kind)
        self._outline = None
        self._text = None
        self.view.settings().set("ai_tsp_native", False)    # text-row scrolling may act again

    # ---- after a drawing: reveal, environment reports -----------------------------------

    def _after_draw(self, host):
        entry, self._reveal_entry = self._reveal_entry, None
        if entry is not None and not self._overlay_open:
            self._reveal_soon(*entry)
        self._report_environment(host)

    def _reveal_soon(self, child_id, where):
        """Scroll the pane so the main entry ``child_id`` shows, once the layout has settled (only the latest reveal runs)."""
        key = next((k for k, _off in self._anchors if self._part_ids.get(k) == child_id), None)
        if key is None:
            return
        self._pin_id += 1           # a reveal beats the pin to the bottom and the position hold
        self._hold_id += 1
        self._reveal_id += 1
        self._reveal_set = None
        reveal_id = self._reveal_id
        for delay in (0, 40, 120, 300, 700):
            sublime.set_timeout(lambda: self._reveal_now(reveal_id, key, where), delay)

    def _entry_span(self, key):
        """(top, bottom) layout y of the entry under anchor ``key``: its caption line down to the next anchor."""
        view = self.view
        offsets = [off for _k, off in self._anchors]
        keys = [k for k, _off in self._anchors]
        index = keys.index(key)
        top = view.text_to_layout(offsets[index])[1]
        if index + 1 < len(offsets):
            bottom = view.text_to_layout(offsets[index + 1])[1]
        else:
            bottom = view.layout_extent()[1]
        return top, bottom

    def _reveal_now(self, reveal_id, key, where):
        view = self.view
        if reveal_id != self._reveal_id or not view.is_valid() or key not in dict(self._anchors):
            return
        current = view.viewport_position()[1]
        if self._reveal_set is not None and abs(current - self._reveal_set) > 3:
            self._reveal_id += 1    # the reader scrolled since our last move: stop fighting them
            return
        top, bottom = self._entry_span(key)
        height = view.viewport_extent()[1]
        if where == "start":
            target = top
        elif where == "end":
            target = bottom - height
        elif top >= current and bottom <= current + height:
            target = current
        elif top < current or bottom - top > height:
            target = top
        else:
            target = bottom - height
        self._reveal_set = max(0, target)
        view.set_viewport_position((0, self._reveal_set), False)

    # ---- scroll and reveal inside containers (state lives in self._local) ---------------

    def _handle_ops(self, host, document, surface_id):
        """Carry out the document's pending scroll ops and a new reveal, before the drawing."""
        for node_id, step in document.take_scrolls():
            self._apply_scroll(host, document, surface_id, node_id, step)
        reveal = document.reveal
        if reveal is not None and (document is not self._reveal_doc or reveal[2] != self._reveal_seen):
            self._reveal_doc = document
            self._reveal_seen = reveal[2]
            self._start_reveal(document, reveal[0], reveal[1])

    def _info(self, node_id):
        info = self._local.get("scrollinfo:" + node_id)
        return info if isinstance(info, dict) else None

    def _apply_scroll(self, host, document, surface_id, node_id, step):
        path = document.ancestry(node_id) or []
        for ancestor, _kind in reversed(path):
            info = self._info(ancestor)
            if info is not None:
                self._scroll_container(ancestor, info, step)
                return
        mode = getattr(host, "surface_mode", lambda _sf: None)(surface_id)
        if mode == "screen" and len(path) > 1 and path[1][0] == "main":
            self._scroll_view(step)     # a screen surface's own main
        # an inline surface's main scrolls with the pane and ignores the op

    def _scroll_container(self, node_id, info, step):
        total, shown = int(info.get("total") or 0), max(1, int(info.get("shown") or 1))
        first = int(info.get("first") or 0)
        last = max(0, total - shown)
        moves = {"line-up": first - 1, "line-down": first + 1, "page-up": first - max(1, shown - 1),
                 "page-down": first + max(1, shown - 1), "start": 0, "end": last}
        new = min(max(moves[step], 0), last)
        self._set_first(node_id, info, new, last)

    def _set_first(self, node_id, info, new, last):
        key = "scroll:" + node_id
        if info.get("follow") and new >= last:
            self._local.pop(key, None)      # at the tail: following (again)
        else:
            self._local[key] = new
        self._local["scrollinfo:" + node_id] = dict(info, first=new)

    def _scroll_view(self, step):
        view = self.view
        line, height = view.line_height() or 20, view.viewport_extent()[1]
        current = view.viewport_position()[1]
        bottom = max(0, view.layout_extent()[1] - height)
        moves = {"line-up": current - line, "line-down": current + line, "page-up": current - (height - line),
                 "page-down": current + (height - line), "start": 0, "end": bottom}
        self._pin_id += 1
        self._hold_id += 1
        view.set_viewport_position((0, min(max(moves[step], 0), bottom)), False)

    def _start_reveal(self, document, node_id, where):
        """Scroll the containers the node sits in; remember the main entry to bring into view."""
        self._reveal_entry = None
        path = document.ancestry(node_id)
        if not path:
            return
        names = [item[0] for item in path]
        for position, ancestor in enumerate(names[:-1]):
            info = self._info(ancestor)
            ids = info.get("ids") if info else None
            if ids:
                inner = next((n for n in names[position + 1:] if n in ids), None)
                if inner is not None:
                    self._reveal_in(ancestor, info, ids.index(inner), where)
        if len(names) > 2 and names[1] == "main":
            self._reveal_entry = (names[2], where)

    def _reveal_in(self, node_id, info, index, where):
        total, shown = int(info.get("total") or 0), max(1, int(info.get("shown") or 1))
        first = int(info.get("first") or 0)
        last = max(0, total - shown)
        if where == "start":
            new = index
        elif where == "end":
            new = index - shown + 1
        elif index < first:
            new = index
        elif index >= first + shown:
            new = index - shown + 1
        else:
            new = first
        self._set_first(node_id, info, min(max(new, 0), last), last)

    # ---- what the spec asks the terminal to report ---------------------------------------

    def _is_dark(self):
        try:
            colour = self.view.style().get("background") or "#000000"
            digits = colour.lstrip("#")[:6]
            red, green, blue = (int(digits[i:i + 2], 16) for i in (0, 2, 4))
            return (0.299 * red + 0.587 * green + 0.114 * blue) < 128
        except (ValueError, AttributeError):
            return True

    def _report_environment(self, host):
        """Send ``resize`` (first drawing, a new width or cell size) and ``theme`` (appearance switched)."""
        if not getattr(host, "hello_seen", False):
            return
        surface_id = host.active_surface()
        view = self.view
        cell = {"w": int(view.em_width() or 8), "h": int(view.line_height() or 17)}
        signature = (surface_id, self._eff_cols, cell["w"], cell["h"])
        if surface_id is not None and signature != self._reported:
            self._reported = signature
            host.send_event({"ev": "resize", "sf": surface_id, "cols": self._eff_cols, "cell": cell, "visible": True})
        dark = self._is_dark()
        if dark != self._dark:
            self._dark = dark
            host.send_event({"ev": "theme", "dark": dark})

    def set_visible(self, visible):
        """The tab was hidden or shown again (call from the tab's activated/deactivated handlers)."""
        host = self._host
        surface_id = host.active_surface() if host is not None else None
        if surface_id is not None and getattr(host, "hello_seen", False):
            host.send_event({"ev": "visible", "sf": surface_id, "visible": bool(visible)})
