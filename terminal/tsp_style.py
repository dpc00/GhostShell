"""TSP stylesheets (the ``s`` message): the CSS a Tern program ships, read and resolved for minihtml.

A Tern surface sheet is real CSS (nesting, ``@media``, ``var()``, ``calc()``, relative colors,
``color-mix()``, ``light-dark()``, flex, grid, ``:checked``...).  Sublime's minihtml has none of
that, so this module is the engine that *resolves* a sheet statically for one element:

* ``parse(css)`` reads the sheet (nesting flattened, ``@media`` conditions kept, selectors parsed
  as Tern accepts them: type, class, id, attribute, descendant, ``>``, ``+``, the pseudo-classes
  of Tern's table, ``::before``/``::after``).  Unsupported selectors drop their rule, as in Tern.
* ``Elem`` models one element of the DOM the drawers produce (tag, classes, attributes, parent,
  siblings), so selectors are matched against the real structure, ``:checked + span`` included.
* ``compute(elem, env)`` runs the cascade: user-agent defaults, matching rules by specificity and
  order, ``!important``; custom properties inherit and resolve ``var()`` (the ``--sf-*``, ``--t1``
  ... variables come from the program palette); shorthands expand; colors and lengths are evaluated.
* ``Computed.box_style()`` writes what minihtml can draw: colour, background, borders (also from
  an inset ``box-shadow`` edge or ring), radius, margin, padding, size, font size, weight, style,
  decoration and alignment.  Layout properties (display, flex, grid, gap, order...) are read by
  the drawer (tsp_draw_el) which lays children out with the means minihtml has.

Pure Python, no Sublime import.
"""

import math
import re

MAX_SHEETS_BYTES = 256 * 1024
NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

BASE_FONT_PX = 13.0         # the design size of a Tern region: 13px draws at the terminal's size
CELL_WIDTH_PX = 8.0         # --sf-cw fallback in the spec's examples
LINE_HEIGHT_PX = 17.0       # --sf-lh

_COMMENT = re.compile(r"/\*.*?\*/", re.S)

# ---------------------------------------------------------------------------------------------
# colours
# ---------------------------------------------------------------------------------------------

_NAMED_TEXT = """
aliceblue f0f8ff antiquewhite faebd7 aqua 00ffff aquamarine 7fffd4 azure f0ffff beige f5f5dc bisque ffe4c4
black 000000 blanchedalmond ffebcd blue 0000ff blueviolet 8a2be2 brown a52a2a burlywood deb887 cadetblue 5f9ea0
chartreuse 7fff00 chocolate d2691e coral ff7f50 cornflowerblue 6495ed cornsilk fff8dc crimson dc143c cyan 00ffff
darkblue 00008b darkcyan 008b8b darkgoldenrod b8860b darkgray a9a9a9 darkgreen 006400 darkgrey a9a9a9
darkkhaki bdb76b darkmagenta 8b008b darkolivegreen 556b2f darkorange ff8c00 darkorchid 9932cc darkred 8b0000
darksalmon e9967a darkseagreen 8fbc8f darkslateblue 483d8b darkslategray 2f4f4f darkslategrey 2f4f4f
darkturquoise 00ced1 darkviolet 9400d3 deeppink ff1493 deepskyblue 00bfff dimgray 696969 dimgrey 696969
dodgerblue 1e90ff firebrick b22222 floralwhite fffaf0 forestgreen 228b22 fuchsia ff00ff gainsboro dcdcdc
ghostwhite f8f8ff gold ffd700 goldenrod daa520 gray 808080 green 008000 greenyellow adff2f grey 808080
honeydew f0fff0 hotpink ff69b4 indianred cd5c5c indigo 4b0082 ivory fffff0 khaki f0e68c lavender e6e6fa
lavenderblush fff0f5 lawngreen 7cfc00 lemonchiffon fffacd lightblue add8e6 lightcoral f08080 lightcyan e0ffff
lightgoldenrodyellow fafad2 lightgray d3d3d3 lightgreen 90ee90 lightgrey d3d3d3 lightpink ffb6c1
lightsalmon ffa07a lightseagreen 20b2aa lightskyblue 87cefa lightslategray 778899 lightslategrey 778899
lightsteelblue b0c4de lightyellow ffffe0 lime 00ff00 limegreen 32cd32 linen faf0e6 magenta ff00ff maroon 800000
mediumaquamarine 66cdaa mediumblue 0000cd mediumorchid ba55d3 mediumpurple 9370db mediumseagreen 3cb371
mediumslateblue 7b68ee mediumspringgreen 00fa9a mediumturquoise 48d1cc mediumvioletred c71585 midnightblue 191970
mintcream f5fffa mistyrose ffe4e1 moccasin ffe4b5 navajowhite ffdead navy 000080 oldlace fdf5e6 olive 808000
olivedrab 6b8e23 orange ffa500 orangered ff4500 orchid da70d6 palegoldenrod eee8aa palegreen 98fb98
paleturquoise afeeee palevioletred db7093 papayawhip ffefd5 peachpuff ffdab9 peru cd853f pink ffc0cb plum dda0dd
powderblue b0e0e6 purple 800080 rebeccapurple 663399 red ff0000 rosybrown bc8f8f royalblue 4169e1
saddlebrown 8b4513 salmon fa8072 sandybrown f4a460 seagreen 2e8b57 seashell fff5ee sienna a0522d silver c0c0c0
skyblue 87ceeb slateblue 6a5acd slategray 708090 slategrey 708090 snow fffafa springgreen 00ff7f
steelblue 4682b4 tan d2b48c teal 008080 thistle d8bfd8 tomato ff6347 turquoise 40e0d0 violet ee82ee
wheat f5deb3 white ffffff whitesmoke f5f5f5 yellow ffff00 yellowgreen 9acd32
"""
NAMED_COLORS = {}
_words = _NAMED_TEXT.split()
for _index in range(0, len(_words), 2):
    _value = int(_words[_index + 1], 16)
    NAMED_COLORS[_words[_index]] = ((_value >> 16) & 255, (_value >> 8) & 255, _value & 255)
del _words, _index, _value

_HEX = re.compile(r"^#([0-9a-fA-F]{3,4}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})$")


def _clamp(value, low, high):
    return max(low, min(high, value))


def to_hex(color):
    """(r, g, b, a) -> "#rrggbb" or "#rrggbbaa"."""
    red, green, blue, alpha = color
    base = "#%02x%02x%02x" % (int(round(_clamp(red, 0, 255))), int(round(_clamp(green, 0, 255))),
                              int(round(_clamp(blue, 0, 255))))
    if alpha < 0.999:
        base += "%02x" % int(round(_clamp(alpha, 0, 1) * 255))
    return base


def blend(front, back, alpha=None):
    """``front`` painted over the opaque ``back`` (alpha defaults to front's own)."""
    a = front[3] if alpha is None else alpha
    return (front[0] * a + back[0] * (1 - a), front[1] * a + back[1] * (1 - a),
            front[2] * a + back[2] * (1 - a), 1.0)


def with_alpha(color, alpha):
    return (color[0], color[1], color[2], _clamp(color[3] * alpha, 0, 1))


def mix(first, second, weight_second):
    """Straight sRGB mix of two colours (weight of ``second`` 0..1)."""
    w = _clamp(weight_second, 0, 1)
    return (first[0] * (1 - w) + second[0] * w, first[1] * (1 - w) + second[1] * w,
            first[2] * (1 - w) + second[2] * w, first[3] * (1 - w) + second[3] * w)


def _linear(channel):
    channel /= 255.0
    return channel / 12.92 if channel <= 0.04045 else ((channel + 0.055) / 1.055) ** 2.4


def _gamma(value):
    value = _clamp(value, 0.0, 1.0)
    return (12.92 * value if value <= 0.0031308 else 1.055 * value ** (1 / 2.4) - 0.055) * 255.0


def luminance(color):
    return 0.2126 * _linear(color[0]) + 0.7152 * _linear(color[1]) + 0.0722 * _linear(color[2])


def _contrast(first, second):
    a, b = luminance(first), luminance(second)
    if a < b:
        a, b = b, a
    return (a + 0.05) / (b + 0.05)


def _srgb_to_oklab(color):
    r, g, b = _linear(color[0]), _linear(color[1]), _linear(color[2])
    l = 0.4122214708 * r + 0.5363325363 * g + 0.0514459929 * b
    m = 0.2119034982 * r + 0.6806995451 * g + 0.1073969566 * b
    s = 0.0883024619 * r + 0.2817188376 * g + 0.6299787005 * b
    l, m, s = (math.copysign(abs(v) ** (1 / 3), v) for v in (l, m, s))
    return (0.2104542553 * l + 0.7936177850 * m - 0.0040720468 * s,
            1.9779984951 * l - 2.4285922050 * m + 0.4505937099 * s,
            0.0259040371 * l + 0.7827717662 * m - 0.8086757660 * s)


def _oklab_to_srgb(lightness, a, b):
    l = (lightness + 0.3963377774 * a + 0.2158037573 * b) ** 3
    m = (lightness - 0.1055613458 * a - 0.0638541728 * b) ** 3
    s = (lightness - 0.0894841775 * a - 1.2914855480 * b) ** 3
    return (_gamma(4.0767416621 * l - 3.3077115913 * m + 0.2309699292 * s),
            _gamma(-1.2684380046 * l + 2.6097574011 * m - 0.3413193965 * s),
            _gamma(-0.0041960863 * l - 0.7034186147 * m + 1.7076147010 * s))


def _rgb_to_hsl(color):
    r, g, b = color[0] / 255.0, color[1] / 255.0, color[2] / 255.0
    high, low = max(r, g, b), min(r, g, b)
    light = (high + low) / 2
    if high == low:
        return 0.0, 0.0, light * 100
    delta = high - low
    sat = delta / (2 - high - low) if light > 0.5 else delta / (high + low)
    if high == r:
        hue = (g - b) / delta + (6 if g < b else 0)
    elif high == g:
        hue = (b - r) / delta + 2
    else:
        hue = (r - g) / delta + 4
    return hue * 60, sat * 100, light * 100


