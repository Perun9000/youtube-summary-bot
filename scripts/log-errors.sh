#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LINES="${1:-200}"
LOG_FILE="${LOG_FILE:-$ROOT_DIR/data/logs/bot.log}"
DOCKER_SERVICE="${DOCKER_SERVICE:-bot}"
ERROR_PATTERN='error|critical|traceback|exception|failed|timeout_retry'

if [[ "$LINES" == "-h" || "$LINES" == "--help" ]]; then
  cat <<'EOF'
Usage:
  ./scripts/log-errors.sh [lines]

Examples:
  ./scripts/log-errors.sh
  ./scripts/log-errors.sh 300
EOF
  exit 0
fi

print_header() {
  local title="$1"
  printf "\n== %s ==\n" "$title"
}

print_no_matches() {
  local lines="$1"
  printf "Совпадений не найдено в последних %s строках.\n" "$lines"
}

print_header "Файл-лог: $LOG_FILE"
if [[ -f "$LOG_FILE" ]]; then
  if ! tail -n "$LINES" "$LOG_FILE" | rg -n -i "$ERROR_PATTERN"; then
    print_no_matches "$LINES"
  fi
else
  echo "Файл лога пока не создан."
fi

print_header "Docker logs: service=$DOCKER_SERVICE"
if docker_output="$(docker compose -f "$ROOT_DIR/docker-compose.yml" logs --tail="$LINES" "$DOCKER_SERVICE" 2>&1)"; then
  if ! printf "%s\n" "$docker_output" | rg -n -i "$ERROR_PATTERN"; then
    print_no_matches "$LINES"
  fi
else
  printf "%s\n" "$docker_output"
fi
