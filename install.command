#!/usr/bin/env bash
# agent-recall: install on macOS by double-clicking in Finder.
cd "$(dirname "$0")" || exit 1
bash ./brainctl.sh install
echo
case "${LANG:-}" in ru*) msg="Нажмите Enter, чтобы закрыть окно…" ;; *) msg="Press Enter to close this window…" ;; esac
read -r -p "$msg" _
