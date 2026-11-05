#!/usr/bin/env bash
# brainctl - install and manage agent-recall on Linux and macOS.
#
# All data work happens in Python (the `brain` command); this script handles the system side:
# finds Python 3.11+ (suggests how to install it), creates a private environment, puts `brain` and
# `brainctl` in ~/.local/bin, adds PATH to the shell profile, schedules the sync (launchd on macOS,
# a systemd user timer or cron on Linux), connects the MCP server to your agents, updates and
# removes the installation.
#
#   ./brainctl.sh install [--interval N] [--vault DIR] [--python PATH] [--lang en|ru|auto]
#                         [-y] [--no-path] [--no-sync] [--auto-sync | --no-task] [--no-mcp]
#   brainctl status | paths | config | log [follow] | sync | doctor
#   brainctl task [add|remove|run|pause|resume] [--interval N]
#   brainctl mcp [add|remove] [all|claude|codex|cline]
#   brainctl update | uninstall [--purge] | help
#
# Language: --lang, AGENT_RECALL_LANG, `language` in config.toml, then LANG / the macOS language.
# Compatible with bash 3.2 (the macOS default).

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
APP_DIR="${AGENT_RECALL_APPDIR:-${XDG_DATA_HOME:-$HOME/.local/share}/agent-recall}"
BIN_DIR="${AGENT_RECALL_BINDIR:-$HOME/.local/bin}"
VENV="$APP_DIR/venv"
VPY="$VENV/bin/python"
STATE_FILE="$APP_DIR/install.env"
MARK_BEGIN="# >>> agent-recall >>>"
MARK_END="# <<< agent-recall <<<"
MIN_MAJOR=3
MIN_MINOR=11

# data: same rule as in Python (config.data_home): AGENT_RECALL_HOME > old ~/.brainsync > ~/.agent-recall
if [ -n "${AGENT_RECALL_HOME:-}" ]; then DATA_DIR="$AGENT_RECALL_HOME"
elif [ ! -e "$HOME/.agent-recall" ] && [ -d "$HOME/.brainsync" ]; then DATA_DIR="$HOME/.brainsync"
else DATA_DIR="$HOME/.agent-recall"; fi
CONFIG_FILE="$DATA_DIR/config.toml"

YES=0; PURGE=0; NO_PATH=0; NO_SYNC=0; NO_TASK=0; AUTO_SYNC=0; NO_MCP=0
INTERVAL=""; VAULT=""; PYTHON_OPT=""; LANG_OPT=""; ACTION=""; TARGET="all"

# ---- language: same priority as in Python (agent_recall/i18n.py) ----------------------------
detect_lang() {
  local v
  case "$LANG_OPT" in en|ru) echo "$LANG_OPT"; return ;; esac
  v="$(printf '%s' "${AGENT_RECALL_LANG:-}" | cut -c1-2 | tr '[:upper:]' '[:lower:]')"
  case "$v" in en|ru) echo "$v"; return ;; esac
  if [ "$LANG_OPT" != auto ] && [ -f "$CONFIG_FILE" ]; then
    v="$({ grep -E '^[[:space:]]*language[[:space:]]*=[[:space:]]*"(en|ru)' "$CONFIG_FILE" || true; } | head -n1 | sed -E 's/.*"(en|ru).*/\1/')"
    case "$v" in en|ru) echo "$v"; return ;; esac
  fi
  for v in "${LC_ALL:-}" "${LC_MESSAGES:-}" "${LANG:-}"; do
    case "$v" in ""|C|C.*|POSIX) continue ;; ru*) echo ru; return ;; *) echo en; return ;; esac
  done
  if [ "$(uname -s)" = Darwin ] && defaults read -g AppleLanguages 2>/dev/null | grep -q '"\{0,1\}ru'; then
    echo ru; return
  fi
  echo en
}
UI_LANG="$(detect_lang)"
t() { if [ "$UI_LANG" = ru ]; then printf '%s' "$2"; else printf '%s' "$1"; fi; }  # t "English" "Русский"

if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then
  C_CYAN=$'\033[36m'; C_GREEN=$'\033[32m'; C_YELLOW=$'\033[33m'; C_RED=$'\033[31m'; C_BOLD=$'\033[1m'; C_OFF=$'\033[0m'
