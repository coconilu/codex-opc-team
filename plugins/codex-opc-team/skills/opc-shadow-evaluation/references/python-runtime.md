# Python runtime

1. Use a working Python 3 command as `<base-python>`: `python` on Windows or `python3` on Unix.
2. Run `<base-python> "<plugin-root>/scripts/opc_memory.py" status` only to resolve `data_root`.
3. Prefer the existing isolated interpreter when present: `<data_root>/venv/Scripts/python.exe` on Windows or `<data_root>/venv/bin/python` on Unix. Otherwise use `<base-python>` in File/Git-only mode.
4. Use the selected `<memory-python>` for `opc_memory.py` and `opc_shadow.py`. Quote paths; PowerShell requires `&` before a quoted executable path.
5. Never install a dependency, enable Mem0, or select an environment inside the plugin, project, or canonical knowledge root for Shadow Evaluation.
