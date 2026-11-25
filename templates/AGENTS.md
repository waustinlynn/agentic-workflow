# AGENTS.md

Repository-specific rules for automated coding (`agentic-coder[bot]`) and review (`agentic-reviewer[bot]`) agents.
Customize the sections below for your repository's stack and conventions.

## Architecture & Directory Layout

- `src/` — Application source code
- `tests/` — Automated test suite

## Verification Commands

Always run these commands before finishing a task (mirroring `.agentic/config.yaml`):

```bash
# Replace with your repo's lint, typecheck, and test commands
# e.g., npm run lint && npm run typecheck && npm test
```

## Coding & Architecture Rules

1. **Keep changes focused:** Solve only the scoped GitHub issue; avoid unrelated refactors or formatting churn.
2. **Testable boundaries:** Inject external dependencies (HTTP clients, databases, cloud SDKs, clocks) so core logic is unit-testable without live network calls.
3. **Avoid speculative abstractions:** Do not introduce interfaces, factories, or abstract base classes unless there are at least two concrete implementations or an explicit test seam.
4. **Stack-specific standards (customize or remove):**
   - *Backend/OOP services:* Keep handlers thin; separate domain logic from transport/persistence layers.
   - *Frontend apps:* Prefer small composable components, explicit prop types, and accessible semantics.
   - *Infrastructure:* Follow least-privilege IAM, keep state/backend configs protected, and avoid hardcoded secrets.

## Testing Standards

1. **Bug fixes are test-first:** Reproduce the bug with a failing test first, verify it fails for the expected reason, then implement the minimal fix.
2. **Test observable behavior:** Assert public contracts, edge cases, and error paths rather than private internals.
3. **Never weaken tests:** Never skip, delete, or relax assertions just to make CI green.

## Protected Paths

Agents must never modify files matching:
- `.github/workflows/**`
- `.agentic/**`
- `CODEOWNERS`