else
  C_CYAN=""; C_GREEN=""; C_YELLOW=""; C_RED=""; C_BOLD=""; C_OFF=""
fi
step() { printf '\n%s==> %s%s\n' "$C_CYAN" "$1" "$C_OFF"; }
ok()   { printf '    %s[ok]%s %s\n' "$C_GREEN" "$C_OFF" "$1"; }
info() { printf '         %s\n' "$1"; }
warn() { printf '    %s[!]%s  %s\n' "$C_YELLOW" "$C_OFF" "$1"; }
fail() { printf '    %s[x]%s  %s\n' "$C_RED" "$C_OFF" "$1" >&2; exit 1; }

ask() {  # ask "question" [default:y|n]
  local default="${2:-y}" hint answer
  if [ "$YES" = 1 ]; then return 0; fi
  if [ "$default" = y ]; then hint="Y/n"; else hint="y/N"; fi
  printf '    %s (%s): ' "$1" "$hint"
  if ! read -r answer; then answer=""; fi
  [ -z "$answer" ] && answer="$default"
  case "$(printf '%s' "$answer" | tr '[:upper:]' '[:lower:]')" in
    y|yes|д|да|Д|Да|ДА|l) return 0 ;;
    *) return 1 ;;
  esac
}

os_name() {
  case "$(uname -s)" in
    Darwin) echo macos ;;
    Linux) echo linux ;;
    *) echo other ;;
  esac
}

sh_quote() { printf "'%s'" "$(printf '%s' "$1" | sed "s/'/'\\\\''/g")"; }

# ---- where the sources are --------------------------------------------------------------------
# Run from the repository: the folder next to this script. Run as the installed copy: from install.env.
SOURCE=""
if [ -f "$SCRIPT_DIR/pyproject.toml" ] && [ -d "$SCRIPT_DIR/agent_recall" ]; then SOURCE="$SCRIPT_DIR"; fi

version() {
  local src="${SOURCE:-${SRC:-}}"
  sed -n 's/^__version__ *= *"\([^"]*\)".*/\1/p' "$src/agent_recall/__init__.py" 2>/dev/null || echo "?"
}

# ---- Python ------------------------------------------------------------------------------------

py_check() {  # py_check EXE -> prints "path version" for Python 3.11+, else fails
  "$1" -c "import sys
if sys.version_info < ($MIN_MAJOR, $MIN_MINOR): raise SystemExit(1)
print(sys.executable, '%d.%d.%d' % sys.version_info[:3])" 2>/dev/null
}

find_python() {
  local c out
  if [ -n "$PYTHON_OPT" ]; then
    py_check "$PYTHON_OPT"
    return
  fi
  for c in python3.14 python3.13 python3.12 python3.11 python3 python \
           /opt/homebrew/bin/python3 /usr/local/bin/python3; do
    if command -v "$c" >/dev/null 2>&1; then
      if out="$(py_check "$c")"; then printf '%s\n' "$out"; return 0; fi
    fi
  done
  return 1
}

pkg_hint() {  # pkg_hint python|venv -> an install command for this system (may be empty)
  local what="$1" ver="${2:-3}"
  case "$(os_name)" in
    macos)
      if [ "$what" = python ] && command -v brew >/dev/null 2>&1; then echo "brew install python@3.12"; else echo ""; fi ;;
    linux)
      if command -v apt-get >/dev/null 2>&1; then
        if [ "$what" = venv ]; then echo "sudo apt-get install -y python${ver}-venv"; else echo "sudo apt-get install -y python3 python3-venv"; fi
      elif command -v dnf >/dev/null 2>&1; then echo "sudo dnf install -y python3"
      elif command -v pacman >/dev/null 2>&1; then echo "sudo pacman -S --needed python"
      elif command -v zypper >/dev/null 2>&1; then echo "sudo zypper install -y python3"
      elif command -v apk >/dev/null 2>&1; then echo "sudo apk add python3"
      else echo ""; fi ;;
    *) echo "" ;;
  esac
}

