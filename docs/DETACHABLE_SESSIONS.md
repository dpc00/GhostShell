# Detachable agent sessions

GhostShell can keep selected terminal sessions alive independently of Sublime
Text. A profile opts in with `"detachable": true` in
`ai_terminal.sublime-settings`.

This is intended for long-running interactive agents such as Codex and Claude.
If Sublime Text closes or its terminal view becomes unusable, the agent and its
ConPTY can remain alive while a later GhostShell client reconnects to them.

## Components

- `_BrokerPty` in `ai_terminal.py` is the Sublime-side client. It connects to
  three local named pipes for output, input, and control messages.
- `tools/agent_broker.py` owns the ConPTY and child agent process. It accepts
  one client at a time and retains a bounded raw-output replay buffer.
- `tools/spawn_outside_job.ps1` starts the broker independently of Sublime's
  process job. It uses a short-lived interactive Windows Scheduled Task.
- A JSON record under the broker registry directory identifies each live
  broker by pipe name, PID, profile, working directory, and child command.

The temporary scheduled task is only a launch trampoline. Broker options,
child arguments, and the environment are written to a randomized, one-use
`.launch` file. The broker consumes and deletes that file, after which the
launcher unregisters the task. The continuing broker normally runs through
`pythonw.exe`, so it does not create a DOS console window.

## Lifecycle

1. GhostShell creates a unique pipe name and one-use launch file.
2. The scheduled-task launcher starts the windowless broker and waits until
   the broker consumes the launch file.
3. The broker creates the ConPTY, starts the requested agent, publishes its
   registry record, and serves its named pipes.
4. GhostShell connects and renders output normally.
5. If the client disappears, the broker keeps the ConPTY and agent alive and
   makes fresh pipe instances available.
6. A new client reconnects, receives retained raw output followed by a replay
   boundary marker, reconstructs the terminal screen, and resumes live I/O.
7. An explicit session end sends `KILL` over the control pipe. The broker then
   stops the child and removes its registry record.

The broker replay buffer is recovery context, not a full transcript. Its
default is 2 MiB and it is clamped to 1–256 MiB. Change
`broker_scrollback_bytes` globally or on an individual profile if necessary.

## User recovery commands

`Ai Terminal: Revive Frozen Tab` disconnects and rebuilds the current tab's
pipe client without terminating the agent. Use this first when a terminal tab
stops accepting keys or repainting but Sublime itself still works.

`Ai Terminal: Recover Orphaned Session...` searches the external registry,
known terminal objects, and broker processes, then offers live sessions that
are not already attached to a usable tab. Use it after reopening Sublime or
when the original tab is gone.

A deliberate single-tab close ends that tab's underlying detachable session.
Closing a Sublime window is treated differently: GhostShell detaches its local
client so restored tabs can reconnect later.

Recovery is not persistence across Windows restart, sign-out, child-agent
exit, or an explicit session end. A broker can only restore output still held
within its bounded replay buffer.

## Live verification

On 2026-08-30, a full Sublime Text restart preserved and reattached both an
active Codex session and a PowerShell session. Both tabs were responsive
immediately after restoration. This verifies the production restart path in
addition to the isolated Scheduled Task integration test below.

## Automated verification

The normal suite does not create scheduled tasks:

```powershell
python -m pytest tests -q
```

The live integration test creates a real temporary Scheduled Task, broker,
ConPTY, `cmd.exe` child, registry record, and named pipes. It verifies:

- the launch file and temporary task disappear;
- the broker PID remains unchanged across reconnects;
- shell state survives each client replacement;
- replay completes for every new client;
- abrupt death of a separate client process releases all pipe endpoints;
- a new client recovers after that simulated editor crash; and
- explicit shutdown removes the broker registry record.

Run the default 10-cycle live test from PowerShell:

```powershell
$env:GHOSTSHELL_RUN_SCHEDULED_BROKER_TEST = "1"
python -m pytest tests/test_scheduled_broker_integration.py -v -s
```

Run a longer soak by setting the cycle count:

```powershell
$env:GHOSTSHELL_RUN_SCHEDULED_BROKER_TEST = "1"
$env:GHOSTSHELL_BROKER_TEST_CYCLES = "100"
python -m pytest tests/test_scheduled_broker_integration.py -q
```

The live test is Windows-only and opt-in because it mutates Task Scheduler
briefly. Its `finally` cleanup explicitly ends the test broker even after an
assertion failure. Broker lifecycle diagnostics are appended to
`~/data/logs/ai_terminal/agent_broker.log`.
