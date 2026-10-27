/-
Model of the session selection in Recover Session: GhostShell ai_terminal.py, `_show_choices`
(lines ~10591-10650 at the commit this was written against).

Modelled: which pipes end up in `sessions`.  Not modelled: the row text, the picker, the
attach itself, and how `_has_usable_view` decides (it is the Bool `usable` below).
-/
namespace GhostShell

structure Term where
  pipe : Option String   -- getattr(term.pty, "pipe_name")
  usable : Bool          -- _has_usable_view(term)

def hit (p : String) (t : Term) : Bool := decide (t.pipe = some p)

/-- `terms_by_pipe[p]` after the loop, for one pipe p:
kept is replaced only by a usable term when kept is not usable. -/
def pick (p : String) : List Term → Option Term → Option Term
  | [], k => k
  | t :: ts, k =>
    pick p ts
      (if hit p t then
        match k with
        | none => some t
        | some k0 => if !k0.usable && t.usable then some t else some k0
       else k)

/-- `_has_usable_view(terms_by_pipe.get(p))`; None is not usable. -/
def kUsable (k : Option Term) : Bool := k.any (·.usable)

def termPipes (ts : List Term) : List String := ts.filterMap (·.pipe)

/-- Each element once, in first-seen order (the `seen_pipes` loop). -/
def dedupGo (acc : List String) : List String → List String
  | [] => acc
  | a :: l => if acc.contains a then dedupGo acc l else dedupGo (acc ++ [a]) l

def dedup (l : List String) : List String := dedupGo [] l

theorem dedupGo_mem (x : String) : ∀ (l acc : List String), x ∈ dedupGo acc l ↔ x ∈ acc ∨ x ∈ l := by
  intro l
  induction l with
  | nil => intro acc; simp [dedupGo]
  | cons a l ih =>
    intro acc
    by_cases h : acc.contains a = true
    · have ha : a ∈ acc := by simpa using h
      rw [show dedupGo acc (a :: l) = dedupGo acc l by simp [dedupGo, ha], ih]
      simp only [List.mem_cons]
      constructor
      · rintro (h1 | h1)
        · exact Or.inl h1
        · exact Or.inr (Or.inr h1)
      · rintro (h1 | h1 | h1)
        · exact Or.inl h1
        · subst h1; exact Or.inl ha
        · exact Or.inr h1
    · have hn : a ∉ acc := by simpa using h
      rw [show dedupGo acc (a :: l) = dedupGo (acc ++ [a]) l by simp [dedupGo, hn], ih]
      simp only [List.mem_append, List.mem_singleton, List.mem_cons]
      simp [or_assoc]

theorem dedupGo_nodup : ∀ (l acc : List String), acc.Nodup → (dedupGo acc l).Nodup := by
  intro l
  induction l with
  | nil => intro acc h; simpa [dedupGo] using h
  | cons a l ih =>
    intro acc h
    by_cases hc : acc.contains a = true
    · have hca : a ∈ acc := by simpa using hc
      rw [show dedupGo acc (a :: l) = dedupGo acc l by simp [dedupGo, hca]]
      exact ih acc h
    · have hn : a ∉ acc := by simpa using hc
      rw [show dedupGo acc (a :: l) = dedupGo (acc ++ [a]) l by simp [dedupGo, hn]]
      apply ih
      rw [List.nodup_append]
      refine ⟨h, (by simp), ?_⟩
      intro x hx y hy hxy
      rw [List.mem_singleton] at hy
      subst hy; subst hxy; exact hn hx

@[simp] theorem dedup_mem (x : String) (l : List String) : x ∈ dedup l ↔ x ∈ l := by
  simp [dedup, dedupGo_mem]

theorem dedup_nodup (l : List String) : (dedup l).Nodup := dedupGo_nodup l [] List.nodup_nil
/-- Sessions taken from the brokers (each pipe once, in first-seen order). -/
def part1 (bs : List String) (ts : List Term) : List String :=
  (dedup bs).filter (fun p => !kUsable (pick p ts none))

/-- Sessions taken from local terminals, only when process discovery failed. -/
def part2 (bs : List String) (ts : List Term) (error : Bool) : List String :=
  if error then
    (dedup (termPipes ts)).filter (fun p => !bs.contains p && !kUsable (pick p ts none))
  else []

def listed (bs : List String) (ts : List Term) (error : Bool) : List String :=
  part1 bs ts ++ part2 bs ts error