ensure_python() {
  step "Python $MIN_MAJOR.$MIN_MINOR+"
  local found cmd
  if ! found="$(find_python)"; then
    [ -z "$PYTHON_OPT" ] || fail "$(t "$PYTHON_OPT is not Python $MIN_MAJOR.$MIN_MINOR+ (check the path in --python)" "$PYTHON_OPT — не Python $MIN_MAJOR.$MIN_MINOR+ (проверьте путь в --python)")"
    warn "$(t "Python $MIN_MAJOR.$MIN_MINOR+ not found" "Python $MIN_MAJOR.$MIN_MINOR+ не найден")"
    cmd="$(pkg_hint python)"
    if [ -n "$cmd" ] && ask "$(t "Run: $cmd ?" "Выполнить: $cmd ?")"; then
      sh -c "$cmd" || warn "$(t "the command failed" "команда завершилась с ошибкой")"
      hash -r
      found="$(find_python)" || true
    fi
    if [ -z "${found:-}" ]; then
      fail "$(t "Install Python 3.11+ (https://www.python.org/downloads/) and run again. If the system one is old, point at another: --python /path/to/python3.12" "Установите Python 3.11+ (https://www.python.org/downloads/) и повторите. Если версия в системе старая — укажите другую: --python /путь/к/python3.12")"
    fi
  fi
  PY_PATH="${found% *}"
  PY_VERSION="${found##* }"
  ok "Python $PY_VERSION: $PY_PATH"
}

ensure_venv() {
  step "$(t "agent-recall environment" "Окружение agent-recall")"
  mkdir -p "$APP_DIR"
  if [ -x "$VPY" ] && ! py_check "$VPY" >/dev/null; then rm -rf "$VENV"; fi   # broken or too old
  if [ ! -x "$VPY" ]; then
    if ! "$PY_PATH" -m venv "$VENV" >/dev/null 2>&1; then
      rm -rf "$VENV"
      local short cmd
      short="$(printf '%s' "$PY_VERSION" | cut -d. -f1,2)"
      cmd="$(pkg_hint venv "$short")"
      warn "$(t "Python cannot create a virtual environment (the venv module is missing)" "Python не может создать окружение (нет модуля venv)")"
      if [ -n "$cmd" ] && ask "$(t "Run: $cmd ?" "Выполнить: $cmd ?")"; then
        sh -c "$cmd" || true
        "$PY_PATH" -m venv "$VENV" >/dev/null 2>&1 || { rm -rf "$VENV"; fail "$(t "Still cannot create the environment. Install the venv package for your Python and run again." "Окружение по-прежнему не создаётся. Установите пакет venv для вашего Python и повторите.")"; }
      else
        fail "$(t "Install the venv package for your Python${cmd:+ ($cmd)} and run again." "Установите пакет venv для вашего Python${cmd:+ ($cmd)} и повторите.")"
      fi
    fi
  fi
  info "$(t "installing packages (pip)…" "ставлю пакеты (pip)…")"
  "$VPY" -m pip install --upgrade --quiet --disable-pip-version-check pip >/dev/null 2>&1 || true
  "$VPY" -m pip install --upgrade --quiet --disable-pip-version-check "${SOURCE}[mcp]" \
    || fail "$(t "pip could not install agent-recall (see above). No internet, or behind a proxy? export HTTPS_PROXY=http://proxy:port" "pip не смог установить agent-recall (см. выше). Нет интернета или нужен прокси? export HTTPS_PROXY=http://proxy:port")"
  ok "$(t "installed into $APP_DIR" "установлено в $APP_DIR")"
}

brain() {  # run the Python part with the installed interpreter
  [ -x "$VPY" ] || fail "$(t "agent-recall is not installed. Run: ./brainctl.sh install" "agent-recall не установлен. Запустите: ./brainctl.sh install")"
  AGENT_RECALL_LANG="${AGENT_RECALL_LANG:-$UI_LANG}" "$VPY" -X utf8 -m agent_recall "$@"
}

# ---- commands on PATH ----------------------------------------------------------------------------

write_shims() {
  step "$(t "Commands brain and brainctl" "Команды brain и brainctl")"
  mkdir -p "$BIN_DIR" "$APP_DIR"
  # a copy of this script, so brainctl keeps working even if the source folder is moved or deleted
  if [ "$SCRIPT_DIR/brainctl.sh" != "$APP_DIR/brainctl.sh" ]; then
    cp "$SCRIPT_DIR/brainctl.sh" "$APP_DIR/brainctl.sh"
  fi
  {
    echo '#!/bin/sh'
    echo "# created by brainctl (agent-recall)"
    echo "exec $(sh_quote "$VPY") -X utf8 -m agent_recall \"\$@\""
  } > "$BIN_DIR/brain"
  {
    echo '#!/bin/sh'
    echo "# created by brainctl (agent-recall)"
    echo "exec bash $(sh_quote "$APP_DIR/brainctl.sh") \"\$@\""
  } > "$BIN_DIR/brainctl"
  chmod +x "$BIN_DIR/brain" "$BIN_DIR/brainctl"
  ok "$(t "in $BIN_DIR" "в $BIN_DIR")"
}

in_path() {
  case ":$PATH:" in *":$1:"*|*":$1/:"*) return 0 ;; *) return 1 ;; esac
}

