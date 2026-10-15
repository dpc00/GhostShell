"""TSP stylesheets (the ``s`` message): a safe subset of a program's CSS, applied to ``el`` nodes.

Sublime's minihtml has no flex, grid, var(), box-shadow or :checked, so most of what a Tern
program writes cannot be shown.  This module reads the sheet, matches its rules against an
``el`` node (tag, .class, #id, ``*``, descendant and ``>`` combinators, comma lists) and keeps
only declarations minihtml draws: colour, background, padding, margin, border-radius,
font-weight, font-style, text-align, text-decoration.  Everything else is dropped.  This does
not make the view look like Tern's; it only honours the simple rules a program writes.
"""

import re

MAX_SHEETS_BYTES = 256 * 1024
NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

_COMMENT = re.compile(r"/\*.*?\*/", re.S)
_COLOR = re.compile(r"^(#[0-9a-fA-F]{3,8}|rgba?\([\d.,%\s/]+\)|[a-zA-Z]+)$")
_LENGTH = re.compile(r"^-?[\d.]+(px|rem|em|%)?$")
_VAR = re.compile(r"^var\(\s*--sf-c-([A-Za-z0-9]+)\s*(?:,\s*(.+?))?\s*\)$")
_SIMPLE = re.compile(r"(\*|[A-Za-z][A-Za-z0-9-]*)|\.([A-Za-z0-9_-]+)|#([A-Za-z0-9_-]+)|(.)")

_SPACING = ("padding", "padding-top", "padding-right", "padding-bottom", "padding-left",
            "margin", "margin-top", "margin-right", "margin-bottom", "margin-left")
_KEYWORDS = {
    "font-weight": ("bold", "normal", "100", "200", "300", "400", "500", "600", "700", "800", "900"),
    "font-style": ("italic", "normal"),
    "text-align": ("left", "right", "center"),
    "text-decoration": ("underline", "none", "line-through"),
}


def parse(css):
    """Rules of ``css`` as [(selector parts, specificity, order, {property: value})].

    ``selector parts`` is a list of (combinator, tag, classes, id) from the leftmost compound.
    At-rules and anything unparsable are skipped."""
    css = _COMMENT.sub("", css or "")
    rules = []
    position = 0
    order = 0
    while True:
        start = css.find("{", position)
        if start < 0:
            break
        head = css[position:start].strip()
        depth, end = 1, start + 1
        while end < len(css) and depth:
            depth += {"{": 1, "}": -1}.get(css[end], 0)
            end += 1
        body = css[start + 1:end - 1]
        position = end
        if head.startswith("@"):
            continue                    # @media, @keyframes, @font-face...: not drawn
        declarations = {}
        for declaration in body.split(";"):
            name, colon, value = declaration.partition(":")
            if colon:
                declarations[name.strip().lower()] = re.sub(r"\s*!important\s*$", "", value.strip(), flags=re.I)
        for selector in head.split(","):
            parts = _selector(selector.strip())
            if parts:
                weight = (sum(1 for p in parts if p[3]), sum(len(p[2]) for p in parts),
                          sum(1 for p in parts if p[1] not in (None, "*")))
                rules.append((parts, weight, order, declarations))
                order += 1
    return rules


def _selector(text):
    """[(combinator, tag, classes, id)] or None when the selector uses something unsupported."""
    parts = []
    combinator = " "
    for token in re.findall(r">|[^\s>]+", text):
        if token == ">":
            combinator = ">"
            continue
        tag, classes, element_id = None, [], None
        position = 0
        while position < len(token):
            match = _SIMPLE.match(token, position)
            if match is None or match.group(4):
                return None             # pseudo-class, attribute, + or ~: cannot match
            if match.group(1):
                tag = match.group(1).lower()
            elif match.group(2):
                classes.append(match.group(2))
            else:
                element_id = match.group(3)
            position = match.end()
        parts.append((combinator, tag, classes, element_id))
        combinator = " "
    return parts or None


def _matches_one(node, part):
    props = node.get("p") or {}
    _combinator, tag, classes, element_id = part
    if node.get("k") != "el":
        have = ["sf-%s" % node.get("k")]
        node_tag, node_id = None, None
    else:
        have = str(props.get("class") or "").split()
        node_tag, node_id = str(props.get("tag") or "div").lower(), props.get("id")
    if tag not in (None, "*") and tag != node_tag:
        return False
    if element_id is not None and element_id != node_id:
        return False
    return all(name in have for name in classes)


def _matches(parts, node, ancestors):
    """True when ``node`` (with its ancestors, outermost first) matches the selector."""
    if not _matches_one(node, parts[-1]):
        return False
    chain = list(ancestors)
    index = len(parts) - 2
    combinator = parts[-1][0]
    while index >= 0:
        if combinator == ">":
            if not chain or not _matches_one(chain[-1], parts[index]):
                return False
            chain.pop()
        else:
            while chain and not _matches_one(chain[-1], parts[index]):
                chain.pop()
            if not chain:
                return False
            chain.pop()
        combinator = parts[index][0]
        index -= 1
    return True


def _color(value, theme):
    match = _VAR.match(value)
    if match:
        found = theme.color(match.group(1)) if theme is not None else None
        if found:
            return found
        value = match.group(2) or ""
    return value if _COLOR.match(value) and value.lower() not in ("inherit", "initial", "unset") else None


def _safe(prop, value, theme):
    """The value to put in a style attribute, or None when minihtml cannot draw it."""
    if "url(" in value.lower():
        return None
    if prop in ("color", "background-color"):     # `background` was mapped to background-color
        return _color(value, theme)
    if prop in _SPACING or prop == "border-radius":
        words = value.split()
        return " ".join(words) if 1 <= len(words) <= 4 and all(_LENGTH.match(w) for w in words) else None
    if prop in _KEYWORDS:
        return value.lower() if value.lower() in _KEYWORDS[prop] else None
    return None


def style_for(rules, node, ancestors, theme=None):
    """The style attribute text (may be "") for an ``el`` node under ``rules`` (see parse)."""
    if not rules:
        return ""
    matched = [rule for rule in rules if _matches(rule[0], node, ancestors)]
    matched.sort(key=lambda rule: (rule[1], rule[2]))
    result = {}
    for _parts, _weight, _order, declarations in matched:
        for prop, value in declarations.items():
            prop = "background-color" if prop == "background" else prop
            safe = _safe(prop, value, theme)
            if safe is not None:
                result[prop] = safe
    return "; ".join("%s: %s" % item for item in result.items())
