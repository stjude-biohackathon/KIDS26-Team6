# wfrec zsh hook -- sourced from ~/.zshrc. See wfrec.bash for the design notes.
#
# zsh is the cleanest of the shells here: preexec/precmd are native hook
# functions rather than a DEBUG trap, so there is no per-pipeline-element
# double-fire to guard against, and $1 in preexec is the line as typed.

[[ -n ${__WFREC_LOADED:-} ]] && return 0
__WFREC_LOADED=1
[[ -o interactive ]] || return 0

: ${WFREC_RUN:=@WFREC_RUN@}
export WFREC_RUN

zmodload zsh/datetime 2>/dev/null
autoload -Uz add-zsh-hook 2>/dev/null || return 0

__WFREC_RS=$'\x1e'
__WFREC_US=$'\x1f'
__WFREC_CMD=""
__WFREC_T0=""
__WFREC_SEQ=0

__wfrec_state() {
    [[ -e $WFREC_RUN/active ]] || return 1
    local line
    IFS=$'\t' read -r __WFREC_SID __WFREC_FLAGS __WFREC_SPOOL < $WFREC_RUN/active 2>/dev/null || return 1
    [[ -n $__WFREC_SID ]] || return 1
    [[ $__WFREC_FLAGS == *s* ]] || return 1
    return 0
}

__wfrec_now_ms() {
    if [[ -n ${EPOCHREALTIME:-} ]]; then
        local whole=${EPOCHREALTIME%.*} frac=${EPOCHREALTIME#*.}
        frac="${frac}000"
        __WFREC_NOW="${whole}${frac[1,3]}"
    else
        __WFREC_NOW="$(( $(date +%s) * 1000 ))"
    fi
}

__wfrec_preexec() {
    # $1 is the command as typed; $3 is the fully expanded form. Record what the
    # analyst wrote, since that is what a skill draft should reproduce.
    __WFREC_CMD=$1
    __wfrec_now_ms
    __WFREC_T0=$__WFREC_NOW
}

__wfrec_precmd() {
    local rc=$?          # MUST be first
    if [[ -n $__WFREC_CMD ]]; then
        local cmd=$__WFREC_CMD
        __WFREC_CMD=""
        if __wfrec_state; then
            __wfrec_now_ms
            local dur=""
            [[ -n $__WFREC_T0 ]] && dur=$(( __WFREC_NOW - __WFREC_T0 ))
            __WFREC_SEQ=$(( __WFREC_SEQ + 1 ))
            print -rn -- "${__WFREC_RS}cmd${__WFREC_US}${__WFREC_NOW}${__WFREC_US}${PWD}${__WFREC_US}${rc}${__WFREC_US}${dur}${__WFREC_US}${cmd}${__WFREC_US}zsh${__WFREC_US}$$${__WFREC_US}${__WFREC_SEQ}"$'\n' \
                >> "${__WFREC_SPOOL}/${HOST%%.*}-$$.rec" 2>/dev/null
        fi
    fi
    return $rc
}

add-zsh-hook preexec __wfrec_preexec
add-zsh-hook precmd __wfrec_precmd