rc_file() {
  local sh; sh="$(basename "${SHELL:-sh}")"
  case "$sh" in
    zsh) echo "${ZDOTDIR:-$HOME}/.zshrc" ;;
    bash) if [ "$(os_name)" = macos ]; then echo "$HOME/.bash_profile"; else echo "$HOME/.bashrc"; fi ;;
    fish) echo "${XDG_CONFIG_HOME:-$HOME/.config}/fish/conf.d/agent-recall.fish" ;;
    *) echo "$HOME/.profile" ;;
  esac
}

add_to_path() {
  if [ "$NO_PATH" = 1 ]; then info "$(t "PATH left as is (--no-path). The commands are in $BIN_DIR" "PATH не меняю (--no-path). Команды лежат в $BIN_DIR")"; return; fi
  local rc line
  rc="$(rc_file)"
  if [ -f "$rc" ] && grep -qF "$MARK_BEGIN" "$rc"; then ok "$(t "PATH is already set in $rc" "PATH уже настроен в $rc")"; return; fi
  if in_path "$BIN_DIR"; then ok "$(t "$BIN_DIR is already on PATH" "$BIN_DIR уже в PATH")"; return; fi
  if ! ask "$(t "Add $BIN_DIR to PATH (a line in $rc)?" "Добавить $BIN_DIR в PATH (строка в $rc)?")"; then
    warn "$(t "Add it yourself: export PATH=\"$BIN_DIR:\$PATH\"" "Добавьте сами: export PATH=\"$BIN_DIR:\$PATH\"")"; return
  fi
  case "$rc" in
    *.fish) line="fish_add_path $(sh_quote "$BIN_DIR")" ;;
    *) line="export PATH=$(sh_quote "$BIN_DIR"):\"\$PATH\"" ;;
  esac
  mkdir -p "$(dirname "$rc")"
  printf '\n%s\n%s\n%s\n' "$MARK_BEGIN" "$line" "$MARK_END" >> "$rc"
  ok "$(t "added to $rc (new terminal windows will see the commands; in this one: source $rc)" "добавлено в $rc (новые окна терминала увидят команды; в этом: source $rc)")"
}

remove_from_path() {
  local rc tmp
  for rc in "$HOME/.zshrc" "${ZDOTDIR:-$HOME}/.zshrc" "$HOME/.bashrc" "$HOME/.bash_profile" "$HOME/.profile" \
            "${XDG_CONFIG_HOME:-$HOME/.config}/fish/conf.d/agent-recall.fish"; do
    [ -f "$rc" ] || continue
    grep -qF "$MARK_BEGIN" "$rc" || continue
    tmp="$(mktemp)"
    awk -v b="$MARK_BEGIN" -v e="$MARK_END" '
      { line[NR] = $0 }
      END {
        out = 0; skip = 0
        for (i = 1; i <= NR; i++) {
          if (line[i] == b) { if (out > 0 && buf[out] == "") out--; skip = 1; continue }
          if (line[i] == e) { skip = 0; continue }
          if (!skip) buf[++out] = line[i]
        }
        for (i = 1; i <= out; i++) print buf[i]
      }' "$rc" > "$tmp"
    cat "$tmp" > "$rc" && rm -f "$tmp"
    case "$rc" in *agent-recall.fish) [ -s "$rc" ] || rm -f "$rc" ;; esac
    ok "$(t "PATH removed from $rc" "убран PATH из $rc")"
  done
}

