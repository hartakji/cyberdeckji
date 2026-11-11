#!/bin/sh
# deck: Network quick check
# group: Network
# icon: icons/network.png
#
# Custom scripts can drive the UI. Lines starting with @ become
# widgets; everything else goes to the log:
#   @progress 40 Scanning     progress bar (0-100)
#   @graph name 12.5          graph value
#   @stat label=value         big number tile
#   @status text              note in the header

set -eu
: "${DECK_PARAM_HOST:?Launch from the deck setup screen}"
: "${DECK_PARAM_ROUNDS:?Launch from the deck setup screen}"
total="$DECK_PARAM_ROUNDS"
n=0
ok=0
while [ "$n" -lt "$total" ]; do
  h="$DECK_PARAM_HOST"
  n=$((n + 1))
  rtt=$(ping -c1 -W1 "$h" 2>/dev/null | sed -n 's/.*time=\([0-9.]*\).*/\1/p')
  if [ -n "$rtt" ]; then
    ok=$((ok + 1))
    echo "@graph $h $rtt"
  else
    echo "$h: timeout"
  fi
  echo "@progress $((n * 100 / total)) Probing $h"
  echo "@stat replies=$ok/$n"
  sleep 0.5
done
echo "finished"
