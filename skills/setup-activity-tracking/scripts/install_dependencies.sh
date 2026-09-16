#!/bin/bash

set -euo pipefail

SCRIPT_DIRECTORY="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly SCRIPT_DIRECTORY
# Referenced by the shared logger after it is sourced.
# shellcheck disable=SC2034
readonly LOG_MODULE="install_dependencies"
# shellcheck disable=SC1091
source "$SCRIPT_DIRECTORY/logging.sh"

readonly DEVSQL_INSTALLER_URL="https://github.com/douglance/devsql/releases/latest/download/devsql-installer.sh"
readonly ATUIN_INSTALLER_URL="https://github.com/atuinsh/atuin/releases/latest/download/atuin-installer.sh"
readonly STATE_DIRECTORY="${XDG_STATE_HOME:-$HOME/.local/state}/autocab"
readonly RECEIPT_PATH="$STATE_DIRECTORY/activity-tracking-install.receipt"
readonly CONFIG_DIRECTORY="${XDG_CONFIG_HOME:-$HOME/.config}"
readonly -a ARTIFACT_PATHS=(
    "$HOME/.cargo/bin/devsql"
    "$CONFIG_DIRECTORY/devsql/devsql-receipt.json"
    "$HOME/.atuin/bin/atuin"
    "$HOME/.atuin/bin/env"
    "$HOME/.atuin/bin/env.fish"
    "$CONFIG_DIRECTORY/atuin/atuin-receipt.json"
)
readonly -a ARTIFACT_COMPONENTS=(devsql devsql atuin atuin atuin atuin)

temporary_directory=""
receipt_tracking_enabled="false"
devsql_installed_by_autocab="false"
atuin_installed_by_autocab="false"
artifact_preexisting=()
shell_name=""
shell_configuration_path=""
shell_path_line=""
shell_init_line=""
shell_path_line_added="false"
shell_init_line_added="false"

path_exists() {
    [[ -e "$1" || -L "$1" ]]
}

hash_file() {
    if command -v sha256sum >/dev/null 2>&1; then
        sha256sum "$1" | awk '{print $1}'
    else
        shasum -a 256 "$1" | awk '{print $1}'
    fi
}

cleanup() {
    if [[ -n "$temporary_directory" && -d "$temporary_directory" ]]; then
        rm -rf -- "$temporary_directory"
    fi
}

component_is_owned() {
    [[ "$1" == "devsql" && "$devsql_installed_by_autocab" == "true" ]] || \
        [[ "$1" == "atuin" && "$atuin_installed_by_autocab" == "true" ]]
}

write_receipt() {
    local temporary_receipt
    local index
    local path

    mkdir -p "$STATE_DIRECTORY"
    chmod 0700 "$STATE_DIRECTORY"
    temporary_receipt="$(mktemp "$STATE_DIRECTORY/.activity-tracking.XXXXXX")"
    chmod 0600 "$temporary_receipt"
    printf 'schema|2\n' > "$temporary_receipt"

    for index in "${!ARTIFACT_PATHS[@]}"; do
        path="${ARTIFACT_PATHS[$index]}"
        component_is_owned "${ARTIFACT_COMPONENTS[$index]}" || continue
        [[ "${artifact_preexisting[$index]}" == "false" ]] || continue
        if [[ "$path" == *'|'* ]]; then
            rm -- "$temporary_receipt"
            return 1
        fi
        if [[ -L "$path" || ( -e "$path" && ! -f "$path" ) ]]; then
            rm -- "$temporary_receipt"
            return 1
        fi
        if [[ -f "$path" ]]; then
            printf 'artifact|%s|%s\n' "$path" "$(hash_file "$path")" \
                >> "$temporary_receipt"
        fi
    done

    if [[ "$shell_path_line_added" == "true" || "$shell_init_line_added" == "true" ]]; then
        if [[ "$shell_configuration_path" == *'|'* ]]; then
            rm -- "$temporary_receipt"
            return 1
        fi
        printf 'shell|%s|%s|%s|%s\n' \
            "$shell_name" "$shell_configuration_path" \
            "$([[ "$shell_path_line_added" == "true" ]] && printf 1 || printf 0)" \
            "$([[ "$shell_init_line_added" == "true" ]] && printf 1 || printf 0)" \
            >> "$temporary_receipt"
    fi
    mv -- "$temporary_receipt" "$RECEIPT_PATH"
}

finalize() {
    local exit_code="$?"

    trap - EXIT
    if [[ "$receipt_tracking_enabled" == "true" ]]; then
        if write_receipt; then
            log_info "Recorded AutoCAB installation ownership."
        else
            log_message "ERROR" \
                "Could not record installation ownership; manual review is required." \
                "${BASH_LINENO[0]}"
            exit_code=1
        fi
    fi
    cleanup
    exit "$exit_code"
}

installation_hint() {
    local command_name="$1"
    local package_name

    if command -v apt-get >/dev/null 2>&1; then
        package_name="$command_name"
        [[ "$command_name" == "xz" ]] && package_name="xz-utils"
        printf 'sudo apt-get install %s' "$package_name"
    elif command -v dnf >/dev/null 2>&1; then
        printf 'sudo dnf install %s' "$command_name"
    elif command -v pacman >/dev/null 2>&1; then
        printf 'sudo pacman -S %s' "$command_name"
    elif command -v brew >/dev/null 2>&1; then
        printf 'brew install %s' "$command_name"
    else
        printf 'install %s with the system package manager' "$command_name"
    fi
}