def _hsl_to_rgb(hue, sat, light):
    hue = (hue % 360) / 360.0
    sat, light = _clamp(sat, 0, 100) / 100.0, _clamp(light, 0, 100) / 100.0
    if sat == 0:
        return light * 255, light * 255, light * 255
    q = light * (1 + sat) if light < 0.5 else light + sat - light * sat
    p = 2 * light - q

    def channel(t):
        t %= 1.0
        if t < 1 / 6:
            return p + (q - p) * 6 * t
        if t < 1 / 2:
            return q
        if t < 2 / 3:
            return p + (q - p) * (2 / 3 - t) * 6
        return p

    return channel(hue + 1 / 3) * 255, channel(hue) * 255, channel(hue - 1 / 3) * 255


# ---------------------------------------------------------------------------------------------
# generic scanning helpers
# ---------------------------------------------------------------------------------------------

def _match_paren(text, open_index):
    """Index of the ``)`` closing the ``(`` at ``open_index`` (strings and nested parens skipped), or -1."""
    depth = 0
    quote = None
    for index in range(open_index, len(text)):
        char = text[index]
        if quote:
            if char == quote:
                quote = None
            continue
        if char in "\"'":
            quote = char
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return index
    return -1


def split_top(text, separators=","):
    """Split ``text`` at top-level ``separators`` (outside parentheses, brackets and quotes)."""
    parts, depth, quote, start = [], 0, None, 0
    for index, char in enumerate(text):
        if quote:
            if char == quote:
                quote = None
            continue
        if char in "\"'":
            quote = char
        elif char in "([":
            depth += 1
        elif char in ")]":
            depth -= 1
        elif depth == 0 and char in separators:
            parts.append(text[start:index])
            start = index + 1
    parts.append(text[start:])
    return parts


def tokens(text):
    """Whitespace separated tokens of a value, functions and strings kept whole; ``/`` is a token."""
    out, depth, quote, current = [], 0, None, []
    for char in text.strip():
        if quote:
            current.append(char)
            if char == quote:
                quote = None
            continue
        if char in "\"'":
            quote = char
            current.append(char)
        elif char in "([":
            depth += 1
            current.append(char)
        elif char in ")]":
            depth -= 1
            current.append(char)
        elif depth == 0 and char.isspace():
            if current:
                out.append("".join(current))
                current = []
        elif depth == 0 and char == "/":
            if current:
                out.append("".join(current))
                current = []
            out.append("/")
        else:
            current.append(char)
    if current:
        out.append("".join(current))
    return out


# ---------------------------------------------------------------------------------------------
# calc(): lengths, numbers, channel math
# ---------------------------------------------------------------------------------------------

class _CalcError(Exception):
    pass


_CALC_NUMBER = re.compile(r"(\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?")
_CALC_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_-]*")
_ANGLE_UNITS = {"deg": 1.0, "grad": 0.9, "rad": 180 / math.pi, "turn": 360.0}


class _Calc:
    """A tiny evaluator of CSS math.  Values are (number, unit) with unit "", "px" or "%"."""

    def __init__(self, text, font_px, vw, vh, ref, names):
        self.s = text
        self.i = 0
        self.font_px, self.vw, self.vh, self.ref, self.names = font_px, vw, vh, ref, names or {}

    def run(self):
        value = self.expr()
        self.ws()
        if self.i != len(self.s):
            raise _CalcError("trailing input")
        return value

    def ws(self):
        while self.i < len(self.s) and self.s[self.i].isspace():
            self.i += 1

    def peek(self):
        self.ws()
        return self.s[self.i] if self.i < len(self.s) else ""

    def expr(self):
        value = self.term()
        while True:
            char = self.peek()
            if char not in ("+", "-"):
                return value
            self.i += 1
            other = self.term()
            value = self._add(value, other, 1 if char == "+" else -1)

    def term(self):
        value = self.factor()
        while True:
            char = self.peek()
            if char not in ("*", "/"):
                return value
            self.i += 1
            other = self.factor()
            if char == "*":
                if value[1] == "":
                    value = (value[0] * other[0], other[1])
                elif other[1] == "":
                    value = (value[0] * other[0], value[1])
                else:
                    raise _CalcError("unit times unit")
            else:
                if other[1] != "" or other[0] == 0:
                    raise _CalcError("bad divisor")
                value = (value[0] / other[0], value[1])

    def factor(self):
        char = self.peek()
        if char in ("+", "-"):
            self.i += 1
            number, unit = self.factor()
            return (-number if char == "-" else number), unit
        if char == "(":
            self.i += 1
            value = self.expr()
            if self.peek() != ")":
                raise _CalcError("missing )")
            self.i += 1
            return value
        match = _CALC_NUMBER.match(self.s, self.i)
        if match:
            self.i = match.end()
            number = float(match.group(0))
            unit_match = re.compile(r"[A-Za-z%]+").match(self.s, self.i)
            unit = ""
            if unit_match:
                self.i = unit_match.end()
                unit = unit_match.group(0).lower()
            return self._unit(number, unit)
        match = _CALC_IDENT.match(self.s, self.i)
        if not match:
            raise _CalcError("unexpected input")
        name = match.group(0)
        self.i = match.end()
        lowered = name.lower()
        if self.i < len(self.s) and self.s[self.i] == "(":
            self.i += 1
            args = []
            if self.peek() != ")":
                while True:
                    args.append(self.expr())
                    char = self.peek()
                    if char == ",":
                        self.i += 1
                        continue
                    break
            if self.peek() != ")":
                raise _CalcError("missing )")
            self.i += 1
            return self._function(lowered, args)
        if lowered in self.names:
            return (float(self.names[lowered]), "")
        if lowered == "pi":
            return (math.pi, "")
        if lowered == "e":
            return (math.e, "")
        raise _CalcError("unknown name %s" % name)

    def _unit(self, number, unit):
        if unit == "":
            return (number, "")
        if unit == "px":
            return (number, "px")
        if unit == "%":
            return (number, "%")
        if unit == "em":
            return (number * self.font_px, "px")
        if unit == "rem":
            return (number * BASE_FONT_PX, "px")
        if unit == "vw":
            return (number * self.vw / 100.0, "px")
        if unit == "vh":
            return (number * self.vh / 100.0, "px")
        if unit in _ANGLE_UNITS:
            return (number * _ANGLE_UNITS[unit], "")
        raise _CalcError("unsupported unit %s" % unit)

    def _px(self, value):
        number, unit = value
        if unit == "%":
            if self.ref is None:
                raise _CalcError("percentage without reference")
            return self.ref * number / 100.0
        return number

    def _add(self, left, right, sign):
        if left[1] == right[1]:
            return (left[0] + sign * right[0], left[1])
        if left[1] == "" and left[0] == 0:
            return (sign * right[0], right[1])
        if right[1] == "" and right[0] == 0:
            return left
        if {left[1], right[1]} == {"px", "%"}:
            return (self._px(left) + sign * self._px(right), "px")
        raise _CalcError("incompatible units")

    def _function(self, name, args):
        if name in ("calc", "") and len(args) == 1:
            return args[0]
        if not args:
            raise _CalcError("empty function")
        if name in ("min", "max"):
            kinds = {a[1] for a in args}
            if len(kinds) > 1:
                args = [(self._px(a), "px") for a in args]
            values = [a[0] for a in args]
            return ((min(values) if name == "min" else max(values)), args[0][1])
        if name == "clamp" and len(args) == 3:
            low, mid, high = args
            if len({low[1], mid[1], high[1]}) > 1:
                low, mid, high = [(self._px(a), "px") for a in args]
            return (max(low[0], min(mid[0], high[0])), mid[1])
        raise _CalcError("unsupported function %s" % name)


def calc(text, font_px=BASE_FONT_PX, vw=800.0, vh=1000.0, ref=None, names=None):
    """Evaluate a CSS math expression to (number, unit), unit in "", "px", "%"; None when it cannot be."""
    try:
        return _Calc(text, font_px, vw, vh, ref, names).run()
    except (_CalcError, ValueError, ZeroDivisionError, OverflowError):
        return None


_KEYWORD_LENGTHS = {"thin": 1.0, "medium": 3.0, "thick": 5.0}


def length(text, font_px=BASE_FONT_PX, vw=800.0, vh=1000.0, ref=None):
    """A CSS length as px (float), a pure percentage as ("%", n), or None (auto, keyword, invalid)."""
    text = (text or "").strip()
    if not text:
        return None
    if text in ("0", "+0", "-0"):
        return 0.0
    result = calc(text, font_px, vw, vh, ref)
    if result is None:
        return None
    number, unit = result
    if unit == "px":
        return number
    if unit == "%":
        return ("%", number) if ref is None else ref * number / 100.0
    return None


# ---------------------------------------------------------------------------------------------
# colour parsing
# ---------------------------------------------------------------------------------------------

_COLOR_FUNCTIONS = ("rgb", "rgba", "hsl", "hsla", "oklab", "oklch", "color-mix", "light-dark")


def is_color_token(token):
    low = token.lower()
    if low.startswith("#"):
        return bool(_HEX.match(low))
    if low in NAMED_COLORS or low in ("transparent", "currentcolor"):
        return True
    match = re.match(r"^([a-z-]+)\(", low)
    return bool(match and match.group(1) in _COLOR_FUNCTIONS)


def _take_color_token(text):
    """Split ``text`` into its leading colour token and the rest."""
    text = text.strip()
    match = re.match(r"^([A-Za-z-]+)\(", text)
    if match:
        close = _match_paren(text, match.end() - 1)
        if close < 0:
            return text, ""
        return text[:close + 1], text[close + 1:]
    parts = text.split(None, 1)
    return parts[0], parts[1] if len(parts) > 1 else ""


def _channel_value(token, names, scale, percent_base):
    """One channel of a colour function: a number, a percentage of ``percent_base``, a keyword or calc()."""
    token = token.strip()
    low = token.lower()
    if low == "none":
        return 0.0
    if low in names:
        return float(names[low])
    if low.endswith("%"):
        try:
            return float(low[:-1]) * percent_base / 100.0
        except ValueError:
            return None
    match = re.match(r"^([+-]?[\d.]+(?:e[+-]?\d+)?)(deg|grad|rad|turn)?$", low)
    if match:
        value = float(match.group(1))
        return value * _ANGLE_UNITS[match.group(2)] if match.group(2) else value
    result = calc(token, names=names)
    if result is None:
        return None
    return result[0] if result[1] != "%" else result[0] * percent_base / 100.0


def _alpha_value(token, names):
    if token is None:
        return 1.0
    token = token.strip()
    if token.endswith("%"):
        try:
            return _clamp(float(token[:-1]) / 100.0, 0, 1)
        except ValueError:
            return None
    value = _channel_value(token, names, 1.0, 1.0)
    return None if value is None else _clamp(value, 0, 1)