# ---- state ------------------------------------------------------------------------------------------

save_state() {
  mkdir -p "$APP_DIR"
  {
    echo "VERSION=$(sh_quote "$(version)")"
    echo "SRC=$(sh_quote "$SOURCE")"
    echo "BIN=$(sh_quote "$BIN_DIR")"
    echo "INSTALLED_AT=$(sh_quote "$(date '+%Y-%m-%d %H:%M')")"
  } > "$STATE_FILE"
}

load_state() {
  [ -f "$STATE_FILE" ] || return 1
  VERSION=""; SRC=""; BIN=""; INSTALLED_AT=""
  # shellcheck disable=SC1090
  . "$STATE_FILE"
}

# ---- scheduled sync -----------------------------------------------------------------------------------

# Scheduling is shared with the TUI/CLI, including detection of legacy jobs.
task_add() { brain auto-sync enable --interval "${INTERVAL:-15}"; }
task_remove() { brain auto-sync disable; }
task_status() { brain auto-sync status; }
task_run() { brain sync; }
# Keep the old command names as aliases. Pausing now removes the OS job as well.
task_pause() { task_remove; }
task_resume() { task_add; }
# ---- commands -------------------------------------------------------------------------------------------

cmd_install() {
  [ "$AUTO_SYNC" = 0 ] || [ "$NO_TASK" = 0 ] || fail "--auto-sync and --no-task cannot be combined"
  [ -n "$SOURCE" ] || fail "$(t "Run install from the agent-recall folder: ./brainctl.sh install" "Запускайте install из папки agent-recall: ./brainctl.sh install")"
  printf '%sagent-recall %s — %s%s\n' "$C_BOLD" "$(version)" "$(t install установка)" "$C_OFF"
  info "$(t "sources: $SOURCE" "исходники: $SOURCE")"
  ensure_python
  ensure_venv
  write_shims
  add_to_path
  save_state

  step "$(t "Config" "Конфиг")"
  if [ -f "$CONFIG_FILE" ]; then
    ok "$(t "exists: $CONFIG_FILE" "уже есть: $CONFIG_FILE")"
  elif [ -n "$VAULT" ]; then brain init --vault "$VAULT"
  else brain init; fi
  if [ -n "$LANG_OPT" ]; then
    local v="$LANG_OPT"; [ "$v" = auto ] && v=""
    "$VPY" -c 'import sys; from pathlib import Path; from agent_recall.app import set_config_value; set_config_value(Path(sys.argv[1]), "language", sys.argv[2])' "$CONFIG_FILE" "$v"
    ok "language = ${v:-auto}"
  fi

  if [ "$NO_SYNC" = 0 ]; then
    step "$(t "First sync (may take a couple of minutes)" "Первая синхронизация (может занять пару минут)")"
    brain sync || warn "$(t "the sync reported errors — see above; brainctl log" "синхронизация сообщила об ошибках — см. выше; brainctl log")"
  fi
  step "$(t "Optional auto-sync" "Автосинхронизация по желанию")"
  local mode=ask
  if [ "$AUTO_SYNC" = 1 ]; then mode=on
  elif [ "$NO_TASK" = 1 ]; then mode=off
  elif [ "$YES" = 1 ]; then mode=preserve; fi
  if [ -n "$INTERVAL" ]; then brain auto-sync setup --mode "$mode" --interval "$INTERVAL"
  else brain auto-sync setup --mode "$mode"; fi
  if [ "$NO_MCP" = 0 ]; then
    step "$(t "Agent connections (MCP)" "Подключение к агентам (MCP)")"
    brain integrate add all || true
  fi
  if [ "$(os_name)" = macos ]; then
    step "macOS"
    info "$(t "If your Obsidian vault is in Documents, Desktop or iCloud Drive, macOS may block the background sync." "Если хранилище Obsidian лежит в «Документах», на «Рабочем столе» или в iCloud Drive, macOS может не пускать туда фоновую синхронизацию.")"
    info "$(t "Then add this Python to System Settings → Privacy & Security → Full Disk Access:" "Тогда добавьте этот Python в «Системные настройки → Конфиденциальность и безопасность → Полный доступ к диску»:")"
    info "  $("$VPY" -c 'import os, sys; print(os.path.realpath(sys.executable))')"
  fi

  step "$(t Check Проверка)"
  brain doctor || true
  printf '\n%s%s%s\n' "$C_GREEN" "$(t "Done. In a new terminal window you have:" "Готово. В новом окне терминала доступны:")" "$C_OFF"
  info "$(t "brain                      — full-screen UI" "brain                      — полноэкранный интерфейс")"
  info "$(t "brain handoff --last -c    — continue in another AI" "brain handoff --last -c    — продолжить в другом ИИ")"
  info "$(t "brainctl status            — installation status" "brainctl status            — состояние установки")"
  info "$(t "brainctl help              — all management commands" "brainctl help              — все команды управления")"
}

