#!/bin/sh
set -eu
bridge_repo_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$bridge_repo_root"
cargo build --release --locked -p wx-cli
bridge_target_dir=$(cargo metadata --format-version 1 --no-deps | python3 -c 'import json,sys; print(json.load(sys.stdin)["target_directory"])')
mkdir -p agent_bridge/bin
cp "$bridge_target_dir/release/wx-cli" agent_bridge/bin/wx-cli
chmod 755 agent_bridge/bin/wx-cli
printf 'Built agent_bridge/bin/wx-cli\n'
