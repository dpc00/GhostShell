/-
Model of the broker's two connection slots in GhostShell: tools/agent_broker.py
  _OutputServer.run_forever (pipe <name>, one instance, ~905-957)
  _InputServer.run_forever  (pipe <name>-in, one instance, ~972-1006)
  _OutputServer.disconnect_client (~867), _InputServer.force_disconnect (~1008)
and the client side: ai_terminal.py _BrokerPty.astart (~1282: output first, then input; input
failure closes the output handle), _pipe_instance_free (~868: probes the OUTPUT pipe only) and
_release_pipe_async (~934: sends DISCONNECT only when that probe says busy).

Modelled: who holds each slot.  Not modelled: timing, the control pipe, the scrollback.
STATUS (2026-10-27): the model shows an asymmetry in the broker, not a reproduced bug.
  * `_OutputServer` frees its slot when a write to the client fails (`client.gone`), but nothing
    tells the input slot; `_InputServer` leaving does free the output slot (`disconnect_client`).
  * `_pipe_instance_free`, the "busy" probe, looks at the output pipe only, and
    `_release_pipe_async` sends DISCONNECT only when that probe says busy.
  * So a client that keeps the input pipe after its output pipe failed would leave the session
    reported "free" while a second client can get output but never input (theorem `split_brain`).
  * Reading the client code, no path was found that does this: `_revive_terminal_client` calls
    `pty.kill()` (closes the input handle) before reattaching, and the Windows Terminal relay is
    disconnected on both pipes by DISCONNECT.  The state is reachable in the model, not shown
    reachable in the running code.
  * `J_preserved_fixed` shows that releasing the input slot together with the output slot on a
    failed write would make J an invariant.  Not applied.
-/
namespace GhostShellSlots

structure St where
  out : Option Nat   -- client holding the output pipe
  inn : Option Nat   -- client holding the input pipe
  deriving DecidableEq

inductive Ev
  | attachOut (c : Nat)   -- a client connects to the output pipe (one instance: only when free)
  | attachIn (c : Nat)    -- the same client then connects the input pipe
  | outFail               -- a write to the output client fails: _send_loop sets client.gone
  | inLeave               -- the input client leaves or is kicked: on_disconnect -> disconnect_client

/-- The code as it is. -/
def step (s : St) : Ev → St
  | .attachOut c => if s.out = none then { s with out := some c } else s
  | .attachIn c => if s.out = some c ∧ s.inn = none then { s with inn := some c } else s
  | .outFail => { s with out := none }                 -- the input slot is not touched
  | .inLeave => { out := none, inn := none }            -- releases the output side too

/-- What `_pipe_instance_free` reports: free when the OUTPUT pipe has no client. -/
def probeFree (s : St) : Bool := decide (s.out = none)

/-- Invariant: nobody holds input alone, and the two slots never belong to different clients. -/
def J (s : St) : Prop :=
  (s.out = none → s.inn = none) ∧ (∀ a b, s.out = some a → s.inn = some b → a = b)

/-- Start: one client A holds both slots. -/
def both (a : Nat) : St := { out := some a, inn := some a }

theorem both_J (a : Nat) : J (both a) := by
  refine ⟨?_, ?_⟩
  · intro h; simp [both] at h
  · intro x y hx hy; simp [both] at hx hy; omega

/-- A failed output write leaves the input slot held while the probe says "free". -/
theorem outFail_leaves_input_held (a : Nat) :
    step (both a) .outFail = { out := none, inn := some a } ∧
    probeFree (step (both a) .outFail) = true := by
  simp [step, both, probeFree]

/-- ...and then another client B gets the output pipe but can never get the input pipe:
the two slots belong to different clients, which breaks J. -/
theorem split_brain (a b : Nat) (hab : a ≠ b) :
    let s := step (step (step (both a) .outFail) (.attachOut b)) (.attachIn b)
    s = { out := some b, inn := some a } ∧ ¬ J s := by
  intro s
  have hs : s = { out := some b, inn := some a } := by
    simp [s, step, both, hab]
  refine ⟨hs, ?_⟩
  intro hJ
  have := hJ.2 b a (by simp [hs]) (by simp [hs])
  exact hab this.symm

/-- The same trace under the check the client makes: it asks DISCONNECT only when the probe says
busy.  Here the probe says free, so no DISCONNECT is sent and attachIn is refused. -/
theorem no_disconnect_sent (a : Nat) : probeFree (step (both a) .outFail) = true :=
  (outFail_leaves_input_held a).2

/-! ### A symmetric version: a failed output write also releases the input side -/

def stepFixed (s : St) : Ev → St
  | .outFail => { out := none, inn := none }
  | e => step s e

/-- With the symmetric release, J holds after every event, from any state where it holds. -/
theorem J_preserved_fixed (s : St) (e : Ev) (h : J s) : J (stepFixed s e) := by
  obtain ⟨h1, h2⟩ := h
  cases e with
  | outFail => simp [stepFixed, J]
  | inLeave => simp [stepFixed, step, J]
  | attachOut c =>
    simp only [stepFixed, step]
    by_cases ho : s.out = none
    · have hi := h1 ho
      simp [ho, hi, J]
    · rw [if_neg ho]; exact ⟨h1, h2⟩
  | attachIn c =>
    simp only [stepFixed, step]
    by_cases hc : s.out = some c ∧ s.inn = none
    · simp [hc, J]
    · rw [if_neg hc]; exact ⟨h1, h2⟩

/-- Without it, J is not preserved: the fault above. -/
theorem J_not_preserved_current : ∃ s e, J s ∧ ¬ J (step s e) := by
  refine ⟨both 0, .outFail, both_J 0, ?_⟩
  intro h
  have := h.1
  simp [step, both] at this

end GhostShellSlots