def _function_color(name, inner, current, dark):
    inner = inner.strip()
    origin = None
    if inner.lower().startswith("from") and (len(inner) == 4 or inner[4].isspace()):
        origin_text, inner = _take_color_token(inner[4:])
        origin = parse_color(origin_text, current, dark)
        if origin is None:
            return None
    flat = re.sub(r"\s*/\s*", " / ", inner)
    pieces = [chunk.strip() for chunk in split_top(flat, ", \t\n") if chunk.strip()]
    alpha_token = None
    if "/" in pieces:
        slash = pieces.index("/")
        alpha_token = " ".join(pieces[slash + 1:]) or None
        pieces = pieces[:slash]
    elif len(pieces) == 4:
        alpha_token = pieces.pop()
    if len(pieces) != 3:
        return None
    names = {}
    base = "rgb" if name in ("rgb", "rgba") else "hsl" if name in ("hsl", "hsla") else name
    if origin is not None:
        names["alpha"] = origin[3]
        if base == "rgb":
            names.update(r=origin[0], g=origin[1], b=origin[2])
        elif base == "hsl":
            h, s, l = _rgb_to_hsl(origin)
            names.update(h=h, s=s, l=l)
        else:
            lightness, a, b = _srgb_to_oklab(origin)
            if base == "oklab":
                names.update(l=lightness, a=a, b=b)
            else:
                names.update(l=lightness, c=math.hypot(a, b), h=math.degrees(math.atan2(b, a)) % 360)
    alpha = _alpha_value(alpha_token, names)
    if alpha is None:
        return None
    if base == "rgb":
        values = [_channel_value(p, names, 255, 255) for p in pieces]
        if None in values:
            return None
        return (_clamp(values[0], 0, 255), _clamp(values[1], 0, 255), _clamp(values[2], 0, 255), alpha)
    if base == "hsl":
        hue = _channel_value(pieces[0], names, 360, 360)
        sat = _channel_value(pieces[1], names, 100, 100)
        light = _channel_value(pieces[2], names, 100, 100)
        if None in (hue, sat, light):
            return None
        red, green, blue = _hsl_to_rgb(hue, sat, light)
        return (red, green, blue, alpha)
    lightness = _channel_value(pieces[0], names, 1.0, 1.0)
    if base == "oklab":
        a = _channel_value(pieces[1], names, 0.4, 0.4)
        b = _channel_value(pieces[2], names, 0.4, 0.4)
        if None in (lightness, a, b):
            return None
    else:
        chroma = _channel_value(pieces[1], names, 0.4, 0.4)
        hue = _channel_value(pieces[2], names, 360, 360)
        if None in (lightness, chroma, hue):
            return None
        a, b = chroma * math.cos(math.radians(hue)), chroma * math.sin(math.radians(hue))
    red, green, blue = _oklab_to_srgb(lightness, a, b)
    return (red, green, blue, alpha)


def _space_coords(color, space):
    if space == "srgb":
        return [color[0] / 255.0, color[1] / 255.0, color[2] / 255.0]
    if space == "srgb-linear":
        return [_linear(color[0]), _linear(color[1]), _linear(color[2])]
    if space == "hsl":
        h, s, l = _rgb_to_hsl(color)
        return [h, s, l]
    lightness, a, b = _srgb_to_oklab(color)
    if space == "oklch":
        return [lightness, math.hypot(a, b), math.degrees(math.atan2(b, a)) % 360]
    return [lightness, a, b]


def _from_space(coords, space, alpha):
    if space == "srgb":
        return (coords[0] * 255, coords[1] * 255, coords[2] * 255, alpha)
    if space == "srgb-linear":
        return (_gamma(coords[0]), _gamma(coords[1]), _gamma(coords[2]), alpha)
    if space == "hsl":
        red, green, blue = _hsl_to_rgb(*coords)
        return (red, green, blue, alpha)
    if space == "oklch":
        a, b = coords[1] * math.cos(math.radians(coords[2])), coords[1] * math.sin(math.radians(coords[2]))
        red, green, blue = _oklab_to_srgb(coords[0], a, b)
        return (red, green, blue, alpha)
    red, green, blue = _oklab_to_srgb(*coords)
    return (red, green, blue, alpha)


def _color_mix(inner, current, dark):
    parts = [p.strip() for p in split_top(inner, ",")]
    if len(parts) != 3:
        return None
    head = parts[0].lower().split()
    if not head or head[0] != "in" or len(head) < 2:
        return None
    space = head[1]
    hue_method = "shorter"
    if len(head) >= 4 and head[3] == "hue":
        hue_method = head[2]
    # lab, lch, hwb and the xyz spaces are mixed in oklab/oklch: the nearest space we convert to
    space = {"lab": "oklab", "xyz": "oklab", "xyz-d50": "oklab", "xyz-d65": "oklab", "lch": "oklch",
             "hwb": "hsl"}.get(space, space)
    if space not in ("srgb", "srgb-linear", "hsl", "oklab", "oklch"):
        return None
    colors, weights = [], []
    for part in parts[1:]:
        token, rest = _take_color_token(part)
        color = parse_color(token, current, dark)
        if color is None:
            return None
        rest = rest.strip()
        weight = None
        if rest:
            if not rest.endswith("%"):
                return None
            try:
                weight = float(rest[:-1])
            except ValueError:
                return None
        colors.append(color)
        weights.append(weight)
    first, second = weights
    if first is None and second is None:
        first, second = 50.0, 50.0
    elif first is None:
        first = 100.0 - second
    elif second is None:
        second = 100.0 - first
    total = first + second
    if total <= 0:
        return None
    alpha_scale = min(1.0, total / 100.0)
    w1, w2 = first / total, second / total
    a1, a2 = colors[0][3], colors[1][3]
    alpha = a1 * w1 + a2 * w2
    c1, c2 = _space_coords(colors[0], space), _space_coords(colors[1], space)
    polar = {"hsl": 0, "oklch": 2}.get(space)
    out = []
    for index in range(3):
        if index == polar:
            h1, h2 = c1[index] % 360, c2[index] % 360
            delta = h2 - h1
            if hue_method == "shorter":
                if delta > 180:
                    h1 += 360
                elif delta < -180:
                    h2 += 360
            elif hue_method == "longer":
                if 0 < delta < 180:
                    h1 += 360
                elif -180 < delta <= 0:
                    h2 += 360
            elif hue_method == "increasing":
                if delta < 0:
                    h2 += 360
            elif hue_method == "decreasing":
                if delta > 0:
                    h1 += 360
            out.append((h1 * w1 + h2 * w2) % 360)
        else:
            if alpha > 0:
                out.append((c1[index] * a1 * w1 + c2[index] * a2 * w2) / alpha)
            else:
                out.append(0.0)
    return _from_space(out, space, alpha * alpha_scale)


def parse_color(text, current=None, dark=True):
    """(r, g, b, a) (0..255, 0..1) for a CSS colour, or None.  ``current`` is the ``currentcolor``."""
    if text is None:
        return None
    text = text.strip()
    low = text.lower()
    if not low:
        return None
    if low.startswith("#"):
        match = _HEX.match(low)
        if not match:
            return None
        digits = match.group(1)
        if len(digits) in (3, 4):
            digits = "".join(c * 2 for c in digits)
        value = [int(digits[i:i + 2], 16) for i in range(0, len(digits), 2)]
        return (float(value[0]), float(value[1]), float(value[2]), (value[3] / 255.0) if len(value) == 4 else 1.0)
    if low == "transparent":
        return (0.0, 0.0, 0.0, 0.0)
    if low == "currentcolor":
        return current
    if low in NAMED_COLORS:
        red, green, blue = NAMED_COLORS[low]
        return (float(red), float(green), float(blue), 1.0)
    match = re.match(r"^([a-z-]+)\(", low)
    if match and text.endswith(")"):
        name = match.group(1)
        inner = text[match.end():-1]
        if name == "light-dark":
            options = split_top(inner, ",")
            if len(options) != 2:
                return None
            return parse_color(options[1] if dark else options[0], current, dark)
        if name == "color-mix":
            return _color_mix(inner, current, dark)
        if name in ("rgb", "rgba", "hsl", "hsla", "oklab", "oklch"):
            return _function_color(name, inner, current, dark)
    return None


# ---------------------------------------------------------------------------------------------
# theme variables
# ---------------------------------------------------------------------------------------------

_FALLBACK_TOKENS = {
    "pageBg": "#0d1117", "cardBg": "#12171d", "border": "#2a3038", "accent": "#00b4ff", "success": "#00ff88",
    "error": "#ff4757", "warning": "#ffb347", "muted": "#9ca3b0", "dim": "#6b7280", "text": "#e6eaf0",
    "toolPendingBg": "#14202b", "toolSuccessBg": "#0f2a1d", "toolErrorBg": "#2d1418", "userMessageBg": "#1b2733",
    "selectedBg": "#1b2733", "infoBg": "#14202b", "mdLink": "#00b4ff", "mdCode": "#ffb347",
    "toolDiffAdded": "#00ff88", "toolDiffRemoved": "#ff4757",
}

# Span token -> palette token (the `--sf-c-<token>` family; the same mapping text spans use).
_SPAN_TOKENS = {"muted": "muted", "dim": "dim", "accent": "accent", "success": "success", "warning": "warning",
                "error": "error", "info": "accent", "link": "mdLink", "num": "warning", "path": "muted",
                "key": "accent", "ins": "toolDiffAdded", "del": "toolDiffRemoved", "code": "mdCode"}

_ANSI = ("#000000", "#cd3131", "#0dbc79", "#e5e510", "#2472c8", "#bc3fbc", "#11a8cd", "#e5e5e5",
         "#666666", "#f14c4c", "#23d18b", "#f5f543", "#3b8eea", "#d670d6", "#29b8db", "#ffffff")

TONES = ("neutral", "accent", "info", "success", "warning", "error", "pending", "muted", "user")
_TONE_VARIABLE = {"neutral": "--sf-neutral", "accent": "--sf-accent", "info": "--sf-info", "success": "--sf-ok",
                  "warning": "--sf-warn", "error": "--sf-bad", "pending": "--live", "muted": "--sf-muted",
                  "user": "--sf-user"}


