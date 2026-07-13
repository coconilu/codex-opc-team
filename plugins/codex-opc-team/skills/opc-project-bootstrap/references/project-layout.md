# Project-local OPC Layout

```text
<project>/
├── AGENTS.md             # Existing guidance; change only with approval
└── .opc/
    ├── project.json      # Portable project ID and name; generated and versionable
    ├── project.md        # Stable brief and boundaries; versionable
    ├── acceptance.md     # Observable definition of done; versionable
    ├── qa/               # Acceptance artifacts when useful
    ├── run.json          # Active runtime marker; ignored
    ├── events.jsonl      # Hook fallback when PLUGIN_DATA is unavailable; ignored
    ├── events.jsonl.*    # Rotated Hook fallback files; ignored
    └── .opc-hook.lock    # Cross-process fallback writer lock; ignored
```

Project files answer “what is true here.” The private File/Git knowledge root answers “what has the team learned elsewhere.” `PLUGIN_DATA` holds provider configuration and rebuildable indexes. Do not copy global knowledge or plugin runtime data into every project. Ignore runtime files with the four exact entries in `../assets/gitignore.snippet`; never ignore all of `.opc` because project contracts and QA evidence remain versionable. Bootstrap creates contracts and metadata only; `opc-manager` owns run creation.
