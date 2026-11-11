"""A custom Python app with a progress bar and a log."""
from deckfw import app, Form, Text, Choice, Toggle, Number


def setup():
    return Form([
        Text("message", "Message", default="Hello from Custom",
             help="A opens the keyboard. B discards text edits."),
        Choice("mode", "Mode", choices=[("Quick", "quick"), ("Relaxed", "relaxed")],
               default="quick", help="Left/Right chooses the pace."),
        Number("steps", "Steps", default=10, minimum=1, maximum=20),
        Toggle("verbose", "Log each step", default=True),
    ], submit_label="Start demo")


@app(name="Custom Python demo", icon="icons/python.png", setup=setup)
def custom_demo(ctx):
    ctx.log(ctx.params["message"])
    steps = ctx.params["steps"]
    for step in range(1, steps + 1):
        ctx.progress(step / steps, f"Step {step} of {steps}")
        if ctx.params["verbose"]:
            ctx.log(f"Step {step} complete")
        ctx.sleep(0.2 if ctx.params["mode"] == "quick" else 0.6)
    ctx.log("Custom demo finished!", color="ok")
