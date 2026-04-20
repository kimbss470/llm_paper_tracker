#!/usr/bin/env bash
set -euo pipefail

# Append a chat summary block to log/chat_YYYY-MM-DD.md in KST.
# Usage:
#   ./scripts/log_chat_summary.sh \
#     --request "..." \
#     --actions "..." \
#     --result "..." \
#     [--notes "..."]

REQUEST=""
ACTIONS=""
RESULT=""
NOTES=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --request)
      REQUEST="${2:-}"
      shift 2
      ;;
    --actions)
      ACTIONS="${2:-}"
      shift 2
      ;;
    --result)
      RESULT="${2:-}"
      shift 2
      ;;
    --notes)
      NOTES="${2:-}"
      shift 2
      ;;
    *)
      echo "Unknown argument: $1" >&2
      exit 1
      ;;
  esac
done

if [[ -z "$REQUEST" || -z "$ACTIONS" || -z "$RESULT" ]]; then
  echo "Required: --request, --actions, --result" >&2
  exit 1
fi

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOG_DIR="$ROOT_DIR/log"
mkdir -p "$LOG_DIR"

DAY_KST="$(TZ=Asia/Seoul date +%F)"
TIME_KST="$(TZ=Asia/Seoul date +%T)"
LOG_FILE="$LOG_DIR/chat_${DAY_KST}.md"

if [[ ! -f "$LOG_FILE" ]]; then
  cat > "$LOG_FILE" <<EOF
# Chat Log ${DAY_KST}

EOF
fi

{
  echo "## ${TIME_KST} KST"
  echo "- Request: ${REQUEST}"
  echo "- Actions: ${ACTIONS}"
  echo "- Result: ${RESULT}"
  if [[ -n "$NOTES" ]]; then
    echo "- Notes: ${NOTES}"
  fi
  echo
} >> "$LOG_FILE"

echo "Appended log entry to ${LOG_FILE}"
