#!/usr/bin/env bash
# agent-recall: install on Linux and macOS. Options are passed to brainctl.sh install.
cd "$(dirname "$0")" && exec bash ./brainctl.sh install "$@"
