"""Shared USB gadget service lifecycle."""

def command(ctx, *args):
    result = ctx.run(["timeout", "--kill-after=2s", "10s", "systemctl",
                      "--no-pager", *args])
    if result.returncode:
        detail = f": {result.lines[-1]}" if result.lines else ""
        raise RuntimeError(f"systemctl {' '.join(args)} failed ({result.returncode}){detail}")
    return result


def check_inactive(ctx, service):
    result = command(ctx, "show", service, "--property=LoadState",
                     "--property=ActiveState")
    properties = dict(line.split("=", 1) for line in result.lines if "=" in line)
    if properties.get("LoadState") != "loaded":
        raise RuntimeError(f"The {service} service is not loaded.")
    if properties.get("ActiveState") != "inactive":
        raise RuntimeError(f"The {service} service is not inactive; leaving it untouched.")


def start(ctx, service):
    ctx.check()
    ctx._usb_otg_service = service
    command(ctx, "start", service)
    ctx.log(f"{service} is active. Press B to stop it.", color="ok")
    while True:
        ctx.sleep(1)


def shutdown(ctx):
    service = getattr(ctx, "_usb_otg_service", None)
    if service:
        ctx.log(f"Stopping {service}.")
        command(ctx, "stop", service)
