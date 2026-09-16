# wfrec remote hook -- strict POSIX sh, sourced from the remote ~/.bashrc and
# ~/.profile by `wfrec ssh`.
#
# This file must run under dash, ksh, BusyBox sh and bash 4.2 on a RHEL login
# node, so it uses none of the following: arrays, ${var//x/y}, [[ ]],
# EPOCHREALTIME, or $'...'. Duration therefore has whole-second resolution,
# which the record reports honestly via its own precision marker.
#
# The spool lives under $HOME deliberately -- the opposite of the local case.
# HPC login nodes are load-balanced, so /tmp on login2 is invisible from
# login1, whereas $HOME is shared. Multi-writer O_APPEND over NFS is unsafe,
# but this design has exactly one writer per file, which makes it safe.

[ -n "${__WFREC_LOADED:-}" ] && return 0
__WFREC_LOADED=1
case $- in *i*) ;; *) return 0 ;; esac

WFREC_RS=$(printf '\036')
WFREC_US=$(printf '\037')
WFREC_SEQ=0
WFREC_HOST=$(hostname 2>/dev/null | cut -d. -f1)
[ -n "$WFREC_HOST" ] || WFREC_HOST=remote

# WFREC_SESSION and WFREC_SPOOL are exported into the environment by the
# `wfrec ssh` wrapper, so the remote side needs no sentinel file of its own and
# no knowledge of the local runtime directory.
__wfrec_enabled() {
    [ -n "${WFREC_SESSION:-}" ] || return 1
    [ -n "${WFREC_SPOOL:-}" ] || return 1
    return 0
}

__wfrec_pre() {
    WFREC_T0=$(date +%s 2>/dev/null)
    WFREC_CMD=$1
}

__wfrec_post() {
    rc=$1
    __wfrec_enabled || return 0
    [ -n "${WFREC_CMD:-}" ] || return 0
    now=$(date +%s 2>/dev/null)
    dur=""
    if [ -n "${WFREC_T0:-}" ]; then
        dur=$(( (now - WFREC_T0) * 1000 ))
    fi
    WFREC_SEQ=$(( WFREC_SEQ + 1 ))
    mkdir -p "$WFREC_SPOOL" 2>/dev/null
    printf '%s%s\n' "$WFREC_RS" \
"cmd${WFREC_US}${now}000${WFREC_US}${PWD}${WFREC_US}${rc}${WFREC_US}${dur}${WFREC_US}${WFREC_CMD}${WFREC_US}sh${WFREC_US}$$${WFREC_US}${WFREC_SEQ}" \
        >> "$WFREC_SPOOL/${WFREC_HOST}-$$.rec" 2>/dev/null
    WFREC_CMD=""
}

# Scheduler wrappers. Capturing the job at *submit* time is essential: `scontrol
# show job` only knows a job while it is in the controller's memory (MinJobAge,
# typically ~5 minutes past completion), and it is what yields the real StdOut,
# StdErr and WorkDir paths instead of guessing slurm-%j.out.
sbatch() {
    out=$(command sbatch "$@" 2>&1); rc=$?
    printf '%s\n' "$out"
    case $out in
        *"Submitted batch job"*)
            jid=${out##* }
            if __wfrec_enabled; then
                detail=$(command scontrol show job "$jid" -o 2>/dev/null | tr '\036\037' '  ')
                mkdir -p "$WFREC_SPOOL" 2>/dev/null
                printf '%s%s\n' "$WFREC_RS" \
"job${WFREC_US}$(date +%s)000${WFREC_US}slurm${WFREC_US}${jid}${WFREC_US}${detail}" \
                    >> "$WFREC_SPOOL/${WFREC_HOST}-jobs.rec" 2>/dev/null
            fi
            ;;
    esac
    return $rc
}

# bash can hook prompts; a plain POSIX sh cannot, so under sh we still get the
# sbatch wrapper above even though per-command capture is unavailable.
if [ -n "${BASH_VERSION:-}" ]; then
    __wfrec_debug() {
        [ -n "${COMP_LINE:-}" ] && return 0
        [ -z "${__WFREC_ARMED:-}" ] && return 0
        case $BASH_COMMAND in __wfrec_*) return 0 ;; esac
        [ "$BASH_COMMAND" = "${PROMPT_COMMAND:-}" ] && return 0
        __WFREC_ARMED=
        __wfrec_pre "$BASH_COMMAND"
        return 0
    }
    __wfrec_prompt() {
        rc=$?
        __wfrec_post "$rc"
        __WFREC_ARMED=1
        return $rc
    }
    trap '__wfrec_debug' DEBUG
    case ";${PROMPT_COMMAND:-};" in
        *";__wfrec_prompt;"*) ;;
        *) PROMPT_COMMAND="__wfrec_prompt${PROMPT_COMMAND:+;$PROMPT_COMMAND}" ;;
    esac
    __WFREC_ARMED=1
fi
