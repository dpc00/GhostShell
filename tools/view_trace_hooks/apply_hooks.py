"""Add view-trace hooks to sublime-mcp and AgentIDE (2026-10-13).

GhostShell's ai_terminal.py records every spot that moves a terminal tab's
viewport or caret (setting "debug_view_trace"). Other plugins that can move the
caret, change focus or scroll the same tab call that tracer through
`sublime._gs_trace_hook`, which ai_terminal.py registers when it loads. This
script inserts those calls (and a small `_gs_hook` helper that does nothing when
GhostShell is not loaded) into:

    sublime-mcp/sublime_mcp.py        (focus changes, caret/selection moves,
                                       show_at_center, the console-capture
                                       show/hide of panels and restore of the
                                       previous view, selection and clipboard)
    AgentIDE/lib/diff_view.py         (focus_group, show, focus_view)
    AgentIDE/lib/context.py           (focus_view, bring-to-front, select+show)

    python apply_hooks.py --check    patch copies in a temp folder and compile them
    python apply_hooks.py --apply    patch the files in place (see the warning)

WARNING: sublime_mcp.py is the file that serves the MCP connection from the
agent to Sublime. When it is saved while Sublime runs, the plugin reloads and the
connection drops. Apply it only as part of a controlled reload of the main
Sublime (see the note reference_restart_or_reload_main_sublime_2026_10_12 in the
sublime-mcp memory folder: detach_all, act, reattach_all).

Idempotent: a file that already has the helper is skipped.
"""
import os
import py_compile
import re
import sys
import tempfile

HELPER = '''def _gs_hook(kind, view=None, **info):
    """Report to GhostShell's view tracer (setting debug_view_trace) if loaded."""
    h = getattr(sublime, "_gs_trace_hook", None)
    if h is not None:
        try:
            h(kind, view, **info)
        except Exception:
            pass


'''

