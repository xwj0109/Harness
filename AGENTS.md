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
