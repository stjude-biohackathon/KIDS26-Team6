# AutoCAB Fish hook. The installer sources this file from config.fish.
# $CMD_DURATION provides elapsed milliseconds.

if set -q __WFREC_LOADED
    exit 0
end
set -g __WFREC_LOADED 1
status is-interactive; or exit 0

if not set -q WFREC_RUN
    set -gx WFREC_RUN "@WFREC_RUN@"
end

set -g __WFREC_RS (printf '\x1e')
set -g __WFREC_US (printf '\x1f')
set -g __WFREC_SEQ 0

function __wfrec_state
    test -e "$WFREC_RUN/active"; or return 1
    set -l line (string split -m 2 \t (head -n 1 "$WFREC_RUN/active" 2>/dev/null))
    test (count $line) -eq 3; or return 1
    set -g __WFREC_SID $line[1]
    set -g __WFREC_FLAGS $line[2]
    set -g __WFREC_SPOOL $line[3]
    test -n "$__WFREC_SID"; or return 1
    string match -q '*s*' -- $__WFREC_FLAGS; or return 1
    return 0
end

function __wfrec_preexec --on-event fish_preexec
    set -g __WFREC_CMD $argv[1]
end

function __wfrec_postexec --on-event fish_postexec
    set -l rc $status
    set -l dur $CMD_DURATION      # already milliseconds
    if not set -q __WFREC_CMD
        return $rc
    end
    set -l cmd $__WFREC_CMD
    set -e __WFREC_CMD
    if __wfrec_state
        set -g __WFREC_SEQ (math $__WFREC_SEQ + 1)
        set -l now (math (date +%s) x 1000)
        printf '%s%s\n' $__WFREC_RS \
            "cmd$__WFREC_US$now$__WFREC_US$PWD$__WFREC_US$rc$__WFREC_US$dur$__WFREC_US$cmd$__WFREC_US""fish$__WFREC_US$fish_pid$__WFREC_US$__WFREC_SEQ" \
            >> "$__WFREC_SPOOL/"(hostname -s 2>/dev/null; or echo host)"-$fish_pid.rec" 2>/dev/null
    end
    return $rc
end