PROJ = r"C:\Users\donal\projects"
TARGETS = {
    os.path.join(PROJ, "sublime-mcp", "sublime_mcp.py"): [
        ('        w.focus_view(v)\n        v.run_command("insert", {"characters": text})\n',
         '        _gs_hook("mcp_focus_view_insert", v)\n        w.focus_view(v)\n        v.run_command("insert", {"characters": text})\n', 1, False),
        ('        snapshot["window"].run_command("show_panel", {"panel": "console"})\n        sublime.set_timeout(do_click, 300)\n',
         '        _gs_hook("mcp_console_show_panel", snapshot.get("view"))\n        snapshot["window"].run_command("show_panel", {"panel": "console"})\n        sublime.set_timeout(do_click, 300)\n', 1, False),
        ('        previous_panel = snapshot["panel"]\n        if previous_panel:\n',
         '        previous_panel = snapshot["panel"]\n        _gs_hook("mcp_console_restore_panel", snapshot.get("view"), panel=previous_panel)\n        if previous_panel:\n', 1, False),
        ('            snapshot["window"].focus_view(previous_view)\n',
         '            _gs_hook("mcp_console_restore_focus", previous_view)\n            snapshot["window"].focus_view(previous_view)\n', 1, False),
        ('                previous_view.sel().clear()\n',
         '                _gs_hook("mcp_console_restore_selection", previous_view)\n                previous_view.sel().clear()\n', 1, False),
        ('        v.sel().clear()\n        v.sel().add(sublime.Region(pt))\n        v.show_at_center(pt)\n',
         '        _gs_hook("mcp_goto_line", v, line=line)\n        v.sel().clear()\n        v.sel().add(sublime.Region(pt))\n        v.show_at_center(pt)\n', 1, False),
        ('        v.sel().clear()\n        v.sel().add(sublime.Region(start_pt, end_pt))\n        v.show_at_center(start_pt)\n',
         '        _gs_hook("mcp_select_lines", v)\n        v.sel().clear()\n        v.sel().add(sublime.Region(start_pt, end_pt))\n        v.show_at_center(start_pt)\n', 1, False),
        ('        w.focus_view(panel)\n        _cp_render(panel)\n',
         '        _gs_hook("mcp_focus_view_panel", panel)\n        w.focus_view(panel)\n        _cp_render(panel)\n', 1, False),
        ('        w.focus_group(group)\n        return {"ok": True}\n',
         '        _gs_hook("mcp_focus_group", None, group=group)\n        w.focus_group(group)\n        return {"ok": True}\n', 1, False),
        (r'^(        )self\.view\.show_at_center\(new_region\)$',
         r'\1_gs_hook("mcp_show_at_center", self.view)\n\1self.view.show_at_center(new_region)', 2, True),
        (r'^(        )self\.view\.show_at_center\(0\)$',
         r'\1_gs_hook("mcp_show_at_center", self.view)\n\1self.view.show_at_center(0)', 1, True),
        (r'^    def (do_click|restore_ui)\(\):\n',
         r'\g<0>        _gs_hook("mcp_console_\1", snapshot.get("view"))\n', 2, True),
    ],
    os.path.join(PROJ, "AgentIDE", "lib", "diff_view.py"): [
        ('        window.focus_group(group)\n    view = window.new_file()\n',
         '        _gs_hook("agentide_focus_group_diff", None, group=group)\n        window.focus_group(group)\n    view = window.new_file()\n', 1, False),
        ('    view.show(sublime.Region(0, 0))\n    window.focus_view(view)\n',
         '    _gs_hook("agentide_diff_show_focus", view)\n    view.show(sublime.Region(0, 0))\n    window.focus_view(view)\n', 1, False),
        ('            window.focus_view(refocus)\n    on_done()\n',
         '            _gs_hook("agentide_diff_refocus", refocus)\n            window.focus_view(refocus)\n    on_done()\n', 1, False),
    ],
    os.path.join(PROJ, "AgentIDE", "lib", "context.py"): [
        ('        window.focus_view(view)\n        if hasattr(window, "bring_to_front"):\n',
         '        _gs_hook("agentide_focus_view_new_content", view)\n        window.focus_view(view)\n        if hasattr(window, "bring_to_front"):\n', 1, False),
        ('    elif focus_behavior == "alternate" and window.active_view() != view:\n        window.focus_view(view)\n',
         '    elif focus_behavior == "alternate" and window.active_view() != view:\n        _gs_hook("agentide_focus_view_alternate", view)\n        window.focus_view(view)\n', 1, False),
        ('        view.sel().clear()\n        view.sel().add(selection)\n        view.show_at_center(selection)\n',
         '        _gs_hook("agentide_select_and_show", view)\n        view.sel().clear()\n        view.sel().add(selection)\n        view.show_at_center(selection)\n', 1, False),
    ],
}


def patch_text(path, text):
    if "_gs_hook(" in text and "def _gs_hook(" in text:
        return None, "already patched"
    crlf = "\r\n" in text
    t = text.replace("\r\n", "\n")
    for old, new, count, is_re in TARGETS[path]:
        n = len(re.findall(old, t, flags=re.M)) if is_re else t.count(old)
        if n != count:
            raise SystemExit("MISMATCH in %s (%d found, %d expected): %r" % (path, n, count, old[:70]))
        t = re.sub(old, new, t, flags=re.M) if is_re else t.replace(old, new)
    m = re.search(r"^(def |class |@)", t, flags=re.M)
    if not m:
        raise SystemExit("no top-level def/class in " + path)
    t = t[:m.start()] + HELPER + t[m.start():]
    if crlf:
        t = t.replace("\n", "\r\n")
    return t, "patched"


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "--check"
    tmp = tempfile.mkdtemp(prefix="gs_hooks_")
    for path in TARGETS:
        text = open(path, encoding="utf8", newline="").read()
        new, what = patch_text(path, text)
        if new is None:
            print(path, "->", what)
            continue
        if mode == "--apply":
            open(path, "w", encoding="utf8", newline="").write(new)
            py_compile.compile(path, doraise=True)
            print(path, "->", what, "(written, compiles)")
        else:
            out = os.path.join(tmp, os.path.basename(path))
            open(out, "w", encoding="utf8", newline="").write(new)
            py_compile.compile(out, doraise=True)
            print(path, "->", what, "(copy compiles: %s)" % out)


if __name__ == "__main__":
    main()