require_command() {
    command -v "$1" >/dev/null 2>&1 || \
        fail "Missing required command '$1'. Run: $(installation_hint "$1")"
}

download_installer() {
    curl --proto '=https' --tlsv1.2 --fail --silent --show-error \
        --location "$1" --output "$2"
    chmod 0700 "$2"
}

install_devsql() {
    local installer_path="$temporary_directory/devsql-installer.sh"

    if path_exists "${ARTIFACT_PATHS[0]}" || command -v devsql >/dev/null 2>&1; then
        log_info "DevSQL is already installed."
        return
    fi
    log_info "Installing DevSQL from its official GitHub release."
    download_installer "$DEVSQL_INSTALLER_URL" "$installer_path"
    devsql_installed_by_autocab="true"
    DEVSQL_INSTALL_DIR="$HOME/.cargo" DEVSQL_NO_MODIFY_PATH=1 \
        bash "$installer_path" --quiet
    [[ -x "${ARTIFACT_PATHS[0]}" ]] || fail "DevSQL installation did not create an executable."
}

install_atuin() {
    local installer_path="$temporary_directory/atuin-installer.sh"

    if path_exists "${ARTIFACT_PATHS[2]}" || command -v atuin >/dev/null 2>&1; then
        log_info "Atuin is already installed."
        return
    fi
    # The higher-level Atuin wrapper may install hooks for detected agents.
    log_info "Installing Atuin from its official GitHub release."
    download_installer "$ATUIN_INSTALLER_URL" "$installer_path"
    atuin_installed_by_autocab="true"
    ATUIN_NO_MODIFY_PATH=1 bash "$installer_path" --quiet
    [[ -x "${ARTIFACT_PATHS[2]}" ]] || fail "Atuin installation did not create an executable."
}

append_shell_line() {
    local configuration_path="$1"
    local configuration_line="$2"
    local search_text="$3"
    local line_kind="$4"

    mkdir -p "$(dirname "$configuration_path")"
    [[ ! -L "$configuration_path" ]] || \
        fail "Refusing to edit a symlinked shell configuration automatically."
    touch "$configuration_path"
    if ! grep -Fq "$search_text" "$configuration_path"; then
        printf '\n%s\n' "$configuration_line" >> "$configuration_path"
        if [[ "$line_kind" == "path" ]]; then
            shell_path_line_added="true"
        else
            shell_init_line_added="true"
        fi
    fi
}

configure_shell() {
    shell_name="$(basename "${SHELL:-}")"
    case "$shell_name" in
        bash | zsh)
            [[ "$shell_name" == "bash" ]] && \
                shell_configuration_path="$HOME/.bashrc" || \
                shell_configuration_path="${ZDOTDIR:-$HOME}/.zshrc"
            # shellcheck disable=SC2016
            shell_path_line='export PATH="$HOME/.cargo/bin:$HOME/.atuin/bin:$PATH"'
            shell_init_line="eval \"\$(atuin init $shell_name --disable-ai)\""
            ;;
        fish)
            shell_configuration_path="$HOME/.config/fish/config.fish"
            # shellcheck disable=SC2016
            shell_path_line='fish_add_path "$HOME/.cargo/bin" "$HOME/.atuin/bin"'
            shell_init_line='atuin init fish --disable-ai | source'
            ;;
        *)
            shell_name=""
            log_warning "Shell integration was not changed; supported shells are Bash, Zsh, and Fish."
            return
            ;;
    esac
    append_shell_line "$shell_configuration_path" "$shell_path_line" "$shell_path_line" "path"
    append_shell_line "$shell_configuration_path" "$shell_init_line" "atuin init $shell_name" "init"
}

main() {
    local command_name
    local index
    local platform_name

    log_info "Starting AutoCAB activity-tracking dependency setup."
    platform_name="$(uname -s)"
    [[ "$platform_name" == "Linux" || "$platform_name" == "Darwin" ]] || \
        fail "This installer supports Linux and macOS only."
    for command_name in bash curl grep tar xz awk; do
        require_command "$command_name"
    done
    if ! command -v sha256sum >/dev/null 2>&1 && \
        ! command -v shasum >/dev/null 2>&1; then
        fail "Missing SHA-256 utility. Install coreutils to provide sha256sum."
    fi
    [[ ! -e "$RECEIPT_PATH" && ! -L "$RECEIPT_PATH" ]] || \
        fail "An AutoCAB installation receipt already exists; uninstall before reinstalling."

    for index in "${!ARTIFACT_PATHS[@]}"; do
        if path_exists "${ARTIFACT_PATHS[$index]}"; then
            artifact_preexisting[index]="true"
        else
            artifact_preexisting[index]="false"
        fi
    done
    temporary_directory="$(mktemp -d)"
    receipt_tracking_enabled="true"
    trap finalize EXIT
    export PATH="$HOME/.cargo/bin:$HOME/.atuin/bin:$PATH"

    install_devsql
    install_atuin
    configure_shell
    log_info "Dependency setup complete. Open a new shell, then run verify_install.py."
    log_info "Atuin account setup, sync, history import, and agent hooks were not enabled."
}

main "$@"
