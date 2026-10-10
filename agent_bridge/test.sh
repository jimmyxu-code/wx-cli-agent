#!/bin/sh
set -eu
bridge_repo_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$bridge_repo_root"
export PYTHONPATH="$bridge_repo_root/agent_bridge${PYTHONPATH:+:$PYTHONPATH}"
python3 -m unittest discover -s agent_bridge/tests -p 'test_*.py'
python3 -m unittest discover -s agent_bridge/backends/receive -p 'test_*.py'
python3 -m unittest discover -s agent_bridge/backends/send -p 'test_*.py'
