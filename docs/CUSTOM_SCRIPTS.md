# Creating custom scripts and apps

Put user tools directly in `custom/`. Both Python framework apps and standalone
Python/Bash scripts work there. Native/bundled tools use the same formats in
`apps/`. Restart the GUI after adding or editing an entry.

Files starting with `.` or `_`, and nested directories, are not discovered.
Choose unique display names if `menu.py` will select entries by name.

## Choose an execution model

| Need | Use | Parameters |
|---|---|---|
| Native widgets, graphs and cooperative Python work | Python `@app` function | `setup()` returning `Form`, then `ctx.params` |
| Existing command-line Python program | Standalone `.py` | Full-filename `.params.json` sidecar, then environment |
| Bash tool | `.sh` / `.bash` | Same JSON sidecar and environment |

For framework Python apps, keep `from deckfw import app` (or a supported import
alias) and the decorated function at module scope so discovery recognizes it.
Do not call the function at import time. A standalone script must not rely on
importing a setup function for discovery.

## Framework Python example

Save as `custom/greeting.py`:

```python
from deckfw import app, Form, Text, Number, Toggle


def setup():
    return Form([
        Text("message", "Message", default="Hello, deck"),
        Number("steps", "Steps", default=5, minimum=1, maximum=20),
        Toggle("verbose", "Log steps", default=True),
    ], submit_label="Start")


@app(name="Greeting", icon="icons/python.png", setup=setup)
def greeting(ctx):
    ctx.log(ctx.params["message"])
    steps = ctx.params["steps"]
    for step in range(1, steps + 1):
        ctx.check()
        ctx.progress(step / steps, f"Step {step}/{steps}")
        if ctx.params["verbose"]:
            ctx.log(f"Completed {step}")
        ctx.sleep(0.2)
    ctx.log("Finished", color="ok")
```

No executable bit, shebang or `menu.py` change is needed. It appears at
Custom > Greeting. Without `setup=`, the function launches directly and
`ctx.params` is empty.

Use `ctx.run(["program", "arg", ...], cwd=..., env=...)` for external commands.
It returns `RunResult(returncode, lines)`; inspect the return code and report
failures. Use `try/finally` for resource cleanup and `ctx.sleep` instead of
`time.sleep` so cancellation wakes promptly. See [README](../README.md) for
the full Context/widget API and `custom/demo.py` for all four field types.

## Standalone Python or Bash example

For Bash, save as `custom/greeting.sh`:

```sh
#!/bin/bash
# deck: Greeting script
# icon: icons/script.png
set -eu
: "${DECK_PARAM_MESSAGE:?Launch through the deck setup form}"
printf '%s\n' "$DECK_PARAM_MESSAGE"
echo "@progress 100 Complete"
```

Save its sidecar as **`custom/greeting.sh.params.json`**:

```json
{
  "submit_label": "Greet",
  "fields": [
    {
      "type": "text",
      "key": "message",
      "label": "Message",
      "default": "Hello from a script",
      "max_length": 100
    }
  ]
}
```

For an equivalent standalone Python script, use `custom/greeting.py` and name
its sidecar `custom/greeting.py.params.json` instead:

```python
# deck: Greeting script
# icon: icons/python.png
import os

print(os.environ["DECK_PARAM_MESSAGE"])
print("@progress 100 Complete")
```

Use only one greeting example at a time to avoid conflicting names/files.
Standalone programs run in their own folder, with captured stdout/stderr and no
interactive stdin. They must not call `input()` or wait for terminal prompts.
A nonzero exit is shown as an error. B cancels the process group.

Without a sidecar, scripts execute directly and should not require parameter
environment variables. A malformed sidecar creates a visible load error rather
than silently dropping the form.

### Interpreter and metadata rules

- `.sh` and `.bash` default to Bash; `.py` defaults to the deck's Python.
- A shebang overrides that choice, including `#!/bin/sh` or
  `#!/usr/bin/env python3`. Named script extensions need no executable bit.
- Other files must be executable; use a shebang for other script languages.
- Optional `# deck:`, `# group:` and `# icon:` headers belong in the first
  20 lines. Without `deck`, the filename becomes a title-cased label.
- `group` does not place custom entries in submenus. Only `menu.py` does.
- Icon paths are relative to the deck root, not `custom/`; e.g.
  `icons/python.png`. Replace existing PNGs or supply another path.

### Parameters and validation

