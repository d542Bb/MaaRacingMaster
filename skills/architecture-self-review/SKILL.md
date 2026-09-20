# Architecture Self-Review

## Trigger
Invoke this skill when:
- an architecture/design proposal is difficult to understand or justify;
- a new abstraction, shared state, layer, interface, manager, registry, or indirection is being introduced;
- an existing design is being refactored across module boundaries;
- the parent explicitly asks for an architecture review or a second-pass challenge.

Do **not** invoke this skill automatically for ordinary implementation work.

## Objective
Challenge the proposed design without automatically replacing it with a preferred pattern.

## Review sequence

### 1. Concrete problem
State the current problem in one sentence.
- What is broken, costly, duplicated, unstable, or hard to change today?
- Reject hypothetical future needs as the sole justification unless there is evidence.

### 2. Boundary and ownership
Determine:
- Who owns each piece of state/behavior?
- Who is allowed to change it?
- Which details must remain private?
- What contract does each consumer actually rely on?

### 3. Coupling and cohesion
Ask:
- What new dependency is introduced?
- Is the dependency on stable semantics or implementation details?
- Are grouped responsibilities changed by the same reasons?
- Is sharing based on one common fact/contract, or merely convenience?

### 4. Change propagation
Trace one realistic change:
- If implementation A changes, who must change?
- If the contract changes, who must change?
- Where does the intended change propagation stop?
- Does the proposal reduce propagation or merely move it?

### 5. KISS / YAGNI
For every new layer or abstraction:
- What current requirement becomes impossible without it?
- Could a simpler design satisfy the same current requirement?
- Is this solving an observed problem or preparing for a speculative one?

### 6. DRY
If duplication is being removed:
- Is the repeated code expressing the same knowledge?
- Or are two similar-looking implementations allowed to evolve independently?
- Could the abstraction reduce duplication while increasing coupling?

### 7. Failure and debugging
Ask:
- When something goes wrong, where can the failure originate?
- Does the design make causality clearer or more opaque?
- Does shared state hide who produced an invalid value?
- Are diagnostic details being mixed into the business contract?

## Output
Return only:
1. **What the proposal clearly improves**
2. **The strongest concrete risk**
3. **What evidence would resolve the remaining uncertainty**
4. **Whether the current design is sufficient without further abstraction**

Do not give a score, tier, or “architectural quality” rating.
Do not introduce a replacement architecture unless the parent asks for one.

## Anti-pattern
Never perform a ritual checklist such as “SRP ✓ DRY ✓ KISS ✓”.
Use only the principles that explain an actual risk or decision.
