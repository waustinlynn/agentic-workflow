# Universal Repository Onboarding Templates

Copy-pasteable starter kits for onboarding any repository type to `agentic-workflow` with zero framework code changes:

| Stack | Template Config | Default `auto_merge` |
|---|---|---|
| **Frontend** (React / Vite / Next.js) | `templates/frontend/.agentic/config.yaml` | `true` |
| **Backend** (Python / Node / Go / .NET) | `templates/backend/.agentic/config.yaml` | `true` |
| **Mobile** (React Native / Flutter / Native) | `templates/mobile/.agentic/config.yaml` | `false` |
| **Infra** (Terraform / OpenTofu / CDK) | `templates/infra/.agentic/config.yaml` | `false` |

Shared templates:
- `templates/AGENTS.md` — Starter engineering, testing, and architecture rules read by both Coder and Reviewer agents.
- `templates/workflows/ci.yml` — Required PR verification check (`verify`).
- `templates/workflows/deploy.yml` — Post-merge deployment workflow triggered on push to `main`.

## Required GitHub Repository Settings

1. **Allow Auto-Merge:** *Settings → General → Pull Requests → Allow auto-merge* (checked).
2. **Branch Protection (`main`):**
   - Require a pull request before merging (at least **1 approval** — satisfied by `agentic-reviewer[bot]`).
   - Require status checks to pass before merging (add `verify` from `ci.yml`).
   - Require conversation resolution before merging (recommended).
3. **Install Both GitHub Apps:** Install both `agentic-coder` and `agentic-reviewer` on the repository.
4. **Register & Verify:** Run `agentic onboard <owner/repo>` (see `docs/onboarding.md`).
