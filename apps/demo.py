"""The smallest useful app: a progress bar and a log."""
from deckfw import app


@app(name="Hello, deck", group="Demo")
def hello(ctx):
    ctx.log("Starting a fake job...")
    for i in range(1, 11):
        ctx.progress(i / 10, f"Step {i} of 10")
        ctx.log(f"step {i} done")
        ctx.sleep(0.4)                     # wakes instantly if you press B
    ctx.log("All done.", color="ok")