cmd_update() {
  load_state || fail "$(t "agent-recall is not installed. Run: ./brainctl.sh install" "agent-recall не установлен. Запустите: ./brainctl.sh install")"
  [ -n "$SOURCE" ] || SOURCE="$SRC"
  [ -f "$SOURCE/pyproject.toml" ] || fail "$(t "Sources not found: $SOURCE. Run ./brainctl.sh update from the new folder." "Исходники не найдены: $SOURCE. Запустите ./brainctl.sh update из новой папки.")"
  step "$(t "Updating the sources" "Обновление исходников")"
  if [ -d "$SOURCE/.git" ] && command -v git >/dev/null 2>&1; then
    if git -C "$SOURCE" rev-parse --abbrev-ref --symbolic-full-name '@{u}' >/dev/null 2>&1; then
      git -C "$SOURCE" pull --ff-only || warn "$(t "git pull failed (local changes?) — continuing with the current sources" "git pull не прошёл (локальные изменения?) — продолжаю с текущими исходниками")"
    else
      info "$(t "the branch has no upstream — skipping git pull" "у ветки нет удалённого репозитория — пропускаю git pull")"
    fi
  else
    info "$(t "not a git repository — update the files yourself (a new archive) and run update again" "не git-репозиторий — обновите файлы сами (новый архив) и повторите update")"
  fi
  PY_PATH="$VPY"; PY_VERSION="$("$VPY" -c 'import sys; print("%d.%d.%d" % sys.version_info[:3])' 2>/dev/null || echo 3)"
  if ! py_check "$VPY" >/dev/null; then ensure_python; fi
  ensure_venv
  BIN_DIR="${BIN:-$BIN_DIR}"
  SCRIPT_DIR="$SOURCE"
  write_shims
  save_state
  ok "$(t "updated to $(version); the scheduled sync picks it up by itself" "обновлено до $(version); синхронизация по расписанию подхватит её сама")"
}

cmd_uninstall() {
  ask "$(t "Remove agent-recall (program, scheduled sync, MCP connections, commands)?" "Удалить agent-recall (программу, расписание, подключения MCP, команды)?")" y || return 0
  step "$(t "Uninstalling" "Удаление")"
  if load_state; then BIN_DIR="${BIN:-$BIN_DIR}"; fi
  task_remove
  if [ -x "$VPY" ]; then brain integrate remove all || true; fi
  local f
  for f in brain brainctl; do
    if [ -f "$BIN_DIR/$f" ] && grep -qF "created by brainctl" "$BIN_DIR/$f"; then rm -f "$BIN_DIR/$f"; fi
  done
  remove_from_path
  rm -rf "$APP_DIR"
  ok "$(t "program and commands removed: $APP_DIR" "программа и команды удалены: $APP_DIR")"
  if [ "$PURGE" = 1 ]; then
    if [ -d "$DATA_DIR" ] && ask "$(t "Also delete your DATA ($DATA_DIR: database, config, logs)? This cannot be undone." "Удалить и ДАННЫЕ ($DATA_DIR: база, конфиг, логи)? Это необратимо.")" n; then
      rm -rf "$DATA_DIR"; ok "$(t "data deleted" "данные удалены")"
    fi
  else
    info "$(t "Data kept in $DATA_DIR (to delete it too: brainctl uninstall --purge)" "Данные сохранены в $DATA_DIR (удалить вместе с ними: brainctl uninstall --purge)")"
  fi
}