def builtin_vars(theme, dark=True):
    """The CSS custom properties Tern defines, as {"--name": "value"}, from the program's palette."""
    tokens = dict(getattr(theme, "tokens", None) or {})

    def token(name):
        return parse_color(tokens.get(name) or _FALLBACK_TOKENS.get(name) or "#808080", None, dark) or (128, 128, 128, 1)

    page, card, text = token("pageBg"), token("cardBg"), token("text")
    muted, dim, accent = token("muted"), token("dim"), token("accent")
    success, warning, error = token("success"), token("warning"), token("error")
    white, black = (255.0, 255.0, 255.0, 1.0), (0.0, 0.0, 0.0, 1.0)

    def fill(color):
        """The colour darkened until white text on it reads (4.5:1)."""
        for _ in range(12):
            if _contrast(white, color) >= 4.5:
                break
            color = mix(color, black, 0.12)
        return color

    live = (168.0, 106.0, 244.0, 1.0)
    out = {
        "--t1": to_hex(text), "--t2": to_hex(blend(text, page, 0.75)), "--t3": to_hex(muted), "--t4": to_hex(dim),
        "--ink": to_hex(text), "--ink2": to_hex(with_alpha(text, 0.66)), "--ink3": to_hex(with_alpha(text, 0.44)),
        "--ink4": to_hex(with_alpha(text, 0.14)),
        "--l1": to_hex(with_alpha(text, 0.06)), "--l2": to_hex(with_alpha(text, 0.09)),
        "--l3": to_hex(with_alpha(text, 0.14)), "--l4": to_hex(with_alpha(text, 0.22)),
        "--accent": to_hex(accent), "--accent-ink": to_hex(accent), "--accent-fill": to_hex(fill(accent)),
        "--a1": to_hex(accent), "--a2": to_hex(accent), "--grad-ink": "#05070b",
        "--ok": to_hex(success), "--ok-fill": to_hex(fill(success)), "--bad": to_hex(error),
        "--bad-fill": to_hex(fill(error)), "--warn": to_hex(warning), "--live": to_hex(live),
        "--live-fill": to_hex(fill(live)), "--ink-pen": "#ff7040", "--ink-pen-fill": "#a9391b",
        "--page": to_hex(page), "--panel": to_hex(page), "--card": to_hex(card),
        "--raise": to_hex(blend(with_alpha(text, 0.05), card)), "--code-bg": to_hex(card), "--tbl-head": to_hex(card),
        "--chip-bg": to_hex(with_alpha(text, 0.07)), "--pop-bg": to_hex(card), "--well": to_hex(with_alpha(text, 0.035)),
        "--well-line": to_hex(with_alpha(text, 0.075)), "--fog": to_hex(card), "--dim": to_hex(with_alpha(page, 0.55)),
        "--rail": to_hex(accent), "--topbar-bg": to_hex(page),
        "--tv-bg": to_hex(page), "--tv-fg": to_hex(text), "--tv-cur": to_hex(text), "--tv-cur-ink": to_hex(page),
        "--tv-sel": to_hex(with_alpha(accent, 0.24)), "--tv-font": "monospace",
        "--r-chip": "6px", "--r-ctl": "8px", "--r-card": "12px", "--r-panel": "16px",
        "--sans": "sans-serif", "--mono": "monospace", "--hand": "cursive",
        "--sf-fs": "13px", "--sf-lh": "%gpx" % LINE_HEIGHT_PX, "--sf-cw": "%gpx" % CELL_WIDTH_PX, "--sf-zoom": "1",
        "--sf-block-gap": "16px", "--sf-inner-gap": "8px",
        "--sf-card-bg": to_hex(with_alpha(white, 0.03)), "--sf-shade": to_hex(with_alpha(text, 0.03)),
        "--sf-ok": to_hex(success), "--sf-warn": to_hex(warning), "--sf-bad": to_hex(error),
        "--sf-info": "#35c7e6", "--sf-muted": to_hex(muted), "--sf-neutral": to_hex(text),
        "--sf-user": "#ff7040", "--sf-accent": to_hex(accent),
        "--sf-sel": to_hex(with_alpha(token("selectedBg"), 0.6)), "--sf-mark-bg": to_hex(with_alpha(token("selectedBg"), 0.6)),
        "--sf-status-bg": to_hex(with_alpha(page, 0.6)),
        "--sf-tint-success": to_hex(with_alpha(token("toolSuccessBg"), 0.6)),
        "--sf-tint-error": to_hex(with_alpha(token("toolErrorBg"), 0.6)),
        "--sf-tint-pending": to_hex(with_alpha(token("toolPendingBg"), 0.6)),
        "--sf-tint-user": to_hex(with_alpha(token("userMessageBg"), 0.6)),
        "--sf-tint-custom": to_hex(with_alpha(token("customMessageBg") if "customMessageBg" in tokens else card, 0.6)),
        "--sf-tint-neutral": to_hex(with_alpha(card, 0.6)), "--sf-tint-info": to_hex(with_alpha(token("infoBg"), 0.6)),
    }
    for index, value in enumerate(_ANSI):
        out["--ansi%d" % index] = value
    for span, palette in _SPAN_TOKENS.items():
        out["--sf-c-%s" % span] = to_hex(token(palette))
    for name, value in tokens.items():
        if isinstance(value, str) and not name.endswith("Bg") and re.match(r"^[A-Za-z0-9_-]+$", name):
            parsed = parse_color(value, None, dark)
            if parsed is not None:
                out["--sf-p-%s" % name] = to_hex(parsed)
    out["--tc"] = out["--sf-neutral"]
    return out


# ---------------------------------------------------------------------------------------------
# selectors
# ---------------------------------------------------------------------------------------------

class Compound:
    __slots__ = ("tag", "ids", "classes", "attrs", "pseudos")

    def __init__(self):
        self.tag = None
        self.ids = []
        self.classes = []
        self.attrs = []         # (name, op, value, case-insensitive)
        self.pseudos = []       # (name, argument)


class Selector:
    """One complex selector: parts = [(combinator before it, Compound)], the pseudo-element it ends in,
    the media conditions it is under and its specificity."""
    __slots__ = ("parts", "pseudo_element", "media", "weight", "need")

    def __init__(self, parts, pseudo_element, media, weight):
        self.parts = parts
        self.pseudo_element = pseudo_element
        self.media = media
        # classes some ancestor must carry (a quick test before the full match)
        need = set()
        for position in range(len(parts) - 1):
            if parts[position + 1][0] in (" ", ">"):
                need.update(parts[position][1].classes)
        self.need = frozenset(need)
        self.weight = weight


_IDENT = re.compile(r"-?[A-Za-z_\u0080-\uffff][A-Za-z0-9_\u0080-\uffff-]*")
_ATTR = re.compile(r"^\s*([A-Za-z_:][\w:.-]*)\s*(?:([~|^$*]?=)\s*(\"[^\"]*\"|'[^']*'|[^\s\]]+)\s*([isIS])?)?\s*$")
_NEVER = ("hover", "active", "focus", "focus-visible", "focus-within", "root")
_STRUCTURAL = ("first-child", "last-child", "only-child", "first-of-type", "last-of-type", "empty",
               "checked", "disabled")
_NTH = ("nth-child", "nth-last-child", "nth-of-type", "nth-last-of-type")
_PSEUDO_ELEMENTS = ("before", "after", "marker", "placeholder", "selection", "-webkit-scrollbar")


def _parse_nth(text):
    text = text.strip().lower().replace(" ", "")
    if text == "odd":
        return 2, 1
    if text == "even":
        return 2, 0
    match = re.match(r"^([+-]?\d*)n([+-]\d+)?$", text)
    if match:
        a = match.group(1)
        a = -1 if a == "-" else 1 if a in ("", "+") else int(a)
        return a, int(match.group(2) or 0)
    if re.match(r"^[+-]?\d+$", text):
        return 0, int(text)
    return None


def _parse_compound(text, i):
    """(Compound, next index, pseudo-element name or None); Compound is None on a syntax problem."""
    compound = Compound()
    n = len(text)
    pseudo_element = None
    start = i
    if i < n and text[i] == "*":
        compound.tag = "*"
        i += 1
    else:
        match = _IDENT.match(text, i)
        if match:
            compound.tag = match.group(0).lower()
            i = match.end()
    while i < n:
        char = text[i]
        if char == ".":
            match = _IDENT.match(text, i + 1)
            if not match:
                return None, i, None
            compound.classes.append(match.group(0))
            i = match.end()
        elif char == "#":
            match = _IDENT.match(text, i + 1)
            if not match:
                return None, i, None
            compound.ids.append(match.group(0))
            i = match.end()
        elif char == "[":
            end = i + 1
            quote = None
            while end < n and (quote or text[end] != "]"):
                if quote:
                    if text[end] == quote:
                        quote = None
                elif text[end] in "\"'":
                    quote = text[end]
                end += 1
            if end >= n:
                return None, i, None
            match = _ATTR.match(text[i + 1:end])
            if not match:
                return None, i, None
            value = match.group(3)
            if value and value[0] in "\"'":
                value = value[1:-1]
            compound.attrs.append((match.group(1).lower(), match.group(2), value, (match.group(4) or "").lower() == "i"))
            i = end + 1
        elif char == ":":
            if i + 1 < n and text[i + 1] == ":":
                match = _IDENT.match(text, i + 2)
                if not match or match.group(0).lower() not in _PSEUDO_ELEMENTS:
                    return None, i, None
                pseudo_element = match.group(0).lower()
                i = match.end()
                break
            match = _IDENT.match(text, i + 1)
            if not match:
                return None, i, None
            name = match.group(0).lower()
            i = match.end()
            argument = None
            if i < n and text[i] == "(":
                close = _match_paren(text, i)
                if close < 0:
                    return None, i, None
                argument = text[i + 1:close]
                i = close + 1
            if name in ("before", "after") and argument is None:
                pseudo_element = name
                break
            if name in _NEVER or name in _STRUCTURAL:
                if argument is not None:
                    return None, i, None
                compound.pseudos.append((name, None))
            elif name in _NTH:
                if argument is None:
                    return None, i, None
                head, _, of_text = argument.lower().partition(" of ")
                parsed = _parse_nth(head)
                if parsed is None:
                    return None, i, None
                of_list = None
                if of_text.strip():
                    of_list = _parse_compound_list(argument[len(head) + 4:])
                    if of_list is None:
                        return None, i, None
                compound.pseudos.append((name, (parsed[0], parsed[1], of_list)))
            elif name in ("not", "is", "where"):
                if argument is None:
                    return None, i, None
                inner = _parse_compound_list(argument)
                if inner is None:
                    return None, i, None
                compound.pseudos.append((name, inner))
            else:
                return None, i, None
        else:
            break
    if i == start:
        return None, i, None
    return compound, i, pseudo_element


