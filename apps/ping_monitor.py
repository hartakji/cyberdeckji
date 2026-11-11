"""Run an external command and turn its output into a live graph."""
import re

from deckfw import app, Graph, Form, Text, Number

RTT = re.compile(r"time[=<]([\d.]+)")


def setup():
    return Form([
        Text("host", "IP / Host", default="1.1.1.1", format="host",
             help="IPv4, IPv6 or hostname; no address ranges."),
        Number("count", "Probes", default=20, minimum=1, maximum=100),
    ], submit_label="Ping")


@app(name="Ping monitor", group="Network", setup=setup)
def ping_monitor(ctx):
    host, count = ctx.params["host"], ctx.params["count"]
    graph = ctx.add(Graph(title=f"round-trip to {host}", unit=" ms", fmt="{:.1f}"))
    seen = []

    def on_line(line):                     # called for every output line
        m = RTT.search(line)
        if m:
            seen.append(float(m.group(1)))
            graph.push(seen[-1])
            ctx.progress(len(seen) / count, f"{len(seen)} / {count} replies")

    # -i 0.5 keeps us under the root-only limit for ping intervals
    result = ctx.run(["ping", "-c", str(count), "-i", "0.5", host], on_line=on_line)
    if seen:
        ctx.log(f"average {sum(seen) / len(seen):.1f} ms over {len(seen)} replies", color="ok")
    elif result.returncode != 0:
        ctx.log("no replies: is the network up?", color="crit")