cmd_status() {
  printf '%sagent-recall%s\n' "$C_BOLD" "$C_OFF"
  step "$(t "Installation" "Установка")"
  if ! load_state || [ ! -x "$VPY" ]; then warn "$(t "not installed (./brainctl.sh install)" "не установлено (./brainctl.sh install)")"; return 0; fi
  ok "$(t "version $VERSION, installed $INSTALLED_AT" "версия $VERSION, установлена $INSTALLED_AT")"
  info "python:  $VPY ($("$VPY" -c 'import sys; print("%d.%d.%d" % sys.version_info[:3])'))"
  info "$(t "sources: " "исходники: ")$SRC$([ -d "$SRC" ] || t "  (folder is gone — update needs the new path)" "  (папки нет — для update нужна новая)")"
  if in_path "$BIN"; then ok "$(t "brain and brainctl are on PATH" "brain и brainctl в PATH")"
  else warn "$(t "$BIN is not on PATH in this window (open a new one)" "$BIN не в PATH в этом окне (откройте новое)")"; fi
  step "$(t "Schedule" "Расписание")"
  task_status
  step "$(t "Data and sources" "Данные и источники")"
  brain doctor || true
}

cmd_log() {
  local log="$DATA_DIR/brain.log"
  [ -f "$log" ] || { warn "$(t "no log yet" "лога ещё нет")"; return 0; }
  if [ "$ACTION" = follow ]; then tail -n 30 -f "$log"; else tail -n 40 "$log"; fi
}

cmd_config() {
  [ -f "$CONFIG_FILE" ] || brain init
  if [ -n "${EDITOR:-}" ]; then "$EDITOR" "$CONFIG_FILE"
  elif [ "$(os_name)" = macos ]; then open -t "$CONFIG_FILE"
  elif command -v xdg-open >/dev/null 2>&1; then xdg-open "$CONFIG_FILE" >/dev/null 2>&1 &
  else info "$CONFIG_FILE"; fi
}

cmd_task() {
  case "$ACTION" in
    add) task_add ;;
    remove) task_remove ;;
    run) task_run ;;
    pause) task_pause ;;
    resume) task_resume ;;
    ""|status) task_status ;;
    *) fail "task: add | remove | run | pause | resume" ;;
  esac
}

cmd_mcp() {
  case "$ACTION" in
    add|remove) brain integrate "$ACTION" "$TARGET" ;;
    ""|status) brain integrate status all ;;
    *) fail "mcp: add | remove [all|claude|codex|cline]" ;;
  esac
}

