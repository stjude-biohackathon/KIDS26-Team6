#!/bin/bash

# Shared console logging for the activity-tracking Bash scripts.

: "${LOG_MODULE:?LOG_MODULE must be set before sourcing logging.sh}"

readonly LOGGER_NAME="AUTOCAB"
readonly ANSI_GREEN=$'\033[32m'
readonly ANSI_YELLOW=$'\033[33m'
readonly ANSI_RED=$'\033[31m'
readonly ANSI_RESET=$'\033[0m'

log_message() {
    local level="$1"
    local message="$2"
    local caller_line="$3"
    local color=""
    local reset=""

    if [[ -t 2 && -z "${NO_COLOR+x}" ]]; then
        case "$level" in
            INFO) color="$ANSI_GREEN" ;;
            WARNING) color="$ANSI_YELLOW" ;;
            ERROR) color="$ANSI_RED" ;;
        esac
        reset="$ANSI_RESET"
    fi
    printf '%b[%s | %s] [%s | %s - line %s]:%b %s\n' \
        "$color" "$level" "$LOGGER_NAME" \
        "$(date '+%b-%d-%Y at %I:%M:%S %p')" "$LOG_MODULE" "$caller_line" \
        "$reset" "$message" >&2
}

log_info() {
    log_message "INFO" "$1" "${BASH_LINENO[0]}"
}

log_warning() {
    log_message "WARNING" "$1" "${BASH_LINENO[0]}"
}

fail() {
    log_message "ERROR" "$1" "${BASH_LINENO[0]}"
    exit 1
}