/-- The kept terminal is usable exactly when some terminal of that pipe is usable
(this is what the 32cec64 fix established: a healthy tab is never shadowed by an old object). -/
theorem pick_usable (p : String) :
    ∀ (ts : List Term) (k : Option Term),
      kUsable (pick p ts k) = (kUsable k || ts.any (fun t => hit p t && t.usable)) := by
  intro ts
  induction ts with
  | nil => intro k; simp [pick]
  | cons t ts ih =>
    intro k
    simp only [pick, ih, List.any_cons]
    by_cases h : hit p t = true
    · cases k with
      | none => simp [h, kUsable]
      | some k0 =>
        cases hk : k0.usable <;> cases htu : t.usable <;> simp [h, kUsable, hk, htu]
    · simp [h, kUsable]

theorem healthy_never_listed (bs : List String) (ts : List Term) (error : Bool) (p : String)
    (t : Term) (ht : t ∈ ts) (hp : t.pipe = some p) (hu : t.usable = true) :
    p ∉ listed bs ts error := by
  have key : kUsable (pick p ts none) = true := by
    rw [pick_usable]
    simp only [kUsable, Option.any_none, Bool.false_or, List.any_eq_true]
    exact ⟨t, ht, by simp [hit, hp, hu]⟩
  unfold listed part1 part2
  simp [List.mem_filter, key]

/-- A broker's session whose terminals are all unusable (or absent) is always listed. -/
theorem broken_broker_listed (bs : List String) (ts : List Term) (error : Bool) (p : String)
    (hb : p ∈ bs) (hno : ∀ t ∈ ts, t.pipe = some p → t.usable = false) :
    p ∈ listed bs ts error := by
  have key : kUsable (pick p ts none) = false := by
    rw [pick_usable]
    simp only [kUsable, Option.any_none, Bool.false_or]
    cases h : ts.any (fun t => hit p t && t.usable)
    · rfl
    · rw [List.any_eq_true] at h
      obtain ⟨t, ht, h2⟩ := h
      simp [hit] at h2
      have := hno t ht h2.1
      simp [this] at h2
  unfold listed part1
  simp [List.mem_append, List.mem_filter, hb, key]

/-- When discovery failed, a broken local terminal's session is listed too, so nothing
broken is ever lost. -/
theorem broken_term_listed_on_error (bs : List String) (ts : List Term) (p : String)
    (ht : p ∈ termPipes ts) (hno : ∀ t ∈ ts, t.pipe = some p → t.usable = false) :
    p ∈ listed bs ts true := by
  by_cases hb : p ∈ bs
  · exact broken_broker_listed bs ts true p hb hno
  · have key : kUsable (pick p ts none) = false := by
      rw [pick_usable]
      simp only [kUsable, Option.any_none, Bool.false_or]
      cases h : ts.any (fun t => hit p t && t.usable)
      · rfl
      · rw [List.any_eq_true] at h
        obtain ⟨t, ht', h2⟩ := h
        simp [hit] at h2
        have := hno t ht' h2.1
        simp [this] at h2
    unfold listed part2
    simp [List.mem_append, List.mem_filter, hb, key, ht]

/-- No pipe is listed twice. -/
theorem listed_nodup (bs : List String) (ts : List Term) (error : Bool) :
    (listed bs ts error).Nodup := by
  unfold listed
  rw [List.nodup_append]
  refine ⟨?_, ?_, ?_⟩
  · exact List.Pairwise.filter _ (dedup_nodup bs)
  · unfold part2
    split
    · exact List.Pairwise.filter _ (dedup_nodup _)
    · exact List.nodup_nil
  · intro a ha b hb hab
    subst hab
    unfold part1 at ha
    unfold part2 at hb
    split at hb
    · simp [List.mem_filter] at ha hb
      simp [ha.1] at hb
    · simp at hb

/-- With a working process discovery, only brokers' pipes are listed (no phantom rows
from dead objects: the b4cc9f0 fix). -/
theorem no_phantoms (bs : List String) (ts : List Term) (p : String)
    (h : p ∈ listed bs ts false) : p ∈ bs := by
  unfold listed part1 part2 at h
  simp [List.mem_append, List.mem_filter] at h
  exact h.1

/-! ### The rule before 32cec64, for contrast -/

/-- Old rule: the dict kept the LAST terminal object seen for a pipe. -/
def pickLast (p : String) : List Term → Option Term → Option Term
  | [], k => k
  | t :: ts, k => pickLast p ts (if hit p t then some t else k)

/-- With a healthy tab followed by an old dead object for the same session (what a plugin reload
leaves behind), the old rule judged the session unusable, so a healthy tab was offered for
"recovery" and picking it would have disconnected it. The fixed `pick` does not. -/
theorem old_rule_lists_healthy_tab :
    let ts : List Term := [⟨some "p", true⟩, ⟨some "p", false⟩]
    kUsable (pickLast "p" ts none) = false ∧ kUsable (pick "p" ts none) = true := by
  decide

end GhostShell