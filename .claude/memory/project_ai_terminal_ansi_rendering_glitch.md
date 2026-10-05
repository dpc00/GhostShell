---
name: project_ai_terminal_ansi_rendering_glitch
description: "GhostShell's ai_terminal (Claude tab) had a rendering glitch on 2026-09-09 — chars out of place, wrong coloring, then abnormal wrapping after resize — suspected trigger was raw ANSI color codes from an uncoloring-stripped node script"
metadata: 
  node_type: memory
  type: project
  originSessionId: 69b34a17-8b23-4cbc-bdfd-4f309354256c
  modified: 2026-09-09T09:02:29.337Z
---

On 2026-09-09, ~20 minutes into a session running `node -e`/`node scriptfile.js`
commands whose `console.log` output included Node's auto-colorized ANSI
escape codes (`\x1b[33m...\x1b[39m`, visible in tool output as
`[33m1907[39m` etc.), the GhostShell `ai_terminal` panel ("Claude tab")
started rendering with characters out of place and wrong colors.
Resizing the pane (the usual first fix for [[project_st_trackpad_zoom_freeze]]-
style PTY issues) did NOT fix it -- it made wrapping abnormal too,
suggesting the corruption is in actual PTY/buffer state, not just a
stale screen paint.

**Why**: user reported it live; declined to switch into debugging
GhostShell immediately, opted to keep working and just log it.

**How to apply**: next time this happens, this is now the second known
report (first: unconditional resize loop from trackpad pinch-zoom, see
[[project_st_trackpad_zoom_freeze]] -- different symptom, same panel).
Suspect uncolorized-terminal-unsafe subprocess output (raw ANSI codes
from tools like `node`, or any CLI that auto-detects a TTY and emits
color codes) as a trigger worth checking first via the existing
asciinema troubleshooting workflow at
`~/data/logs/ai_terminal_asciinema_casts_for_troubleshooting_rendering/` [CHECK 2026-10-05: ~/data/logs was purged, see reference_where_campaign_records_live, so those recordings are probably gone].
Not yet root-caused -- resize-to-repaint is NOT a reliable fix for this
particular symptom (unlike the trackpad-zoom freeze, where restoring
font size did fix it). If it recurs, worth actually digging into
`ai_terminal.py`'s ANSI/escape-sequence handling rather than treating
it as self-resolving.

**Concrete evidence captured 2026-09-09**: since the `ai_terminal` panel
hosting the Claude tab is itself a Sublime view, a sublime-mcp
`get_active_file` call (aimed at something else, happened to land on
the globally-focused Claude tab) read its raw buffer content directly
and showed real character-level corruption, not just a screen-paint
issue: the line "Now let me extract tool calls from these 50 most
recent transcripts." was stored in the buffer as "Nownletgmenextract
tool2callskfrom these 50 most recent transcripts." (letters
dropped/merged), and a block of prior output ("sort -rn | head -50 |
cut..." + "Waiting...") was duplicated in the buffer. This means the
corruption is written into the PTY-fed view's text content itself, not
a rendering-layer illusion -- `get_active_file`/similar sublime-mcp
reads against the `ai_terminal` view are a fast, no-asciinema way to
capture a corruption sample for future root-causing.
