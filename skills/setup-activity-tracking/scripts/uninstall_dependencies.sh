#!/bin/bash

set -euo pipefail

SCRIPT_DIRECTORY="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly SCRIPT_DIRECTORY
# Referenced by the shared logger after it is sourced.
# shellcheck disable=SC2034
readonly LOG_MODULE="uninstall_dependencies"
# shellcheck disable=SC1091
source "$SCRIPT_DIRECTORY/logging.sh"

readonly CONFIRMATION_PHRASE="REMOVE AUTOCAB ACTIVITY TRACKING"
readonly STATE_DIRECTORY="${XDG_STATE_HOME:-$HOME/.local/state}/autocab"
readonly RECEIPT_PATH="$STATE_DIRECTORY/activity-tracking-install.receipt"
readonly CONFIG_DIRECTORY="${XDG_CONFIG_HOME:-$HOME/.config}"

artifact_paths=()
artifact_hashes=()
shell_name=""
shell_configuration_path=""
shell_path_line_added="0"
shell_init_line_added="0"
shell_path_line=""
shell_init_line=""
check_status=""
check_reason=""
plan_ready_count=0
plan_manual_count=0

path_exists() {
    [[ -e "$1" || -L "$1" ]]
}

hash_file() {
    if command -v sha256sum >/dev/null 2>&1; then
        sha256sum "$1" | awk '{print $1}'
    elif command -v shasum >/dev/null 2>&1; then
        shasum -a 256 "$1" | awk '{print $1}'
    else
        fail "Missing SHA-256 utility. Install coreutils to provide sha256sum."
    fi
}

display_path() {
    if [[ "$1" == "$HOME/"* ]]; then
        # This is display text, not a path that the shell should expand.
        # shellcheck disable=SC2088
        printf '~/%s' "${1#"$HOME/"}"
    else
        basename "$1"
    fi
}

artifact_path_is_allowed() {
    case "$1" in
        "$HOME/.cargo/bin/devsql" | \
        "$CONFIG_DIRECTORY/devsql/devsql-receipt.json" | \
        "$HOME/.atuin/bin/atuin" | \
        "$HOME/.atuin/bin/env" | \
        "$HOME/.atuin/bin/env.fish" | \
        "$CONFIG_DIRECTORY/atuin/atuin-receipt.json")
            return 0
            ;;
        *)
            return 1
            ;;
    esac
}

load_receipt() {
    local record
    local first
    local second
    local third
    local fourth
    local extra
    local schema_seen="false"
    local shell_seen="false"

    [[ ! -L "$RECEIPT_PATH" && -f "$RECEIPT_PATH" ]] || \
        fail "The AutoCAB ownership receipt is not a regular file."
    while IFS='|' read -r record first second third fourth extra || [[ -n "$record" ]]; do
        case "$record" in
            schema)
                [[ "$schema_seen" == "false" && "$first" == "2" && \
                    -z "$second$third$fourth$extra" ]] || \
                    fail "The AutoCAB ownership receipt has an invalid schema."
                schema_seen="true"
                ;;
            artifact)
                [[ -n "$first" && "$second" =~ ^[0-9a-f]{64}$ && \
                    -z "$third$fourth$extra" ]] || fail "Invalid artifact receipt record."
                artifact_paths+=("$first")
                artifact_hashes+=("$second")
                ;;
            shell)
                [[ "$shell_seen" == "false" && "$first" =~ ^(bash|zsh|fish)$ && \
                    -n "$second" && "$third" =~ ^[01]$ && "$fourth" =~ ^[01]$ && \
                    -z "$extra" ]] || fail "Invalid shell receipt record."
                shell_name="$first"
                shell_configuration_path="$second"
                shell_path_line_added="$third"
                shell_init_line_added="$fourth"
                shell_seen="true"
                ;;
            *)
                fail "The AutoCAB ownership receipt contains an unknown record."
                ;;
        esac
    done < "$RECEIPT_PATH"
    [[ "$schema_seen" == "true" ]] || fail "The ownership receipt has no schema."
}

check_artifact() {
    local path="$1"
    local expected_hash="$2"

    check_reason=""
    if ! artifact_path_is_allowed "$path"; then
        check_status="manual_review"
        check_reason="path is outside the uninstall allowlist"
    elif ! path_exists "$path"; then
        check_status="absent"
    elif [[ -L "$path" || ! -f "$path" ]]; then
        check_status="manual_review"
        check_reason="artifact is no longer a regular file"
    elif [[ "$(hash_file "$path")" != "$expected_hash" ]]; then
        check_status="manual_review"
        check_reason="artifact changed after installation"
    else
        check_status="ready"
    fi
}

set_shell_lines() {
    case "$shell_name" in
        bash | zsh)
            # shellcheck disable=SC2016
            shell_path_line='export PATH="$HOME/.cargo/bin:$HOME/.atuin/bin:$PATH"'
            shell_init_line="eval \"\$(atuin init $shell_name --disable-ai)\""
            ;;
        fish)
            # shellcheck disable=SC2016
            shell_path_line='fish_add_path "$HOME/.cargo/bin" "$HOME/.atuin/bin"'
            shell_init_line='atuin init fish --disable-ai | source'
            ;;
        "") ;;
    esac
}

allowed_shell_path() {
    case "$shell_name" in
        bash) printf '%s' "$HOME/.bashrc" ;;
        zsh) printf '%s' "${ZDOTDIR:-$HOME}/.zshrc" ;;
        fish) printf '%s' "$HOME/.config/fish/config.fish" ;;
        "") printf '' ;;
    esac
}

