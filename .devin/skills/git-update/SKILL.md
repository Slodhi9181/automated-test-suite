---
name: git-update
description: Review working-tree changes, draft a commit, and push to GitHub (Slodhi9181/automated-test-suite). Always asks for confirmation before committing/pushing.
argument-hint: "[optional: what changed, to guide the commit message]"
allowed-tools:
  - exec
  - read
  - grep
---

Sync this project's changes to git (branch `main`). Remotes: `origin` fetches from GitHub (git@github.com:Slodhi9181/automated-test-suite.git) and has **two push URLs** (GitHub + Bitbucket) — a single `git push origin HEAD` syncs both hosts.

Procedure:

1. Run in parallel: `git status`, `git diff` (unstaged), `git diff --cached` (staged), and `git log --oneline -5` (to match past commit message style).
2. If there are NO changes (clean tree and `git status` shows "up to date with 'origin/main'"), say so and stop — never commit or push when nothing changed.
3. Summarize what changed and why in a few bullets, then draft a concise commit message focused on the "why", not the "what". Check the diff for sensitive info (credentials, tokens, internal IPs in newly added files) and flag any concerns.
4. **Ask the user for confirmation before running `git add`/`git commit`/`git push`.** Present the proposed commit message and let them approve or edit it. If the user already gave approval for this specific update (e.g. they invoked this skill saying "commit and push this"), confirmation is granted.
5. Commit using the repo identity passed per-invocation (git config identity is intentionally not persisted on this machine):

```bash
git -c user.name="Slodhi" -c user.email="slodhi@enphaseenergy.com" commit -m "$(cat <<'EOF'
<message>

Generated with [Devin](https://devin.ai)

Co-Authored-By: Devin <158243242+devin-ai-integration[bot]@users.noreply.github.com>
EOF
)"
```

6. Push with `git push origin HEAD` (upstream is already set to `origin/main`).
7. If a pre-commit hook modifies files and the commit fails, stage the modified files and retry once.

Never: force-push, amend published commits, rewrite history, delete branches, bypass hooks (`--no-verify`), or update `git config`. Never push unless the user asked for it.
