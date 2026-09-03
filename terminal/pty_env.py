"""PTY child environment sanitization (pure — no Sublime).

The Sublime plugin host (and agent shells that launched it) often carry
anti-TUI defaults: NO_COLOR=1, FORCE_COLOR=0, TERM=dumb. ConPTY children
inherit that block and apps like Grok report color=none (see `grok doctor`).

If Sublime Text itself was launched from inside a running Claude Code
session (e.g. this very session spawning a Sublime process), a spawned
Claude tab inherits that session's own identity: CLAUDECODE=1,
CLAUDE_CODE_CHILD_SESSION=1, and per-session bridge/messaging/session-id
vars pointing at a socket that has nothing to do with the new tab.
CLAUDE_CODE_CHILD_SESSION=1 makes the new claude process treat itself as a
nested child -- confirmed live 2026-09-02: cursor-key prompt-history replay
and session logging both come up disabled, not just cosmetically different.
This is worth stripping unconditionally (not just for the "Claude" profile)
since the inheritance path is the parent shell, not the target profile, and
a stripped var a profile actually wants back can still be set via its own
spawn_env, applied after this.

Profile spawn_env is applied last so intentional overrides still win.
"""

_DUMB_TERMS = frozenset(("", "dumb", "unknown", "none"))

# Prefix match, not an exact-name list: covers CLAUDECODE itself and the
# whole CLAUDE_CODE_* family (child-session flag, bridge/messaging socket,
# session id, entrypoint, sse port, execpath, ...) without needing to name
# every one Claude Code currently sets or might add later. Deliberately
# narrower than a bare "CLAUDE" prefix -- CLAUDE_PID, CLAUDE_EFFORT, and any
# other CLAUDE_* a profile sets on purpose are left alone.
_CLAUDE_SESSION_IDENTITY_PREFIXES = ("CLAUDECODE", "CLAUDE_CODE_")


def _is_claude_session_identity_var(name):
    return name.startswith(_CLAUDE_SESSION_IDENTITY_PREFIXES)


def sanitize_pty_env(base_env, profile_env=None):
    """Return env for a color-capable interactive TUI child.

    Parameters
    ----------
    base_env : mapping
        Typically ``os.environ`` (or a copy).
    profile_env : mapping or None
        Profile/settings ``spawn_env`` overrides; applied after sanitization.
    """
    env = {
        k: v for k, v in base_env.items()
        if not _is_claude_session_identity_var(k)
    }
    env.pop("NO_COLOR", None)
    fc = env.get("FORCE_COLOR")
    if fc is None or fc == "" or fc == "0":
        env["FORCE_COLOR"] = "1"
    term = (env.get("TERM") or "").lower()
    if term in _DUMB_TERMS:
        env["TERM"] = "xterm-256color"
    if not env.get("COLORTERM"):
        env["COLORTERM"] = "truecolor"
    # Terminal-brand detection (Grok and similar TUIs) is a static env-var
    # lookup, not a live capability probe -- confirmed empirically: a full
    # ai_terminal session produces zero CSI ?u (kitty-flags) queries either
    # direction. With no brand marker set, Grok's Windows fallback guesses
    # "Windows Terminal", whose table entry has no Kitty keyboard protocol
    # support, regardless of what this terminal can actually do. This embeds
    # libghostty-vt (see ghostty_engine.py/ghostty_vt.py) and genuinely
    # answers kitty-flags queries and encodes Kitty-protocol keys once
    # pushed, so "ghostty" is the accurate brand, not a spoof -- the same
    # convention as reporting TERM=xterm-256color instead of an unknown value.
    if not env.get("TERM_PROGRAM"):
        env["TERM_PROGRAM"] = "ghostty"
    if profile_env:
        env.update(profile_env)
    return env