| Type | Required declaration | Notes |
|---|---|---|
| `Text` / `text` | key, label; valid default for required fields | `required`, `max_length`, `format`: text/ip/host |
| `Choice` / `choice` | key, label, choices, default | Pairs of display label and scalar value; arrows wrap |
| `Toggle` / `toggle` | key, label | Boolean default, false if omitted |
| `Number` / `number` | key, label, default, minimum, maximum | Positive step, default 1; bounds/default align to the step |

All fields accept `help`; Python fields additionally accept
`validator(value)` returning `None` or an error string. Choice values are
strings, booleans or finite numbers, not objects/lists. JSON field type names
are lowercase; JSON cannot declare Python validators.

Keys must match `[A-Za-z_][A-Za-z0-9_]*` and be unique ignoring case. Python
gets typed values; scripts get strings in `DECK_PARAM_<UPPERCASE_KEY>`:
booleans are `true`/`false`, numbers use locale-independent serialization.
Convert with `int(...)`, `float(...)`, or `value == "true"` in Python.

Python forms may pass `visible_when={"field_key": predicate}` to `Form`.
Each predicate receives the current dictionary of all field values and returns
whether that field should be shown. Hidden values remain in the setup screen
but are not validated or included in submitted parameters. Predicates must be
cheap and deterministic. `Form.changed(values, key)` and
`Form.submitted(values)` may be overridden to reset dependent controls after a
change or a successful submission. These Python-only features are not
available in JSON sidecars.

Host validation accepts a single IP or ASCII hostname, not network ranges,
URLs, or arbitrary shell expressions. Required empty text is an invalid
default; supply a valid default or use `required=False`. Defaults reset on
fresh launch. Fixed choices are supported; dependent/dynamic choices are not.

Never pass parameter values through `eval`, shell interpolation or
`shell=True`. Quote shell variables and use argument lists in Python. Field
validation does not replace checking permissions and command-specific options.

### Script output protocol

| Printed line | GUI result |
|---|---|
| `@progress 40 Scanning` | Progress at 40 percent with label |
| `@progress ? Waiting` | Indeterminate progress |
| `@graph latency 12.5` | Add a sample to a named graph |
| `@stat hosts=12` | Set a stat tile |
| `@status Connecting` | Change header note |
| Any ordinary line | Append to the log |

Framework `ctx.progress` uses **0..1**, unlike script `@progress` which uses
**0..100**. Standalone Python output is unbuffered by the runner. Other tools
may need their own flushing options.

## Explicit menu placement

Unplaced scripts appear at the Custom root automatically. To put Greeting
inside Custom > Examples, adapt `menu.py` like this:

```python
from deckfw import Menu, Submenu, Launch, default_menu, custom_menu


def build(specs):
    defaults = {item.label: item for item in default_menu(specs).items}
    greeting = next((spec for spec in specs
                     if spec.source == "custom" and spec.name == "Greeting"), None)
    declared = []
    if greeting is not None:
        declared = [
            Submenu("Examples", Menu("Examples", [
                Launch(greeting),
            ]), icon="icons/folder.png"),
        ]
    return Menu("Main", [
        defaults["Apps"],
        Submenu("Custom", custom_menu(specs, declared), icon="icons/custom.png"),
        defaults["System"],
        defaults["Exit"],
    ])
```

This is a full illustrative replacement; merge it with existing declarations
if you want to keep their placement. Use concrete nested `Menu` instances.
`custom_menu` omits a root shortcut for scripts already placed anywhere in the
declaration, while keeping unrelated custom entries visible.

## Before handing off a new tool

### Shutdown and state restoration

Use `@app(..., shutdown=shutdown)` with a `shutdown(ctx)` callback when work changes
system state. It runs once after the worker unwinds, on success, error or
cancellation. It may call `ctx.run` even after cancellation; check restoration
return codes and raise on failure. Save original state before changing it and
make cleanup safe to repeat. See [README shutdown hooks](../README.md#shutdown-hooks-and-restoration).

Standalone Python/Bash tools can add `# shutdown: _cleanup.py` or
`# shutdown: _cleanup.sh` in the first 20 lines. Helpers are resolved relative to
the entry file, run only after its process stops and receive the same parameter
environment. Underscore-prefixed helper files are not discovered as menu entries.
Use signal handlers/traps in the main script as well where appropriate; helper
and signal cleanup must be idempotent. Subprocess cancellation allows 10 seconds
before SIGKILL. The GUI waits at most 15 seconds per worker on exit; power loss or
forced termination can still prevent restoration.

Check discovery without import-time work, valid defaults, setup cancel, Run,
expected output, nonzero failures and B cancellation. Confirm it appears only
where intended and fits the handheld screen. Mock external commands rather
than using privileged actions or real network scans for tests.
See [Testing](TESTING.md) for commands and relevant examples.
