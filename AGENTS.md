# Repository instructions

This is a public Codex plugin marketplace repository. Keep runtime state and organizational knowledge outside this checkout.

## Boundaries

- Treat `plugins/codex-opc-team/assets/knowledge-template` as synthetic bootstrap data only.
- Never commit a real manager profile, approved private experience, raw conversation, hook payload, credential, user-home path, session identifier, or project runtime log.
- Keep File/Git knowledge authoritative. Mem0 is optional, lazy-loaded, and rebuildable.
- Do not make installers edit global Codex roles or feature flags silently.
- Do not accept implementer self-report as independent QA evidence.

## Validation

Run these commands before committing:

```text
python scripts/validate_repo.py
python -m unittest discover -s tests -p "test_*.py" -v
python scripts/privacy_scan.py
```

When the Codex system skills are available, also run the official plugin validator and the Skill quick validator described in `CONTRIBUTING.md`.

## Changes

- Keep Skills concise and move detailed policy into one-level `references/` files.
- Add tests for safety, fallback, migration, or memory-policy behavior changes.
- Update the relevant ADR when changing an accepted architecture boundary.