check_shell_line() {
    local line="$1"

    check_reason=""
    if [[ "$shell_configuration_path" != "$(allowed_shell_path)" ]]; then
        check_status="manual_review"
        check_reason="shell configuration is outside the uninstall allowlist"
    elif ! path_exists "$shell_configuration_path"; then
        check_status="absent"
    elif [[ -L "$shell_configuration_path" || ! -f "$shell_configuration_path" ]]; then
        check_status="manual_review"
        check_reason="shell configuration is not a regular file"
    elif grep -Fqx "$line" "$shell_configuration_path"; then
        check_status="ready"
    else
        check_status="absent"
    fi
}

log_check() {
    local description="$1"
    local path="$2"
    local label

    case "$check_status" in
        ready)
            label="READY"
            plan_ready_count=$((plan_ready_count + 1))
            ;;
        absent) label="ABSENT" ;;
        manual_review)
            label="MANUAL_REVIEW"
            plan_manual_count=$((plan_manual_count + 1))
            ;;
    esac
    if [[ "$check_status" == "manual_review" ]]; then
        log_message "ERROR" \
            "$label: $description ($(display_path "$path")); $check_reason" \
            "${BASH_LINENO[0]}"
    elif [[ "$check_status" == "ready" ]]; then
        log_warning "$label: $description ($(display_path "$path"))"
    else
        log_info "$label: $description ($(display_path "$path"))"
    fi
}

for_each_artifact() {
    local operation="$1"
    local index

    for index in "${!artifact_paths[@]}"; do
        check_artifact "${artifact_paths[$index]}" "${artifact_hashes[$index]}"
        case "$operation" in
            plan)
                log_check "Remove AutoCAB-installed artifact" "${artifact_paths[$index]}"
                ;;
            validate)
                [[ "$check_status" != "manual_review" ]] || \
                    fail "A dependency artifact changed after planning."
                ;;
            remove)
                if [[ "$check_status" == "ready" ]]; then
                    rm -- "${artifact_paths[$index]}"
                    log_info "Removed recorded artifact ($(display_path "${artifact_paths[$index]}"))"
                fi
                ;;
        esac
    done
}

for_each_shell_line() {
    local operation="$1"
    local line
    local description

    [[ -n "$shell_name" ]] || return 0
    set_shell_lines
    for line in "$shell_path_line" "$shell_init_line"; do
        if [[ "$line" == "$shell_path_line" ]]; then
            [[ "$shell_path_line_added" == "1" ]] || continue
            description="Remove AutoCAB-added PATH line"
        else
            [[ "$shell_init_line_added" == "1" ]] || continue
            description="Remove AutoCAB-added Atuin initialization line"
        fi
        check_shell_line "$line"
        if [[ "$operation" == "plan" ]]; then
            log_check "$description" "$shell_configuration_path"
        elif [[ "$check_status" == "manual_review" ]]; then
            fail "Shell configuration changed after planning."
        fi
    done
}

print_plan() {
    plan_ready_count=0
    plan_manual_count=0
    for_each_artifact plan
    for_each_shell_line plan
    if (( plan_ready_count == 0 && plan_manual_count == 0 )); then
        log_info "No AutoCAB-owned dependency artifacts remain."
    fi
}

remove_shell_lines() {
    local temporary_file
    local backup_path

    [[ -n "$shell_name" && -f "$shell_configuration_path" ]] || return 0
    if ! { [[ "$shell_path_line_added" == "1" ]] && \
        grep -Fqx "$shell_path_line" "$shell_configuration_path"; } && \
        ! { [[ "$shell_init_line_added" == "1" ]] && \
        grep -Fqx "$shell_init_line" "$shell_configuration_path"; }; then
        return 0
    fi

    backup_path="${shell_configuration_path}.autocab-backup-$(date '+%Y%m%d%H%M%S')"
    cp -p -- "$shell_configuration_path" "$backup_path"
    temporary_file="$(mktemp "${shell_configuration_path}.autocab.XXXXXX")"
    awk \
        -v path_line="$shell_path_line" -v init_line="$shell_init_line" \
        -v remove_path="$shell_path_line_added" -v remove_init="$shell_init_line_added" \
        '!(remove_path == "1" && $0 == path_line) && !(remove_init == "1" && $0 == init_line)' \
        "$shell_configuration_path" > "$temporary_file"
    cat "$temporary_file" > "$shell_configuration_path"
    rm -- "$temporary_file"
    log_info "Backed up and updated shell configuration ($(display_path "$shell_configuration_path"))"
}

main() {
    local apply_requested="false"

    if [[ $# -eq 2 && "$1" == "apply" && "$2" == "$CONFIRMATION_PHRASE" ]]; then
        apply_requested="true"
    elif [[ $# -ne 0 ]]; then
        fail "Usage: uninstall_dependencies.sh [apply \"$CONFIRMATION_PHRASE\"]"
    fi
    if ! path_exists "$RECEIPT_PATH"; then
        log_info "No active AutoCAB activity-tracking installation receipt exists."
        return
    fi
    load_receipt
    print_plan

    if [[ "$apply_requested" == "false" ]]; then
        log_info "Dry run complete. No files were changed."
        (( plan_manual_count == 0 ))
        return
    fi

    (( plan_manual_count == 0 )) || \
        fail "Manual-review artifacts prevent automatic uninstall."
    for_each_artifact validate
    for_each_shell_line validate
    for_each_artifact remove
    remove_shell_lines
    rm -- "$RECEIPT_PATH"
    log_info "Uninstall verified. The AutoCAB ownership receipt was removed."
}

main "$@"
