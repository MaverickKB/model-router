#!/bin/sh
# Generic maintenance-gate toggle: creates/removes a flag file read by origin_policy.
# usage: origin-gate.sh [FLAG_FILE] on|off|status   (FLAG_FILE defaults to $MODEL_ROUTER_MAINTENANCE_FLAG)
set -eu
if [ $# -ge 2 ]; then FLAG=$1; shift; else FLAG=${MODEL_ROUTER_MAINTENANCE_FLAG:?give a flag file or set MODEL_ROUTER_MAINTENANCE_FLAG}; fi
case "${1:-status}" in
  on) : > "$FLAG"; echo "gate ON  ($FLAG)";;
  off) rm -f "$FLAG"; echo "gate OFF ($FLAG)";;
  status) if [ -e "$FLAG" ]; then echo "gate ON  ($FLAG)"; else echo "gate OFF ($FLAG)"; fi;;
  *) echo "usage: $0 [FLAG_FILE] on|off|status" >&2; exit 2;;
esac