cmd_help() {
  if [ "$UI_LANG" = ru ]; then
    cat <<'EOF'
brainctl — установка и управление agent-recall (Linux, macOS)

  install [--interval 15] [--vault DIR] [--python PATH] [--lang en|ru|auto] [-y]
          [--no-path] [--no-sync] [--auto-sync | --no-task] [--no-mcp]
                         Python 3.11+ (подскажет команду установки) → окружение →
                         команды brain и brainctl в ~/.local/bin → PATH → конфиг →
                         первая синхронизация → выбор расписания → подключение к агентам
                         По умолчанию выключена; -y сохраняет текущее состояние.
                         --auto-sync включает, --no-task отключает и удаляет задачу.
  update                 git pull исходников и переустановка
  uninstall [--purge]    удалить программу; с --purge ещё и базу с конфигом

  status                 что установлено, расписание, источники, агенты
  paths                  все пути: конфиг, база, лог, Claude Code, Codex, Obsidian
  config                 открыть config.toml ($EDITOR)
  log [follow]           последние строки лога (follow — следить)

  task                   состояние расписания (launchd / systemd / cron)
  task add [--interval N]  создать / пересоздать (по умолчанию каждые 15 мин.)
  task remove | run | pause | resume

  mcp                    к каким агентам подключён
  mcp add [all|claude|codex|cline]
  mcp remove [all|claude|codex|cline]

  sync                   синхронизировать сейчас (то же, что brain sync -v)
  doctor                 проверка путей и токенов (то же, что brain doctor)

Работа с данными — команда brain: brain, brain search, brain handoff ... (brain --help)
Язык: --lang en|ru, AGENT_RECALL_LANG или language = "en" в config.toml
EOF
    return
  fi
  cat <<'EOF'
brainctl — install and manage agent-recall (Linux, macOS)

  install [--interval 15] [--vault DIR] [--python PATH] [--lang en|ru|auto] [-y]
          [--no-path] [--no-sync] [--auto-sync | --no-task] [--no-mcp]
                         Python 3.11+ (suggests the install command) → environment →
                         brain and brainctl in ~/.local/bin → PATH → config →
                         first sync → choose scheduling → agent connections
                         Off by default; -y preserves the current state.
                         --auto-sync enables; --no-task disables and removes the job.
  update                 git pull the sources and reinstall
  uninstall [--purge]    remove the program; --purge also deletes the database and config

  status                 what is installed, schedule, sources, agents
  paths                  all paths: config, database, log, Claude Code, Codex, Obsidian
  config                 open config.toml ($EDITOR)
  log [follow]           last lines of the log (follow — keep watching)

  task                   schedule status (launchd / systemd / cron)
  task add [--interval N]  create / recreate (default every 15 min)
  task remove | run | pause | resume

  mcp                    which agents are connected
  mcp add [all|claude|codex|cline]
  mcp remove [all|claude|codex|cline]

  sync                   sync now (same as brain sync -v)
  doctor                 check paths and tokens (same as brain doctor)

Working with data is the brain command: brain, brain search, brain handoff ... (brain --help)
Language: --lang en|ru, AGENT_RECALL_LANG or language = "ru" in config.toml
EOF
}

# ---- arguments ---------------------------------------------------------------------------------------

COMMAND="${1:-help}"
[ $# -gt 0 ] && shift
POSITIONAL=0
while [ $# -gt 0 ]; do
  case "$1" in
    -y|--yes) YES=1 ;;
    --purge) PURGE=1 ;;
    --no-path) NO_PATH=1 ;;
    --no-sync) NO_SYNC=1 ;;
    --no-task|--no-auto-sync) NO_TASK=1 ;;
    --auto-sync) AUTO_SYNC=1 ;;
    --no-mcp) NO_MCP=1 ;;
    --interval) INTERVAL="${2:?--interval needs minutes}"; shift ;;
    --vault) VAULT="${2:?--vault needs a path}"; shift ;;
    --python) PYTHON_OPT="${2:?--python needs a path}"; shift ;;
    --lang)
      LANG_OPT="${2:?--lang needs en, ru or auto}"; shift
      case "$LANG_OPT" in en|ru|auto) ;; *) printf '%s--lang: en, ru, auto%s\n' "$C_RED" "$C_OFF" >&2; exit 2 ;; esac
      UI_LANG="$(detect_lang)" ;;
    -*) printf '%s%s %s%s\n' "$C_RED" "$(t "Unknown option:" "Неизвестный параметр:")" "$1" "$C_OFF" >&2; cmd_help; exit 2 ;;
    *)
      POSITIONAL=$((POSITIONAL + 1))
      if [ "$POSITIONAL" = 1 ]; then ACTION="$1"; else TARGET="$1"; fi ;;
  esac
  shift
done

case "$COMMAND" in
  install) cmd_install ;;
  update) cmd_update ;;
  uninstall) cmd_uninstall ;;
  status) cmd_status ;;
  paths) brain paths ;;
  config) cmd_config ;;
  log) cmd_log ;;
  task) cmd_task ;;
  mcp) cmd_mcp ;;
  sync) brain sync -v ;;
  doctor) brain doctor ;;
  help|-h|--help) cmd_help ;;
  *) printf '%s%s %s%s\n' "$C_RED" "$(t "Unknown command:" "Неизвестная команда:")" "$COMMAND" "$C_OFF" >&2; cmd_help; exit 2 ;;
esac
