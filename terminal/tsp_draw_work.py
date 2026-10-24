"""TSP drawers for: spinner, shimmer, elapsed, rate, progress, meter, chart, effort, editor, input, tool, agent, checklist, block, prefs.

DRAWERS maps a kind to a drawer function(node, ctx) -> HTML; tsp_html registers it over its own.

What a drawer reads from the Context besides the documented fields (all optional, read with getattr defaults):

  ctx.reduce_motion   True when the host reports reduced motion: spinners hold their first frame, shimmer is plain text,
                      an indeterminate progress bar is a full-width faint fill, the effort fire does not flicker.
  ctx.stack           the ancestor wire nodes of the node being drawn (outermost first, the node itself excluded).
  ctx.local["focus"]  id of the surface's focused node (editor and input draw the live caret and focus ring there).
  ctx.clock.now       seconds (monotonic); shimmer, rate easing, pulses and flicker are functions of it.
  ctx.unframed        set here while the body of a tool/agent is drawn, so nested cards and tools can draw borderless.
  ctx.caret_blink     opt-in: a focused field's caret blinks (the view must redraw for it).

minihtml has no hover, no tooltips on plain elements, no box-shadow, no gradients and no SVG, so shapes that need
them (rings, bars with rounded ends, hatching, block grids, the effort fire) are drawn as small cached PNG images,
and tooltips are carried as the ``title`` of an inert link.
"""

import bisect
import functools
import html
import math
import re

from . import tsp_html

DRAWERS = {}

INERT_HREF = "tsp:none"        # a link that does nothing when clicked (it only carries a tooltip)
NBSP = "&nbsp;"


# ---- small helpers ----------------------------------------------------------------------------------------

def _num(value, default=0.0):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    return value


def _clamp(value, low=0.0, high=1.0):
    return max(low, min(high, value))


def _now(ctx):
    value = getattr(ctx.clock, "now", None)
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _still(ctx):
    return bool(getattr(ctx, "reduce_motion", False))


def _focus_id(ctx):
    local = ctx.local if isinstance(ctx.local, dict) else {}
    value = local.get("focus")
    return value if value is not None else getattr(ctx, "focus", None)


def _props(node):
    return node.get("p") or {}


def _color(ctx, name, default=None):
    return ctx.theme.color(name, default)


@functools.lru_cache(maxsize=512)
def _rgb(color):
    text = (color or "").lstrip("#")
    if len(text) == 3:
        text = "".join(char * 2 for char in text)
    try:
        return (int(text[0:2], 16), int(text[2:4], 16), int(text[4:6], 16))
    except ValueError:
        return (128, 128, 128)


def _hex(rgb):
    return "#%02x%02x%02x" % tuple(int(max(0, min(255, round(value)))) for value in rgb)


def _mix(first, second, amount):
    """The colour ``amount`` (0..1) of the way from ``first`` to ``second`` (both "#rrggbb")."""
    a, b = _rgb(first), _rgb(second)
    return _hex(tuple(a[i] + (b[i] - a[i]) * amount for i in range(3)))


def _token_color(token, ctx):
    """The colour a span token or program palette token draws in, or None when it names no colour."""
    mapped = tsp_html._SPAN_COLORS.get(token)
    if mapped:
        return ctx.theme.color(mapped)
    if token in ctx.theme.tokens:
        return ctx.theme.tokens[token]
    return None


def _tone_color(tone, ctx, default="accent"):
    if tone in (None, "", "neutral") or not isinstance(tone, str):
        return ctx.theme.color(default)
    name = {"pending": "dim", "user": "accent", "info": "accent"}.get(tone, tone)
    return ctx.theme.color(name, ctx.theme.color(default))


def _esc_attr(text):
    return html.escape(str(text), quote=True)


def _plain_len(inner_html):
    return len(re.sub(r"<[^>]+>|&[#a-zA-Z0-9]+;", "x", inner_html))


def _tip(inner, title, ctx, color=None):
    """``inner`` with a tooltip (an inert link carrying a title)."""
    if not title:
        return inner
    return '<a href="%s" title="%s" style="text-decoration: none; color: %s">%s</a>' % (
        INERT_HREF, _esc_attr(title), color or _color(ctx, "text"), inner)


def _link(ctx, event, inner, title=None, color=None):
    """``inner`` as a click target for ``event`` (without a surface it is just the content)."""
    if event is None or ctx.surface_id is None:
        return _tip(inner, title, ctx, color)
    tip = ' title="%s"' % _esc_attr(title) if title else ""
    return '<a href="%s"%s style="text-decoration: none; color: %s">%s</a>' % (
        tsp_html.event_href(event), tip, color or _color(ctx, "text"), inner)


def _event(ctx, name, node, **extra):
    event = {"ev": name, "sf": ctx.surface_id, "id": node.get("id")}
    event.update(extra)
    return event


def _img(src, width, height):
    return '<img src="%s" width="%d" height="%d">' % (src, int(round(width)), int(round(height)))


def _keep_spaces(markup):
    """Runs of spaces and tabs in the text parts of ``markup`` kept (minihtml folds them otherwise)."""
    out = []
    for part in re.split(r"(<[^>]+>)", markup):
        if part.startswith("<") and part.endswith(">"):
            out.append(part)
        else:
            out.append(re.sub(r" {2,}", lambda m: NBSP * len(m.group(0)), part).replace("\t", NBSP * 4))
    return "".join(out)


def _all_spaces(markup):
    """Every space in the text parts of ``markup`` fixed (white-space: pre)."""
    out = []
    for part in re.split(r"(<[^>]+>)", markup):
        out.append(part if part.startswith("<") and part.endswith(">") else part.replace(" ", NBSP).replace("\t", NBSP * 4))
    return "".join(out)


# ---- text runs, with the shimmer / pulse effects -------------------------------------------------------------

def _style_of(tokens, ctx, ignore_color=False):
    """(css list, colour or None, hide) for a space-separated span token string."""
    style, color, hide = [], None, False
    for token in (tokens or "").split():
        if token == "hide":
            hide = True
        elif token == "strong":
            style.append("font-weight: bold")
        elif token == "em":
            style.append("font-style: italic")
        elif token == "mark":
            style.append("background-color: %s" % _color(ctx, "selectedBg"))
        elif token == "typo":
            style.append("text-decoration: underline")
            color = _color(ctx, "error")
        else:
            found = _token_color(token, ctx)
            if found:
                color = found
    return style, (None if ignore_color else color), hide


def _run(text, tokens, ctx, color=None, ignore_color=False):
    """One run of text in the style of ``tokens``; ``color`` overrides the token colour."""
    style, token_color, hide = _style_of(tokens, ctx, ignore_color)
    if hide or text == "":
        return ""
    chosen = color or token_color
    if chosen:
        style.append("color: %s" % chosen)
    body = tsp_html.esc(text).replace("\n", "<br>")
    return '<span style="%s">%s</span>' % ("; ".join(style), body) if style else body


SHIMMER_PERIOD = 2.25
SHIMMER_STEP = 0.045


