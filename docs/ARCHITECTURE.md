# Architecture and code map

See [contributor instructions](../INSTRUCTIONS.md) for change rules and
[custom scripts](CUSTOM_SCRIPTS.md) for authoring examples.

## Modules

| File | Responsibility |
|---|---|
| `deck.py` | Set import path, determine deck root, invoke main |
| `deckfw/main.py` | Initialize pygame, build the root menu, route events, draw status/body/footer |
| `deckfw/config.py` | Defaults and user/repository JSON overrides |
| `deckfw/loader.py` | Discover entries, distinguish framework Python from scripts, read metadata/sidecars |
| `deckfw/context.py` | `AppSpec`, registry, `@app`, app context, cancellation, script output protocol |
| `deckfw/menu.py` | Menu item types, menu/confirmation screens, default tree, Custom placement |
| `deckfw/screens.py` | Navigation stack and worker-backed app screen |
| `deckfw/parameters.py` | Field/form declarations, validation, sidecar parsing, environment serialization |
| `deckfw/forms.py` | Setup rows and transactional text editor |
| `deckfw/inputmap.py` | Keyboard/controller events, action mapping, directional repeat, scoped text input |
| `deckfw/runner.py` | Background pipe reader, output queue, process-group cancellation |
| `deckfw/widgets.py` | Layout containers, labels, graphs, stats, progress, logs, selectable lists |
| `deckfw/theme.py` | Resolution-derived fonts, colors, text/icon caches, root-relative image paths |
| `deckfw/statusbar.py` | System status display |
| `deckfw/__init__.py` | Public app-facing exports |
| `menu.py` / `menu.example.py` | User-editable explicit menu declaration / example |
| `apps/`, `custom/`, `icons/` | Native entries, user entries, replaceable PNG assets |

## Startup and discovery

```text
deck.py
  main(root)
    config.load(root)
    pygame + Theme + StatusBar + InputMapper + Navigator
    discover(root, cfg)
      scan apps_dir -> source="native"
      scan custom_dir -> source="custom"
    build_root_menu(root, specs)
      menu.py build(specs), or default_menu(specs)
    push MenuScreen
    input/update/draw loop
```

Config precedence is defaults, `~/.config/cyberdeck/config.json`, then
`config.json` beside `deck.py`. Nested dictionaries are merged. Entry directories
default to `apps` and `custom`; the keys are `apps_dir` and `custom_dir`.

Discovery only scans direct children. For Python, an AST check recognizes
top-level `@app` decorators imported from `deckfw`, including import aliases.
Recognized framework modules are imported and register `AppSpec` objects through
the decorator. Keep module-level code free of long-running or destructive work.
Other supported files are wrapped as standalone subprocess entries without
executing them during discovery.

Scripts get optional `deck`, `group` and `icon` headers from the first 20 lines.
Their full filename plus `.params.json` identifies a parameter sidecar. A bad
entry becomes an error app rather than stopping discovery. Results are sorted by
group and name. The registry is process-global and discovery appends to it:
tests must isolate it, and callers should not assume repeated discovery reloads
cleanly. `order` is stored metadata but is not currently used by this sorting.

## Menu ownership

`default_menu` partitions entries by `source`. Native `group="System"` entries
go directly into System; remaining native entries populate Apps. Apps uses a
single list when at most one group exists, otherwise one submenu per group.
Native group strings are not currently split into nested paths.

Custom defaults to a flat list regardless of group headers. The supplied
`menu.py` explicitly places the network script in Custom > Network. The
`custom_menu(specs, items)` helper recursively collects placed `Launch.spec`
objects and appends unplaced custom specs at its root.

`Launch` opens an app or its setup. `Run` wraps a command in an app output screen.
`Command` tries available command alternatives asynchronously and reports a
toast. `Action` invokes a menu-screen callback. Command/Action can ask for
confirmation. `menu.py` exceptions fall back to the default menu with an error
entry.

## Parameters and launch lifecycle

```text
MenuScreen -> Launch
  no setup -> AppScreen -> worker run(ctx)
  setup    -> SetupScreen
                TextEditor -> accept/cancel back to setup
                validated Run -> AppScreen -> worker run(ctx)
                B -> originating menu
```

The setup screen calls a fresh `spec.setup()`, which must return a `Form`.
Fields validate their defaults when the form is constructed. Run validates
selections again and supplies a separate parameter snapshot to `Context`.
Python apps read typed values from `ctx.params`.

The setup screen remains below its app on the stack, preserving edited values
for reruns. Leaving setup and launching again recreates defaults. Text editing
uses a working copy; cancelling never commits it. Dynamic/dependent choices and
selection persistence are not implemented.

Standalone sidecars use the same fields. The loader validates sidecars during
discovery and loads them again when setup opens. The script wrapper converts
validated values to `DECK_PARAM_<UPPERCASE_KEY>` and calls `ctx.run` with a
per-process environment and the script's directory as cwd.

## Threading and subprocesses

The main thread owns screens, input and rendering. `AppScreen` starts one daemon
worker for the selected function. Workers update widgets or call `Context`
helpers. `Runner` starts a process in its own session/process group, merges
stderr into stdout, disables stdin and reads bytes on a background thread.
`poll()` drains the queue and parses output; `Context.run()` polls it from the
app worker, whereas menu `Command` runners are polled in menu updates.

`Context.cancel()` sets an event and stops tracked runners. Process stop sends
SIGTERM to the group, with delayed SIGKILL if still alive. Python cancellation
is cooperative; `Cancelled` unwinds the function at cancellation checkpoints.
Unexpected app exceptions replace output with a traceback. Nonzero standalone
exits set error status.

`AppSpec.shutdown` is an optional callback invoked once by `AppScreen._work`
in its `finally` block. During cleanup, context cancellation checks do not
interrupt restoration commands; new cancel requests do not stop cleanup runners.
Standalone `shutdown` headers are wrapped in the same callback mechanism.
Cancellation waits for the main subprocess to stop before unwinding into cleanup;
SIGTERM grace is 10 seconds. GUI close joins each app worker for up to 15 seconds
and warns on timeout.

The native port scanner uses one worker and a queue of main-thread table actions.
It publishes immutable device/result snapshots; only the worker executes
arp-scan/Nmap through `ctx.run`. Nmap XML is held in a temporary directory cleaned
on every exit path, independent of the bounded output log. Targets are validated
discovered IPv4 hosts on the selected interface's subnet.

Focusable widgets can explicitly opt into local B handling via `handles_back`
and `hints`; `AppScreen` offers B to those widgets before default cancellation.
The port scanner enables this only on completed results, to return to devices.
Shutdown protection takes precedence; existing widgets retain their B behavior.

## Rendering and input

The main loop reserves space for the status bar and bottom hints before drawing
the current screen. Test layouts with that reduced body, not the full window.
Text/icon caches belong to `Theme`. Menu `ListView` supports optional hint/icon
columns while preserving old tuple forms.

`InputMapper` emits named actions and repeats held directions. In text mode it
routes physical keyboard events to `TextEditor.text_event` instead of handling
Q, Space or Backspace as global shortcuts. Controller actions still work.
`main.py` processes events individually because an editor may open/close in the
middle of a frame.