def _parse_compound_list(text):
    out = []
    for piece in split_top(text, ","):
        piece = piece.strip()
        compound, end, pseudo_element = _parse_compound(piece, 0)
        if compound is None or end != len(piece) or pseudo_element is not None:
            return None
        out.append(compound)
    return out or None


def _compound_weight(compound):
    a = len(compound.ids)
    b = len(compound.classes) + len(compound.attrs)
    c = 1 if compound.tag not in (None, "*") else 0
    for name, argument in compound.pseudos:
        if name in ("not", "is"):
            best = max((_compound_weight(x) for x in argument), default=(0, 0, 0))
            a, b, c = a + best[0], b + best[1], c + best[2]
        elif name == "where":
            continue
        elif name in _NTH:
            b += 1
            if argument[2]:
                best = max((_compound_weight(x) for x in argument[2]), default=(0, 0, 0))
                a, b, c = a + best[0], b + best[1], c + best[2]
        else:
            b += 1
    return a, b, c


def parse_selector(text, media=()):
    """A Selector, or None when it uses something Tern's engine rejects."""
    text = text.strip()
    if not text:
        return None
    parts = []
    combinator = None
    pseudo_element = None
    i, n = 0, len(text)
    while i < n:
        j = i
        while j < n and text[j].isspace():
            j += 1
        spaced = j > i
        i = j
        if i >= n:
            break
        char = text[i]
        if char in ">+":
            if not parts or combinator is not None:
                return None
            combinator = char
            i += 1
            continue
        if char in "~|":
            return None
        if pseudo_element is not None:
            return None
        if parts and combinator is None:
            if not spaced:
                return None
            combinator = " "
        compound, i, pseudo_element = _parse_compound(text, i)
        if compound is None:
            return None
        parts.append((combinator, compound))
        combinator = None
    if not parts or combinator is not None:
        return None
    weight = [0, 0, 0]
    for _comb, compound in parts:
        w = _compound_weight(compound)
        weight = [weight[0] + w[0], weight[1] + w[1], weight[2] + w[2]]
    if pseudo_element in ("before", "after", "marker", "placeholder", "selection"):
        weight[2] += 1
    return Selector(parts, pseudo_element, tuple(media), tuple(weight))


# ---------------------------------------------------------------------------------------------
# sheet parsing
# ---------------------------------------------------------------------------------------------

class Declarations:
    """The declarations of one rule: ``normal`` and ``important`` are {property: value}."""
    __slots__ = ("normal", "important")

    def __init__(self):
        self.normal = {}
        self.important = {}

    def __bool__(self):
        return bool(self.normal or self.important)

    def items(self):
        return list(self.normal.items())


def _statements(body):
    """Split a block body into ("decl", text) and ("block", prelude, inner) items."""
    items = []
    depth, quote, start = 0, None, 0
    i, n = 0, len(body)
    while i < n:
        char = body[i]
        if quote:
            if char == "\\":
                i += 1
            elif char == quote:
                quote = None
        elif char in "\"'":
            quote = char
        elif char in "([":
            depth += 1
        elif char in ")]":
            depth -= 1
        elif depth <= 0 and char == ";":
            text = body[start:i].strip()
            if text:
                items.append(("decl", text))
            start = i + 1
        elif depth <= 0 and char == "{":
            prelude = body[start:i].strip()
            level, j, inner_quote = 1, i + 1, None
            while j < n and level:
                c = body[j]
                if inner_quote:
                    if c == inner_quote:
                        inner_quote = None
                elif c in "\"'":
                    inner_quote = c
                elif c == "{":
                    level += 1
                elif c == "}":
                    level -= 1
                j += 1
            items.append(("block", prelude, body[i + 1:j - 1 if level == 0 else j]))
            start = j
            i = j
            continue
        i += 1
    text = body[start:].strip()
    if text:
        items.append(("decl", text))
    return items


def _parse_media(prelude):
    """[(feature, value)] for a supported @media prelude, or None (the block is dropped)."""
    text = prelude.strip().lower()
    if not text or "," in text:
        return None
    text = re.sub(r"\b(screen|all|only)\b", " ", text)
    conditions = []
    for piece in re.split(r"\band\b", text):
        piece = piece.strip()
        if not piece:
            continue
        match = re.match(r"^\(\s*([a-z-]+)\s*:\s*([^)]+?)\s*\)$", piece)
        if not match:
            return None
        feature, value = match.group(1), match.group(2)
        if feature in ("max-width", "max-height"):
            number = re.match(r"^([\d.]+)px$", value)
            if not number:
                return None
            conditions.append((feature, float(number.group(1))))
        elif feature == "prefers-reduced-motion" and value == "reduce":
            conditions.append((feature, value))
        elif feature == "prefers-reduced-transparency" and value in ("reduce", "no-preference"):
            conditions.append((feature, value))
        elif feature == "prefers-contrast" and value in ("more", "no-preference"):
            conditions.append((feature, value))
        else:
            return None
    return conditions


def _resolve_nested(selector_text, parents):
    """The full selector texts of a nested selector under ``parents`` (a list of texts)."""
    text = selector_text.strip()
    if not parents:
        return [text]
    out = []
    for parent in parents:
        if "&" in text:
            out.append(text.replace("&", parent))
        elif text[:1] in ">+~":
            out.append("%s %s" % (parent, text))
        else:
            out.append("%s %s" % (parent, text))
    return out


def _parse_declaration(text, declarations):
    name, colon, value = text.partition(":")
    if not colon:
        return
    name = name.strip()
    if not name.startswith("--"):
        name = name.lower()
    value = value.strip()
    important = False
    match = re.search(r"\s*!\s*important\s*$", value, flags=re.I)
    if match:
        value = value[:match.start()].strip()
        important = True
    if not name or not value:
        return
    if important:
        declarations.important[name] = value
        declarations.normal.pop(name, None)
    else:
        declarations.normal[name] = value


def _parse_rules(text, parents, media, out):
    for item in _statements(text):
        if item[0] == "decl":
            continue                    # declarations outside a rule have nothing to apply to
        _kind, prelude, inner = item
        if prelude.startswith("@"):
            lowered = prelude.lower()
            if lowered.startswith("@media"):
                conditions = _parse_media(prelude[6:])
                if conditions is not None:
                    _parse_rules(inner, parents, media + tuple(conditions), out)
            continue                    # @keyframes, @font-face, unsupported at-rules: not drawn
        _parse_rule(prelude, inner, parents, media, out)


def _parse_rule(prelude, body, parents, media, out):
    texts = []
    for piece in split_top(prelude, ","):
        texts.extend(_resolve_nested(piece, parents))
    selectors = []
    for text in texts:
        selector = parse_selector(text, media)
        if selector is None:
            return                      # one invalid selector drops the whole rule, nested rules too
        selectors.append(selector)
    declarations = Declarations()

    def flush():
        if declarations.normal or declarations.important:
            copy = Declarations()
            copy.normal.update(declarations.normal)
            copy.important.update(declarations.important)
            for selector in selectors:
                out.append((selector, copy))
            declarations.normal.clear()
            declarations.important.clear()

    for item in _statements(body):
        if item[0] == "decl":
            _parse_declaration(item[1], declarations)
            continue
        _kind, nested_prelude, nested_body = item
        if nested_prelude.startswith("@"):
            if nested_prelude.lower().startswith("@media"):
                conditions = _parse_media(nested_prelude[6:])
                if conditions is None:
                    continue
                flush()
                inner_out = []
                _parse_rule_body_with_media(nested_body, texts, media + tuple(conditions), inner_out)
                out.extend(inner_out)
            continue
        flush()
        _parse_rule(nested_prelude, nested_body, texts, media, out)
    flush()


def _parse_rule_body_with_media(body, texts, media, out):
    """A nested @media: its body is declarations for the parent selector (and may nest further)."""
    selectors = []
    for text in texts:
        selector = parse_selector(text, media)
        if selector is None:
            return
        selectors.append(selector)
    declarations = Declarations()
    for item in _statements(body):
        if item[0] == "decl":
            _parse_declaration(item[1], declarations)
    if declarations:
        for selector in selectors:
            out.append((selector, declarations))
    for item in _statements(body):
        if item[0] == "block" and not item[1].startswith("@"):
            _parse_rule(item[1], item[2], texts, media, out)


def parse(css):
    """Rules of ``css`` as [(Selector, specificity, order, Declarations)], in cascade order."""
    css = _COMMENT.sub("", css or "")
    found = []
    _parse_rules(css, [], (), found)
    return [(selector, selector.weight, order, declarations) for order, (selector, declarations) in enumerate(found)]


# ---------------------------------------------------------------------------------------------
# the element model and selector matching
# ---------------------------------------------------------------------------------------------

class Elem:
    """One element of the DOM a drawer produces.

    ``tag`` is the tag as the browser has it (a ``form`` is a ``div``, a control is a ``span``);
    ``logical`` is what the program asked for.  ``siblings`` is the shared list of the parent's
    child elements, ``index`` this element's place in it."""

    def __init__(self, tag, classes=(), attrs=None, node=None, parent=None, siblings=None, index=0,
                 logical=None, ua=None, empty=False):
        self.tag = tag
        self.logical = logical or tag
        self.classes = tuple(classes)
        self.attrs = attrs if attrs is not None else {}
        self.node = node
        self.parent = parent
        self.siblings = siblings if siblings is not None else [self]
        self.index = index
        self.ua = ua or {}              # user-agent declarations {property: value}
        self.empty = empty
        self.kids = None                # child Elems once built (list)
        self.computed = None
        self.pseudo_computed = {}
        self.meta = {}                  # free space for the drawer (control state, list depth...)
        self.anc_classes = (parent.anc_classes | frozenset(parent.classes)) if parent is not None else frozenset()

    def previous(self):
        return self.siblings[self.index - 1] if self.index > 0 else None


def _attr_test(elem, attribute):
    name, op, value, insensitive = attribute
    have = elem.attrs.get(name)
    if have is None:
        return False
    if op is None:
        return True
    have = str(have)
    if insensitive:
        have, value = have.lower(), value.lower()
    if op == "=":
        return have == value
    if op == "~=":
        return value in have.split() if value and not any(c.isspace() for c in value) else False
    if op == "|=":
        return have == value or have.startswith(value + "-")
    if op == "^=":
        return bool(value) and have.startswith(value)
    if op == "$=":
        return bool(value) and have.endswith(value)
    if op == "*=":
        return bool(value) and value in have
    return False