def _shimmer_color(index, now, mode, low, mid, high):
    if now is None:
        return low
    t = now - index * SHIMMER_STEP
    if t < 0:
        return low
    cycle = int(t // SHIMMER_PERIOD)
    phase = t - cycle * SHIMMER_PERIOD
    if mode == "kitt" and cycle % 2 == 1:
        phase = SHIMMER_PERIOD - phase
    p = phase / SHIMMER_PERIOD

    def stepped(amount):
        return int(amount * 7) / 7.0

    if p < 0.14:
        return _mix(low, high, stepped(p / 0.14))
    if p < 0.28:
        return _mix(high, mid, stepped((p - 0.14) / 0.14))
    if p < 0.42:
        return _mix(mid, low, stepped((p - 0.28) / 0.14))
    return low


def _shimmer_palette(ctx, palette):
    low, mid, high = _color(ctx, "dim"), _color(ctx, "text"), _color(ctx, "muted")
    if isinstance(palette, dict):
        for key in ("low", "mid", "high"):
            value = palette.get(key)
            if isinstance(value, str) and value.split():
                found = _token_color(value.split()[0], ctx)
                if found:
                    if key == "low":
                        low = found
                    elif key == "mid":
                        mid = found
                    else:
                        high = found
    return low, mid, high


def _shimmer(text, tokens, ctx, mode="classic", palette=None, offset=0):
    """``text`` with a light sweeping across it, character by character (a function of the clock)."""
    if _still(ctx):
        return _run(text, tokens, ctx, ignore_color=True)
    ctx.animated = True
    style, _color_unused, hide = _style_of(tokens, ctx)
    if hide or text == "":
        return ""
    low, mid, high = _shimmer_palette(ctx, palette)
    now = _now(ctx)
    pieces, run, run_color = [], [], None

    def flush():
        if not run:
            return
        body = "".join(run).replace("\n", "<br>")
        css = list(style) + ["color: %s" % run_color]
        pieces.append('<span style="%s">%s</span>' % ("; ".join(css), body))

    for index, char in enumerate(text):
        if char == "\n":
            run.append("\n")
            continue
        shown = _shimmer_color(index + offset, now, mode, low, mid, high)
        if shown != run_color:
            flush()
            run, run_color = [], shown
        run.append(tsp_html.esc(char).replace(" ", NBSP))
    flush()
    return "".join(pieces)


def _pulse_color(ctx, base):
    """``base`` breathing towards the dim colour (a function of the clock; steady without one)."""
    now = _now(ctx)
    if now is None or _still(ctx):
        return base
    ctx.animated = True
    level = 0.5 * (1 - math.cos(2 * math.pi * now / 1.2))
    return _mix(_color(ctx, "dim"), base, 0.35 + 0.65 * level)


def _spans(value, ctx, base=""):
    """A str or span list as inline HTML; spans with ``fx`` shimmer or pulse get the effect."""
    if isinstance(value, list):
        out, offset = [], 0
        for span in value:
            if not isinstance(span, dict):
                continue
            text = span.get("t", "") or ""
            tokens = ("%s %s" % (base, span.get("s") or "")).strip()
            fx = span.get("fx")
            if fx == "shimmer":
                out.append(_shimmer(text, tokens, ctx, offset=offset))
            elif fx == "pulse":
                _style, token_color, _hide = _style_of(tokens, ctx)
                out.append(_run(text, tokens, ctx, color=_pulse_color(ctx, token_color or _color(ctx, "text"))))
            else:
                out.append(_run(text, tokens, ctx))
            offset += len(text)
        return "".join(out)
    if isinstance(value, str):
        return _run(value, base, ctx)
    return ""


# ---- tiny rasterizer: rings, bars and grids as cached PNGs ---------------------------------------------------

def _render_png(width, height, sampler, scale=2, samples=3):
    """A PNG data URL of ``sampler(x, y) -> (r, g, b, alpha 0..1) | None`` over a width x height CSS px area."""
    full_w, full_h = int(round(width * scale)), int(round(height * scale))
    count = samples * samples

    def pixel(px, py):
        total = red = green = blue = 0.0
        for j in range(samples):
            y = (py + (j + 0.5) / samples) / scale
            for i in range(samples):
                x = (px + (i + 0.5) / samples) / scale
                got = sampler(x, y)
                if got:
                    alpha = got[3]
                    total += alpha
                    red += got[0] * alpha
                    green += got[1] * alpha
                    blue += got[2] * alpha
        if total <= 0:
            return (0, 0, 0, 0)
        return (int(red / total), int(green / total), int(blue / total), int(255 * total / count))

    return tsp_html._png_from_pixels(full_w, full_h, pixel)


def _in_round_rect(x, y, x0, y0, x1, y1, radius):
    if x < x0 or x > x1 or y < y0 or y > y1:
        return False
    radius = min(radius, (x1 - x0) / 2.0, (y1 - y0) / 2.0)
    cx = min(max(x, x0 + radius), x1 - radius)
    cy = min(max(y, y0 + radius), y1 - radius)
    return (x - cx) ** 2 + (y - cy) ** 2 <= radius * radius


def _over(top, bottom):
    """``top`` = (r, g, b, alpha) laid over the opaque colour ``bottom`` = (r, g, b), as an opaque colour."""
    alpha = top[3]
    if alpha >= 1:
        return top
    return (top[0] * alpha + bottom[0] * (1 - alpha), top[1] * alpha + bottom[1] * (1 - alpha),
            top[2] * alpha + bottom[2] * (1 - alpha), 1.0)


@functools.lru_cache(maxsize=512)
def _bar_png(width, height, track, fills, marks, unknown, pad):
    """A rounded bar.  ``fills``: ((from, to, rgb, hatch, alpha), ...) as fractions of the width;
    ``marks``: ((at, rgb, icon), ...); ``unknown`` a dashed empty track; ``pad`` room above and below for marks."""
    radius = height / 2.0
    icon_centers = [min(max(at, 0.0), 1.0) * width for at, _rgb_unused, icon in marks if icon]

    def sampler(x, y):
        ty = y - pad
        for at, rgb, icon in marks:
            mx = min(max(at, 0.0), 1.0) * width
            if icon:
                if (x - mx) ** 2 + (y - (pad + height / 2.0)) ** 2 <= 4.5 ** 2:
                    return rgb + (1.0,)
            elif abs(x - mx) <= 1.0 and 0.5 <= y <= height + 2 * pad - 0.5:
                return rgb + (1.0,)
        if ty < 0 or ty > height:
            return None
        for center in icon_centers:
            if abs(x - center) < 7.0:
                return None
        if not _in_round_rect(x, ty, 0, 0, width, height, radius):
            return None
        if unknown:
            return None if int(x // 4) % 2 else track + (1.0,)
        top = None
        for start, end, rgb, hatch, alpha in fills:
            if start * width <= x < end * width or (end >= 1.0 and x <= width and start * width <= x):
                shade = alpha * 0.5 if hatch and (int(x + ty) // 2) % 2 == 0 else alpha
                top = rgb + (shade,)
        if top is None:
            return track + (1.0,)
        return _over(top, track)

    return _render_png(width, height + 2 * pad, sampler)


def _bar_img(width, height, track, fills, marks=(), unknown=False, pad=0):
    return _img(_bar_png(width, height, track, tuple(fills), tuple(marks), unknown, pad), width, height + 2 * pad)


@functools.lru_cache(maxsize=512)
def _ring_png(size, track, arcs, marks, dashed, pad):
    """A ring filling clockwise from 12 o'clock.  ``arcs``: ((from, to, rgb, hatch), ...) as fractions of a turn;
    ``marks``: ((at, rgb), ...) short radial ticks."""
    unit = size / 16.0
    center = size / 2.0 + pad
    outer, inner = 7.7 * unit, 5.3 * unit

    def sampler(x, y):
        dx, dy = x - center, y - center
        d = math.hypot(dx, dy)
        turn = (math.atan2(dx, -dy) % (2 * math.pi)) / (2 * math.pi)
        for at, rgb in marks:
            gap = abs(turn - min(max(at, 0.0), 1.0))
            gap = min(gap, 1 - gap) * 2 * math.pi * max(d, 0.001)
            if inner - 0.3 * unit <= d <= outer + 0.9 * unit and gap <= 0.7 * unit:
                return rgb + (1.0,)
        if d < inner or d > outer:
            return None
        if dashed:
            return track + (1.0,) if int(turn * 16) % 2 == 0 else None
        for start, end, rgb, hatch in arcs:
            if start <= turn < end or (end >= 1.0 and turn >= start):
                return rgb + (0.5,) if hatch and int(turn * 40) % 2 == 0 else rgb + (1.0,)
        return track + (1.0,)

    return _render_png(size + 2 * pad, size + 2 * pad, sampler)


def _ring_img(size, track, arcs=(), marks=(), dashed=False):
    pad = 1 if marks else 0
    return _img(_ring_png(size, track, tuple(arcs), tuple(marks), dashed, pad), size + 2 * pad, size + 2 * pad)


@functools.lru_cache(maxsize=256)
def _orbit_png(size, rgb, step):
    """A faint ring with a dot on it at one of 12 positions."""
    center = size / 2.0
    radius = size * 0.36
    angle = step * math.pi / 6.0
    dot = (center + radius * math.sin(angle), center - radius * math.cos(angle))

    def sampler(x, y):
        if (x - dot[0]) ** 2 + (y - dot[1]) ** 2 <= (size * 0.13) ** 2:
            return rgb + (1.0,)
        if abs(math.hypot(x - center, y - center) - radius) <= size * 0.045:
            return rgb + (0.3,)
        return None

    return _render_png(size, size, sampler)


@functools.lru_cache(maxsize=64)
def _cells_png(columns, cell_w, cell_h, gap, cells):
    """A grid of rounded cells, row by row.  ``cells``: ((rgb, hatch) or None for an empty cell, ...)."""
    rows = (len(cells) + columns - 1) // columns
    pitch_x, pitch_y = cell_w + gap, cell_h + gap
    width = columns * pitch_x - gap
    height = rows * pitch_y - gap

    def sampler(x, y):
        column, row = int(x // pitch_x), int(y // pitch_y)
        lx, ly = x - column * pitch_x, y - row * pitch_y
        index = row * columns + column
        if lx > cell_w or ly > cell_h or column >= columns or index >= len(cells):
            return None
        if not _in_round_rect(lx, ly, 0, 0, cell_w, cell_h, 1.5):
            return None
        cell = cells[index]
        if cell is None:
            return None
        return cell[0] + (0.5 if cell[1] and (int(lx + ly) // 2) % 2 == 0 else 1.0,)

    return _render_png(width, height, sampler, scale=2, samples=2)


@functools.lru_cache(maxsize=256)
def _pixel_png(rgb, alpha):
    return tsp_html._png_from_pixels(1, 1, lambda x, y: tuple(int(v) for v in rgb) + (int(255 * alpha),))


def _block_img(rgb, alpha, width, height):
    return _img(_pixel_png(rgb, alpha), width, height)


# ---- spinner -----------------------------------------------------------------------------------------------------

_BRAILLE = "\u280b\u2819\u2839\u2838\u283c\u2834\u2826\u2827\u2807\u280f"
_STARBURST = "\u00b7\u2722\u2733\u2736\u273b\u273d\u273b\u2736\u2733\u2722"
_SPINNER_STYLES = ("braille", "dots", "starburst", "orbit")


def _draw_spinner(node, ctx):
    props = _props(node)
    style = props.get("style") if props.get("style") in _SPINNER_STYLES else "braille"
    color = _tone_color(props.get("tone"), ctx)
    still = _still(ctx)
    now = None if still else _now(ctx)
    if not still:
        ctx.animated = True
    if style in ("braille", "starburst"):
        frames, period = (_BRAILLE, 1.0) if style == "braille" else (_STARBURST, 1.2)
        index = int((now % period) / period * len(frames)) if now is not None else 0
        indicator = '<span style="color: %s">%s</span>' % (color, frames[index])
    elif style == "dots":
        dim = _color(ctx, "pageBg")
        dots = []
        for delay in (0.0, 0.16, 0.32):
            level = 1.0
            if now is not None:
                phase = ((now - delay) % 1.2) / 1.2
                level = 0.3 + 0.7 * (1 - abs(2 * phase - 1))
            dots.append('<span style="color: %s">&bull;</span>' % _mix(dim, color, level))
        indicator = "".join(dots)
    else:
        step = int(((now % 0.9) / 0.9) * 12) if now is not None else 0
        indicator = _img(_orbit_png(12, _rgb(color), step), 12, 12)
    label = _spans(props.get("label"), ctx)
    return indicator + (NBSP + label if label else "")


# ---- shimmer -----------------------------------------------------------------------------------------------------

def _draw_shimmer(node, ctx):
    props = _props(node)
    mode = "kitt" if props.get("mode") == "kitt" else "classic"
    spans = props.get("spans")
    if isinstance(spans, list):
        parts, offset = [], 0
        for span in spans:
            if not isinstance(span, dict):
                continue
            text = span.get("t", "") or ""
            parts.append(_shimmer(text, span.get("s") or "", ctx, mode, props.get("palette"), offset))
            offset += len(text)
        return "".join(parts)
    return _shimmer(props.get("text") or "", "", ctx, mode, props.get("palette"))


# ---- elapsed, rate -------------------------------------------------------------------------------------------------

def _fmt_time(milliseconds, fmt="short"):
    ms = max(0, int(milliseconds))
    if fmt == "clock":
        seconds = ms // 1000
        if seconds < 3600:
            return "%d:%02d" % (seconds // 60, seconds % 60)
        return "%d:%02d:%02d" % (seconds // 3600, seconds % 3600 // 60, seconds % 60)
    if ms < 60000:
        return "%d.%ds" % (ms // 1000, ms % 1000 // 100)
    seconds = ms // 1000
    if seconds < 3600:
        return "%dm %02ds" % (seconds // 60, seconds % 60)
    return "%dh %02dm" % (seconds // 3600, seconds % 3600 // 60)


def _draw_elapsed(node, ctx):
    props = _props(node)
    fmt = "clock" if props.get("format") == "clock" else "short"
    stopped = props.get("stopped")
    if isinstance(stopped, (int, float)) and not isinstance(stopped, bool):
        shown = stopped
    else:
        age = _num(props.get("age"), 0)
        elapsed = ctx.clock.elapsed_ms(node.get("id"), age)
        if age < 0:
            shown = max(0, -elapsed)         # a countdown: what is left of -age
            if shown > 0:
                ctx.animated = True
        else:
            shown = elapsed
            ctx.animated = True
    return '<span style="color: %s">%s</span>' % (_color(ctx, "dim"), _fmt_time(shown, fmt))


_RATE_STATE = {}
_RATE_EASE = 0.42


def _rate_value(ctx, key, target):
    """The value a rate shows now: it eases from the last shown value to the new one over 420 ms (ease-out cubic)."""
    now = _now(ctx)
    if now is None:
        return target
    if len(_RATE_STATE) > 4000:
        _RATE_STATE.clear()
    state = _RATE_STATE.get(key)
    if state is None:
        _RATE_STATE[key] = [target, target, now]
        return target

    def eased():
        progress = (now - state[2]) / _RATE_EASE
        if progress >= 1:
            return state[1]
        return state[0] + (state[1] - state[0]) * (1 - (1 - progress) ** 3)

    if state[1] != target:
        state[:] = [eased(), target, now]
    if (now - state[2]) / _RATE_EASE < 1:
        ctx.animated = True
    return eased()


def _draw_rate(node, ctx):
    props = _props(node)
    target = _num(props.get("value"), 0)
    value = _rate_value(ctx, (ctx.surface_id, node.get("id")), target)
    text = "%.1f" % value if abs(value) < 100 else "%.0f" % value
    unit = props.get("unit")
    if unit:
        text += " " + str(unit)
    return '<span style="color: %s">%s</span>' % (_color(ctx, "muted"), tsp_html.esc(text))


# ---- progress ------------------------------------------------------------------------------------------------------

PROGRESS_WIDTH = 160


def _draw_progress(node, ctx):
    props = _props(node)
    value = props.get("value")
    fill = _rgb(_tone_color(props.get("tone"), ctx))
    track = _rgb(_color(ctx, "border"))
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        share = round(_clamp(value) * 100) / 100.0
        fills = ((0.0, share, fill, False, 1.0),) if share > 0 else ()
    elif _still(ctx):
        fills = ((0.0, 1.0, fill, False, 0.35),)
    else:
        now = _now(ctx)
        step = int(((now % 1.3) / 1.3) * 16) if now is not None else 3
        left = -0.3 + 1.32 * step / 16.0
        fills = ((left, left + 0.3, fill, False, 1.0),)
        ctx.animated = True
    out = _bar_img(PROGRESS_WIDTH, 4, track, fills)
    label = _spans(props.get("label"), ctx)
    if label:
        out += NBSP + '<span style="color: %s">%s</span>' % (_color(ctx, "muted"), label)
    return "<div>%s</div>" % out


# ---- meter ---------------------------------------------------------------------------------------------------------

_METER_BAR_WIDTH = 120
_METER_SIZES = {"sm": 0, "md": 1, "lg": 2}
_METER_HEIGHT = (4, 6, 10)
_METER_RING = (12, 16, 24)
_METER_ROWS = (2, 5, 10)


def _meter_parts(props, ctx, base_rgb):
    """[(from, to, rgb, hatch, is_gap)] for the stacked parts, clamped so they never pass the whole."""
    out, used = [], 0.0
    for part in props.get("parts") or []:
        if not isinstance(part, dict):
            continue
        share = min(max(_num(part.get("value"), 0.0), 0.0), 1.0 - used)
        token = part.get("token")
        gap = token == "track"
        rgb = base_rgb
        if isinstance(token, str) and not gap:
            found = _token_color(token, ctx)
            if found:
                rgb = _rgb(found)
        out.append((used, used + share, rgb, bool(part.get("hatch")), gap))
        used += share
    return out


def _meter_title(props):
    if props.get("title"):
        return props["title"]
    labelled = [p for p in props.get("parts") or [] if isinstance(p, dict) and p.get("label")]
    if not labelled:
        return None
    return " \u00b7 ".join("%s %d%%" % (p["label"], round(_clamp(_num(p.get("value"), 0.0)) * 100)) for p in labelled)


def _draw_meter(node, ctx):
    props = _props(node)
    style = props.get("style") if props.get("style") in ("bar", "ring", "blocks") else "bar"
    size = _METER_SIZES.get(props.get("size"), 0)
    value = props.get("value")
    has_value = isinstance(value, (int, float)) and not isinstance(value, bool)
    value = _clamp(value) if has_value else None
    thresholds = props.get("thresholds") if isinstance(props.get("thresholds"), dict) else {}
    level = None
    if value is not None:
        if isinstance(thresholds.get("bad"), (int, float)) and value >= thresholds["bad"]:
            level = "bad"
        elif isinstance(thresholds.get("warn"), (int, float)) and value >= thresholds["warn"]:
            level = "warn"
    tone = props.get("tone")
    if tone not in (None, "", "neutral"):
        base = _tone_color(tone, ctx)
    elif level == "bad":
        base = _color(ctx, "error")
    elif level == "warn":
        base = _color(ctx, "warning")
    else:
        base = _color(ctx, "accent")
    base_rgb = _rgb(base)
    track = _rgb(_color(ctx, "border"))
    has_parts = isinstance(props.get("parts"), list) and bool(props.get("parts"))
    parts = _meter_parts(props, ctx, base_rgb) if has_parts else []
    marks = []
    for mark in props.get("marks") or []:
        if isinstance(mark, dict) and isinstance(mark.get("at"), (int, float)) and not isinstance(mark.get("at"), bool):
            rgb = _rgb(_tone_color(mark.get("tone"), ctx, "text"))
            marks.append((_clamp(mark["at"]), rgb, bool(mark.get("icon"))))
    unknown = value is None and not has_parts
    if style == "bar":
        height = _METER_HEIGHT[size]
        if has_parts:
            fills = tuple((a, b, rgb, hatch, 1.0) for a, b, rgb, hatch, gap in parts if not gap and b > a)
        elif value is not None and value > 0:
            fills = ((0.0, value, base_rgb, False, 1.0),)
        else:
            fills = ()
        pad = 5 if any(m[2] for m in marks) else (2 if marks else 0)
        shape = _bar_img(_METER_BAR_WIDTH, height, track, fills, marks, unknown, pad)
    elif style == "ring":
        diameter = _METER_RING[size]
        if has_parts:
            arcs = tuple((a, b, rgb, hatch) for a, b, rgb, hatch, gap in parts if not gap and b > a)
        elif value is not None and value > 0:
            arcs = ((0.0, value, base_rgb, False),)
        else:
            arcs = ()
        shape = _ring_img(diameter, track, arcs, tuple((at, rgb) for at, rgb, icon in marks), unknown)
    else:
        steps = props.get("steps")
        stepped = isinstance(steps, int) and not isinstance(steps, bool) and 1 <= steps <= 20
        total = steps if stepped else 10 * _METER_ROWS[size]
        cells = [None] * total
        if has_parts:
            done, cumulative = 0, 0.0
            for a, b, rgb, hatch, gap in parts:
                cumulative = b
                upto = int(round(cumulative * total))
                for index in range(done, min(upto, total)):
                    cells[index] = None if gap else (rgb, hatch)
                done = max(done, upto)
        elif value is not None:
            for index in range(int(round(value * total))):
                cells[index] = (base_rgb, False)
        off = (track, False)
        grid = tuple(cell if cell is not None else None for cell in cells)
        if stepped:
            shown = tuple(cell if cell is not None else off for cell in grid)
            src = _cells_png(total, 6, 14, 3, shown)
            shape = _img(src, total * 9 - 3, 14)
        else:
            shown = tuple(cell if cell is not None else off for cell in grid)
            src = _cells_png(10, 8, 8, 2, shown)
            rows = total // 10
            shape = _img(src, 98, rows * 10 - 2)
    out = shape
    label = _spans(props.get("label"), ctx)
    if label:
        out += NBSP + '<span style="color: %s">%s</span>' % (_color(ctx, "muted"), label)
    total_text = _spans(props.get("total"), ctx)
    if total_text:
        out += NBSP + '<span style="color: %s">%s</span>' % (_color(ctx, "dim"), total_text)
    return "<span>%s</span>" % _tip(out, _meter_title(props), ctx)


# ---- chart ---------------------------------------------------------------------------------------------------------

_CHART_CELL = {"sm": 8, "md": 11, "lg": 13}
_CHART_LINES = {"sm": 2, "md": 4, "lg": 8}
_LINE_PX = 14
_HEAT_ALPHA = (0.20, 0.40, 0.65, 1.0)


def _chart_color(props, ctx):
    token = props.get("token")
    found = _token_color(token, ctx) if isinstance(token, str) and token else None
    return _rgb(found or _color(ctx, "accent"))


def _heat_level(value):
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    if value <= 0:
        return 0
    if value <= 0.25:
        return 1
    if value <= 0.5:
        return 2
    if value <= 0.75:
        return 3
    return 4


def _spacer(width, ctx):
    return _img(_pixel_png((0, 0, 0), 0.0), max(1, width), 1)


def _chart_heatmap(props, ctx):
    cells = [row for row in props.get("cells") or [] if isinstance(row, list)]
    size = _CHART_CELL.get(props.get("size"), 11)
    color = _chart_color(props, ctx)
    empty = _rgb(_color(ctx, "borderMuted"))
    tips = props.get("tips") if isinstance(props.get("tips"), list) else []
    labels = props.get("rows") if isinstance(props.get("rows"), list) else []
    label_width = max([len(str(label)) for label in labels] or [0])
    pitch = size + 3
    left = label_width * 5 + (4 if label_width else 0)
    lines = []
    month_labels = []
    for entry in props.get("cols") or []:
        if isinstance(entry, dict) and isinstance(entry.get("at"), int) and not isinstance(entry.get("at"), bool) \
                and entry.get("label") not in (None, ""):
            month_labels.append((entry["at"], str(entry["label"])))
    if month_labels:
        pieces, used = [], 0
        for at, label in sorted(month_labels):
            target = left + at * pitch
            if target > used:
                pieces.append(_spacer(target - used, ctx))
                used = target
            pieces.append(tsp_html.esc(label))
            used += len(label) * 5 + 4
            pieces.append(NBSP)
        lines.append('<div style="font-size: 0.7rem; color: %s">%s</div>' % (_color(ctx, "dim"), "".join(pieces)))
    for row_index, row in enumerate(cells):
        out = []
        if label_width:
            label = str(labels[row_index]) if row_index < len(labels) and labels[row_index] is not None else ""
            out.append('<tt style="color: %s">%s</tt>&nbsp;' % (_color(ctx, "dim"), tsp_html.esc(label.ljust(label_width)).replace(" ", NBSP)))
        row_tips = tips[row_index] if row_index < len(tips) and isinstance(tips[row_index], list) else []
        for column, value in enumerate(row):
            level = _heat_level(value)
            if level is None:
                cell = _block_img((0, 0, 0), 0.0, size, size)
            elif level == 0:
                cell = _block_img(empty, 1.0, size, size)
            else:
                cell = _block_img(color, _HEAT_ALPHA[level - 1], size, size)
            tip = row_tips[column] if column < len(row_tips) and isinstance(row_tips[column], str) and row_tips[column] else None
            out.append(_tip(cell, tip, ctx) if tip else cell)
            out.append("&#8201;")
        lines.append('<div style="font-size: 0.5rem">%s</div>' % "".join(out))
    return "".join(lines)


def _chart_domain(props, values, marks):
    """The (low, high) value domain of a bars or spark chart: ``min`` (default 0) to ``max`` (default the
    largest series or mark value). High is kept above low."""
    low = props.get("min")
    low = low if isinstance(low, (int, float)) and not isinstance(low, bool) else 0
    high = props.get("max")
    if not isinstance(high, (int, float)) or isinstance(high, bool):
        high = max(values + [m[0] for m in marks] + [0])
    return low, (high if high > low else low + 1)


def _chart_marks(props):
    """The chart's dashed reference lines as (value, tone, title)."""
    return [(mark["value"], mark.get("tone"), mark.get("title")) for mark in props.get("marks") or []
            if isinstance(mark, dict) and isinstance(mark.get("value"), (int, float))
            and not isinstance(mark.get("value"), bool)]


@functools.lru_cache(maxsize=512)
def _chart_column_png(width, height, bar, fill, lines, phase):
    """One chart column ``height`` tall: a bar ``bar`` pixels high from the bottom, with a dashed reference
    line (row from the bottom, colour) across it; ``phase`` staggers the dashes from column to column."""
    rows = {height - 1 - row: rgb for row, rgb in lines}

    def pixel(x, y):
        if y in rows and (x + phase) % 4 < 2:
            return tuple(rows[y]) + (255,)
        if y >= height - bar:
            return tuple(fill) + (255,)
        return (0, 0, 0, 0)
    return tsp_html._png_from_pixels(width, height, pixel)


def _chart_bars(props, ctx, spark):
    series = [s for s in props.get("series") or [] if isinstance(s, dict)]
    if not series:
        return ""
    color = _chart_color(props, ctx)
    height = _LINE_PX if spark else _CHART_LINES.get(props.get("size"), 4) * _LINE_PX
    available = min(_avail(ctx) * getattr(ctx, "cell_px", 7), 300 if spark else 480)
    width = max(1, min(4, available // len(series) - 1)) if spark else max(2, min(24, available // len(series) - 3))
    marks = _chart_marks(props)
    low, high = _chart_domain(props, [_num(s.get("value"), 0) for s in series], marks)

    def fraction(value):
        return (_clamp(value, low, high) - low) / (high - low)
    lines = tuple((min(height - 1, int(round(height * fraction(value)))), _rgb(_tone_color(tone, ctx, "text")))
                  for value, tone, _title in marks)
    out = []
    for index, entry in enumerate(series):
        frac = fraction(_num(entry.get("value"), 0))
        bar_height = max(1, int(round(height * frac))) if frac > 0 else 1
        tone = entry.get("tone")
        fill = _rgb(_tone_color(tone, ctx)) if isinstance(tone, str) and tone not in ("", "neutral") else color
        if lines:
            piece = _img(_chart_column_png(width, height, bar_height, fill, lines, index * width % 4), width, height)
        else:
            piece = _block_img(fill, 1.0, width, bar_height)
        tip = entry.get("title") or entry.get("label")
        out.append(_tip(piece, str(tip), ctx) if tip else piece)
        out.append("&#8201;")
    body = '<div style="font-size: 0.5rem">%s</div>' % "".join(out)
    notes = [str(title) for _value, _tone, title in marks if title]
    if notes:
        body += '<div style="color: %s; font-size: 0.7rem">%s</div>' % (
            _color(ctx, "dim"), tsp_html.esc(" · ".join(notes)))
    return body


def _draw_chart(node, ctx):
    props = _props(node)
    kind = props.get("kind")
    if kind == "bars":
        body = _chart_bars(props, ctx, False)
    elif kind == "spark":
        body = _chart_bars(props, ctx, True)
    else:
        body = _chart_heatmap(props, ctx)
    summary = _spans(props.get("summary"), ctx)
    if summary:
        body += '<div style="color: %s">%s</div>' % (_color(ctx, "muted"), summary)
    return '<div style="margin: 4px 0">%s</div>' % body


# ---- effort ----------------------------------------------------------------------------------------------------------

_EFFORT_RUNGS = {"off": 0, "minimal": 1, "low": 2, "medium": 3, "high": 4, "xhigh": 5, "max": 6}
_EFFORT_COLORS = {"minimal": ("thinkingMinimal", "muted"), "low": ("thinkingLow", "info"),
                  "medium": ("thinkingMedium", "accent"), "high": ("thinkingHigh", "warning"),
                  "xhigh": ("thinkingXhigh", "error"), "max": ("thinkingMax", "error")}
_FIRE_HIGH = "#ffc24a"
_FLAMES = ((3.0, 4.0, 2.6), (2.4, 3.6, 3.2), (3.2, 3.2, 2.4), (2.6, 4.2, 3.0))


@functools.lru_cache(maxsize=64)
def _fire_png(fire, high, flame, ember):
    """The max effort glyph: a closed ring in the fire gradient with a crown of flames and an ember inside."""
    cx, cy, outer, inner = 7.0, 11.0, 6.4, 4.4
    ring_top = cy - outer
    tips = [(3.6, ring_top - _FLAMES[flame][0]), (7.0, ring_top - _FLAMES[flame][1]), (10.4, ring_top - _FLAMES[flame][2])]
    ember_h = 1.5 + 0.3 * ember

    def sampler(x, y):
        d = math.hypot(x - cx, y - cy)
        if (x - cx) ** 2 / 1.2 ** 2 + (y - 11.4) ** 2 / ember_h ** 2 <= 1:
            return _rgb(high) + (1.0,)
        if inner <= d <= outer:
            return _rgb(_mix(fire, high, _clamp((y - 4.0) / 14.0))) + (1.0,)
        base = ring_top + 1.0
        for tip_x, tip_y in tips:
            if tip_y <= y <= base:
                half = 1.7 * (y - tip_y) / max(base - tip_y, 0.1)
                if abs(x - tip_x) <= half:
                    return _rgb(_mix(high, fire, _clamp((y - tip_y) / 5.0))) + (1.0,)
        return None

    return _render_png(14, 18, sampler)


def _draw_effort(node, ctx):
    level = _props(node).get("level")
    rung = _EFFORT_RUNGS.get(level) if isinstance(level, str) else None
    track = _rgb(_color(ctx, "border"))
    if rung is None:
        return _ring_img(14, track, (), (), True)
    if level == "max":
        palette, fallback = _EFFORT_COLORS["max"]
        fire = _mix(ctx.theme.tokens.get(palette) or _token_color(fallback, ctx) or _color(ctx, "error"), "#ff8a00", 0.5)
        now = _now(ctx)
        flame, ember = 0, 0
        if now is not None and not _still(ctx):
            ctx.animated = True
            flame = int(now / 0.35) % 4
            step = int(now * 10) % 9
            ember = min(step, 8 - step)
        return _img(_fire_png(fire, _FIRE_HIGH, flame, ember), 14, 18)
    if rung == 0:
        return _ring_img(14, track)
    palette, fallback = _EFFORT_COLORS[level]
    arc = _rgb(ctx.theme.tokens.get(palette) or _token_color(fallback, ctx) or _color(ctx, "accent"))
    return _ring_img(14, track, ((0.0, rung / 6.0, arc, False),))


# ---- editor, input ---------------------------------------------------------------------------------------------------

_KEYWORDS = frozenset((
    "if else elif for while return def class import from as with try except finally raise yield lambda pass break continue "
    "function var let const new this async await switch case default do in of is not and or true false null nil none "
    "fn pub struct enum impl trait use mod match loop mut self static void int char bool string type interface extends "
    "package public private protected final local then end begin select where insert update delete create table").split())
_HIGHLIGHT = re.compile(r"""(?P<comment>\#[^\n]*|//[^\n]*|--[^\n]*|/\*.*?\*/)|(?P<string>"(?:\\.|[^"\\\n])*"?|'(?:\\.|[^'\\\n])*'?|`[^`]*`?)"""
                        r"""|(?P<number>\b\d[\d_.]*\b)|(?P<word>[A-Za-z_][A-Za-z0-9_]*)""", re.S)
_HIGHLIGHT_TOKENS = {"comment": "dim", "string": "success", "number": "num"}


def _offset_converter(text):
    """A function from a UTF-16 offset to a character index of ``text`` (a split surrogate pair rounds down)."""
    if all(ord(char) <= 0xFFFF for char in text):
        size = len(text)
        return lambda offset: max(0, min(size, offset))
    starts, position = [], 0
    for char in text:
        starts.append(position)
        position += 2 if ord(char) > 0xFFFF else 1
    total = position

    def convert(offset):
        if offset >= total:
            return len(text)
        if offset <= 0:
            return 0
        return bisect.bisect_right(starts, offset) - 1

    return convert


def _int_or_none(value):
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _highlight_tokens(text):
    """One span token (or None) per character for the code in ``text``: a small generic highlighter."""
    tokens = [None] * len(text)
    for match in _HIGHLIGHT.finditer(text):
        kind = match.lastgroup
        if kind == "word":
            if match.group(0).lower() not in _KEYWORDS:
                continue
            token = "key"
        else:
            token = _HIGHLIGHT_TOKENS[kind]
        for index in range(match.start(), match.end()):
            tokens[index] = token
    return tokens


def _decorated(entry, ctx):
    if entry is None or entry == "":
        return "", 0
    return _spans(entry, ctx), len(tsp_html.plain_text(entry))


def _caret_html(ctx, empty, focused, readonly):
    if readonly and not focused:
        return ""
    color = _color(ctx, "accent")
    if not focused:
        color = _mix(color, _color(ctx, "pageBg"), 0.7)        # dimmed, as the spec's opacity 0.3
    elif getattr(ctx, "caret_blink", False) and not _still(ctx):
        now = _now(ctx)
        if now is not None:
            ctx.animated = True
            if int(now * 2) % 2:
                color = _mix(color, _color(ctx, "pageBg"), 0.85)
    return '<span style="color: %s">%s</span>' % (color, "&#9608;" if empty else "&#9615;")


def _in_omp_composer(node, ctx):
    role = str(_props(node).get("role") or "")
    if role.startswith("omp.editor"):
        return True
    return any(str((_props(item)).get("role") or "").startswith("omp.editor") for item in getattr(ctx, "stack", None) or [])


def _mode_chip(props, ctx, omp):
    mode = props.get("mode")
    if not isinstance(mode, str) or not mode:
        return ""
    lowered = mode.lower()
    if omp:
        if lowered == "insert":
            return ""
        tone = "warning" if lowered == "visual" else "accent"
    else:
        tone = "accent" if lowered == "insert" else "muted"
    return tsp_html.chip(mode, ctx, tone) + NBSP


def _draw_editor(node, ctx):
    """A text field whose text the program owns: ``editor`` (multi-line) and ``input`` (one line).

    The text is drawn with the program's ``decor`` ranges (a span token string, with an optional ``fx`` of
    ``shimmer`` or ``pulse``; ``hide`` leaves the characters out), the selection ``anchor``..``cursor``, the caret,
    the inline completion ``ghost`` right after the caret, the ``placeholder`` of an empty field, the ``prompt``, a
    ``mode`` chip, ``lang`` code (mono face and a small highlighter unless ``highlight`` is false), per-line
    ``gutter`` and ``virt`` spans, and a ``maxLines`` window with ``topline`` / ``followCursor`` scrolling.
    Offsets are UTF-16 code units, as in the spec.  The only event is ``focus`` (a click in an editable field that
    does not hold the focus); the focused field draws the live caret and an accent ring.
    """
    props = _props(node)
    node_id = node.get("id")
    is_input = node.get("k") == "input"
    text = props.get("text") if isinstance(props.get("text"), str) else ""
    convert = _offset_converter(text)
    raw_cursor, raw_anchor = _int_or_none(props.get("cursor")), _int_or_none(props.get("anchor"))
    cursor = convert(raw_cursor) if raw_cursor is not None else len(text)
    anchor = convert(raw_anchor) if raw_anchor is not None else None
    selected = (min(anchor, cursor), max(anchor, cursor)) if anchor is not None and anchor != cursor else None
    focused = _focus_id(ctx) == node_id
    readonly = bool(props.get("readonly"))
    nowrap = is_input or bool(props.get("nowrap"))
    code = bool(props.get("lang"))
    omp = _in_omp_composer(node, ctx)

    decor = []
    for entry in props.get("decor") or []:
        if not isinstance(entry, dict):
            continue
        start, end = _int_or_none(entry.get("from")), _int_or_none(entry.get("to"))
        if start is None or end is None:
            continue
        start, end = convert(start), convert(end)
        if start < end:
            decor.append((start, end, str(entry.get("s") or ""), entry.get("fx")))

    tokens_by_char = _highlight_tokens(text) if code and props.get("highlight") is not False and len(text) < 20000 else None
    cuts = {0, len(text), cursor}
    for start, end, _s, _fx in decor:
        cuts.update((start, end))
    if selected:
        cuts.update(selected)
    for index, char in enumerate(text):
        if char == "\n":
            cuts.update((index, index + 1))
        if tokens_by_char is not None and index and tokens_by_char[index] != tokens_by_char[index - 1]:
            cuts.add(index)
    points = sorted(cuts)

    caret = _caret_html(ctx, not text, focused or omp, readonly)       # omp's composer always owns the keys
    ghost_text = props.get("ghost") if isinstance(props.get("ghost"), str) else ""
    ghost = '<span style="color: %s">%s</span>' % (_color(ctx, "dim"), tsp_html.esc(ghost_text).replace("\n", "<br>")) if ghost_text else ""
    selection_bg = tsp_html._hex_with_alpha(_color(ctx, "accent"), 0.30)
    lines = [[]]
    for start, end in zip(points, points[1:]):
        if start == cursor:
            lines[-1].append(caret + ghost)
        piece = text[start:end]
        if piece == "\n":
            lines.append([])
            continue
        covering = [d for d in decor if d[0] <= start and end <= d[1]]
        tokens = " ".join(d[2] for d in covering)
        if tokens_by_char is not None and tokens_by_char[start]:
            tokens = (tokens_by_char[start] + " " + tokens).strip()
        fx = next((d[3] for d in covering if d[3] in ("shimmer", "pulse")), None)
        if fx == "shimmer":
            rendered = _shimmer(piece, tokens, ctx, offset=start)
        elif fx == "pulse":
            _st, token_color, _hide = _style_of(tokens, ctx)
            rendered = _run(piece, tokens, ctx, color=_pulse_color(ctx, token_color or _color(ctx, "text")))
        else:
            rendered = _run(piece, tokens, ctx)
        if selected and selected[0] <= start and end <= selected[1] and rendered:
            rendered = '<span style="background-color: %s">%s</span>' % (selection_bg, rendered)
        lines[-1].append(rendered)
    if cursor >= len(text):
        lines[-1].append(caret + ghost)
    if not text and not ghost_text:
        placeholder = props.get("placeholder")
        if isinstance(placeholder, str) and placeholder:
            lines[-1].append('<span style="color: %s">%s</span>' % (_color(ctx, "dim"), tsp_html.esc(placeholder)))

    # line decorations: the gutter column and the end-of-line virtual text, indexed by logical line
    gutter, virt = props.get("gutter") or [], props.get("virt") or []
    width = max([_decorated(entry, ctx)[1] for entry in gutter] or [0]) if isinstance(gutter, list) else 0
    rendered_lines = []
    for index, pieces in enumerate(lines):
        prefix = ""
        if width:
            entry_html, entry_len = _decorated(gutter[index] if index < len(gutter) else None, ctx)
            prefix = '<span style="color: %s">%s%s</span>' % (_color(ctx, "dim"), entry_html, NBSP * (width - entry_len))
        suffix = ""
        if isinstance(virt, list) and index < len(virt):
            virt_html, virt_len = _decorated(virt[index], ctx)
            if virt_len:
                suffix = '<span style="color: %s">%s%s</span>' % (_color(ctx, "dim"), NBSP * 2, virt_html)
        rendered_lines.append(prefix + "".join(pieces) + suffix)

    # the window: maxLines caps the height, topline / followCursor / the view's scroll place it
    total = len(rendered_lines)
    cap = _int_or_none(props.get("maxLines")) if not is_input else None
    if cap and cap > 0 and total > cap:
        caret_line = text.count("\n", 0, cursor)
        topline = _int_or_none(props.get("topline"))
        scrolled = ctx.local.get("scroll:%s" % node_id) if isinstance(ctx.local, dict) else None
        first = topline if topline is not None else (scrolled if _int_or_none(scrolled) is not None else 0)
        if topline is None and props.get("followCursor") is not False:
            if caret_line < first:
                first = caret_line
            elif caret_line >= first + cap:
                first = caret_line - cap + 1
        first = max(0, min(first, total - cap))
        shown_lines = rendered_lines[first:first + cap]
        if isinstance(ctx.local, dict):
            ctx.local["scrollinfo:%s" % node_id] = {"total": total, "shown": cap, "first": first, "follow": False}
    else:
        shown_lines = rendered_lines
        if isinstance(ctx.local, dict) and cap and cap > 0:
            ctx.local["scrollinfo:%s" % node_id] = {"total": total, "shown": total, "first": 0, "follow": False}
    body = "<br>".join(shown_lines)
    body = _all_spaces(body) if nowrap else _keep_spaces(body)
    if code:
        body = "<tt>%s</tt>" % body

    label = ""
    if props.get("prompt"):
        label += '<span style="color: %s">%s</span>' % (_color(ctx, "dim"), _spans(props["prompt"], ctx))
    label += _mode_chip(props, ctx, omp)
    content = label + body
    if not readonly and not focused and ctx.surface_id is not None:
        content = _link(ctx, {"ev": "focus", "sf": ctx.surface_id, "id": node_id}, content, props.get("title"))
    inner = "<div>%s</div>" % content
    if omp or getattr(ctx, "stack", None) is None:
        return "<div>%s</div>" % inner         # inside omp's composer box: no box of its own
    ring = _color(ctx, "accent") if focused else _color(ctx, "border")
    if is_input:
        background = _color(ctx, "pageBg") if focused else _color(ctx, "cardBg")
        look = "background-color: %s; border: 1px solid %s; border-radius: 6px; padding: 2px 8px; margin: 2px 0" % (background, ring)
    else:
        look = "border: 1px solid %s; border-radius: 6px; padding: 6px 10px; margin: 2px 0" % ring
    return '<div style="%s">%s</div>' % (look, inner)


DRAWERS["editor"] = _draw_editor
DRAWERS["input"] = _draw_editor
DRAWERS["spinner"] = _draw_spinner
DRAWERS["shimmer"] = _draw_shimmer
DRAWERS["elapsed"] = _draw_elapsed
DRAWERS["rate"] = _draw_rate
DRAWERS["progress"] = _draw_progress
DRAWERS["meter"] = _draw_meter
DRAWERS["chart"] = _draw_chart
DRAWERS["effort"] = _draw_effort


# ---- shared by tool, agent, checklist, block ---------------------------------------------------------------------------

def _local_key(prefix, node, ctx):
    """The key of a node's view-only state: the prop ``key`` (else the id) among the siblings under the same parent."""
    stack = getattr(ctx, "stack", None) or []
    parent = stack[-1].get("id") if stack else ""
    return "%s:%s|%s" % (prefix, parent, _props(node).get("key") or node.get("id"))


def _folded(node, ctx):
    """Whether a node is folded: the user's remembered flip of a collapsible node, until the program changes ``collapsed``
    (the same state card and section keep); without ``collapsible`` the ``collapsed`` prop alone decides."""
    props = _props(node)
    if tsp_html.flippable(props):
        return tsp_html.fold_state(node, ctx)
    return bool(props.get("collapsed"))


def _avail(ctx):
    """The cells one line may use here (the full width less the indentation of the containers around)."""
    return int(getattr(ctx, "avail", None) or ctx.cols)


def _plain_ansi(text):
    """Captured terminal output as plain text: escape sequences and colours dropped, CRLF as LF."""
    return tsp_html._SGR.sub("", tsp_html._ANSI_STRIP.sub("", text)).replace("\r\n", "\n")


def _unframed(ctx):
    return bool(getattr(ctx, "unframed", False))


def _draw_body_unframed(children, ctx, rules=False):
    """The children of a tool or agent drawn in order, nested cards and tools unframed; a child whose kind differs
    from the one before gets a hairline above it when ``rules``."""
    previous = getattr(ctx, "unframed", False)
    ctx.unframed = True
    try:
        out, last = [], None
        for child in children:
            if _props(child).get("hidden"):
                continue
            inner = tsp_html.draw(child, ctx)
            if not inner:
                continue
            if rules and last is not None and child.get("k") != last:
                inner = '<div style="border-top: 1px solid %s; margin-top: 4px; padding-top: 4px">%s</div>' % (_color(ctx, "borderMuted"), inner)
            out.append(inner)
            last = child.get("k")
        return "".join(out)
    finally:
        ctx.unframed = previous


def _is_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


_TOOL_ICONS = {
    "bash": "\u276f", "shell": "\u276f", "read": "\u2263", "edit": "\u270e", "write": "\u271a", "grep": "\u2315",
    "glob": "\u2315", "search": "\u2315", "find": "\u2726", "ls": "\u25a4", "web": "\u25cd", "fetch": "\u25cd",
    "browser": "\u25cd", "todo": "\u2611", "task": "\u263b", "eval": "\u2039\u203a", "python": "py", "lsp": "{}",
    "proc": "\u223f"}
_PLUG_ICON = "\u2301"


def _tool_icon(name):
    return _TOOL_ICONS.get(name if isinstance(name, str) else "", _PLUG_ICON)


# ---- tool --------------------------------------------------------------------------------------------------------------

_TOOL_STATUS = ("pending", "running", "done", "error", "cancelled")
_CMD_TOKEN = re.compile(r"""[ \t]+|'[^']*'?|"(?:\\.|[^"\\])*"?|\$\{[^}]*\}?|\$[A-Za-z_][A-Za-z0-9_]*|&&|\|\||>>|[|;&<>()]|[^\s|;&<>()'"$]+|\$""")
_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_WRAPPERS = frozenset(("sudo", "env", "time", "npx", "uvx", "doas", "nohup", "command", "exec", "nice", "bunx", "pnpx"))
_PATH = re.compile(r"^(?P<dir>.*[\\/])?(?P<name>[^\\/:]*?)(?P<ln>:\d+(?:-\d+)?)?$")


def _command_html(text, ctx):
    """One shell line split into runs: program words, flags, quoted strings, variables, operators."""
    colors = {
        "cmd": ctx.theme.tokens.get("syntaxFunction") or _color(ctx, "accent"),
        "flag": ctx.theme.tokens.get("syntaxKeyword") or _color(ctx, "warning"),
        "str": ctx.theme.tokens.get("syntaxString") or _color(ctx, "success"),
        "var": ctx.theme.tokens.get("syntaxVariable") or _color(ctx, "mdCode"),
        "op": ctx.theme.tokens.get("syntaxOperator") or _color(ctx, "dim")}
    out, expect = [], True
    for match in _CMD_TOKEN.finditer(text):
        token = match.group(0)
        if token.strip() == "":
            out.append(NBSP * len(token))
            continue
        kind = None
        if token[0] in "'\"":
            kind = "str"
        elif token[0] == "$":
            kind = "var"
        elif token in ("&&", "||", "|", ";", "&", ">>", ">", "<", "(", ")"):
            kind = "op"
            if token in ("&&", "||", "|", ";", "&", "(", ")"):
                expect = True
        elif expect and not _ASSIGNMENT.match(token):
            kind = "cmd"
            expect = token.rsplit("/", 1)[-1].rsplit("\\", 1)[-1] in _WRAPPERS
        elif token.startswith("-"):
            kind = "flag"
        body = tsp_html.esc(token)
        out.append('<span style="color: %s">%s</span>' % (colors[kind], body) if kind else body)
    return "<tt>%s</tt>" % "".join(out)


def _tool_target(props, ctx, folded, node):
    target = props.get("target")
    if target is None or target == "" or target == []:
        return ""
    muted = _color(ctx, "muted")
    if not isinstance(target, str):
        return '<span style="color: %s">%s</span>' % (muted, _spans(target, ctx))
    kind = props.get("targetKind")
    if kind == "command":
        limit = max(24, _avail(ctx) - 30)
        text = target if not folded or len(target) <= limit else target[:limit - 1] + "\u2026"
        return '<span style="color: %s">%s</span>' % (muted, _command_html(text, ctx))
    if kind == "path":
        match = _PATH.match(target)
        if match:
            directory, name, line = match.group("dir") or "", match.group("name") or "", match.group("ln") or ""
            return ('<tt><span style="color: %s">%s</span><b>%s</b><span style="color: %s">%s</span></tt>'
                    % (_color(ctx, "dim"), tsp_html.esc(directory), tsp_html.esc(name), _color(ctx, "dim"), tsp_html.esc(line)))
    if kind in ("pattern", "query"):
        quote = '<span style="color: %s">%%s</span>' % _color(ctx, "dim")
        return '<span style="color: %s"><tt>%s%s%s</tt></span>' % (muted, quote % "\u201c", tsp_html.esc(target), quote % "\u201d")
    return '<span style="color: %s"><tt>%s</tt></span>' % (muted, tsp_html.esc(target))


def _meta_html(entry, ctx):
    """One ``meta`` fact: spans as given; in a plain string +N is the added colour and -N the removed one."""
    if not isinstance(entry, str):
        return _spans(entry, ctx)
    out = []
    for word in re.split(r"(\s+)", entry):
        if re.fullmatch(r"\+\d+", word):
            out.append('<span style="color: %s">%s</span>' % (_color(ctx, "toolDiffAdded"), tsp_html.esc(word)))
        elif re.fullmatch(r"[-\u2212]\d+", word):
            out.append('<span style="color: %s">%s</span>' % (_color(ctx, "toolDiffRemoved"), tsp_html.esc(word)))
        else:
            out.append(tsp_html.esc(word))
    return "".join(out)


_TEXT_KINDS = ("ansi", "code", "diff", "text", "md", "math")


def _text_lines(text):
    lines = text.replace("\r\n", "\n").split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return lines


def _line_count(child, width):
    """The drawn lines of a body child: a wrapped line counts as many."""
    props = _props(child)
    text = props.get("text") if isinstance(props.get("text"), str) else None
    if text is None:
        text = tsp_html.node_text(child)
    if child.get("k") == "ansi":
        text = _plain_ansi(text)
    return sum(max(1, -(-len(line) // width)) for line in _text_lines(text)) or 1


def _clamp_children(children, limit, tail, width):
    """(children cut to ``limit`` lines (the last ones for ``tail``), the number of lines left out)."""
    items = [c for c in children if not _props(c).get("hidden")]
    counts = [_line_count(c, width) for c in items]
    total = sum(counts)
    if total <= limit:
        return items, 0
    order = list(zip(items, counts))
    if tail:
        order.reverse()
    kept, budget = [], limit
    for child, count in order:
        if budget <= 0:
            break
        text = _props(child).get("text")
        if count <= budget:
            kept.append(child)
            budget -= count
        elif child.get("k") in _TEXT_KINDS and isinstance(text, str):
            lines = _text_lines(text)
            cut = lines[-budget:] if tail else lines[:budget]
            clone = dict(child)
            clone["p"] = dict(_props(child), text="\n".join(cut))
            kept.append(clone)
            budget = 0
        else:
            break
    if tail:
        kept.reverse()
    return kept, total - (limit - budget)


def _action_event(node, ctx, act, toggle):
    """The event of an action id run on ``node``: the built-ins do what they do on any node, others send ``action``."""
    if act == "toggle":
        return toggle
    if act == "zoom":
        return None
    probe = dict(node)
    probe["p"] = dict(_props(node), actions={"click": act})
    return tsp_html._click_event(probe, ctx)


def _orbit_glyph(ctx, color):
    still = _still(ctx)
    now = None if still else _now(ctx)
    if not still:
        ctx.animated = True
    step = int(((now % 0.9) / 0.9) * 12) if now is not None else 0
    return _img(_orbit_png(12, _rgb(color), step), 12, 12)


def _draw_tool(node, ctx):
    """One step of work: a head drawn from data (icon, verb, target, meta, badges, note, exit chip, timer, status glyph,
    chevron, action buttons) over a body made of the children, collapsible with an optional line preview."""
    props = _props(node)
    status = props.get("status") if props.get("status") in _TOOL_STATUS else "pending"
    collapsible = bool(props.get("collapsible"))
    folded = _folded(node, ctx)
    toggle = tsp_html.fold_event(node, ctx) if collapsible else None
    nested = _unframed(ctx)
    inline = props.get("frame") == "inline"
    intent = props.get("intent") if isinstance(props.get("intent"), str) and props.get("intent") else None

    def piece(inner, title=None):
        return _link(ctx, toggle, inner + NBSP, title or intent)

    head = [piece('<span style="color: %s">%s</span>' % (_color(ctx, "toolTitle", _color(ctx, "muted")), tsp_html.esc(_tool_icon(props.get("name")))))]
    title = props.get("title")
    if title:
        head.append(piece("<b>%s</b>" % _spans(title, ctx)))
    target = _tool_target(props, ctx, folded, node)
    if target:
        tip = None
        if isinstance(props.get("href"), str) and props["href"]:
            tip = "Opens %s" % props["href"]
        head.append(piece(target, tip))
    for meta in props.get("meta") or []:
        head.append(piece('<span style="color: %s">%s</span>' % (_color(ctx, "dim"), _meta_html(meta, ctx))))
    for badge in props.get("badges") or []:
        if isinstance(badge, dict):
            head.append(piece(tsp_html.chip(badge.get("text", ""), ctx, badge.get("tone") or "accent"), badge.get("title")))
    if props.get("note"):
        head.append(piece('<span style="color: %s">%s</span>' % (_color(ctx, "dim"), _spans(props["note"], ctx))))
    if _is_number(props.get("exit")) and props["exit"] != 0:
        head.append(piece(tsp_html.chip("exit %s" % props["exit"], ctx, "error")))
    age, took = props.get("age"), props.get("took")
    shown = None
    if status in ("running", "pending") and _is_number(age):
        shown = ctx.clock.elapsed_ms(node.get("id"), age)
        ctx.animated = True
    elif _is_number(took):
        shown = took
    elif _is_number(age):
        shown = age
    if shown is not None:
        head.append(piece('<span style="color: %s">%s</span>' % (_color(ctx, "dim"), _fmt_time(shown))))
    if status == "running":
        head.append(piece(_orbit_glyph(ctx, _color(ctx, "accent"))))
    elif status == "error":
        head.append(piece('<span style="color: %s">&#8855;</span>' % _color(ctx, "error")))
    elif status == "cancelled":
        head.append(piece('<span style="color: %s">&#8856;</span>' % _color(ctx, "dim")))
    if collapsible:
        head.append(piece('<span style="color: %s">%s</span>' % (_color(ctx, "dim"), "&#9656;" if folded else "&#9662;")))
    for action in props.get("tools") or []:
        if not isinstance(action, dict) or not action.get("id"):
            continue
        label = tsp_html.esc(action.get("label") or action["id"])
        keys = "".join(" " + tsp_html.keycap([key], ctx) for key in action.get("keys") or [])
        button = ('<span style="background-color: %s; color: %s; border-radius: 4px; padding: 0 6px">%s%s</span>'
                  % (_color(ctx, "selectedBg"), _color(ctx, "muted"), label, keys))
        head.append(_link(ctx, _action_event(node, ctx, str(action["id"]), toggle), button + NBSP))
    if isinstance(props.get("href"), str) and props["href"]:
        head.append(_link(ctx, {"local": "open", "href": props["href"]}, '<span style="color: %s">&#8599;</span>' % _color(ctx, "dim"), "Open %s" % props["href"]))
    head_html = "<div>%s</div>" % "".join(head)

    children = node.get("c") or []
    body, more, tail = "", "", False
    preview = props.get("preview") if isinstance(props.get("preview"), dict) else None
    if not folded:
        body = _draw_body_unframed(children, ctx, rules=True)
    elif preview and children and not tsp_html.preview_gives_way(ctx):
        tail = _is_number(preview.get("tail")) and preview["tail"] > 0       # the tail wins when both are given
        limit = int(preview["tail"] if tail else _num(preview.get("lines"), 0))
        if limit > 0:
            kept, hidden = _clamp_children(children, limit, tail, max(20, _avail(ctx) - 8))
            body = _draw_body_unframed(kept, ctx, rules=True)
            if hidden:
                label = ("%d earlier line%s" if tail else "%d more line%s") % (hidden, "" if hidden == 1 else "s")
                button = '<span style="color: %s">%s &middot; Show all %s</span>' % (
                    _color(ctx, "dim"), label, tsp_html.keycap(["\u2303O"], ctx))
                more = "<div>%s</div>" % _link(ctx, tsp_html.fold_event(node, ctx), button)
        else:
            tail = False
    # a tail preview's Show all button sits between the head and the body, a lines preview's below the body
    before, after = (more, "") if tail else ("", more)
    if nested:
        return '<div style="margin: 2px 0">%s%s%s%s</div>' % (
            head_html, before, ('<div style="padding-left: 12px">%s</div>' % body) if body else "", after)
    if inline:
        wrapped = ('<div style="border-left: 1px solid %s; margin-left: 6px; padding-left: 10px">%s</div>' % (_color(ctx, "borderMuted"), body)) if body else ""
        return '<div style="margin: 3px 0">%s%s%s%s</div>' % (head_html, before, wrapped, after)
    background = {"done": "toolSuccessBg", "error": "toolErrorBg"}.get(status, "toolPendingBg")
    edge_color = {"running": "accent", "error": "error", "cancelled": "dim"}.get(status)
    edge = "border-left: 3px solid %s; " % _color(ctx, edge_color) if edge_color else ""
    wrapped = ('<div style="margin-top: 4px">%s</div>' % body) if body else ""
    return ('<div style="background-color: %s; border: 1px solid %s; %sborder-radius: 8px; padding: 8px 12px; margin: 5px 0">%s%s%s%s</div>'
            % (_color(ctx, background), _color(ctx, "border"), edge, head_html, before, wrapped, after))


# ---- agent -------------------------------------------------------------------------------------------------------------

_AGENT_STATUS = ("pending", "running", "done", "failed", "aborted", "idle", "parked")


def _compact(value):
    if abs(value) >= 1000000:
        return "%gM" % round(value / 1000000.0, 1)
    if abs(value) >= 1000:
        return "%dK" % round(value / 1000.0)
    return "%d" % value


def _agent_glyph(status, ctx):
    if status == "running":
        return '<span style="color: %s">&#9679;</span>' % _pulse_color(ctx, _color(ctx, "accent"))
    if status == "done":
        return '<span style="color: %s">&#10003;</span>' % _color(ctx, "success")
    if status == "failed":
        return '<span style="color: %s">&#10007;</span>' % _color(ctx, "error")
    if status == "aborted":
        return '<span style="color: %s">&#8856;</span>' % _color(ctx, "dim")
    return '<span style="color: %s">&#9675;</span>' % _color(ctx, "dim")


def _live_time(ctx, key, status, age, took):
    """The timer text of an agent part: live from ``age`` while running or pending, else the frozen ``took`` (or ``age``)."""
    if status in ("running", "pending") and _is_number(age):
        ctx.animated = True
        return _fmt_time(ctx.clock.elapsed_ms(key, age))
    if _is_number(took):
        return _fmt_time(took)
    if _is_number(age):
        return _fmt_time(age)
    return None


def _agent_stats(node, props, ctx, status):
    """[(html, plain length)] for the right-hand stats."""
    out = []
    dim, muted = _color(ctx, "dim"), _color(ctx, "muted")
    model = props.get("model")
    if isinstance(model, str) and model:
        short = model.rsplit("/", 1)[-1]
        dot = ""
        if isinstance(props.get("thinking"), str) and props["thinking"]:
            dot = '<span style="color: %s">&#9679;</span> ' % (ctx.theme.tokens.get(props["thinking"]) or dim)
        chip = ('<span style="background-color: %s; color: %s; border-radius: 4px; padding: 0 6px">%s%s</span>'
                % (_color(ctx, "selectedBg"), muted, dot, tsp_html.esc(short)))
        out.append((_tip(chip, model, ctx), len(short) + 3))
    stats = props.get("stats") if isinstance(props.get("stats"), dict) else {}
    if _is_number(stats.get("tools")) and stats["tools"] > 0:
        text = "\u2692%d" % stats["tools"]
        out.append(('<span style="color: %s">%s</span>' % (muted, text), len(text)))
    if _is_number(stats.get("requests")) and stats["requests"] > 0:
        text = "%d req" % stats["requests"]
        out.append(('<span style="color: %s">%s</span>' % (muted, text), len(text)))
    if _is_number(stats.get("done")) and status == "running":
        percent = int(round(_clamp(stats["done"]) * 100))
        ring = _ring_img(12, _rgb(_color(ctx, "border")), ((0.0, percent / 100.0, _rgb(_color(ctx, "accent")), False),))
        out.append((ring + '<span style="color: %s">%d%%</span>' % (muted, percent), 5))
    context = stats.get("context")
    if _is_number(context):
        fraction = _clamp(context if context <= 1 else context / 100.0)
        color = _color(ctx, "error") if fraction >= 0.9 else (_color(ctx, "warning") if fraction >= 0.75 else _color(ctx, "accent"))
        label = stats.get("contextLabel") if isinstance(stats.get("contextLabel"), str) and stats.get("contextLabel") else "%d%% context" % round(fraction * 100)
        bar = _bar_img(40, 4, _rgb(_color(ctx, "border")), ((0.0, fraction, _rgb(color), False, 1.0),) if fraction > 0 else ())
        out.append((_tip(bar, label, ctx), 6))
    elif _is_number(stats.get("tokens")):
        text = _compact(stats["tokens"])
        out.append(('<span style="color: %s">%s</span>' % (muted, text), len(text)))
    if _is_number(stats.get("cost")) and stats["cost"] > 0:
        text = "$%.2f" % stats["cost"] if stats["cost"] >= 0.01 else "<$0.01"
        out.append(('<span style="color: %s">%s</span>' % (muted, tsp_html.esc(text)), len(text)))
    shown = _live_time(ctx, "%s:stats" % node.get("id"), status, stats.get("age"), stats.get("took"))
    if shown:
        out.append(('<span style="color: %s">%s</span>' % (dim, shown), len(shown)))
    return out


def _agent_now_line(node, props, ctx, status):
    """Line 2: a retry countdown, else the tool running now (icon, name, shimmering intent, timer); "" when empty."""
    dim = _color(ctx, "dim")
    retry = props.get("retry") if isinstance(props.get("retry"), dict) else None
    if retry is not None:
        attempt = int(_num(retry.get("attempt"), 1))
        maximum = int(_num(retry.get("max"), 1))
        delay = _num(retry.get("delay"), 0)
        age = retry.get("age")
        elapsed = ctx.clock.elapsed_ms("%s:retry" % node.get("id"), age) if _is_number(age) else 0
        remaining = max(0, delay - elapsed)
        if remaining > 0 and _is_number(age):
            ctx.animated = True
        text = '<span style="color: %s">&#8635; retry %d/%d in %s</span>' % (_color(ctx, "warning"), attempt, maximum, _fmt_time(remaining))
        return _tip(text, retry.get("error") if isinstance(retry.get("error"), str) else None, ctx, _color(ctx, "warning"))
    tool = props.get("tool") if isinstance(props.get("tool"), dict) else None
    if status != "running" or tool is None:
        return ""
    parts = ['<span style="color: %s">%s</span>' % (dim, tsp_html.esc(_tool_icon(tool.get("name")))),
             '<span style="color: %s">%s</span>' % (_color(ctx, "muted"), tsp_html.esc(tool.get("name") or ""))]
    intent = tool.get("intent")
    if isinstance(intent, list):
        intent = [dict(span, fx="shimmer") if isinstance(span, dict) and not span.get("fx") else span for span in intent]
        parts.append(_spans(intent, ctx))
    elif isinstance(intent, str) and intent:
        parts.append(_shimmer(intent, "", ctx))
    shown = _live_time(ctx, "%s:tool" % node.get("id"), status, tool.get("age"), None)
    if shown:
        parts.append('<span style="color: %s">%s</span>' % (dim, shown))
    return " ".join(part for part in parts if part)


def _draw_agent(node, ctx):
    """One worker as a row (glyph, name, badges, task, stats on the right), a second line for what it runs now or
    its retry countdown, and its children as the expanded content."""
    props = _props(node)
    status = props.get("status") if props.get("status") in _AGENT_STATUS else "pending"
    collapsible = bool(props.get("collapsible"))
    folded = _folded(node, ctx)
    toggle = tsp_html.fold_event(node, ctx) if collapsible else None
    depth = int(_clamp(_num(props.get("depth"), 0), 0, 6))
    available = max(30, _avail(ctx) - 2)

    left, left_len = [_agent_glyph(status, ctx), "<b>%s</b>" % tsp_html.esc(props.get("name") or "")], 2 + len(str(props.get("name") or ""))
    if isinstance(props.get("agent"), str) and props["agent"]:
        left.append(tsp_html.chip(props["agent"], ctx, "muted"))
        left_len += len(props["agent"]) + 3
    for badge in props.get("badges") or []:
        if isinstance(badge, dict):
            left.append(tsp_html.chip(badge.get("text", ""), ctx, badge.get("tone") or "accent"))
            left_len += len(str(badge.get("text", ""))) + 3
    stats = _agent_stats(node, props, ctx, status)
    right_html = "&nbsp;&nbsp;".join(item[0] for item in stats)
    right_len = sum(item[1] for item in stats) + 2 * max(0, len(stats) - 1)
    task = props.get("task")
    task_plain = tsp_html.plain_text(task) if task else ""
    title = None
    if task_plain:
        budget = max(10, available - left_len - right_len - 4)
        if len(task_plain) > budget:
            title = task_plain
            task_html = '<span style="color: %s">%s</span>' % (_color(ctx, "muted"), tsp_html.esc(task_plain[:budget - 1] + "\u2026"))
            task_len = budget
        else:
            task_html = '<span style="color: %s">%s</span>' % (_color(ctx, "muted"), _spans(task, ctx))
            task_len = len(task_plain)
        left.append(task_html)
        left_len += task_len + 1
    gap = max(2, available - left_len - right_len)
    left_html = " ".join(left)
    left_html = _link(ctx, toggle, left_html, title) if toggle else _tip(left_html, title, ctx)
    lines = "<div>%s%s</div>" % (left_html, NBSP * gap + right_html if right_html else "")
    now_line = _agent_now_line(node, props, ctx, status)
    if now_line:
        lines += '<div style="padding-left: 18px; color: %s">%s</div>' % (_color(ctx, "dim"), now_line)
    kids = ""
    if not folded and node.get("c"):
        kids = '<div style="padding-left: 14px">%s</div>' % _draw_body_unframed(node.get("c") or [], ctx)
    margin = "margin: 5px 0 5px %dpx" % (depth * 22) if depth else "margin: 5px 0"
    if _unframed(ctx):
        return '<div style="%s">%s%s</div>' % (margin, lines, kids)
    return ('<div style="background-color: %s; border: 1px solid %s; border-radius: 8px; padding: 8px 12px; %s">%s%s</div>'
            % (_color(ctx, "cardBg"), _color(ctx, "border"), margin, lines, kids))


# ---- checklist ---------------------------------------------------------------------------------------------------------

_CK_STATUS = ("pending", "active", "done", "dropped", "blocked")


def _ck_items(phase):
    return [item for item in (phase.get("items") or []) if isinstance(item, dict) and item.get("id") not in (None, "")]


def _ck_status(item):
    return item.get("status") if item.get("status") in _CK_STATUS else "pending"


def _struck(value):
    """Text struck through.  minihtml has no line-through, so each character gets a combining long stroke overlay."""
    def strike(text):
        return "".join(char if char.isspace() else char + "\u0336" for char in text)

    if isinstance(value, str):
        return strike(value)
    if isinstance(value, list):
        return [dict(span, t=strike(span.get("t", "") or "")) if isinstance(span, dict) else span for span in value]
    return value


def _ck_item_html(item, ctx):
    status = _ck_status(item)
    glyph, tone = {"pending": ("&#9675;", "muted"), "active": ("&#9673;", "accent"), "done": ("&#10003;", "success"),
                   "dropped": ("&#8211;", "dim"), "blocked": ("&#9888;", "warning")}[status]
    color = _pulse_color(ctx, _color(ctx, tone)) if status == "active" else _color(ctx, tone)
    text = _spans(item.get("text"), ctx)
    if status == "active":
        text = "<b>%s</b>" % text
    elif status in ("done", "dropped"):
        text = '<span style="color: %s">%s</span>' % (_color(ctx, "muted" if status == "done" else "dim"), _spans(_struck(item.get("text")), ctx))
    out = '<div style="padding-left: 12px"><span style="color: %s">%s</span> %s</div>' % (color, glyph, text)
    if item.get("note"):
        out += '<div style="padding-left: 30px; color: %s">%s</div>' % (
            _color(ctx, "warning" if status == "blocked" else "dim"), _spans(item["note"], ctx))
    return out


def _ck_ring(done, total, ctx):
    complete = total > 0 and done >= total
    color = _color(ctx, "success" if complete else "accent")
    arcs = ((0.0, (done / float(total)) if total else 0.0, _rgb(color), False),) if done > 0 else ()
    return _ring_img(12, _rgb(_color(ctx, "border")), arcs)


def _ck_full(node, ctx, phases):
    """The ``full`` list: a row per phase (progress ring, title, done/total) with its items; complete phases fold."""
    out = []
    lone = len(phases) == 1 and not phases[0].get("title")
    for phase in phases:
        items = _ck_items(phase)
        done = sum(1 for item in items if _ck_status(item) in ("done", "dropped"))
        complete = bool(items) and done == len(items)
        initial = phase.get("collapsed") if isinstance(phase.get("collapsed"), bool) else complete
        key = "%s|%s" % (_local_key("ckphase", node, ctx), phase.get("id") or "")
        stored = ctx.local.get(key) if isinstance(ctx.local, dict) else None
        folded = stored if isinstance(stored, bool) else initial
        if not lone:
            row = "%s&nbsp;%s&nbsp;<b>%s</b>&nbsp;<span style=\"color: %s\">%d/%d</span>" % (
                '<span style="color: %s">%s</span>' % (_color(ctx, "dim"), "&#9656;" if folded else "&#9662;"),
                _ck_ring(done, len(items), ctx), _spans(phase.get("title"), ctx), _color(ctx, "dim"), done, len(items))
            out.append("<div>%s</div>" % _link(ctx, {"local": "set", "key": key, "value": not folded}, row))
        if lone or not folded:
            out.extend(_ck_item_html(item, ctx) for item in items)
    return "".join(out)


def _draw_checklist(node, ctx):
    """A phased todo list as ``full`` rows, a ``hud`` pill with a popover, or a ``reminder`` header that discloses the
    open items.  Folds and the reminder's disclosure are local; the hud pill also sends ``activate {item: "hud"}``."""
    props = _props(node)
    mode = props.get("mode") if props.get("mode") in ("full", "hud", "reminder") else "full"
    phases = [p for p in props.get("phases") or [] if isinstance(p, dict)]
    every = [item for phase in phases for item in _ck_items(phase)]
    done = sum(1 for item in every if _ck_status(item) in ("done", "dropped"))
    if mode == "full":
        return "<div>%s</div>" % _ck_full(node, ctx, phases)
    key = _local_key("ckopen", node, ctx)
    opened = bool(ctx.local.get(key)) if isinstance(ctx.local, dict) else False
    if mode == "hud":
        current = next((i for i in every if _ck_status(i) == "active"), None) or next((i for i in every if _ck_status(i) == "pending"), None)
        if current is not None:
            label = _spans(current.get("text"), ctx)
        else:
            label = "All done" if every and done == len(every) else "Todos"
        pill = ('<span style="background-color: %s; border-radius: 10px; padding: 2px 10px">%s&nbsp;%s&nbsp;<span style="color: %s">%d/%d</span></span>'
                % (tsp_html._hex_with_alpha(_color(ctx, "accent"), 0.16), _ck_ring(done, len(every), ctx), label, _color(ctx, "dim"), done, len(every)))
        event = {"local": "set", "key": key, "value": not opened, "send": _event(ctx, "activate", node, item="hud")}
        out = '<div style="text-align: right">%s</div>' % _link(ctx, event, pill)
        if opened:
            out = tsp_html._card_shell(_ck_full(node, ctx, phases), ctx, "cardBg", "borderAccent") + out
        return "<div>%s</div>" % out
    open_items = [item for item in every if _ck_status(item) in ("pending", "active", "blocked")]
    count = len(open_items)
    head = ('<span style="color: %s">&#9888;</span>&nbsp;<b>%d todo%s still open</b>' % (_color(ctx, "warning"), count, "" if count == 1 else "s"))
    if props.get("note"):
        head += '&nbsp;<span style="color: %s">%s</span>' % (_color(ctx, "dim"), _spans(props["note"], ctx))
    head += '&nbsp;<span style="color: %s">%s</span>' % (_color(ctx, "dim"), "&#9662;" if opened else "&#9656;")
    out = "<div>%s</div>" % _link(ctx, {"local": "set", "key": key, "value": not opened}, head)
    if opened:
        out += "".join(_ck_item_html(item, ctx) for item in open_items)
    return "<div>%s</div>" % out


# ---- block -------------------------------------------------------------------------------------------------------------

_SUBCOMMANDS = frozenset((
    "git", "cargo", "docker", "npm", "pnpm", "yarn", "kubectl", "helm", "gh", "go", "pip", "pip3", "uv", "bun", "deno",
    "dotnet", "brew", "apt", "apt-get", "dnf", "systemctl", "terraform", "make", "podman", "az", "gcloud", "aws", "rustup",
    "poetry", "conda", "winget", "choco", "scoop", "composer", "gradle", "mvn", "flutter", "heroku", "nvm", "pyenv"))
_LENS_ICONS = (("git", "\u2442"), ("docker", "\u25a3"), ("npm", "\u2b22"), ("node", "\u2b22"), ("cargo", "\u2699"),
               ("rust", "\u2699"), ("k8s", "\u2388"), ("kubectl", "\u2388"))


def _block_label(line):
    """The lens label of a command line: the program and, for git, cargo, docker, npm, kubectl, ... its subcommand,
    skipping NAME=value assignments and wrappers such as sudo, env, time, npx, uvx."""
    words = (line or "").split()
    index = 0
    while index < len(words):
        word = words[index]
        if _ASSIGNMENT.match(word):
            index += 1
        elif word.rsplit("/", 1)[-1].rsplit("\\", 1)[-1] in _WRAPPERS:
            index += 1
            while index < len(words) and words[index].startswith("-"):
                index += 1
        else:
            break
    if index >= len(words):
        return ""
    program = words[index].rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    if program.lower().endswith(".exe"):
        program = program[:-4]
    if program in _SUBCOMMANDS:
        for word in words[index + 1:]:
            if not word.startswith("-"):
                return "%s %s" % (program, word)
    return program


def _draw_block(node, ctx):
    """Tern's command-lens block: the native lens view or the raw output (a local choice per block), a quiet bar with the
    lens label, the Native/Raw control and Copy, a left edge by state and a foot with the exit badge and duration."""
    props = _props(node)
    children = [child for child in node.get("c") or []]
    raw = next((c for c in children if c.get("id") == "raw"), None) or next((c for c in reversed(children) if c.get("k") == "ansi"), None)
    native = next((c for c in children if c is not raw), None)
    key = _local_key("block", node, ctx)
    stored = ctx.local.get(key) if isinstance(ctx.local, dict) else None
    if native is None:
        view = "raw"
    elif raw is None:
        view = "native"
    else:
        view = stored if stored in ("native", "raw") else "native"
    shown = native if view == "native" else raw
    running = props.get("state") == "running"
    exit_code = props.get("exit")
    failed = not running and _is_number(exit_code) and exit_code != 0
    edge = _color(ctx, "accent") if running else (_color(ctx, "error") if failed else _color(ctx, "borderMuted"))

    command = props.get("cmd") if isinstance(props.get("cmd"), str) else ""
    line = props.get("line") if isinstance(props.get("line"), str) and props.get("line") else command
    lens = props.get("lens") if isinstance(props.get("lens"), str) else ""
    icon = "\u276f"
    if not lens.startswith("plugin."):
        icon = next((glyph for prefix, glyph in _LENS_ICONS if lens.startswith(prefix)), icon)
    dim = _color(ctx, "dim")
    label = _block_label(line)
    bar = []
    if label or icon:
        lens_html = '<span style="color: %s">%s&nbsp;%s</span>' % (dim, tsp_html.esc(icon), tsp_html.esc(label))
        bar.append(_tip(lens_html, command or None, ctx, dim))
    if native is not None:
        segments = []
        for name, title in (("native", "Native"), ("raw", "Raw")):
            on = view == name
            look = ('<span style="background-color: %s; color: %s; border-radius: 4px; padding: 0 6px">%s</span>' % (
                tsp_html._hex_with_alpha(_color(ctx, "accent"), 0.22), _color(ctx, "accent"), title)) if on else (
                '<span style="color: %s; padding: 0 6px">%s</span>' % (dim, title))
            segments.append(_link(ctx, {"local": "set", "key": key, "value": name}, look))
        bar.append("".join(segments))
    raw_text = _plain_ansi(tsp_html.node_text(raw)) if raw is not None else ""
    bar.append(_link(ctx, {"local": "copy", "text": raw_text}, '<span style="color: %s">%s</span>' % (dim, tsp_html.ICONS.get("copy", "&#10697;")), "Copy output"))
    out = '<div style="text-align: right">%s</div>' % "&nbsp;&nbsp;".join(bar)
    out += tsp_html.draw(shown, ctx) if shown is not None else ""
    foot = []
    if failed:
        foot.append(tsp_html.chip("exit %d" % exit_code, ctx, "error"))
    took = props.get("took")
    if not running and _is_number(took) and took > 1000:
        foot.append('<span style="color: %s">%s</span>' % (dim, _fmt_time(took)))
    if foot:
        out += '<div>%s</div>' % "&nbsp;&nbsp;".join(foot)
    role = props.get("role") if isinstance(props.get("role"), str) and props.get("role") else "lens.%s" % lens
    return ('<div class="%s" data-role="%s" data-state="%s" data-view="%s" style="border-left: 2px solid %s; padding-left: 10px; margin: 4px 0">%s</div>'
            % ("failed" if failed else "", _esc_attr(role), "running" if running else "done", view, edge, out))


DRAWERS["tool"] = _draw_tool
DRAWERS["agent"] = _draw_agent
DRAWERS["checklist"] = _draw_checklist
DRAWERS["block"] = _draw_block


# ---- prefs -------------------------------------------------------------------------------------------------------------

@functools.lru_cache(maxsize=16)
def _switch_png(on, accent, off, knob):
    width, height = 26, 14

    def sampler(x, y):
        if not _in_round_rect(x, y, 0, 0, width, height, 7):
            return None
        knob_x = 19.0 if on else 7.0
        if (x - knob_x) ** 2 + (y - 7.0) ** 2 <= 5.2 ** 2:
            return knob + (1.0,)
        return (accent if on else off) + (1.0,)

    return _render_png(width, height, sampler)


def _pf_options(control):
    out = []
    for option in control.get("options") or []:
        if isinstance(option, dict) and "value" in option:
            out.append((option["value"], str(option.get("label") if option.get("label") not in (None, "") else option["value"]),
                        option.get("detail") if isinstance(option.get("detail"), str) else None))
    return out


def _pf_mark(text, query, ctx):
    """``text`` escaped, with the words of ``query`` marked."""
    words = [word for word in (query or "").split() if word]
    if not words:
        return tsp_html.esc(text)
    out, at = [], 0
    background = tsp_html._hex_with_alpha(_color(ctx, "accent"), 0.35)
    for match in re.finditer("|".join(re.escape(word) for word in words), str(text), re.I):
        out.append(tsp_html.esc(text[at:match.start()]))
        out.append('<span style="background-color: %s">%s</span>' % (background, tsp_html.esc(match.group(0))))
        at = match.end()
    out.append(tsp_html.esc(text[at:]))
    return "".join(out)


def _pf_button(label, ctx, enabled=True, on=False):
    """A small button face (a stepper end, a segment, an action)."""
    if on:
        return ('<span style="background-color: %s; color: %s; border-radius: 4px; padding: 0 8px">%s</span>'
                % (tsp_html._hex_with_alpha(_color(ctx, "accent"), 0.25), _color(ctx, "accent"), label))
    return ('<span style="background-color: %s; color: %s; border-radius: 4px; padding: 0 8px">%s</span>'
            % (_color(ctx, "selectedBg"), _color(ctx, "muted" if enabled else "dim"), label))


def _pf_number_out(value):
    return int(value) if float(value).is_integer() else value


def _pf_next_number(control, direction):
    """The value a stepper button moves to (None when there is nowhere to go): the next key of ``labels`` when given,
    else value +- step clamped to min/max."""
    value = _num(control.get("value"), 0)
    labels = control.get("labels")
    if isinstance(labels, dict) and labels:
        keys = []
        for key in labels:
            try:
                keys.append(float(key))
            except (TypeError, ValueError):
                pass
        keys.sort()
        ahead = [k for k in keys if k > value] if direction > 0 else [k for k in keys if k < value]
        if not ahead:
            return None
        return _pf_number_out(ahead[0] if direction > 0 else ahead[-1])
    step = _num(control.get("step"), 1) or 1
    target = value + direction * step
    if _is_number(control.get("min")):
        target = max(control["min"], target)
    if _is_number(control.get("max")):
        target = min(control["max"], target)
    return None if target == value else _pf_number_out(target)


def _pf_text_field(shown, ctx, editing_here, mono, muted):
    """The look of a text field: a well with the value, a placeholder or the draft with its caret."""
    border = _color(ctx, "accent" if editing_here else "border")
    if muted:
        inner = '<span style="color: %s">%s</span>' % (_color(ctx, "dim"), tsp_html.esc(shown))
    else:
        inner = tsp_html.esc(shown)
        if mono:
            inner = "<tt>%s</tt>" % inner
    inner = _all_spaces(inner)
    return ('<span style="background-color: %s; border: 1px solid %s; border-radius: 4px; padding: 1px 8px">%s</span>'
            % (_color(ctx, "pageBg"), border, inner or NBSP * 8))


def _pf_control(node, row, ctx, editing, query):
    """(the control's HTML on the row, extra HTML below the row) for one settings row."""
    control = row.get("control") if isinstance(row.get("control"), dict) else {}
    kind = control.get("k")
    row_id = row.get("id")
    off = bool(row.get("disabled"))
    here = isinstance(editing, dict) and editing.get("row") == row_id
    accent, dim, muted = _color(ctx, "accent"), _color(ctx, "dim"), _color(ctx, "muted")

    def act(event, inner, title=None):
        return inner if off else _link(ctx, event, inner, title)

    def change(value):
        return _event(ctx, "change", node, item=row_id, value=value)

    if kind == "switch":
        on = bool(control.get("on"))
        image = _img(_switch_png(on, _rgb(accent), _rgb(_mix(_color(ctx, "border"), _color(ctx, "muted"), 0.3)),
                                 _rgb(_color(ctx, "text"))), 26, 14)
        return act(change(not on), image), ""
    if kind == "action":
        label = tsp_html.esc(control.get("label") or "Edit\u2026")
        event = _event(ctx, "action", node, act=control.get("act") or "edit", value=row_id)
        return act(event, _pf_button(label, ctx, not off)), ""
    if kind == "choice":
        options = _pf_options(control)
        value = control.get("value")
        style = control.get("style")
        segmented = style == "segmented" or (style in (None, "auto") and 2 <= len(options) <= 4 and sum(len(o[1]) for o in options) <= 28)
        if segmented:
            pieces = []
            for option_value, label, detail in options:
                shown = "<tt>%s</tt>" % tsp_html.esc(label) if control.get("mono") else tsp_html.esc(label)
                face = _pf_button(shown, ctx, not off, option_value == value)
                pieces.append(act(change(option_value), face, detail))
            return "&#8201;".join(pieces), ""
        label = next((o[1] for o in options if o[0] == value), "" if value is None else str(value))
        shown = "<tt>%s</tt>" % tsp_html.esc(label) if control.get("mono") else tsp_html.esc(label)
        face = '<span style="color: %s">%s</span>&nbsp;<span style="color: %s">&#9662;</span>' % (accent, shown, dim)
        if here:
            button = act(_event(ctx, "action", node, act="close", value=row_id), face)
            highlighted = editing.get("option") if "option" in editing else value
            lines = []
            for option_value, option_label, detail in options:
                current = option_value == value
                mark = '<span style="color: %s">%s</span>' % (accent, "&#10003;") if current else NBSP
                text = tsp_html.esc(option_label)
                if control.get("mono"):
                    text = "<tt>%s</tt>" % text
                line = "%s&nbsp;%s" % (mark, text)
                if detail:
                    line += '<br><span style="color: %s">&nbsp;&nbsp;%s</span>' % (dim, tsp_html.esc(detail))
                background = ("background-color: %s; " % tsp_html._soft_selection(ctx)) if option_value == highlighted else ""
                lines.append('<div style="%sborder-radius: 4px; padding: 1px 8px">%s</div>' % (background, act(change(option_value), line)))
            menu = '<div style="margin-left: 28px; border: 1px solid %s; border-radius: 6px; padding: 2px 0">%s</div>' % (accent, "".join(lines))
            return button, menu
        return act(_event(ctx, "activate", node, item=row_id), face), ""
    if kind == "number":
        value = _num(control.get("value"), 0)
        labels = control.get("labels") if isinstance(control.get("labels"), dict) else {}
        shown = labels.get(str(value))
        if shown is None:
            shown = labels.get("%g" % value)
        if shown is None:
            shown = "%g" % value + (" " + str(control["unit"]) if control.get("unit") else "")
        buttons = []
        for symbol, direction in (("\u2212", -1), ("+", 1)):
            target = _pf_next_number(control, direction)
            target = _pf_next_number(control, direction)
            face = _pf_button(symbol, ctx, target is not None and not off)
            buttons.append(face if target is None else act(change(target), face))
        return "%s&nbsp;<span style=\"color: %s\">%s</span>&nbsp;%s" % (buttons[0], accent, tsp_html.esc(str(shown)), buttons[1]), ""
    if kind == "text":
        value = control.get("value") if isinstance(control.get("value"), str) else ""
        secret = bool(control.get("secret"))
        if here:
            draft = editing.get("draft") if isinstance(editing.get("draft"), str) else value
            cursor = _int_or_none(editing.get("cursor"))
            shown_draft = "\u2022" * len(draft) if secret else draft
            index = _offset_converter(draft)(cursor) if cursor is not None else len(draft)
            caret = '<span style="color: %s">&#9615;</span>' % accent
            inner = tsp_html.esc(shown_draft[:index]) + caret + tsp_html.esc(shown_draft[index:])
            inner = _all_spaces(inner)
            if control.get("mono"):
                inner = "<tt>%s</tt>" % inner
            field = ('<span style="background-color: %s; border: 1px solid %s; border-radius: 4px; padding: 1px 8px">%s</span>'
                     % (_color(ctx, "pageBg"), accent, inner))
            return field, ""
        if value:
            field = _pf_text_field("\u2022" * min(len(value), 12) if secret else value, ctx, False, bool(control.get("mono")), False)
        else:
            placeholder = control.get("placeholder") if isinstance(control.get("placeholder"), str) else "Not set"
            field = _pf_text_field(placeholder, ctx, False, False, True)
        return act(_event(ctx, "activate", node, item=row_id), field), ""
    if kind == "keys":
        chords = []
        for chord in control.get("keys") or []:
            if isinstance(chord, (list, tuple)):
                chords.append("&#8201;".join(tsp_html.keycap([str(key)], ctx) for key in chord))
        return "&nbsp;&nbsp;".join(chords), ""
    if kind == "multi":
        options = _pf_options(control)
        values = [v for v in control.get("values") or []]
        highlighted = editing.get("option") if here and isinstance(editing, dict) else None
        if control.get("ordered"):
            order = [v for v in values if any(o[0] == v for o in options)]
            rest = [o[0] for o in options if o[0] not in order]
            lines = []
            for position, option_value in enumerate(order + rest):
                label, detail = next((o[1], o[2]) for o in options if o[0] == option_value)
                enabled = option_value in order
                if enabled:
                    moved_up = order[:]
                    moved_down = order[:]
                    index = order.index(option_value)
                    if index > 0:
                        moved_up[index - 1], moved_up[index] = moved_up[index], moved_up[index - 1]
                    if index < len(order) - 1:
                        moved_down[index + 1], moved_down[index] = moved_down[index], moved_down[index + 1]
                    toggled = [v for v in order if v != option_value]
                    up = act(change(moved_up), '<span style="color: %s">&#9650;</span>' % muted) if index > 0 else NBSP
                    down = act(change(moved_down), '<span style="color: %s">&#9660;</span>' % muted) if index < len(order) - 1 else NBSP
                    number = '<span style="color: %s">%d</span>' % (dim, index + 1)
                else:
                    toggled = order + [option_value]
                    up = down = NBSP
                    number = NBSP
                switch = _img(_switch_png(enabled, _rgb(accent), _rgb(_mix(_color(ctx, "border"), _color(ctx, "muted"), 0.3)),
                                          _rgb(_color(ctx, "text"))), 26, 14)
                background = ("background-color: %s; " % tsp_html._soft_selection(ctx)) if option_value == highlighted else ""
                body = '<span style="color: %s">&#10303;</span>&nbsp;%s&nbsp;%s' % (dim, number, tsp_html.esc(label))
                lines.append('<div style="%spadding: 1px 8px">%s&nbsp;%s&nbsp;%s&nbsp;%s</div>' % (
                    background, up, down, act(change(toggled), body, detail), act(change(toggled), switch)))
            return "", '<div style="margin-left: 28px">%s</div>' % "".join(lines)
        chips = []
        for option_value, label, detail in options:
            enabled = option_value in values
            flipped = set(values) ^ {option_value}
            new_values = [o[0] for o in options if o[0] in flipped] + [v for v in values if v in flipped and not any(o[0] == v for o in options)]
            look = _pf_button(tsp_html.esc(label), ctx, not off, enabled)
            if option_value == highlighted:
                look = '<span style="border-bottom: 1px solid %s">%s</span>' % (accent, look)
            chips.append(act(change(new_values), look, detail))
        return "&#8201;".join(chips), ""
    return "", ""


def _pf_row(node, ctx, row, width, focus, editing, query, below=None):
    """One settings row: label, changed dot, Reset, control, then its hint / warning / reason lines."""
    theme = ctx.theme
    row_id = row.get("id")
    off = bool(row.get("disabled"))
    on_focus = row_id == focus
    control = row.get("control") if isinstance(row.get("control"), dict) else {}
    label_text = str(row.get("label") if row.get("label") not in (None, "") else row_id)
    label = _pf_mark(label_text, query, ctx)
    if on_focus:
        label = "<b>%s</b>" % label
    plain = len(label_text)
    if row.get("warning"):
        label = '<span style="color: %s">&#9888;</span>&nbsp;%s' % (_color(ctx, "warning"), label)
        plain += 2
    if off:
        label = '<span style="color: %s">%s</span>' % (_color(ctx, "dim"), label)
    dot = ""
    if row.get("changed"):
        default = row.get("defaultLabel")
        tip = "Changed from default: %s" % default if default else "Changed from default"
        dot = _tip('<span style="color: %s">&#9679;</span>' % _color(ctx, "accent"), tip, ctx, _color(ctx, "accent"))
        plain += 2
    marker = '<span style="color: %s">&#9656;</span>' % _color(ctx, "accent") if on_focus else NBSP
    left = marker + NBSP + label
    if ctx.surface_id is not None and not off:
        left = _link(ctx, _event(ctx, "select", node, item=row_id), left)
    elif off:
        left = _tip(left, row["disabled"] if isinstance(row.get("disabled"), str) else None, ctx, _color(ctx, "dim"))
    reset = ""
    if row.get("changed") and control.get("k") not in ("action", "keys") and not off:
        reset = _link(ctx, _event(ctx, "change", node, item=row_id, value=None),
                      '<span style="color: %s">Reset</span>' % _color(ctx, "muted")) + NBSP
    inline, extra = _pf_control(node, row, ctx, editing, query)
    padding = NBSP * max(2, width - plain + 2)
    line = "%s%s%s%s%s" % (left, dot and NBSP + dot, padding, reset, inline)
    background = "background-color: %s; border-radius: 4px; " % tsp_html._soft_selection(ctx) if on_focus else ""
    out = '<div style="%spadding: 2px 8px">%s</div>' % (background, line)
    notes = ((row.get("hint"), "dim"), (row.get("warning"), "warning"), (row.get("disabled") if isinstance(row.get("disabled"), str) else None, "dim"))
    for note, tone in notes:
        if note:
            out += '<div style="color: %s; padding-left: 28px; font-size: 0.85rem">%s</div>' % (
                theme.color(tone), _pf_mark(note, query, ctx) if tone == "dim" and note == row.get("hint") else tsp_html.esc(note))
    if below and below["html"] and not below["used"] and row_id == below["row"]:
        below["used"] = True        # the program's picker for this row opens right under it, where the reader clicked
        extra += '<div style="margin-left: 20px">%s</div>' % below["html"]
    return out + extra


def _pf_section(node, ctx, section, focus, editing, query, below=None):
    rows = [row for row in section.get("rows") or [] if isinstance(row, dict) and row.get("id") not in (None, "")]
    parts = []
    if section.get("title"):
        title = '<div style="color: %s; margin-top: 8px; font-weight: bold">%s</div>' % (_color(ctx, "muted"), tsp_html.esc(section["title"]))
        if section.get("id") not in (None, "") and section.get("id") == focus:
            title = '<div style="background-color: %s; border-radius: 4px">%s</div>' % (tsp_html._soft_selection(ctx), title)
        parts.append(title)
    width = min(36, max([len(str(r.get("label") if r.get("label") not in (None, "") else r.get("id"))) for r in rows] or [0]))
    for row in rows:
        parts.append(_pf_row(node, ctx, row, width, focus, editing, query, below))
    return "".join(parts)


def _draw_prefs(node, ctx):
    """A settings page drawn from the program's data: title with Close, a search field, the page nav (groups, changed counts,
    disabled reasons), the page header, sections of rows with all seven control kinds, search results grouped by page, and
    the children placed by role (status preview after the ``status-line`` section, editors in a card, the rest after the
    sections).  Every pointer action is sent back as the spec's events; the program answers with new props."""
    props = _props(node)
    title = props.get("title") if isinstance(props.get("title"), str) and props.get("title") else "Settings"
    pages = [p for p in props.get("pages") or [] if isinstance(p, dict)]
    page_id = props.get("page") if isinstance(props.get("page"), str) else ""
    raw_query = props.get("query") if isinstance(props.get("query"), str) else ""
    query = raw_query.strip()
    focus = props.get("focus")
    editing = props.get("editing") if isinstance(props.get("editing"), dict) else None
    accent, dim, muted = _color(ctx, "accent"), _color(ctx, "dim"), _color(ctx, "muted")
    parts = []

    close = _link(ctx, _event(ctx, "action", node, act="close"), '<span style="color: %s">%s&nbsp;Close</span>' % (muted, tsp_html.keycap(["esc"], ctx)))
    parts.append('<div><b style="color: %s">%s</b>&nbsp;&nbsp;%s</div>' % (accent, tsp_html.esc(title), close))

    cursor = _int_or_none(props.get("cursor"))
    if cursor is not None:
        index = _offset_converter(raw_query)(cursor)
        field = tsp_html.esc(raw_query[:index]) + '<span style="color: %s">&#9615;</span>' % accent + tsp_html.esc(raw_query[index:])
    elif query:
        field = tsp_html.esc(query)
    else:
        field = '<span style="color: %s">Search settings</span>' % dim
    parts.append('<div style="color: %s">%s&nbsp;%s</div>' % (muted, tsp_html.ICONS.get("search", "&#8981;"), field))

    # the nav: pages, grouped by consecutive ``group``
    lines, current, group = [], [], object()
    for page in pages:
        if page.get("group") != group and current:
            lines.append(current)
            current = []
        if page.get("group") != group and page.get("group"):
            current.append('<span style="color: %s">%s</span>' % (dim, tsp_html.esc(page["group"])))
        group = page.get("group")
        label = tsp_html.esc(page.get("label") or page.get("id") or "")
        changed = page.get("changed") if _is_number(page.get("changed")) and page.get("changed") > 0 else 0
        if changed:
            label += '&nbsp;<span style="color: %s">%d</span>' % (accent, changed)
        disabled = page.get("disabled")
        if page.get("id") == page_id and not query:
            tab = '<span style="background-color: %s; border-radius: 4px; padding: 1px 6px">%s</span>' % (tsp_html._soft_selection(ctx), label)
        else:
            tab = '<span style="color: %s; padding: 0 3px">%s</span>' % (dim if disabled else muted, label)
        if disabled:
            tab = _tip(tab, disabled if isinstance(disabled, str) else None, ctx, dim)
        else:
            tip = "%d changed from default" % changed if changed else None
            tab = _link(ctx, _event(ctx, "action", node, act="page", value=page.get("id")), tab, tip)
        current.append(tab)
    if current:
        lines.append(current)
    if lines:
        parts.append('<div style="margin: 4px 0">%s</div>' % "<br>".join(" &nbsp; ".join(line) for line in lines))

    # children placed by role
    status_child, editors, extras = None, [], []
    for child in node.get("c") or []:
        role = _props(child).get("role")
        if role == "omp.prefs.preview.status":
            status_child = child
        elif role == "omp.prefs.editor" or child.get("k") == "picker":
            editors.append(child)
        else:
            extras.append(child)
    # A picker the program opens for a row (a long choice) goes under that row, which is the focused one; at the top
    # of the card it opened out of sight whenever the reader had scrolled down.  With no such row on the page it stays
    # at the top.
    editors_html = "".join(tsp_html._card_shell(tsp_html.draw(child, ctx), ctx, "pageBg", "borderAccent") for child in editors)
    below = {"row": focus, "html": editors_html, "used": False}
    editors_at = len(parts)

    sections = [s for s in props.get("sections") or [] if isinstance(s, dict)
                and (s.get("id") == "status-line" or any(isinstance(r, dict) and r.get("id") not in (None, "") for r in s.get("rows") or []))]

    def draw_sections(items):
        out = []
        for section in items:
            out.append(_pf_section(node, ctx, section, focus, editing, query, below))
            if section.get("id") == "status-line" and status_child is not None:
                out.append(tsp_html.draw(status_child, ctx))
        return "".join(out)

    if query:
        if not sections:
            parts.append('<div style="color: %s; padding: 6px 8px">No settings match \u201c%s\u201d</div>' % (muted, tsp_html.esc(query)))
        by_id = {p.get("id"): p for p in pages}
        index, count = 0, len(sections)
        while index < count:
            group_page = sections[index].get("page")
            end = index
            while end < count and sections[end].get("page") == group_page:
                end += 1
            page = by_id.get(group_page)
            heading = tsp_html.esc((page.get("label") if page else None) or group_page or "")
            if page is not None and page.get("disabled"):
                head = _tip('<b style="color: %s">%s</b>' % (dim, heading), page["disabled"] if isinstance(page.get("disabled"), str) else None, ctx, dim)
            elif page is not None:
                head = _link(ctx, _event(ctx, "action", node, act="page", value=page.get("id")), "<b>%s</b>" % heading)
            else:
                head = "<b>%s</b>" % heading
            parts.append('<div style="margin-top: 8px">%s</div>' % head)
            parts.append(draw_sections(sections[index:end]))
            index = end
    else:
        page = next((p for p in pages if p.get("id") == page_id), None)
        if page is not None:
            number = "%02d" % (pages.index(page) + 1)
            label = page.get("label") or page.get("id") or ""
            parts.append('<div style="color: %s"><tt>%s &middot; %s</tt></div>' % (dim, number, tsp_html.esc(label)))
            parts.append('<div style="font-size: 1.2rem; font-weight: bold">%s</div>' % tsp_html.esc(label))
        if isinstance(props.get("lead"), str) and props["lead"]:
            parts.append('<div style="color: %s">%s</div>' % (muted, tsp_html.esc(props["lead"])))
        if sections:
            parts.append(draw_sections(sections))
        if not any(any(isinstance(r, dict) for r in s.get("rows") or []) for s in sections):
            parts.append('<div style="color: %s; padding: 6px 8px">Nothing to set here</div>' % muted)
    if editors_html and not below["used"]:
        parts.insert(editors_at, editors_html)
    for child in extras:
        parts.append(tsp_html.draw(child, ctx))
    return tsp_html._card_shell("".join(parts), ctx, "cardBg", "borderAccent" if focus else "border")


DRAWERS["prefs"] = _draw_prefs
