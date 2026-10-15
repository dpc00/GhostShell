---
name: test-oldest-supported-build-before-release-2026-10-15
description: v0.2.7 shipped broken on Python 3.8 hosts (build 4200 and earlier) because I never ran it there; before any GhostShell release run the real flow on the portable 4200 (3.8)
metadata:
  type: feedback
---

2026-10-15: I released v0.2.7 (the one-thread rework) after testing only on build 4215, where plugins run on Python 3.14. I had named the 3.8 risk as a gate, dropped it when Donald said 4215 translates 3.8 to 3.14 (true for 4215, not for 4200 and earlier), and wrote "older builds not tried" in the notes. When he asked "you are saying it won't work on 4200? Why didn't you point that out?" I ran it on the 3.8 portable: typing did nothing ("got Future attached to a different loop": an `asyncio.Queue()` made outside the loop binds to the wrong loop before Python 3.10). Fixed in v0.2.8 (a2158c3), warning added to the v0.2.7 release page.

**Why:** the declared minimum is Python 3.8 (`.python-version`), the code uses private asyncio calls, and "works on 3.14" proves nothing about 3.8.

**How to apply:** before tagging a GhostShell release, run spawn, typing, Close Tab keep-alive, Recover Session, resize, a plugin reload and End Session on the 3.8 portable (`D:\st_portable_4200`, HTTP bridge 9520; install a `git archive` copy of the tag plus `terminal\bin\ghostty-vt.dll` into its Data\Packages, recycle the copy afterwards; never pick the live Claude session in its Recover list). Say "untested" only when it truly cannot be run, not when I skipped it. 3.8-only traps so far: asyncio.Queue/Lock/Event created outside a running loop.
