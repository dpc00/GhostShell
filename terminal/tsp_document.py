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

# Every component kind in the v1 vocabulary (omp packages/wire/src/tsp.ts TSP_KINDS).
TSP_KINDS = (
    "col", "row", "card", "section", "rule", "spacer", "text", "md", "code",
    "diff", "ansi", "math", "image", "kv", "table", "tree", "badge", "kbd",
    "icon", "spinner", "shimmer", "elapsed", "progress", "rate", "list",
    "item", "tabs", "editor", "input", "status", "seg", "overlay", "toast",
    "rows", "picker", "prefs", "tool", "checklist", "agent", "chart", "meter",
    "effort",
)
# Kinds whose main text the "text" and "splice" ops address (the "text" prop).
TSP_TEXT_KINDS = ("text", "md", "code", "ansi", "math", "editor", "input", "shimmer")


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


class TspDocument:
    """A surface document: the root (its id is the surface id) plus an id index."""

    def __init__(self, surface_id):
        self.surface_id = surface_id
        self._root = _Node(surface_id, "col", None)
        self._index = {surface_id: self._root}
        self.settled = set()    # ids that got a "settle" hint and no later change
        self.focus = None       # id of the focused node
        self.suspended = False

    def has(self, node_id):
        return node_id in self._index

    def close(self):
        """``x`` with keep=true: drop the live-only regions; ``main`` stays."""
        for region in ("dock", "layer"):
            node = self._index.get(region)
            if node is None or node.parent is not self._root:
                continue
            self._root.children.remove(node)
            self._unregister(node)
        self.focus = None

    def apply_frame(self, frame):
        """Apply ``frame`` (a dict with "s" and "ops"); return a list of errors.

        Each error is {"s": frame number, "op": op index, "msg": text}; the
        caller may send these back to the program as ``error`` events.
        """
        errors = []
        for index, op in enumerate(frame.get("ops", [])):
            try:
                self._apply(op)
            except OpError as error:
                errors.append({"s": frame.get("s"), "op": index, "msg": str(error)})
        return errors

    def snapshot(self):
        """The whole document as wire-shaped dicts."""
        return self._export(self._root)

    def get(self, node_id):
        """Snapshot of one subtree, or None for an unknown id."""
        node = self._index.get(node_id)
        return self._export(node) if node is not None else None

    # ----- internals ---------------------------------------------------

    def _export(self, node):
        out = {"id": node.id, "k": node.kind}
        if node.props:
            out["p"] = dict(node.props)
        if node.children:
            out["c"] = [self._export(child) for child in node.children]
        return out

    def _node(self, node_id):
        node = self._index.get(node_id)
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
        handler = getattr(self, "_op_" + str(name), None)
        if handler is None or not name.isalpha():
            raise OpError("unknown op %s" % (name,))
        try:
            handler(*op[1:])
        except TypeError:
            raise OpError("wrong number of arguments for op %s" % (name,))

    def _op_add(self, node_id, parent_id, before, wire):
        if not isinstance(wire, dict) or wire.get("id") != node_id:
            raise OpError("add id %s does not match node id" % (node_id,))
        parent = self._node(parent_id)
        index = self._insert_index(parent, before)
        built = self._build(wire, parent, set())
        parent.children.insert(index, built)
        self._register(built)
        self._unsettle(parent)

    def _op_set(self, node_id, props):
        node = self._node(node_id)
        if not isinstance(props, dict):
            raise OpError("set props must be an object")
        for key, value in props.items():
            if value is None:
                node.props.pop(key, None)
            else:
                node.props[key] = value
        self._apply_picker_patch(node)
        self._unsettle(node)

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
        self._unsettle(node)

    def _op_splice(self, node_id, at, delete, text):
        node = self._text_node(node_id)
        current = self._text(node)
        # Offsets are UTF-16 code units on the wire.
        units = _utf16_units(current)
        if (not isinstance(at, int) or not isinstance(delete, int)
                or at < 0 or delete < 0 or at + delete > len(units)):
            raise OpError("splice range %s+%s outside text of length %d"
                          % (at, delete, len(units)))
        inserted = _utf16_units(text)
        node.props["text"] = _from_utf16_units(units[:at] + inserted + units[at + delete:])
        self._unsettle(node)

    def _op_move(self, node_id, parent_id, before):
        node = self._node(node_id)
        if node is self._root:
            raise OpError("cannot move the root")
        parent = self._node(parent_id)
        walk = parent
        while walk is not None:
            if walk is node:
                raise OpError("cannot move %s into its own subtree" % (node_id,))
            walk = walk.parent
        if before == node_id:
            raise OpError("move before itself")
        old_parent = node.parent
        # Check "before" first so a rejected move leaves the tree untouched.
        if before is not None:
            sibling = self._index.get(before)
            if sibling is None or sibling.parent is not parent:
                raise OpError("%s is not a child of %s" % (before, parent_id))
        old_parent.children.remove(node)
        parent.children.insert(self._insert_index(parent, before), node)
        node.parent = parent
        self._unsettle(old_parent)
        self._unsettle(node)

    def _op_del(self, node_id):
        node = self._node(node_id)
        if node is self._root:
            raise OpError("cannot delete the root")
        parent = node.parent
        parent.children.remove(node)
        self._unregister(node)
        self._unsettle(parent)

    def _op_settle(self, node_id):
        self._node(node_id)
        self.settled.add(node_id)

    def _op_focus(self, node_id):
        if node_id is not None:
            self._node(node_id)
        self.focus = node_id

    def _op_reveal(self, node_id, where=None):
        self._node(node_id)     # a hint for the screen; nothing to change here

    def _op_scroll(self, node_id, by=None):
        self._node(node_id)

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
        sibling = self._index.get(before)
        if sibling is None or sibling.parent is not parent:
            raise OpError("%s is not a child of %s" % (before, parent.id))
        return parent.children.index(sibling)

    def _build(self, wire, parent, seen):
        node_id = wire.get("id")
        if not isinstance(node_id, str) or not node_id:
            raise OpError("node without id")
        kind = wire.get("k")
        if kind not in TSP_KINDS:
            raise OpError("unknown kind %s" % (kind,))
        if node_id in self._index or node_id in seen:
            raise OpError("duplicate id %s" % (node_id,))
        seen.add(node_id)
        node = _Node(node_id, kind, parent)
        for key, value in (wire.get("p") or {}).items():
            if value is not None:
                node.props[key] = value
        self._apply_picker_patch(node)
        for child in wire.get("c") or []:
            node.children.append(self._build(child, node, seen))
        return node

    def _register(self, node):
        self._index[node.id] = node
        for child in node.children:
            self._register(child)

    def _unregister(self, node):
        self._index.pop(node.id, None)
        self.settled.discard(node.id)
        if self.focus == node.id:
            self.focus = None
        for child in node.children:
            self._unregister(child)


def _utf16_units(text):
    """``text`` as a list of UTF-16 code units (ints), because TSP offsets use them."""
    data = text.encode("utf-16-le", "surrogatepass")
    return [data[i] | (data[i + 1] << 8) for i in range(0, len(data), 2)]


def _from_utf16_units(units):
    data = b"".join(unit.to_bytes(2, "little") for unit in units)
    return data.decode("utf-16-le", "surrogatepass")
