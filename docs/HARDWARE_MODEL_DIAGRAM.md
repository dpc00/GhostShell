# GhostShell as a digital circuit (omp / Gemini 3.7 Flash, 2026-09-20)

Verbatim from ai_2026-09-20_122143_767945.log lines 1004-1088. The block boundaries are omp's own reading of ai_terminal.py, not verified against the code. Its "8ms clamp loop" is out of date: the clamp loop was changed to 500ms on 2026-09-19 (57624a0).

 Thinking of GhostShell as a digital circuit architecture with buses, registers, state machines, and multiplexers makes the entire
 system clear and concrete.

 Here is the exact hardware translation of GhostShell:

 ```
                          ┌────────────────────────────────────────────────────────┐
                          │                SUBLIME TEXT ENVIRONMENT                │
                          │                                                        │
  [Keyboard / Mouse] ────►│ [Input Demux] ──────────┐                              │
                          │   │ (plain keys)        │ (Shift/Ctrl drag)            │
                          │   ▼                     ▼                              │
                          │ [PTY UART Tx]      [ST Native Selection]               │
                          └──────┬─────────────────────▲───────────────────────────┘
                                 │ Byte Stream         │
                                 ▼                     │
                 ┌───────────────────────────────┐     │
                 │   CHILD AGENT / PROCESS       │     │
                 │  (Claude, Grok, Codex, etc.)  │     │
                 └───────────────┬───────────────┘     │
                                 │ PTY Rx Byte Stream  │
                                 ▼                     │
     ┌─────────────────────────────────────────────────┴────────────────────────┐
     │ GHOSTSHELL CORE (Hardware Circuit)                                       │
     │                                                                          │
     │  ┌──────────────────────────────────────────────────────────────────┐    │
     │  │ 1. ANSI / VT100 Decoder FSM (Finite State Machine)               │    │
     │  │    • Printable characters ───────────► Write Data Bus [7:0]      │    │
     │  │    • CSI / Cursor commands ──────────► Cursor Position Registers │    │
     │  │    • DECSET 1049 strobe ─────────────► ALT_SCREEN_LATCH (D-FF)   │    │
     │  │    • DECSET 1000/1002 strobe ────────► MOUSE_TRACK_LATCH (D-FF)  │    │
     │  └────────┬─────────────────────────────────────┬───────────────────┘    │
     │           │                                     │                        │
     │           ▼                                     ▼                        │
     │  ┌─────────────────────────┐          ┌───────────────────────┐          │
     │  │ 2. Dual Video RAM       │          │ 3. Cursor & Caret MUX │          │
     │  │   [ Main Screen RAM ]   │          │   [ Hardware (cx,cy) ]│          │
     │  │   [ Alt Screen RAM  ]   │          │   [ Clamped (cx,cy)  ]│          │
     │  │            │            │          │           │           │          │
     │  │     2-to-1 MUX          │          │      2-to-1 MUX       │          │
     │  │   (SEL = ALT_LATCH)     │          │   (SEL = PIN_ENABLE)  │          │
     │  └────────────┬────────────┘          └───────────┬───────────┘          │
     │               │                                   │                      │
     │               ▼                                   ▼                      │
     │         [ Video Bus ]                       [ Caret Bus ]                │
     └───────────────┬───────────────────────────────────┬──────────────────────┘

 ### Hardware Blocks Breakdown

 #### 1. ANSI Stream Decoder (Finite State Machine)

 - Hardware Equivalent: Mealy/Moore State Machine + Instruction Decoder.
 - Function: Reads incoming bytes from the PTY UART. If it sees raw characters, it asserts the RAM_WRITE_ENABLE line. If it sees ESC [
   (0x1B 0x5B), it branches into the escape-decode state, parsing row/column numbers and pulsing control lines (CUP_STROBE,
   CLEAR_LINE, SET_ALT_SCREEN).

 #### 2. Dual-Buffer Video RAM & Screen Multiplexer

 - Hardware Equivalent: Two banks of Dual-Port Static RAM + 2-to-1 Bus Multiplexer.
 - Bank A (Main RAM + Ring Buffer): Regular scrolling console text + circular FIFO history.
 - Bank B (Alt Screen RAM): Dedicated 1-page framebuffer used by fullscreen TUIs (Grok, Codex, htop).


 #### 3. Cursor & Viewport Alignment Unit

 - Hardware Equivalent: Arithmetic Logic Unit (ALU) + Multiplexer.
 - Registers:
   - Hardware_Cursor (X, Y): Raw position requested by the child process.
   - Viewport_Offset (Y): Vertical scroll offset in Sublime Text.
   - Clamped_Cursor (X, Y): Output of ALU applying boundary gates (caret_footer_pinning_enabled, follow_ignore_trailing_lines).
 - Why it glitched: In electronics terms, when the agent app rapidly alternates between updating content at row 10 and parking its
   hardware cursor on the status line (row 45), an unlatched/unpinned cursor line causes high-frequency ringing on the viewport scroll
   comparator, resulting in visual "jumping".







 #### 4. The Detachable Broker

 - Hardware Equivalent: An external FIFO Ring Buffer on an isolated power supply (keeps running even if Sublime Text power is switched
   off).
 - On reconnect, it replays the last 2 MiB byte window into the ANSI Decoder FSM to restore circuit state.
