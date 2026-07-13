---
name: opc-retrospective
description: Convert completed or failed OPC work into concise evidence-backed organizational experience candidates without polluting durable memory. Use after manager handoff, after a meaningful failure and recovery, when a problem may recur, or when the user asks the team to remember what it learned.
---

# OPC Retrospective

Create candidates, not automatic policy changes.

## Workflow

1. Resolve the skill directory as the directory containing this `SKILL.md`, then resolve the plugin root as `<skill-dir>/../..`; read `references/candidate-policy.md` and `references/python-runtime.md`.
2. Use base Python for the initial memory `status`, select `<memory-python>`, and run `<memory-python> "<plugin-root>/scripts/opc_memory.py" doctor`. Stop if canonical File/Git knowledge is not initialized or valid.
3. Inspect the current run record, project diff, acceptance report, failure evidence, and final outcome.
4. Identify only lessons reusable beyond the exact incident.
5. State the trigger, observation, causal inference, reusable action, scope, owner, confidence, evidence, and proposed validation.
6. Read `project_id` from `.opc/project.json`. Search pending and approved records with `<memory-python> "<plugin-root>/scripts/opc_memory.py" query <terms> --project-id <current-project-id> --include-unapproved`. If there is no project context, omit `--project-id` and search global records only. Resolve every result to its canonical record before checking duplicates, conflicts, or supersession.
7. Choose scope before creating a candidate. For `scope=project`, require `.opc/project.json` and run `<memory-python> "<plugin-root>/scripts/opc_memory.py" add-candidate --type <type> --summary <summary> --content <lesson> --scope project --project-id <current-project-id> --owner <owner> --confidence <0..1> --source <run-id> --evidence artifact=<relative-ref>`. For `scope=global`, use the same required fields with `--scope global` and omit `--project-id` even when a project is open. Reject other scopes unless their dedicated identity/context contract exists. Never derive project identity from or embed machine-specific absolute paths.
8. Report candidates separately from approved knowledge. Candidate creation itself does not authorize automatic staging or a Git commit. Do not edit `AGENTS.md`, Skills, role templates, or approved experiences in this workflow.

Do not store raw chat, hook payloads, session identifiers, secrets, personal inferences, or lengthy traces. If evidence does not support causality or reuse, record no candidate.
