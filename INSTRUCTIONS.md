# Contributor and agent instructions

Start with [AGENTS.md](AGENTS.md) for the documentation map. These instructions
describe repository conventions and the compatibility boundaries to preserve.

## Working approach

1. Read the relevant implementation and tests before editing. The source is the
   authority when an old comment or example disagrees with behavior.
2. Inspect `git status --short`. Preserve unrelated staged and unstaged changes.
   Do not commit, push or change branches unless the user requests it.
3. Make the smallest complete change. Reuse existing widgets, runners and input
   mapping rather than adding a second execution or navigation path.
4. Update directly related documentation and examples when changing public APIs.
5. Run checks appropriate to the change; see [Testing](docs/TESTING.md).

## Architecture boundaries

- `deck.py` supplies the repository root to `deckfw.main.main`.
- Keep pygame rendering and event handling on the main thread.
- Framework app functions run in an `AppScreen` worker. Use `Context` and widgets
  for output; do not call pygame drawing or event functions from that worker.
- Use `ctx.run([...])` for child processes. It integrates output and cancellation.
  Do not use shell strings, `shell=True`, `eval`, or uncancellable blocking
  subprocess helpers as a shortcut.
- Keep setup functions and validators cheap, deterministic and free of user work.
  Return a `Form`; let the framework collect and validate inputs.
- Expose app-facing APIs through `deckfw/__init__.py`. Preserve existing
  `run(ctx)` functions and optional/defaulted constructor arguments.
- Follow the existing Python style: four spaces, readable names, minimal comments,
  no unnecessary dependencies or speculative abstractions.

## Behavior to preserve

| Surface | Required behavior |
|---|---|
| Main menu | Apps, Custom, System, Exit; Controls and native System monitor stay inside System |
| Discovery | Both folders accept framework Python apps and standalone scripts; skip hidden/underscore files and subdirectories |
| Custom placement | Undeclared scripts appear at the Custom root; explicit `Launch(spec)` placement suppresses the extra root entry |
| Groups | Custom group metadata does not create its tree; native System-group entries go to System |
| No setup | Launch immediately with an empty `ctx.params` |
| Setup | No worker before validated Run; returning from the app returns to setup; reopening from the launcher resets defaults |
| Text input | Q/Space type normally while editing; Escape cancels the edit; controller quit chord still works |
| Icons | Paths resolve from the deck root; cached proportional scaling; bad icons warn without breaking launch |
| Errors | Load/setup failures and failed scripts must remain visible, not look like successful execution |

`custom_menu` needs concrete `Menu` objects, not callable submenu providers, to
find explicitly placed scripts. The general `Submenu` API supports providers,
but do not introduce them inside the Custom declaration.

## Cancellation and safety

Use `ctx.sleep`, `ctx.check` or `ctx.loop` in long-running Python work. Use
`try/finally` for cleanup. The second B press closes the screen but cannot kill a
Python worker thread; code must cooperate with cancellation.

Optional `shutdown(ctx)` hooks run after execution unwinds, once per run.
Use `@app(shutdown=...)` or standalone `# shutdown: _cleanup.sh` metadata.
Cancellation is suppressed for hook context operations so restoration can run.
Do not run cleanup concurrently with active work or from a screen-close callback.
Keep restoration bounded and idempotent; failed cleanup must remain visible.

Standalone processes have no interactive stdin. Quote all shell expansions,
validate inputs, and keep parameter values as data. `DECK_PARAM_*` overrides are
per child process, not mutations of `os.environ`. A form is not a permission
boundary: imported framework apps and executed scripts are trusted local code.

Do not invoke reboot, shutdown, privileged controls or real network operations
just to test a change. Use mocks or temporary stub executables as existing tests
do. Do not write bytecode, screenshots or temporary test files into tracked
source paths deliberately; use temporary directories or session artifacts.

## Change checklist

- New app/script: read [Custom scripts](docs/CUSTOM_SCRIPTS.md).
- New field type: wire Python declarations, JSON parsing, validation, adjustment,
  display, exports, documentation and tests.
- Input change: exercise both controller actions and physical keyboard text
  routing, including transitions within one event frame.
- Layout change: exercise 320x240 and 640x480 with the real reduced body area.
  `Label` defaults to top alignment; form rows opt into `valign="center"`.
- Discovery/menu change: verify source tagging, default root entries, nested
  placement, missing-script behavior and no duplicate root shortcut.
