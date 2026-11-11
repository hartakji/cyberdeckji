"""Settings. Override any of these in ~/.config/cyberdeck/config.json or
./config.json (next to deck.py). Nested dicts (like "buttons") are merged."""

import json
import os

DEFAULTS = {
    "fullscreen": True,
    "size": [640, 480],            # window size when running with --windowed
    "fps": 30,
    "apps_dir": "apps",
    "custom_dir": "custom",
    "disk_path": "/",              # which filesystem the DSK gauge watches
    "stats_interval": 1.0,         # seconds between status-bar refreshes
    "repeat_delay": 0.35,          # hold a direction: delay before repeating
    "repeat_rate": 0.08,           # ...then one repeat every N seconds
    # Joystick button numbers. Find yours with `jstest /dev/input/js0`.
    # You can also map "up"/"down"/"left"/"right" here if your D-pad
    # shows up as buttons instead of a hat.
    "buttons": {"a": 0, "b": 1, "x": 2, "y": 3, "select": 6, "start": 7},
}


def load(root):
    cfg = json.loads(json.dumps(DEFAULTS))  # deep copy
    paths = [
        os.path.expanduser("~/.config/cyberdeck/config.json"),
        os.path.join(root, "config.json"),
    ]
    for path in paths:
        try:
            with open(path) as f:
                user = json.load(f)
        except (OSError, ValueError):
            continue
        for key, value in user.items():
            if isinstance(value, dict) and isinstance(cfg.get(key), dict):
                cfg[key].update(value)
            else:
                cfg[key] = value
    return cfg
