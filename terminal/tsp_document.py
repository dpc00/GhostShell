"""TSP document: the tree of components a program has described to the terminal.

This is the STATE BLOCK of GhostShell's TSP support.  It applies "frames" (an
atomic list of operations) to a tree of nodes, the way the spec says a terminal
does.  A rejected operation is reported and skipped without affecting the rest
of the frame.

Port of omp's reference applier (packages/tui/src/native/apply.ts, main branch,
2026-10-01) -- VERIFIED by reading it; behaviour for each op follows that file.

A node on the wire is {"id": str, "k": kind, "p": {props}, "c": [children]}.
Ops are lists:  ["add", id, parent, before_id_or_None, node]
                ["set", id, {prop: value or None}]      (None removes the prop)
                ["text", id, "append" | "replace", text]
                ["splice", id, at, delete_count, text]
                ["move", id, parent, before_id_or_None]
                ["del", id]   ["settle", id]   ["focus", id_or_None]
                ["reveal", id, where]  ["scroll", id, by]
                ["suspend"]   ["resume"]
"""

import inspect
import re

from . import tsp_style

# Every component kind in the v1 vocabulary (omp packages/wire/src/tsp.ts TSP_KINDS).
TSP_KINDS = (
    "col", "row", "card", "section", "rule", "spacer", "text", "md", "code",
    "diff", "ansi", "math", "image", "kv", "table", "tree", "badge", "kbd",
    "icon", "spinner", "shimmer", "elapsed", "progress", "rate", "list",
    "item", "tabs", "editor", "input", "status", "seg", "overlay", "toast",
    "rows", "picker", "prefs", "tool", "checklist", "agent", "chart", "meter",
    "effort",
)
# Kinds of the Tern SDK vocabulary (stencil-hq/tern-sdk wire.py KINDS) that omp's list lacks.
# The document accepts them always; the host offers them in its hello reply only to programs
# other than omp (see TspHost._handle_query), so omp's own rendering path is unchanged.
TSP_EXTRA_KINDS = ("media", "block", "el")
TSP_ACCEPTED_KINDS = TSP_KINDS + TSP_EXTRA_KINDS
# Kinds whose main text the "text" and "splice" ops address (the "text" prop).
TSP_TEXT_KINDS = ("text", "md", "code", "ansi", "math", "editor", "input", "shimmer", "el")
REGIONS = ("main", "dock", "layer")     # the only children the root takes
MAX_NODES = 200000                      # per surface, root included
MAX_DEPTH = 64                          # levels, the root being level 1
NODE_OVERHEAD = 192                     # bytes counted per node on top of its id and props (retention estimate)


class OpError(Exception):
    """One operation was rejected; the message says why."""


class _Node:
    """One node of the document."""

    def __init__(self, node_id, kind, parent):
        self.id = node_id
        self.kind = kind
        self.props = {}
        self.children = []
        self.parent = parent
        self.cost = 0           # estimated bytes (retention budget)
        self.keys = None        # key prop -> child, only once a child has a key


_SIGNATURES = {}


