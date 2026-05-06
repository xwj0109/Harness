# AGENTS.md

This repository implements a local-first agent harness.

Hard rules:
- Do not use OpenAI API or OPENAI_API_KEY.
- Do not add paid API fallback.
- Do not add hosted fallback.
- Do not read or expose secrets.
- Do not modify `.harness/`, `.git/`, `.env*`, `*.pem`, `*.key`, `*.sqlite`, or `secrets/`.
- Keep changes small, typed, and tested.
- Preserve local/private data-boundary safeguards.
- Codex is a supervised external agent backend, not a raw model provider.
- Existing tests must pass.

Planning workflow:
- Use `docs/plans/` for roadmap snapshots, implementation plans, and next-step planning artifacts.
- Treat planning files as repo-tracked documentation only; they do not authorize broad implementation work by themselves.
- Never use `.harness/`, `.git/`, `.env*`, secret-like files, SQLite files, or `secrets/` as planning or edit targets.