def _nth_ok(a, b, position):
    if a == 0:
        return position == b
    offset = position - b
    return offset % a == 0 and offset // a >= 0


def _pseudo_test(name, argument, elem):
    if name in _NEVER:
        return False
    if name == "checked":
        return "checked" in elem.attrs
    if name == "disabled":
        return "disabled" in elem.attrs
    if name == "empty":
        return elem.empty
    siblings = elem.siblings
    if name in ("first-child", "last-child", "only-child"):
        first = elem.index == 0
        last = elem.index == len(siblings) - 1
        return first if name == "first-child" else last if name == "last-child" else (first and last)
    if name in ("first-of-type", "last-of-type"):
        same = [s for s in siblings if s.tag == elem.tag]
        return (same[0] is elem) if name == "first-of-type" else (same[-1] is elem)
    if name in ("not", "is", "where"):
        hit = any(_compound_matches(c, elem) for c in argument)
        return (not hit) if name == "not" else hit
    if name in _NTH:
        a, b, of_list = argument
        if name.endswith("of-type"):
            pool = [s for s in siblings if s.tag == elem.tag]
        elif of_list:
            pool = [s for s in siblings if any(_compound_matches(c, s) for c in of_list)]
            if elem not in pool:
                return False
        else:
            pool = siblings
        position = pool.index(elem) + 1
        if "last" in name:
            position = len(pool) - position + 1
        return _nth_ok(a, b, position)
    return False


def _compound_matches(compound, elem):
    if compound.tag not in (None, "*") and compound.tag != elem.tag:
        return False
    for identifier in compound.ids:
        if elem.attrs.get("id") != identifier:
            return False
    for name in compound.classes:
        if name not in elem.classes:
            return False
    for attribute in compound.attrs:
        if not _attr_test(elem, attribute):
            return False
    for name, argument in compound.pseudos:
        if not _pseudo_test(name, argument, elem):
            return False
    return True


def _match_from(parts, index, elem):
    combinator, compound = parts[index]
    if not _compound_matches(compound, elem):
        return False
    if index == 0:
        return True
    if combinator == ">":
        return elem.parent is not None and _match_from(parts, index - 1, elem.parent)
    if combinator == "+":
        previous = elem.previous()
        return previous is not None and _match_from(parts, index - 1, previous)
    ancestor = elem.parent
    while ancestor is not None:
        if _match_from(parts, index - 1, ancestor):
            return True
        ancestor = ancestor.parent
    return False


def matches(selector, elem):
    """True when ``elem`` matches ``selector`` (its pseudo-element, if any, is not considered)."""
    return _match_from(selector.parts, len(selector.parts) - 1, elem)


# ---------------------------------------------------------------------------------------------
# environment, cascade
# ---------------------------------------------------------------------------------------------

class Env:
    """What the cascade needs for one drawing: the theme, the parsed sheets and the viewport."""

    def __init__(self, theme, rules=(), viewport=(800.0, 1000.0), dark=None):
        self.theme = theme
        self.rules = tuple(rules)
        self.vw, self.vh = float(viewport[0]), float(viewport[1])
        self.dark = bool(getattr(theme, "dark", True)) if dark is None else dark
        self.vars = builtin_vars(theme, self.dark)
        self._index = None
        self._root = None

    def media_ok(self, media):
        for feature, value in media:
            if feature == "max-width" and self.vw > value:
                return False
            if feature == "max-height" and self.vh > value:
                return False
            if feature == "prefers-reduced-motion":
                return False
            if feature == "prefers-reduced-transparency" and value == "reduce":
                return False
            if feature == "prefers-contrast" and value == "more":
                return False
        return True

    def _build_index(self):
        index = {"c": {}, "t": {}, "*": []}
        for position, rule in enumerate(self.rules):
            selector = rule[0]
            if not self.media_ok(selector.media):
                continue
            last = selector.parts[-1][1]
            if last.classes:
                index["c"].setdefault(last.classes[0], []).append(position)
            elif last.tag not in (None, "*"):
                index["t"].setdefault(last.tag, []).append(position)
            else:
                index["*"].append(position)
        self._index = index

    def candidates(self, elem, pseudo_element=None):
        """Matching rules [(selector, weight, order, declarations)] for ``elem``."""
        if self._index is None:
            self._build_index()
        index = self._index
        positions = set(index["*"])
        positions.update(index["t"].get(elem.tag, ()))
        for name in elem.classes:
            positions.update(index["c"].get(name, ()))
        found = []
        for position in positions:
            rule = self.rules[position]
            if rule[0].pseudo_element != pseudo_element:
                continue
            if rule[0].need and not rule[0].need <= elem.anc_classes:
                continue
            if matches(rule[0], elem):
                found.append(rule)
        found.sort(key=lambda rule: (rule[1], rule[2]))
        return found

    def root_computed(self):
        if self._root is None:
            root = Computed(None, None, self)
            root.customs = dict(self.vars)
            root.font_px = BASE_FONT_PX
            root.color = parse_color(self.vars["--t1"]) or (230.0, 234.0, 240.0, 1.0)
            root.bg = parse_color(self.vars["--page"]) or (13.0, 17.0, 23.0, 1.0)
            self._root = root
        return self._root


_INHERITED = frozenset((
    "color", "font-family", "font-size", "font-style", "font-weight", "line-height", "text-align", "white-space",
    "text-transform", "list-style-type", "list-style-position", "visibility", "letter-spacing", "cursor",
    "text-wrap", "word-break", "overflow-wrap", "tab-size", "font-variant-numeric"))

_LONGHANDS = {
    "margin": ("margin-top", "margin-right", "margin-bottom", "margin-left"),
    "padding": ("padding-top", "padding-right", "padding-bottom", "padding-left"),
    "inset": ("top", "right", "bottom", "left"),
    "border-width": tuple("border-%s-width" % s for s in ("top", "right", "bottom", "left")),
    "border-style": tuple("border-%s-style" % s for s in ("top", "right", "bottom", "left")),
    "border-color": tuple("border-%s-color" % s for s in ("top", "right", "bottom", "left")),
    "border": tuple("border-%s-%s" % (s, k) for s in ("top", "right", "bottom", "left") for k in ("width", "style", "color")),
    "border-radius": ("border-top-left-radius", "border-top-right-radius", "border-bottom-right-radius",
                      "border-bottom-left-radius"),
    "background": ("background-color", "background-image"),
    "font": ("font-style", "font-weight", "font-size", "line-height", "font-family"),
    "flex": ("flex-grow", "flex-shrink", "flex-basis"),
    "flex-flow": ("flex-direction", "flex-wrap"),
    "gap": ("row-gap", "column-gap"),
    "place-items": ("align-items", "justify-items"),
    "place-content": ("align-content", "justify-content"),
    "overflow": ("overflow-x", "overflow-y"),
    "text-decoration": ("text-decoration-line",),
    "list-style": ("list-style-type", "list-style-position"),
    "grid-template": ("grid-template-rows", "grid-template-columns"),
}
for _side in ("top", "right", "bottom", "left"):
    _LONGHANDS["border-%s" % _side] = tuple("border-%s-%s" % (_side, k) for k in ("width", "style", "color"))
del _side

_ALIASES = {"grid-gap": "gap", "grid-row-gap": "row-gap", "grid-column-gap": "column-gap", "word-wrap": "overflow-wrap",
            "-webkit-user-select": "user-select", "-webkit-box-orient": "-webkit-box-orient"}

_BORDER_STYLES = frozenset(("none", "hidden", "solid", "dashed", "dotted", "double", "groove", "ridge", "inset", "outset"))
_FONT_SIZES = {"xx-small": 9.0, "x-small": 10.0, "small": 11.5, "medium": 13.0, "large": 15.0, "x-large": 18.0,
               "xx-large": 24.0}
_LIST_TYPES = frozenset(("none", "disc", "circle", "square", "decimal"))


def _box4(values):
    count = len(values)
    if count == 1:
        return [values[0]] * 4
    if count == 2:
        return [values[0], values[1], values[0], values[1]]
    if count == 3:
        return [values[0], values[1], values[2], values[1]]
    if count == 4:
        return list(values)
    return None


def _is_length_like(token):
    return bool(re.match(r"^[-+]?[\d.]", token)) or token.startswith(("calc(", "min(", "max(", "clamp("))


