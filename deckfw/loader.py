"""Finds native apps in apps/ and custom tools in custom/.

A broken app never takes the deck down: it shows up in the launcher as
"! name" and opens a screen with the error."""

import ast
import importlib.util
import os
import re
import shlex
import sys
import tokenize
import traceback

from .context import AppSpec, register, registered
from .parameters import load_form, parameter_env

_HEADER = re.compile(r"^\s*(?:#|//)\s*(deck|group|icon|shutdown):\s*(.+?)\s*$")


def discover(root, cfg):
    apps_dir = os.path.join(root, cfg["apps_dir"])
    custom_dir = os.path.join(root, cfg["custom_dir"])
    _load_apps(apps_dir)
    if os.path.realpath(custom_dir) != os.path.realpath(apps_dir):
        _load_entries(custom_dir, framework_apps=True, source="custom")
    return sorted(registered(), key=lambda s: (s.group.lower(), s.name.lower()))


def _broken(name, tb):
    def run(ctx):
        ctx.log("Could not load this app:", color="crit")
        ctx.log(tb)
        ctx.status = "error"
    register(AppSpec("! " + name, run, "errors"))


def _load_apps(directory):
    _load_entries(directory, framework_apps=True)


def _is_framework_app(path):
    """Inspect decorators without executing standalone Python scripts."""
    with tokenize.open(path) as f:
        tree = ast.parse(f.read(), filename=path)
    decorators = set()
    modules = set()
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.module == "deckfw":
            decorators.update(alias.asname or alias.name for alias in node.names
                              if alias.name == "app")
        elif isinstance(node, ast.Import):
            modules.update(alias.asname or alias.name for alias in node.names
                           if alias.name == "deckfw")
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        for decorator in node.decorator_list:
            target = decorator.func if isinstance(decorator, ast.Call) else decorator
            if isinstance(target, ast.Name) and target.id in decorators:
                return True
            if (isinstance(target, ast.Attribute) and target.attr == "app"
                    and isinstance(target.value, ast.Name) and target.value.id in modules):
                return True
    return False


def _load_entries(directory, framework_apps=False, source="native"):
    if not os.path.isdir(directory):
        return
    for fname in sorted(os.listdir(directory)):
        if fname.startswith((".", "_")):
            continue
        path = os.path.join(directory, fname)
        if not os.path.isfile(path):
            continue
        previous = len(registered())
        try:
            if framework_apps and fname.endswith(".py") and _is_framework_app(path):
                spec = importlib.util.spec_from_file_location("deck_app_" + fname[:-3], path)
                module = importlib.util.module_from_spec(spec)
                sys.modules[spec.name] = module
                spec.loader.exec_module(module)
                continue
            cmd = _interpreter(path)
            if cmd is None:
                continue
            name, group, icon = _metadata(path)
            shutdown_path = _shutdown_path(path)
            shutdown = None
            if shutdown_path:
                shutdown_cmd = _interpreter(shutdown_path)
                if shutdown_cmd is None:
                    raise ValueError("Shutdown script must be Python, Bash or executable")
                shutdown = _script_runner(shutdown_cmd, cleanup=True)
            name = name or os.path.splitext(fname)[0].replace("_", " ").replace("-", " ").title()
            sidecar = path + ".params.json"
            setup = None
            if os.path.isfile(sidecar):
                load_form(sidecar)
                setup = lambda sidecar=sidecar: load_form(sidecar)
            register(AppSpec(name, _script_runner(cmd), group or "",
                             icon=icon or "icons/script.png", setup=setup, shutdown=shutdown))
        except Exception:
            _broken(fname, traceback.format_exc())
        finally:
            for entry in registered()[previous:]:
                entry.source = source


def _interpreter(path):
    path = os.path.abspath(path)
    if not path.endswith((".sh", ".bash", ".py")) and not os.access(path, os.X_OK):
        return None
    with open(path, "rb") as f:
        first = f.readline(4096)
    if first.startswith(b"#!"):
        interpreter = shlex.split(first[2:].decode("utf-8").strip())
        if not interpreter:
            raise ValueError(f"Empty shebang in {path}")
        return interpreter + [path]
    if path.endswith((".sh", ".bash")):
        return ["bash", path]
    if path.endswith(".py"):
        return [sys.executable, path]
    if os.access(path, os.X_OK):
        return [path]
    return None


def _metadata(path):
    metadata = {}
    with open(path, errors="replace") as f:
        for _, line in zip(range(20), f):
            m = _HEADER.match(line)
            if m:
                metadata[m.group(1)] = m.group(2)
    return metadata.get("deck"), metadata.get("group"), metadata.get("icon")


def _shutdown_path(path):
    with open(path, errors="replace") as stream:
        for _, line in zip(range(20), stream):
            match = _HEADER.match(line)
            if match and match.group(1) == "shutdown":
                target = os.path.abspath(os.path.join(os.path.dirname(path), match.group(2)))
                if not os.path.isfile(target):
                    raise ValueError(f"Shutdown script not found: {target}")
                return target
    return None


def _script_runner(cmd, cleanup=False):
    def run(ctx):
        result = ctx.run(cmd, protocol=True, cwd=os.path.dirname(cmd[-1]),
                         env=parameter_env(ctx.params))
        if result.returncode not in (0, None):
            if cleanup:
                raise RuntimeError(f"Shutdown command exited with {result.returncode}")
            ctx.log(f"[exit code {result.returncode}]", color="crit")
            ctx.status = "error"
    return run