class TspDocument:
    """A surface document: the root (its id is the surface id) plus an id index."""

    def __init__(self, surface_id, max_nodes=MAX_NODES, max_depth=MAX_DEPTH):
        self.surface_id = surface_id
        self.max_nodes = max_nodes
        self.max_depth = max_depth
        self._root = _Node(surface_id, "col", None)
        self._root.cost = NODE_OVERHEAD + len(surface_id)
        self._index = {surface_id: self._root}
        self.bytes = self._root.cost    # estimated size of the document, for the retention budget
        self.node_errors = []   # errors about nodes (unknown el tag), taken by the host after each frame
        self._tagged = []
        self._flips = {}        # (parent id, key or "#id") -> [drawn checked, checked the program last sent]
        self.settled = set()    # ids that got a "settle" hint and no later change
        self.reveal = None      # (id, where, seq) of the latest reveal op, for the view
        self.reveal_seq = 0
        self.scrolls = []       # (id, step) scroll ops waiting for the view
        self.focus = None       # id of the focused node
        self.suspended = False
        self.sheets = {}        # stylesheet name -> css text, in the order first sent
        self.role = None        # the `o` role (data-surface of the regions)
        self.app = None         # the program's hello app (data-app)
        self.cwd = None         # the pane's working directory, when the host knows it

    def set_sheet(self, name, css):
        """Install, replace or (empty css) remove a stylesheet; return an error text or None.

        Nothing changes on an error: a bad name, css that is not a string, or more than 256 KiB
        of css over all the surface's sheets."""
        if not isinstance(name, str) or not tsp_style.NAME_RE.match(name):
            return "bad stylesheet name (1-64 of A-Z a-z 0-9 _ -)"
        if css is not None and not isinstance(css, str):
            return "css is not a string"
        total = sum(len(text.encode("utf-8")) for key, text in self.sheets.items() if key != name)
        if css and total + len(css.encode("utf-8")) > tsp_style.MAX_SHEETS_BYTES:
            return "stylesheets over 256 KiB"
        if css:
            self.sheets[name] = css         # a replaced sheet keeps its place
        else:
            self.sheets.pop(name, None)
        return None

    def sheet_bytes(self):
        return sum(len(text.encode("utf-8")) for text in self.sheets.values())

    def has(self, node_id):
        return node_id in self._index

    def node_count(self):
        return len(self._index)

    def close(self):
        """``x`` with keep=true: drop the live-only regions; ``main`` stays."""
        for region in ("dock", "layer"):
            node = self._index.get(region)
            if node is None or node.parent is not self._root:
                continue
            self._detach(node)
        self.focus = None

    def apply_frame(self, frame):
        """Apply ``frame`` (a dict with "s" and "ops"); return a list of errors.

        Each error is {"s": frame number, "op": op index, "msg": text}; the
        caller may send these back to the program as ``error`` events.  An op that is
        rejected is skipped alone and later ops see the document without it.
        """
        errors = []
        ops = frame.get("ops")
        if not isinstance(ops, list):
            return errors
        self._tagged = []
        for index, op in enumerate(ops):
            try:
                self._apply(op)
            except OpError as error:
                errors.append({"s": frame.get("s"), "op": index, "msg": str(error)})
        tagged, self._tagged = self._tagged, []
        if tagged:
            self._report_tags(tagged)
        return errors

    def snapshot(self, drawn=True):
        """The whole document as wire-shaped dicts.  ``drawn`` shows the user's local control flips
        (what is on screen); False gives exactly what the program sent (for a replay)."""
        snapshot = self._export(self._root, drawn)
        if self.sheets:
            snapshot["sheets"] = list(self.sheets.values())
        for field in ("role", "app", "cwd"):
            if getattr(self, field):
                snapshot[field] = getattr(self, field)
        return snapshot

    def get(self, node_id):
        """Snapshot of one subtree, or None for an unknown id."""
        node = self._index.get(node_id) if isinstance(node_id, str) else None
        return self._export(node, True) if node is not None else None

    # ----- internals -------------------------------------------------------

    def _export(self, node, drawn=True):
        out = {"id": node.id, "k": node.kind}
        if node.props:
            props = dict(node.props)
            if drawn and node.kind == "el" and self._flips and self._control_type(node):
                flip = self._flips.get(self._flip_key(node))
                if flip is not None:
                    props["checked"] = flip[0]
            out["p"] = props
        elif drawn and node.kind == "el" and self._flips and self._control_type(node):
            flip = self._flips.get(self._flip_key(node))
            if flip is not None:
                out["p"] = {"checked": flip[0]}
        if node.children:
            out["c"] = [self._export(child, drawn) for child in node.children]
        return out

    def _node(self, node_id):
        node = self._index.get(node_id) if isinstance(node_id, str) else None
        if node is None:
            raise OpError("unknown id %s" % (node_id,))
        return node

    def _unsettle(self, node):
        if not self.settled:
            return
        while node is not None:
            self.settled.discard(node.id)
            node = node.parent

    def _apply(self, op):
        if not isinstance(op, (list, tuple)) or not op:
            raise OpError("op is not a list")
        name = op[0]
        if not isinstance(name, str) or not name.isalpha():
            raise OpError("unknown op %s" % (name,))
        handler = getattr(self, "_op_" + name, None)
        if handler is None:
            raise OpError("unknown op %s" % (name,))
        signature = _SIGNATURES.get(name)
        if signature is None:
            signature = _SIGNATURES[name] = inspect.signature(handler)
        try:
            signature.bind(*op[1:])
        except TypeError:
            raise OpError("wrong number of arguments for op %s" % (name,))
        handler(*op[1:])

    def _op_add(self, node_id, parent_id, before, wire):
        if not isinstance(node_id, str) or not node_id:
            raise OpError("add needs a node id")
        if not isinstance(wire, dict) or wire.get("id") != node_id:
            raise OpError("add id %s does not match node id" % (node_id,))
        parent = self._node(parent_id)
        if before is not None and not isinstance(before, str):
            raise OpError("add before must be a sibling id or null")
        index = self._insert_index(parent, before)
        if parent is self._root and node_id not in REGIONS:
            raise OpError("the root of %s takes only main, dock and layer" % self.surface_id)
        depth = self._depth(parent) + 1
        built = self._build(wire, parent, set(), depth, [self.max_nodes - len(self._index)])
        self._key_add(parent, built)
        parent.children.insert(index, built)
        self._register(built)
        self._unsettle(parent)
        found = []
        self._collect_el(built, found)
        if found:
            for node in found:
                self._checked_arrives(node, None)
            self._tagged.extend(found)

    def _op_set(self, node_id, props):
        node = self._node(node_id)
        if not isinstance(props, dict):
            raise OpError("set props must be an object")
        previous_checked = bool(node.props.get("checked")) if node.kind == "el" else None
        if "key" in props and node.parent is not None:
            new = props["key"] if isinstance(props["key"], str) and props["key"] else None
            if new is not None and new != self._key_of(node):
                owner = node.parent.keys.get(new) if node.parent.keys else None
                if owner is not None and owner is not node:
                    raise OpError("duplicate child key %s" % new)
            self._key_remove(node.parent, node)
        for key, value in props.items():
            if value is None:
                node.props.pop(key, None)
            else:
                node.props[key] = value
        if node.parent is not None:
            self._key_add(node.parent, node)
        self._apply_picker_patch(node)
        self._recost(node)
        self._unsettle(node)
        if node.kind == "el":
            if "checked" in props:
                self._checked_arrives(node, previous_checked)
            self._tagged.append(node)

    @staticmethod
    def _apply_picker_patch(node):
        """A picker's ``itemsAdd`` / ``itemsDel`` are changes to its ``items`` catalogue (spec:
        "sent once and patched by id"), not state: apply them and drop them."""
        if node.kind != "picker":
            return
        added = node.props.pop("itemsAdd", None)
        deleted = node.props.pop("itemsDel", None)
        if not added and not deleted:
            return
        items = list(node.props.get("items") or [])
        position = {item.get("id"): n for n, item in enumerate(items) if isinstance(item, dict)}
        for entry in added or []:
            if isinstance(entry, dict) and entry.get("id") in position:
                items[position[entry["id"]]] = entry
            elif isinstance(entry, dict):
                position[entry.get("id")] = len(items)
                items.append(entry)
        if deleted:
            gone = set(deleted)
            items = [item for item in items if not (isinstance(item, dict) and item.get("id") in gone)]
        node.props["items"] = items

    def _op_text(self, node_id, mode, text):
        node = self._text_node(node_id)
        if not isinstance(text, str):
            raise OpError("text must be a string")
        if mode == "append":
            node.props["text"] = self._text(node) + text
        elif mode == "replace":
            node.props["text"] = text
        else:
            raise OpError("unknown text mode %s" % (mode,))
        self._after_text(node)

    def _after_text(self, node):
        self._recost(node)
        self._unsettle(node)
        if node.kind == "el":
            self._tagged.append(node)

    def _op_splice(self, node_id, at, delete, text):
        node = self._text_node(node_id)
        if not isinstance(text, str):
            raise OpError("splice text must be a string")
        current = self._text(node)
        if (not isinstance(at, int) or not isinstance(delete, int) or isinstance(at, bool)
                or isinstance(delete, bool) or at < 0 or delete < 0):
            raise OpError("splice at and del must be non-negative integers")
        if current.isascii() and text.isascii():
            if at + delete > len(current):
                raise OpError("splice range %s+%s outside text of length %d" % (at, delete, len(current)))
            node.props["text"] = current[:at] + text + current[at + delete:]
            self._after_text(node)
            return
        # Offsets are UTF-16 code units on the wire.
        units = _utf16_units(current)
        if at + delete > len(units):
            raise OpError("splice range %s+%s outside text of length %d" % (at, delete, len(units)))
        for edge in (at, at + delete):
            if 0 < edge < len(units) and 0xDC00 <= units[edge] <= 0xDFFF and 0xD800 <= units[edge - 1] <= 0xDBFF:
                raise OpError("splice at %d splits a surrogate pair" % edge)
        inserted = _utf16_units(text)
        node.props["text"] = _from_utf16_units(units[:at] + inserted + units[at + delete:])
        self._after_text(node)

    def _op_move(self, node_id, parent_id, before):
        node = self._node(node_id)
        if node is self._root:
            raise OpError("cannot move the root")
        parent = self._node(parent_id)
        if before is not None and not isinstance(before, str):
            raise OpError("move before must be a sibling id or null")
        walk = parent
        while walk is not None:
            if walk is node:
                raise OpError("cannot move %s into its own subtree" % (node_id,))
            walk = walk.parent
        if before == node_id:
            raise OpError("move before itself")
        old_parent = node.parent
        # Check everything first so a rejected move leaves the tree untouched.
        if before is not None:
            sibling = self._index.get(before)
            if sibling is None or sibling.parent is not parent:
                raise OpError("%s is not a child of %s" % (before, parent_id))
        if parent is self._root and node_id not in REGIONS:
            raise OpError("the root of %s takes only main, dock and layer" % self.surface_id)
        if old_parent is self._root and parent is not self._root:
            raise OpError("%s stays under the root of %s" % (node_id, self.surface_id))
        if self._depth(parent) + self._height(node) > self.max_depth:
            raise OpError("depth limit %d reached" % self.max_depth)
        if parent is not old_parent:
            key = self._key_of(node)
            owner = parent.keys.get(key) if key is not None and parent.keys else None
            if owner is not None:
                raise OpError("duplicate child key %s" % key)
        self._key_remove(old_parent, node)
        old_parent.children.remove(node)
        parent.children.insert(self._insert_index(parent, before), node)
        node.parent = parent
        self._key_add(parent, node)
        self._unsettle(old_parent)
        self._unsettle(node)

    def _op_del(self, node_id):
        node = self._node(node_id)
        if node is self._root:
            raise OpError("cannot delete the root")
        self._detach(node)

    def _op_settle(self, node_id):
        self._node(node_id)
        self.settled.add(node_id)

    def _op_focus(self, node_id):
        if node_id is not None:
            self._node(node_id)
        self.focus = node_id

    def _op_reveal(self, node_id, where=None):
        self._node(node_id)
        # Only the latest reveal applies: a new one replaces the one the view has not yet shown.
        self.reveal_seq += 1
        self.reveal = (node_id, where if where in ("start", "end") else "nearest", self.reveal_seq)

    _SCROLL_STEPS = ("line-up", "line-down", "page-up", "page-down", "start", "end")

    def _op_scroll(self, node_id, by=None):
        self._node(node_id)
        if by not in self._SCROLL_STEPS:
            raise OpError("unknown scroll step %s" % (by,))
        self.scrolls.append((node_id, by))
        del self.scrolls[:-100]

    def take_scrolls(self):
        """The scroll ops not yet carried out by the view, oldest first (and forget them)."""
        taken, self.scrolls = self.scrolls, []
        return taken

    def ancestry(self, node_id):
        """[(id, kind)] from the root down to ``node_id`` (itself last), or None for an unknown id."""
        node = self._index.get(node_id)
        if node is None:
            return None
        path = []
        while node is not None:
            path.append((node.id, node.kind))
            node = node.parent
        path.reverse()
        return path


    def _op_suspend(self):
        self.suspended = True

    def _op_resume(self):
        self.suspended = False

    def _text_node(self, node_id):
        node = self._node(node_id)
        if node.kind not in TSP_TEXT_KINDS:
            raise OpError("%s (%s) has no primary text" % (node_id, node.kind))
        return node

    @staticmethod
    def _text(node):
        value = node.props.get("text")
        return value if isinstance(value, str) else ""

    def _insert_index(self, parent, before):
        if before is None:
            return len(parent.children)
        sibling = self._index.get(before) if isinstance(before, str) else None
        if sibling is None or sibling.parent is not parent:
            raise OpError("%s is not a child of %s" % (before, parent.id))
        return parent.children.index(sibling)

    def _depth(self, node):
        """Level of ``node``: the root is level 1."""
        level = 1
        while node.parent is not None:
            node = node.parent
            level += 1
        return level

    @staticmethod
    def _height(node):
        """Levels in the subtree under and including ``node``."""
        best = 0
        for child in node.children:
            best = max(best, TspDocument._height(child))
        return best + 1

    def _build(self, wire, parent, seen, depth, budget):
        if not isinstance(wire, dict):
            raise OpError("node is not an object")
        node_id = wire.get("id")
        if not isinstance(node_id, str) or not node_id:
            raise OpError("node without id")
        kind = wire.get("k")
        if kind not in TSP_ACCEPTED_KINDS:
            raise OpError("unknown kind %s" % (kind,))
        if node_id in self._index or node_id in seen:
            raise OpError("duplicate id %s" % (node_id,))
        if depth > self.max_depth:
            raise OpError("depth limit %d reached at %s" % (self.max_depth, node_id))
        budget[0] -= 1
        if budget[0] < 0:
            raise OpError("node limit %d reached" % self.max_nodes)
        props = wire.get("p")
        if props is not None and not isinstance(props, dict):
            raise OpError("props of %s must be an object" % node_id)
        children = wire.get("c")
        if children is not None and not isinstance(children, list):
            raise OpError("children of %s must be an array" % node_id)
        seen.add(node_id)
        node = _Node(node_id, kind, parent)
        for key, value in (props or {}).items():
            if value is not None:
                node.props[key] = value
        self._apply_picker_patch(node)
        for child in children or []:
            built = self._build(child, node, seen, depth + 1, budget)
            self._key_add(node, built)
            node.children.append(built)
        node.cost = _cost(node)
        return node

    # ----- sibling keys ("duplicate child key") -----------------------------

    @staticmethod
    def _key_of(node):
        key = node.props.get("key")
        return key if isinstance(key, str) and key else None

    def _key_add(self, parent, node, replacing=None):
        key = self._key_of(node)
        if key is None:
            return
        if parent.keys is None:
            parent.keys = {}
        owner = parent.keys.get(key)
        if owner is not None and owner is not node and owner is not replacing:
            raise OpError("duplicate child key %s" % key)
        parent.keys[key] = node

    def _key_remove(self, parent, node):
        key = self._key_of(node)
        if key is not None and parent is not None and parent.keys and parent.keys.get(key) is node:
            del parent.keys[key]

    # ----- registration and bytes -----------------------------------------

    def _register(self, node):
        self._index[node.id] = node
        self.bytes += node.cost
        for child in node.children:
            self._register(child)

    def _unregister(self, node):
        self._index.pop(node.id, None)
        self.bytes -= node.cost
        self.settled.discard(node.id)
        if self.focus == node.id:
            self.focus = None
        if node.kind == "el" and node.parent is not None and self._key_of(node) is None:
            self._flips.pop((node.parent.id, "#" + node.id), None)      # a flip without a key dies with its node
        for child in node.children:
            self._unregister(child)

    def _recost(self, node):
        cost = _cost(node)
        self.bytes += cost - node.cost
        node.cost = cost

    def settled_children(self):
        """Ids of ``main``'s children the program settled itself (and nothing changed since), oldest first."""
        main = self._index.get("main")
        if main is None or main.parent is not self._root:
            return []
        return [child.id for child in main.children if child.id in self.settled]

    def drop(self, node_id):
        """Remove a subtree for the retention budget; returns the bytes freed (0 for an unknown id)."""
        node = self._index.get(node_id)
        if node is None or node is self._root:
            return 0
        before = self.bytes
        self._detach(node)
        return before - self.bytes

    def _detach(self, node):
        parent = node.parent
        parent.children.remove(node)
        self._key_remove(parent, node)
        self._unregister(node)
        self._unsettle(parent)

    # ----- el controls ------------------------------------------------------

    @staticmethod
    def _tag(node):
        return str(node.props.get("tag", "")).lower() if node.kind == "el" else ""

    def _control_type(self, node):
        """"checkbox" or "radio" for an el input control, else None."""
        if self._tag(node) != "input":
            return None
        kind = str(node.props.get("type") or "").lower()
        return kind if kind in ("checkbox", "radio") else None

    @staticmethod
    def _name(node):
        name = node.props.get("name")
        return name if isinstance(name, str) and name else None

    @staticmethod
    def _value(node):
        value = node.props.get("value")
        return value if isinstance(value, str) else "on"

    def _nearest_form(self, node):
        """The nearest form around ``node`` (the node itself when it is one), or None."""
        while node is not None and node is not self._root:
            if self._tag(node) == "form":
                return node
            node = node.parent
        return None

    def _scope_controls(self, form):
        """The controls of ``form`` (None: those outside every form), document order, nested forms skipped."""
        start = form if form is not None else self._root
        stack = list(reversed(start.children))
        while stack:
            node = stack.pop()
            if self._tag(node) == "form":
                continue
            if self._control_type(node):
                yield node
            stack.extend(reversed(node.children))

    def _flip_key(self, node):
        return (node.parent.id if node.parent is not None else None, self._key_of(node) or "#" + node.id)

    def _drawn(self, node):
        """The state a control shows: the user's flip when there is one, else the program's ``checked``."""
        flip = self._flips.get(self._flip_key(node))
        return flip[0] if flip is not None else bool(node.props.get("checked"))

    def _radio_group(self, node):
        name = self._name(node)
        if name is None:
            return []
        form = self._nearest_form(node.parent) if node.parent is not None else None
        return [other for other in self._scope_controls(form)
                if other is not node and self._control_type(other) == "radio" and self._name(other) == name]

    def _checked_arrives(self, node, previous):
        """A node arrived (``previous`` None) or its props changed: a ``checked`` different from the one the
        program last sent wins over a local flip, and a radio set to true this way drops its group's flips."""
        kind = self._control_type(node)
        if kind is None:
            return
        sent = bool(node.props.get("checked"))
        key = self._flip_key(node)
        flip = self._flips.get(key)
        last = previous if previous is not None else (flip[1] if flip is not None else None)
        changed = last is not None and sent != last
        if flip is not None:
            if changed:
                del self._flips[key]
            else:
                flip[1] = sent
        if changed and sent and kind == "radio":
            for other in self._radio_group(node):
                self._flips.pop(self._flip_key(other), None)

    def form_values(self, node_id):
        """The ``values`` of the form around ``node_id``, or None outside every form."""
        node = self._index.get(node_id) if isinstance(node_id, str) else None
        form = self._nearest_form(node) if node is not None else None
        if form is None:
            return None
        groups = {}
        for control in self._scope_controls(form):
            name = self._name(control)
            if name is not None and not control.props.get("disabled"):
                groups.setdefault(name, []).append(control)
        values = {}
        for name, members in groups.items():
            radios = [m for m in members if self._control_type(m) == "radio"]
            if radios:
                picked = [m for m in radios if self._drawn(m)]
                values[name] = self._value(picked[0]) if picked else None
            elif len(members) == 1:
                values[name] = self._drawn(members[0])
            else:
                values[name] = [self._value(m) for m in members if self._drawn(m)]
        return values

    def click_control(self, node_id):
        """A click on a checkbox or radio ``el``: flip it locally.  Returns the ``change`` event fields
        (value, checked, name when it has one), or None when nothing changes: not a control, disabled,
        or a radio that is already checked."""
        node = self._index.get(node_id) if isinstance(node_id, str) else None
        kind = self._control_type(node) if node is not None else None
        if kind is None or node.props.get("disabled"):
            return None
        drawn = self._drawn(node)
        if kind == "radio":
            if drawn:
                return None
            for other in self._radio_group(node):
                if self._drawn(other):
                    self._flips[self._flip_key(other)] = [False, bool(other.props.get("checked"))]
            drawn = True
        else:
            drawn = not drawn
        self._flips[self._flip_key(node)] = [drawn, bool(node.props.get("checked"))]
        self._unsettle(node)
        fields = {"value": self._value(node), "checked": drawn}
        name = self._name(node)
        if name is not None:
            fields["name"] = name
        return fields

    # ----- unknown el tags --------------------------------------------------

    def _collect_el(self, node, found):
        if node.kind == "el":
            found.append(node)
        for child in node.children:
            self._collect_el(child, found)

    def _report_tags(self, nodes):
        for node in nodes:
            if self._index.get(node.id) is not node:
                continue
            message = _unknown_tag_message(node.props)
            if message:
                self.node_errors.append({"id": node.id, "msg": message})

    def take_node_errors(self):
        """Errors about nodes (an el with an unknown tag) found by the last frame, once each: [{"id","msg"}]."""
        taken, self.node_errors = self.node_errors, []
        return taken


