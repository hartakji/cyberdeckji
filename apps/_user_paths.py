"""User media locations, including launchers run through sudo."""

import os
from pathlib import Path
import pwd


def invoking_user():
    if os.geteuid() == 0 and "SUDO_UID" in os.environ:
        return pwd.getpwuid(int(os.environ["SUDO_UID"]))
    return None


def directory(kind):
    user = invoking_user()
    home = Path(user.pw_dir) if user is not None else Path.home()
    return home / kind
