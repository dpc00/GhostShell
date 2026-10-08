---
name: ghostshell-stale-tab-phone-2026-10-07
description: 2026-10-07 Donald on his phone reported the Claude tab not updating and showing a permission prompt from a minute earlier; what was observed from the desktop, cause not established
metadata:
  type: project
---

**Report (Donald, 2026-10-07, using the phone):** "Bug in GhostShell. Tab not updating, shows stale permission prompt from a minute ago."

**Observed from the desktop, read-only, same moment:** the main Sublime's Claude tab (an ai_terminal view, broker pipe `ghostshell_32909392747d496da0b3`) held the old approval prompt for a `gh issue create` command as its last text. While Claude Code was running commands, `view.change_count()` stayed at 1979347 and `view.size()` at 16952 over a 2 second sample, so the view was not being updated even though the session was active. Sublime stayed responsive (one "slow dispatch took 2.00s" line). The console showed no new GhostShell exceptions, only a run of 10 consecutive `[ai_terminal] resized PTY to ...` lines (95x41, 94x34, 88x32, 89x39, 75x31, 73x26, 71x31, 69x26, 84x35, 83x29) after the SettingsUI install lines.

**Not established:** the cause. A link to the phone's terminal size changes is a guess based on the resize lines and on [[project_st_trackpad_zoom_freeze]] (resize loops) and [[project_ghostshell_follow_patch_2026_10_03]] (view/viewport problems in the same tab). Nothing was changed in GhostShell and nothing was restarted.

**How to apply:** if it happens again, first sample `change_count` twice a couple of seconds apart (a frozen count means the render loop stopped, not just a stale screen), then look at the last `resized PTY` lines and whether a remote client is attached; use the recorder approach in [[project_ghostshell_follow_patch_2026_10_03]] if a longer trace is needed.
