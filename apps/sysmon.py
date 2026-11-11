"""Live system monitor: graph + stat tiles, refreshed once a second."""
import os
import time

import psutil

from deckfw import app, Graph, Stat


@app(name="System monitor", group="System")
def sysmon(ctx):
    uptime = Stat("Uptime", "-")
    load = Stat("Load 1m", "-")
    procs = Stat("Processes", "-")
    ctx.row(uptime, load, procs)
    graph = ctx.add(Graph(title="CPU / MEM", unit="%", y_min=0, y_max=100,
                          names=["cpu", "mem"]))

    boot = psutil.boot_time()
    psutil.cpu_percent(None)               # prime the counter
    for _ in ctx.loop(1.0):                # runs until the user presses B
        graph.push(psutil.cpu_percent(None), series=0)
        graph.push(psutil.virtual_memory().percent, series=1)
        secs = int(time.time() - boot)
        uptime.value = f"{secs // 3600}h{secs % 3600 // 60:02d}m"
        load.value = f"{os.getloadavg()[0]:.2f}"
        procs.value = str(len(psutil.pids()))
