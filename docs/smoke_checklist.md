# Smoke Checklist

Use this checklist after changes to the isolated Codex edit route, the Docker test runner, or the simple edit loop. Commands assume the repository root is the current directory.

## Inspect Repository State

```bash
git status --short
git log --oneline --decorate -5
```

## Run Local Unit Suite

```bash
pytest -q
```

## Build Local Docker Test Image

```bash
docker build -f Dockerfile.harness-test -t harness-test:local .
```

## Run Direct Docker Tests

```bash
harness tests run --project . -- python -m pytest -q
```

The command requires approval. Approve only after confirming the prompt shows a sanitized temporary workspace mounted to `/workspace`, not the active project root.

## Verify Latest Docker Run Artifacts

```bash
LATEST=$(ls -td .harness/runs/run_* | head -1)
cat "$LATEST/test_result.json"
cat "$LATEST/final_report.md"
```

## Optional: Codex Isolated Edit Smoke

The following commands create commits. Run them only in a disposable branch or when you intentionally want smoke-test commits.

Create a scratch file and commit it:

```bash
cat > scratch_codex_edit.py <<'EOF'
def greet():
    return "hello"
EOF
git add scratch_codex_edit.py
git commit -m "Add scratch file for Codex edit smoke test"
```

Create or refresh the required hosted data-boundary approval profile:

```bash
harness approvals add --backend codex_cli --data-boundary hosted_provider --project . --task-types codex_code_edit --duration-days 1
```

Run Codex in an isolated workspace:

```bash
harness run "Modify only scratch_codex_edit.py. Add a docstring inside greet(). Do not create, delete, or modify any other files." --project . --task-type codex_code_edit --keep-isolation
```

When prompted, use `view full diff`, `deny all changes`, or `approve all changes` according to the smoke objective. Denial should leave the active file unchanged.

Verify the active file after denial or apply-back:

```bash
cat scratch_codex_edit.py
git status --short
```

Optional cleanup. These commands also create a commit:

```bash
git rm scratch_codex_edit.py
git commit -m "Remove scratch Codex smoke file"
```

## Expected Safety Properties

- `codex_code_edit` edits only an isolated workspace until explicit apply-back approval.
- Denying apply-back leaves the active project unchanged.
- Direct Docker tests mount only a sanitized temporary workspace to `/workspace`.
- Docker test network is disabled by default.
- Docker test denial does not call Docker.
- `run_tests` is model-visible only for `simple_code_edit`.
- No command commits or pushes unless an optional smoke step explicitly runs `git commit`.