def expand(prop, value):
    """Longhands {name: value} of a (possibly shorthand) declaration, or None when invalid."""
    prop = _ALIASES.get(prop, prop)
    toks = tokens(value)
    if not toks:
        return None
    if prop in ("margin", "padding", "inset", "border-width", "border-style", "border-color"):
        values = _box4(toks)
        return None if values is None else dict(zip(_LONGHANDS[prop], values))
    if prop == "border" or (prop.startswith("border-") and prop[7:] in ("top", "right", "bottom", "left")):
        width, style, color = "medium", "none", "currentcolor"
        for token in toks:
            low = token.lower()
            if low in _BORDER_STYLES:
                style = low
            elif low in _KEYWORD_LENGTHS or _is_length_like(low):
                width = token
            else:
                color = token
        sides = ("top", "right", "bottom", "left") if prop == "border" else (prop[7:],)
        out = {}
        for side in sides:
            out["border-%s-width" % side] = width
            out["border-%s-style" % side] = style
            out["border-%s-color" % side] = color
        return out
    if prop == "border-radius":
        horizontal = toks[:toks.index("/")] if "/" in toks else toks
        values = _box4(horizontal)
        return None if values is None else dict(zip(_LONGHANDS["border-radius"], values))
    if prop == "background":
        layers = split_top(value, ",")
        color, image = "transparent", "none"
        for layer in layers:
            for token in tokens(layer):
                low = token.lower()
                if is_color_token(token):
                    color = token
                elif low.startswith(("linear-gradient(", "radial-gradient(", "repeating-linear-gradient(",
                                     "repeating-radial-gradient(")):
                    if image == "none":
                        image = token
        return {"background-color": color, "background-image": image}
    if prop == "font":
        if "/" in toks:
            slash = toks.index("/")
            size_index = slash - 1
            if size_index < 0 or slash + 1 >= len(toks):
                return None
            out = {"font-size": toks[size_index], "line-height": toks[slash + 1],
                   "font-family": " ".join(toks[slash + 2:])}
            lead = toks[:size_index]
        else:
            size_index = next((n for n, t in enumerate(toks) if _is_length_like(t) or t.lower() in _FONT_SIZES), None)
            if size_index is None or size_index + 1 >= len(toks):
                return None
            out = {"font-size": toks[size_index], "line-height": "normal", "font-family": " ".join(toks[size_index + 1:])}
            lead = toks[:size_index]
        out["font-style"], out["font-weight"] = "normal", "normal"
        for token in lead:
            low = token.lower()
            if low in ("italic", "oblique"):
                out["font-style"] = low
            elif low in ("bold", "bolder", "lighter") or re.match(r"^\d+$", low):
                out["font-weight"] = low
        return out
    if prop == "flex":
        low = value.strip().lower()
        if low == "none":
            return {"flex-grow": "0", "flex-shrink": "0", "flex-basis": "auto"}
        if low == "auto":
            return {"flex-grow": "1", "flex-shrink": "1", "flex-basis": "auto"}
        numbers = [t for t in toks if re.match(r"^[\d.]+$", t)]
        basis = next((t for t in toks if not re.match(r"^[\d.]+$", t)), "0%")
        return {"flex-grow": numbers[0] if numbers else "1", "flex-shrink": numbers[1] if len(numbers) > 1 else "1",
                "flex-basis": basis}
    if prop == "flex-flow":
        out = {"flex-direction": "row", "flex-wrap": "nowrap"}
        for token in toks:
            low = token.lower()
            if low in ("row", "row-reverse", "column", "column-reverse"):
                out["flex-direction"] = low
            elif low in ("wrap", "nowrap", "wrap-reverse"):
                out["flex-wrap"] = low
        return out
    if prop == "gap":
        return {"row-gap": toks[0], "column-gap": toks[1] if len(toks) > 1 else toks[0]}
    if prop == "place-items":
        return {"align-items": toks[0], "justify-items": toks[1] if len(toks) > 1 else toks[0]}
    if prop == "place-content":
        return {"align-content": toks[0], "justify-content": toks[1] if len(toks) > 1 else toks[0]}
    if prop == "overflow":
        return {"overflow-x": toks[0], "overflow-y": toks[1] if len(toks) > 1 else toks[0]}
    if prop == "text-decoration":
        lines = [t.lower() for t in toks if t.lower() in ("underline", "overline", "line-through", "none")]
        return {"text-decoration-line": " ".join(lines) if lines else "none"}
    if prop == "list-style":
        out = {"list-style-type": "disc", "list-style-position": "outside"}
        for token in toks:
            low = token.lower()
            if low in _LIST_TYPES or low.startswith(("\"", "'")):
                out["list-style-type"] = low
            elif low in ("inside", "outside"):
                out["list-style-position"] = low
        return out
    if prop == "grid-template":
        if "/" in toks:
            slash = toks.index("/")
            return {"grid-template-rows": " ".join(toks[:slash]), "grid-template-columns": " ".join(toks[slash + 1:])}
        return {"grid-template-rows": value.strip()}
    return {prop: value.strip()}


def _substitute(value, get, depth=0):
    """``value`` with var() replaced through ``get(name)``; None when a var cannot be resolved."""
    if "var(" not in value:
        return value
    if depth > 32:
        return None
    out = []
    i = 0
    while True:
        j = value.find("var(", i)
        if j < 0:
            out.append(value[i:])
            break
        if j > 0 and (value[j - 1].isalnum() or value[j - 1] in "-_"):
            out.append(value[i:j + 4])
            i = j + 4
            continue
        out.append(value[i:j])
        close = _match_paren(value, j + 3)
        if close < 0:
            return None
        inner = value[j + 4:close]
        pieces = split_top(inner, ",")
        name = pieces[0].strip()
        fallback = ",".join(pieces[1:]).strip() if len(pieces) > 1 else None
        found = get(name)
        if found is None and fallback is not None:
            found = _substitute(fallback, get, depth + 1)
        if found is None:
            return None
        out.append(found)
        i = close + 1
    return "".join(out)


class Computed:
    """The resolved style of one element (or pseudo-element)."""

    def __init__(self, elem, parent, env):
        self.elem = elem
        self.parent = parent
        self.env = env
        self.props = {}
        self.customs = dict(parent.customs) if parent is not None else {}
        self.font_px = parent.font_px if parent is not None else BASE_FONT_PX
        self.color = parent.color if parent is not None else (230.0, 234.0, 240.0, 1.0)
        self.bg = parent.bg if parent is not None else (13.0, 17.0, 23.0, 1.0)
        self.opacity = 1.0
        self.bold = parent.bold if parent is not None else False

    # -- raw access --------------------------------------------------------------------------

    def get(self, prop, default=None):
        return self.props.get(prop, default)

    def inherited(self, prop, default=None):
        node = self
        while node is not None:
            if prop in node.props:
                return node.props[prop]
            node = node.parent
        return default

    @property
    def display(self):
        return (self.props.get("display") or "inline").strip().lower()

    # -- typed access ------------------------------------------------------------------------

    def length(self, prop, ref=None):
        value = self.props.get(prop)
        if value is None:
            return None
        return length(value, self.font_px, self.env.vw, self.env.vh, ref)

    def px(self, prop, default=0.0):
        value = self.length(prop)
        return value if isinstance(value, float) else default

    def color_of(self, prop):
        value = self.props.get(prop)
        if value is None:
            return None
        return parse_color(value, self.color, self.env.dark)

    def number(self, prop, default=None):
        value = self.props.get(prop)
        if value is None:
            return default
        result = calc(value)
        if result is None:
            return default
        if result[1] == "%":
            return result[0] / 100.0
        return result[0]

    def family(self):
        """"mono", "sans" or None for the (inherited) font-family."""
        value = self.inherited("font-family")
        if not value:
            return None
        low = value.lower()
        if any(word in low for word in ("monospace", "mono", "courier", "consolas", "menlo", "code")):
            return "mono"
        if any(word in low for word in ("sans", "serif", "system-ui", "arial", "helvetica", "geist", "cursive", "fantasy")):
            return "sans"
        return None

    # -- emission ----------------------------------------------------------------------------

    def _borders(self):
        """{side: (width px, rgba)} for the sides with a visible border, plus the box-shadow edges."""
        sides = {}
        for side in ("top", "right", "bottom", "left"):
            style = (self.props.get("border-%s-style" % side) or "none").lower()
            if style in ("none", "hidden"):
                continue
            raw = self.props.get("border-%s-width" % side) or "medium"
            width = _KEYWORD_LENGTHS.get(raw.lower())
            if width is None:
                value = length(raw, self.font_px, self.env.vw, self.env.vh)
                width = value if isinstance(value, float) else 0.0
            if width <= 0:
                continue
            color = parse_color(self.props.get("border-%s-color" % side) or "currentcolor", self.color, self.env.dark)
            if color is None:
                color = self.color
            sides[side] = (width, color)
        shadow = self.props.get("box-shadow")
        if shadow and shadow.strip().lower() != "none":
            for edge_side, width, color in _shadow_edges(shadow, self):
                sides.setdefault(edge_side, (width, color))
        return sides

    def box_style(self, inline=False, skip=()):
        """(style text, {"mono": bool}) for the declarations minihtml draws.  ``skip`` names groups to leave
        out: "margin", "padding-h", "padding-v", "size", "text-align", "background", "border"."""
        out = []
        flags = {"mono": False, "sans": False}
        parent = self.parent
        opacity = self.opacity
        hidden = (self.inherited("visibility") or "visible").lower() in ("hidden", "collapse")
        declared_color = "color" in self.props
        if declared_color or opacity < 1 or hidden:
            color = self.color
            if hidden:
                color = (parent.bg if parent is not None else self.bg)
            elif opacity < 1:
                color = blend(color, parent.bg if parent is not None else self.bg, color[3] * opacity)
            out.append("color: %s" % to_hex(color))
        if "background" not in skip and not hidden:
            background = self.color_of("background-color")
            if background is None or background[3] == 0:
                image = (self.props.get("background-image") or "none").lower()
                if image != "none" and (background is None or background[3] == 0):
                    background = self._gradient_color(self.props["background-image"]) or background
            if background is not None and background[3] > 0:
                out.append("background-color: %s" % to_hex(with_alpha(background, opacity)))
        borders = {} if ("border" in skip or hidden) else self._borders()
        if borders:
            values = [borders.get(s) for s in ("top", "right", "bottom", "left")]
            if all(values) and all(v == values[0] for v in values):
                out.append("border: %s solid %s" % (_px(values[0][0]), to_hex(with_alpha(values[0][1], opacity))))
            else:
                for side in ("top", "right", "bottom", "left"):
                    if side in borders:
                        out.append("border-%s: %s solid %s" % (side, _px(borders[side][0]),
                                                               to_hex(with_alpha(borders[side][1], opacity))))
        radius = self._radius()
        if radius:
            out.append("border-radius: %s" % radius)
        if "margin" not in skip:
            for side in ("top", "right", "bottom", "left"):
                if inline and side in ("top", "bottom"):
                    continue
                value = self.length("margin-%s" % side)
                if isinstance(value, float) and abs(value) >= 0.5:
                    out.append("margin-%s: %s" % (side, _px(value)))
        for side in ("top", "right", "bottom", "left"):
            group = "padding-h" if side in ("left", "right") else "padding-v"
            if group in skip:
                continue
            value = self.length("padding-%s" % side)
            if isinstance(value, float) and value >= 0.5:
                out.append("padding-%s: %s" % (side, _px(value)))
        if "size" not in skip and not inline:
            for prop in ("width", "min-width", "max-width", "height", "min-height", "max-height"):
                value = self.length(prop)
                if isinstance(value, float) and value >= 0:
                    out.append("%s: %s" % (prop, _px(value)))
                elif isinstance(value, tuple):
                    out.append("%s: %s%%" % (prop, ("%g" % round(value[1], 2))))
        if "font-size" in self.props and parent is not None and abs(self.font_px - parent.font_px) > 0.01:
            out.append("font-size: %srem" % ("%.3g" % (self.font_px / BASE_FONT_PX)))
        if "font-weight" in self.props and parent is not None and self.bold != parent.bold:
            out.append("font-weight: %s" % ("bold" if self.bold else "normal"))
        if "font-style" in self.props:
            italic = self.props["font-style"].lower() in ("italic", "oblique")
            parent_italic = (parent.inherited("font-style") or "normal").lower() in ("italic", "oblique") if parent else False
            if italic != parent_italic:
                out.append("font-style: %s" % ("italic" if italic else "normal"))
        if "text-decoration-line" in self.props:
            lines = [w for w in self.props["text-decoration-line"].lower().split() if w in ("underline", "line-through")]
            out.append("text-decoration: %s" % (" ".join(lines) if lines else "none"))
        if "text-align" not in skip and "text-align" in self.props:
            align = self.props["text-align"].lower()
            align = {"start": "left", "end": "right", "match-parent": "left", "justify": "left"}.get(align, align)
            if align in ("left", "right", "center"):
                out.append("text-align: %s" % align)
        family = self.family()
        parent_family = parent.family() if parent is not None else None
        if "font-family" in self.props and family != parent_family:
            if family == "mono":
                flags["mono"] = True
            elif family == "sans":
                flags["sans"] = True
                out.append("font-family: sans-serif")
        return "; ".join(out), flags

    def _radius(self):
        corners = []
        for name in _LONGHANDS["border-radius"]:
            raw = self.props.get(name)
            first = raw.split()[0] if raw else "0"
            value = length(first, self.font_px, self.env.vw, self.env.vh)
            if isinstance(value, tuple):
                value = 999.0 if value[1] >= 50 else None
            corners.append(value if isinstance(value, float) else 0.0)
        if not any(c > 0 for c in corners):
            return ""
        corners = [min(c, 999.0) for c in corners]
        if all(c == corners[0] for c in corners):
            return _px(corners[0])
        return " ".join(_px(c) for c in corners)

    def _gradient_color(self, image):
        """A gradient has no minihtml form: its first and last stop mixed (the nearest honest flat colour)."""
        open_index = image.find("(")
        close = _match_paren(image, open_index) if open_index >= 0 else -1
        if close < 0:
            return None
        stops = []
        for piece in split_top(image[open_index + 1:close], ","):
            piece = piece.strip()
            if not piece:
                continue
            token, _rest = _take_color_token(piece)
            color = parse_color(token, self.color, self.env.dark)
            if color is not None:
                stops.append(color)
        if not stops:
            return None
        return mix(stops[0], stops[-1], 0.5) if len(stops) > 1 else stops[0]