def _estimate(value):
    """Rough size in bytes of a prop value, for the retention budget."""
    if isinstance(value, str):
        return len(value)
    if isinstance(value, (list, tuple)):
        return 8 + sum(_estimate(item) for item in value)
    if isinstance(value, dict):
        return 8 + sum(len(str(key)) + _estimate(item) for key, item in value.items())
    return 8


def _cost(node):
    """Estimated bytes one node holds: a fixed overhead, its id and its props."""
    try:
        return NODE_OVERHEAD + len(node.id) + sum(len(str(key)) + _estimate(value) for key, value in node.props.items())
    except RecursionError:
        return NODE_OVERHEAD * 4


_unknown_tag = []


def _unknown_tag_message(props):
    """The error text for an el with an unknown tag (tsp_draw_el owns the tag list), or None."""
    if not _unknown_tag:
        try:
            from . import tsp_draw_el
            _unknown_tag.append(getattr(tsp_draw_el, "unknown_tag_message", None))
        except Exception:
            _unknown_tag.append(None)
    function = _unknown_tag[0]
    if function is None:
        return None
    try:
        return function(props)
    except Exception:
        return None


def check_css(css):
    """Problems a stylesheet has that the window reports: ["<line>:<col>: message", ...].

    ``url()`` and ``position: fixed`` declarations are dropped and reported, ``@import`` is reported as
    unsupported, ``@font-face`` is ignored, an unbalanced brace is a parse error.  The sheet is still installed."""
    clean = re.sub(r"/\*.*?(\*/|$)", lambda match: re.sub(r"[^\n]", " ", match.group(0)), css, flags=re.S)
    problems = []

    def place(index):
        line = clean.count("\n", 0, index) + 1
        return "%d:%d" % (line, index - (clean.rfind("\n", 0, index) + 1) + 1)

    for match in re.finditer(r"url\s*\(", clean, flags=re.I):
        problems.append((match.start(), "url() is not allowed and was dropped"))
    for match in re.finditer(r"position\s*:\s*fixed", clean, flags=re.I):
        problems.append((match.start(), "position: fixed is not allowed and was dropped"))
    for match in re.finditer(r"@import\b", clean, flags=re.I):
        problems.append((match.start(), "@import is not supported"))
    depth = 0
    for index, char in enumerate(clean):
        if char == "{":
            depth += 1
        elif char == "}":
            if depth == 0:
                problems.append((index, "unmatched }"))
            else:
                depth -= 1
    if depth:
        problems.append((len(clean), "unexpected end of input: %d block%s not closed" % (depth, "" if depth == 1 else "s")))
    problems.sort()
    return ["%s: %s" % (place(index), message) for index, message in problems]


def _utf16_units(text):
    """``text`` as a list of UTF-16 code units (ints), because TSP offsets use them."""
    data = text.encode("utf-16-le", "surrogatepass")
    return [data[i] | (data[i + 1] << 8) for i in range(0, len(data), 2)]


def _from_utf16_units(units):
    data = b"".join(unit.to_bytes(2, "little") for unit in units)
    return data.decode("utf-16-le", "surrogatepass")
