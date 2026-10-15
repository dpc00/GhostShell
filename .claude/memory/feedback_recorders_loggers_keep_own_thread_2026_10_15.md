---
name: recorders-loggers-keep-own-thread-2026-10-15
description: Exception to his one-thread rule: cast recorders and loggers (disk writers) keep their own thread; I removed those threads unasked, he said no, restored
metadata:
  type: feedback
---

2026-10-15, Donald: "cast recorders or logger should have their own thread!" after I moved CastRecorder (and the session text log debounce) onto the main thread as part of the one-thread conversion (e584a98).

**Why:** disk writes (slow disk, antivirus) must never stall the editor; the original design had a writer thread for exactly that. His one-thread preference ([[feedback_one_thread_async_await_promises_2026_10_15]] in the sublime-mcp repo) is about program logic and I over-applied it to file I/O.

**How to apply:** file writers for recordings and logs (cast, session text log, any new log) keep a dedicated thread or an equivalent off-main-thread mechanism; do not move them onto the UI thread or the aio loop. Restored in the commit after e584a98; the rest of the conversion (broker, timers, client pipes, discover, download) stays one-thread. Map: [[project_one_thread_conversion_map_2026_10_15]].
