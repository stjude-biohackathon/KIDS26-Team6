# AutoCAB Bash hook. The installer sources this file from ~/.bashrc.
#
# The hook uses shell built-ins and `history 1` to record the full command.
# Each process writes to its own spool file and reads the active sentinel at
# every prompt. Capture stays responsive while the daemon restarts.

[ -n "${__WFREC_LOADED:-}" ] && return 0
__WFREC_LOADED=1
case $- in *i*) ;; *) return 0 ;; esac   # interactive shells only

: "${WFREC_RUN:=@WFREC_RUN@}"
export WFREC_RUN

__WFREC_RS=$'\x1e'
__WFREC_US=$'\x1f'
__WFREC_CMD=
__WFREC_T0=
__WFREC_ARMED=1
__WFREC_LAST_HIST=
__WFREC_SEQ=0

# Read the sentinel using only builtins. Returns 1 when we must not record.
# Absence of the file means "not recording", so stopping is a single unlink.
__wfrec_state() {
    [ -e "$WFREC_RUN/active" ] || return 1
    IFS=$'\t' read -r __WFREC_SID __WFREC_FLAGS __WFREC_SPOOL < "$WFREC_RUN/active" 2>/dev/null || return 1
    [ -n "$__WFREC_SID" ] || return 1
    case $__WFREC_FLAGS in *s*) return 0 ;; *) return 1 ;; esac
}

# Epoch milliseconds, no fork. EPOCHREALTIME is bash >= 5.0; older bash
# (RHEL 7/8 login nodes, macOS /bin/bash 3.2) degrades to whole seconds and the
# record reports its lower precision rather than pretending to have more.
if [ -n "${EPOCHREALTIME:-}" ]; then
    __WFREC_PRECISION=ms
    __wfrec_now_ms() {
        local whole=${EPOCHREALTIME%.*} frac=${EPOCHREALTIME#*.}
        frac=${frac}000
        __WFREC_NOW="${whole}${frac:0:3}"
    }
elif [ -n "${EPOCHSECONDS:-}" ]; then
    __WFREC_PRECISION=s
    __wfrec_now_ms() { __WFREC_NOW="${EPOCHSECONDS}000"; }
else
    __WFREC_PRECISION=none
    __wfrec_now_ms() { __WFREC_NOW=0; }
fi

__wfrec_preexec() {
    # The DEBUG trap also fires during programmable completion and for each
    # simple command inside a pipeline or loop. Arming in precmd and disarming
    # here collapses all of that to one record per prompt.
    [ -n "${COMP_LINE:-}" ] && return 0
    [ -z "$__WFREC_ARMED" ] && return 0
    [ "$BASH_COMMAND" = "${PROMPT_COMMAND:-}" ] && return 0
    case $BASH_COMMAND in __wfrec_*) return 0 ;; esac

    local hist
    hist=$(HISTTIMEFORMAT='' LC_ALL=C builtin history 1 2>/dev/null)
    # Bare Enter re-runs the hook without adding to history; comparing against
    # the previous entry keeps us from recording the same command twice.
    [ "$hist" = "$__WFREC_LAST_HIST" ] && return 0
    __WFREC_LAST_HIST=$hist

    # Strip `   123  ` -- leading blanks, the history number, then blanks.
    hist=${hist#"${hist%%[![:space:]]*}"}
    hist=${hist#"${hist%%[![:digit:]]*}"}
    hist=${hist#"${hist%%[![:space:]]*}"}
    [ -n "$hist" ] || hist=$BASH_COMMAND

    __WFREC_ARMED=
    __WFREC_CMD=$hist
    __wfrec_now_ms
    __WFREC_T0=$__WFREC_NOW
    return 0
}

__wfrec_precmd() {
    local rc=$?          # MUST be the first statement
    if [ -n "$__WFREC_CMD" ]; then
        local cmd=$__WFREC_CMD
        __WFREC_CMD=
        if __wfrec_state; then
            __wfrec_now_ms
            local dur=
            [ -n "$__WFREC_T0" ] && dur=$(( __WFREC_NOW - __WFREC_T0 ))
            __WFREC_SEQ=$(( __WFREC_SEQ + 1 ))
            printf '%s%s\n' "$__WFREC_RS" \
"cmd${__WFREC_US}${__WFREC_NOW}${__WFREC_US}${PWD}${__WFREC_US}${rc}${__WFREC_US}${dur}${__WFREC_US}${cmd}${__WFREC_US}bash${__WFREC_US}$$${__WFREC_US}${__WFREC_SEQ}" \
                >> "$__WFREC_SPOOL/${HOSTNAME%%.*}-$$.rec" 2>/dev/null
        fi
    fi
    __WFREC_ARMED=1
    return $rc           # never clobber the user's $? -- prompts depend on it
}

trap '__wfrec_preexec' DEBUG
case ";${PROMPT_COMMAND:-};" in
    *";__wfrec_precmd;"*) ;;
    *) PROMPT_COMMAND="__wfrec_precmd${PROMPT_COMMAND:+;$PROMPT_COMMAND}" ;;
esac
