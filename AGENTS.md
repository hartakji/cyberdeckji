# Agent entry point

This repository implements a pygame launcher and app framework for button-only
Linux handhelds. Read [INSTRUCTIONS.md](INSTRUCTIONS.md) before changing code.

## Reading map

| Task | Read |
|---|---|
| Run the GUI or learn the public API | [README.md](README.md) |
| Modify the framework safely | [INSTRUCTIONS.md](INSTRUCTIONS.md) |
| Understand modules, lifecycle and data flow | [Architecture](docs/ARCHITECTURE.md) |
| Create a custom Python/Bash script or parameter form | [Custom scripts](docs/CUSTOM_SCRIPTS.md) |
| Choose checks and troubleshoot failures | [Testing](docs/TESTING.md) |

Keep the current main menu order **Apps, Custom, System, Exit**. Native entries
come from `apps/`; user entries come from `custom/`. Unplaced custom entries
belong at the Custom root, not in automatic group submenus.

For a new custom tool, prefer adding its file and optional parameter sidecar
without changing the framework. Edit `menu.py` only for explicit placement.
Use existing public exports from `deckfw`; do not draw from an app worker or
execute user work at module import time.