def _px(value):
    return "%spx" % ("%g" % round(value, 2))


def _shadow_edges(text, computed):
    """The (side, width, colour) borders an inset shadow edge or ring stands for; other shadows have no form."""
    edges = []
    for shadow in split_top(text, ","):
        toks = tokens(shadow)
        inset = False
        numbers = []
        color = None
        for token in toks:
            if token.lower() == "inset":
                inset = True
            elif _is_length_like(token):
                value = length(token, computed.font_px, computed.env.vw, computed.env.vh)
                numbers.append(value if isinstance(value, float) else 0.0)
            else:
                color = parse_color(token, computed.color, computed.env.dark)
        if len(numbers) < 2 or color is None:
            continue
        x, y = numbers[0], numbers[1]
        blur = numbers[2] if len(numbers) > 2 else 0.0
        spread = numbers[3] if len(numbers) > 3 else 0.0
        if blur > 0:
            continue
        if x == 0 and y == 0 and spread > 0:
            for side in ("top", "right", "bottom", "left"):
                edges.append((side, spread, color))
        elif inset and spread == 0 and (x != 0 or y != 0):
            if x > 0:
                edges.append(("left", x, color))
            if x < 0:
                edges.append(("right", -x, color))
            if y > 0:
                edges.append(("top", y, color))
            if y < 0:
                edges.append(("bottom", -y, color))
    return edges


def _lookup_custom(name, raw, parent_customs, stack, cache):
    if name in cache:
        return cache[name]
    if name in raw:
        if name in stack:
            return None
        stack.append(name)
        value = _substitute(raw[name], lambda n: _lookup_custom(n, raw, parent_customs, stack, cache))
        stack.pop()
        if value is None:
            cache[name] = None
            return None
        cache[name] = value.strip()
        return cache[name]
    return parent_customs.get(name)


def _cascade_entries(elem, env, pseudo_element=None):
    entries = []
    sequence = 0
    if pseudo_element is None:
        for prop, value in elem.ua.items():
            entries.append(((0, (-1, 0, 0), 0, sequence), prop, value))
            sequence += 1
    for selector, weight, order, declarations in env.candidates(elem, pseudo_element):
        for prop, value in declarations.normal.items():
            entries.append(((0, weight, order, sequence), prop, value))
            sequence += 1
        for prop, value in declarations.important.items():
            entries.append(((1, weight, order, sequence), prop, value))
            sequence += 1
    entries.sort(key=lambda entry: entry[0])
    return entries


def _derive(computed, parent):
    """Fill the numbers that depend on the cascade's result (font size, colour, weight, opacity...)."""
    env = computed.env
    props = computed.props
    size = props.get("font-size")
    if size:
        low = size.lower()
        parent_px = parent.font_px
        if low in _FONT_SIZES:
            computed.font_px = _FONT_SIZES[low]
        elif low == "larger":
            computed.font_px = parent_px * 1.2
        elif low == "smaller":
            computed.font_px = parent_px / 1.2
        else:
            value = length(size, parent_px, env.vw, env.vh, parent_px)
            if isinstance(value, float) and value > 0:
                computed.font_px = value
    color = props.get("color")
    if color:
        parsed = parse_color(color, parent.color, env.dark)
        if parsed is not None:
            computed.color = parsed
    weight = props.get("font-weight")
    if weight:
        low = weight.lower()
        if low == "bold":
            computed.bold = True
        elif low == "normal":
            computed.bold = False
        elif low == "bolder":
            computed.bold = True
        elif low == "lighter":
            computed.bold = False
        else:
            try:
                computed.bold = float(low) >= 600
            except ValueError:
                pass
    opacity = props.get("opacity")
    if opacity:
        value = calc(opacity)
        if value is not None:
            computed.opacity = _clamp(value[0] / 100.0 if value[1] == "%" else value[0], 0.0, 1.0)
    background = computed.color_of("background-color")
    if (background is None or background[3] == 0) and props.get("background-image", "none").lower() != "none":
        background = computed._gradient_color(props["background-image"]) or background
    if background is not None and background[3] > 0:
        computed.bg = blend(background, parent.bg, background[3] * computed.opacity)


def compute(elem, env):
    """The Computed style of ``elem`` (cached on the element)."""
    if elem.computed is not None:
        return elem.computed
    parent = compute(elem.parent, env) if elem.parent is not None else env.root_computed()
    computed = Computed(elem, parent, env)
    entries = _cascade_entries(elem, env)
    raw_customs = {}
    tone = elem.attrs.get("data-tone")
    if tone in _TONE_VARIABLE:
        raw_customs["--tc"] = "var(%s)" % _TONE_VARIABLE[tone]
    for _key, prop, value in entries:
        if prop.startswith("--"):
            raw_customs[prop] = value
    cache = {}
    for name in raw_customs:
        resolved = _lookup_custom(name, raw_customs, parent.customs, [], cache)
        if resolved is not None:
            computed.customs[name] = resolved
    get = lambda name: computed.customs.get(name)
    _apply_entries(computed, entries, parent, get)
    _derive(computed, parent)
    elem.computed = computed
    return computed


def _apply_entries(computed, entries, parent, get):
    props = computed.props
    for _key, prop, value in entries:
        if prop.startswith("--"):
            continue
        prop = _ALIASES.get(prop, prop)
        resolved = _substitute(value, get)
        names = _LONGHANDS.get(prop, (prop,))
        if resolved is None:
            for name in names:
                props.pop(name, None)
            continue
        low = resolved.strip().lower()
        if prop == "all":
            if low in ("initial", "unset"):
                props.clear()
            continue
        if low in ("inherit", "initial", "unset"):
            for name in names:
                if low == "inherit":
                    source = parent.inherited(name) if name in _INHERITED else parent.props.get(name)
                    if source is None:
                        props.pop(name, None)
                    else:
                        props[name] = source
                else:
                    props.pop(name, None)
            continue
        expanded = expand(prop, resolved)
        if expanded is None:
            continue
        props.update(expanded)


def compute_pseudo(elem, env, name):
    """The Computed style of ``elem``'s ::before / ::after (None when there is no generated content)."""
    if name in elem.pseudo_computed:
        return elem.pseudo_computed[name]
    parent = compute(elem, env)
    entries = _cascade_entries(elem, env, name)
    result = None
    if entries:
        computed = Computed(elem, parent, env)
        raw_customs = {p: v for _k, p, v in entries if p.startswith("--")}
        cache = {}
        for custom in raw_customs:
            resolved = _lookup_custom(custom, raw_customs, parent.customs, [], cache)
            if resolved is not None:
                computed.customs[custom] = resolved
        _apply_entries(computed, entries, parent, lambda n: computed.customs.get(n))
        _derive(computed, parent)
        content = computed.props.get("content")
        if content and content.strip().lower() not in ("none", "normal"):
            text = _content_text(content, elem)
            if text is not None:
                computed.props["content"] = text
                result = computed
    elem.pseudo_computed[name] = result
    return result


def _content_text(content, elem):
    out = []
    for token in tokens(content):
        if token[:1] in "\"'" and len(token) >= 2:
            out.append(re.sub(r"\\([0-9a-fA-F]{1,6})\s?", lambda m: chr(int(m.group(1), 16)), token[1:-1]))
        elif token.lower().startswith("attr(") and token.endswith(")"):
            out.append(str(elem.attrs.get(token[5:-1].strip().lower(), "")))
        else:
            return None
    return "".join(out)


# ---------------------------------------------------------------------------------------------
# compatibility: the old tsp_html drawer asked for a style attribute of one node
# ---------------------------------------------------------------------------------------------

def style_for(rules, node, ancestors, theme=None):
    """The style attribute text (may be "") for an ``el`` node under ``rules`` (see parse)."""
    if not rules:
        return ""
    env = Env(theme, rules)
    parent = None
    for ancestor in list(ancestors) + [node]:
        props = ancestor.get("p") or {}
        if ancestor.get("k") == "el":
            tag = str(props.get("tag") or "div").lower()
            classes = ["sf", "sf-el"] + str(props.get("class") or "").split()
        else:
            tag = ""
            classes = ["sf", "sf-%s" % ancestor.get("k")]
        parent = Elem(tag, classes, {}, ancestor, parent)
    return compute(parent, env).box_style()[0]
