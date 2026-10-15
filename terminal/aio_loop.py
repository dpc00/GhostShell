"""One asyncio loop for the whole plugin, run on Sublime's main thread with no thread of its own.

The loop is a ProactorEventLoop (IOCP: overlapped pipes, sockets, subprocesses, process-handle waits) whose
IocpProactor.select never blocks. Each step is one `_run_once()` call, scheduled with `sublime.set_timeout`: at once
while work is ready, else every IDLE_DELAY_MS. Measured in a portable Sublime: 0 extra threads, idle cost the same
as a bare set_timeout chain, a 50 ms asyncio.sleep takes about 50 ms.

Rules for code that runs on this loop: it runs on the UI thread, so never block (no blocking file or pipe calls, no
time.sleep, no subprocess.run) and keep every callback short.

`ensure_started()` creates the loop on first use; `shutdown()` cancels its tasks and stops it (plugin_unloaded).
The running loop is remembered on the `sublime` module, so a reload of this module stops the previous loop's step
chain instead of leaving two chains running.
"""
import asyncio
import threading
import traceback

import sublime

IDLE_DELAY_MS = 10

_PREVIOUS_ATTR = "_gs_aio_loop"
_MAIN_THREAD = threading.get_ident()   # plugin modules are imported on Sublime's main thread


class _SteppedLoop(asyncio.ProactorEventLoop):
    def __init__(self):
        super().__init__()
        real_select = self._proactor.select
        self._proactor.select = lambda timeout=None: real_select(0)   # a step never blocks
        self._stepping = False

    def start(self):
        asyncio.events._set_running_loop(self)
        self._thread_id = threading.get_ident()
        self._stepping = True
        self._step()

    def _step(self):
        if not self._stepping:
            return
        try:
            self._run_once()
        except BaseException:
            print("[ai_terminal] aio loop step failed:\n%s" % traceback.format_exc())
        if self._stepping:
            sublime.set_timeout(self._step, 0 if self._ready else IDLE_DELAY_MS)

    def halt(self):
        """Cancel every task, let the cancellations run, then stop stepping and close the loop."""
        tasks = [t for t in asyncio.all_tasks(self) if not t.done()]
        for task in tasks:
            task.cancel()
        for _ in range(5):
            try:
                self._run_once()
            except BaseException:
                break
        self._stepping = False
        try:
            if asyncio.events._get_running_loop() is self:
                asyncio.events._set_running_loop(None)
            self._thread_id = None      # is_running() looks at this; close() refuses a running loop
            self.close()
        except BaseException:
            print("[ai_terminal] aio loop close failed:\n%s" % traceback.format_exc())


_loop = None


def ensure_started():
    """The plugin's loop, created and started on first call (main thread only)."""
    global _loop
    if _loop is None:
        previous = getattr(sublime, _PREVIOUS_ATTR, None)
        if previous is not None:
            previous.halt()
        _loop = _SteppedLoop()
        setattr(sublime, _PREVIOUS_ATTR, _loop)
        _loop.start()
    return _loop


def run(coro):
    """Schedule a coroutine on the loop (starting it if needed) and return its Task.
    From another thread the coroutine is handed to the main thread and None is returned."""
    if threading.get_ident() != _MAIN_THREAD:
        sublime.set_timeout(lambda: run(coro), 0)
        return None
    task = ensure_started().create_task(coro)
    task.add_done_callback(_report_failure)
    return task


def _report_failure(task):
    if task.cancelled():
        return
    error = task.exception()
    if error is not None:
        print("[ai_terminal] background task failed:\n%s"
              % "".join(traceback.format_exception(type(error), error, error.__traceback__)))


def call_later(delay_s, callback, *args):
    """loop.call_later on the plugin's loop; returns a TimerHandle with .cancel().
    From another thread (the loop is not thread-safe) the call is handed to the main thread and None is returned."""
    if threading.get_ident() != _MAIN_THREAD:
        sublime.set_timeout(lambda: call_later(delay_s, callback, *args), 0)
        return None
    return ensure_started().call_later(delay_s, callback, *args)


def shutdown():
    global _loop
    loop, _loop = _loop, None
    if loop is not None:
        loop.halt()
        if getattr(sublime, _PREVIOUS_ATTR, None) is loop:
            setattr(sublime, _PREVIOUS_ATTR, None)
