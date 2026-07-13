# OPC Memory Python Resolution

Resolve one interpreter before any workflow uses `opc_memory.py`:

1. Set `<base-python>` to a working Python 3 command: `python` on Windows or `python3` on Unix. Do not install packages into it.
2. Run `<base-python> "<plugin-root>/scripts/opc_memory.py" [global-options] status`. This initial call exists only to obtain the resolved `data_root`; global options such as `--knowledge-root` and `--data-root` must appear before `status`.
3. Derive the isolated interpreter for the current platform:
   - Windows: `<data_root>/venv/Scripts/python.exe`
   - Unix: `<data_root>/venv/bin/python`
4. If that file exists, set `<memory-python>` to it. Every later `opc_memory.py` command in the workflow must use `<memory-python>`.
5. If it does not exist, set `<memory-python>` to `<base-python>`. Stay in complete File/Git mode unless the user explicitly approves the isolated Mem0 setup flow.
6. Quote all resolved paths. In PowerShell invoke a resolved executable path with `& "<path>"`; on Unix invoke `"<path>"`. Command notation such as `<memory-python>` means to use the platform-appropriate invocation.
7. Never choose a virtual environment under the plugin root, plugin cache, project repository, or canonical knowledge root.
8. If the isolated interpreter exists but cannot run the CLI, report a damaged private environment and stop memory mutations. Do not silently fall back to global Python or global `pip`.

Use `<memory-python>` for `opc_knowledge.py` too after resolution when both CLIs participate in one workflow. This keeps the plugin on one Python runtime without making Mem0 mandatory.
