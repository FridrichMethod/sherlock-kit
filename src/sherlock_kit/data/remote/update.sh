# shk update N NAME TOKEN FIELD ARGS...: an identity guard, then one scontrol update of one field
# of a pending job or of a whole array (N is a job id or an ArrayJobId), then one re-read:
#   time M                                       TimeLimit=M (whole minutes, never more)
#   partition VALUE REQUEUE GPUS_OK SAME_CLASS   Partition=VALUE
#   features FEATURES PREFER                     Features= and/or Prefer= ("" when not given)
#   throttle M BORROWED                          ArrayTaskThrottle=M
# TOKEN is "" when not given. Every record of scontrol show job N is printed as SHK_BEFORE|. A
# failed check prints SHK_REFUSED <why> and a request that every record already shows prints
# SHK_UNCHANGED: nothing ran. Otherwise scontrol update runs once, never retried: SHK_DISPATCHED
# <rc>, its stdout as SHK_OUT| and stderr as SHK_ERR| lines, then each record of one re-read as
# SHK_AFTER| (or SHK_AFTER_FAILED <rc>). Slurm applies an update record by record and field by
# field without rollback, and its exit code tells neither way whether anything changed: only a
# comparison of the re-read with the request decides. Always exits 0.
job_id=$1 expected_name=$2 expected_token=$3 field=$4 value=${5:-}
for v in $(env | sed -n "s/^\([A-Za-z_][A-Za-z0-9_]*\)=.*/\1/p"); do
    case $v in SLURM_CONF) ;; SBATCH_* | SQUEUE_* | SACCT_* | SLURM_*) unset "$v" ;; esac
done
LC_ALL=C # the patterns below match bytes, whatever locale the login shell has
export LC_ALL
# Slurm text is cut to 1999 bytes plus LF (precision counts bytes under LC_ALL=C), so the
# line after it always starts a new line.
refuse() {
    printf "SHK_REFUSED %s\n" "$1"
    [ -z "${2:-}" ] || printf "%.1999s\n" "$2" | sed "s/^/SHK_ERR|/"
    exit 0
}
# word KEY TEXT: got is the value of the first word of TEXT (which starts with a space) that
# starts with KEY=, or empty.
word() {
    got=""
    case $2 in *" $1="*) got=${2#*" $1="} got=${got%%" "*} ;; esac
}
# minutes TEXT: mins is the [D-]HH:MM:SS of scontrol in whole minutes (seconds round up), or
# empty for any other text (UNLIMITED, Partition_Limit, N/A).
minutes() {
    mins="" days=0 clock=$1
    case $1 in *-*) days=${1%%-*} clock=${1#*-} ;; esac
    case $days in "" | *[!0-9]* | 0?*) return ;; esac
    case $clock in [0-9][0-9]:[0-5][0-9]:[0-5][0-9]) ;; *) return ;; esac
    hours=${clock%%:*} secs=${clock##*:} mins=${clock#*:} mins=${mins%:*}
    mins=$((days * 1440 + ${hours#0} * 60 + ${mins#0} + (${secs#0} > 0)))
}
# listed NAME LIST, subset A B (every member of A is in B), meets A B (some member of A is in
# B): on comma lists.
listed() { case ",$2," in *",$1,"*) ;; *) return 1 ;; esac }
subset() {
    set -- "$1," "$2"
    while [ -n "$1" ]; do
        listed "${1%%,*}" "$2" || return 1
        set -- "${1#*,}" "$2"
    done
}
meets() {
    set -- "$1," "$2"
    while [ -n "$1" ]; do
        ! listed "${1%%,*}" "$2" || return 0
        set -- "${1#*,}" "$2"
    done
    return 1
}
# Slurm prints JobId, the array identity, ArrayTaskThrottle and JobName first, the fixed fields
# next and free text (Command, SubmitLine, WorkDir, Comment, paths) last, with the optional
# TresPer* fields after it. Pin the leading fields, read fixed fields only between JobName and
# Command and Comment at its first occurrence, so a comment such as "rerun of ArrayJobId=400
# Partition=gpu" cannot shadow a real field.
parse() {
    lead=${1%%" JobName="*} array_id="" throttle_now=""
    case $lead in
    JobId=*" ArrayJobId="*" ArrayTaskId="*)
        actual_id=${lead%%" "*} actual_id=${actual_id#JobId=}
        array_id=${lead#*" ArrayJobId="} array_id=${array_id%%" "*}
        case $lead in *" ArrayTaskThrottle="*) throttle_now=${lead##*" ArrayTaskThrottle="} ;; esac
        ;;
    *) actual_id=${lead#JobId=} ;;
    esac
    rest=${1#"$lead JobName=$expected_name "} rest=" $rest"
    head=${rest%%" Command="*}
}
uid=$(id -u)
out=$(scontrol show job "$job_id" --oneliner 2>&1) || refuse query "$out"
# Only record lines count (stderr warnings are not records).
records=$(printf "%s\n" "$out" | grep "^JobId=")
[ -n "$records" ] || refuse ambiguous
printf "%s\n" "$records" | sed "s/^/SHK_BEFORE|/"
# The record loops read a here-document, so they run in this shell and a refusal ends it.
count=0 own="" arrays=0 members=0
while IFS= read -r record; do
    parse "$record"
    case $actual_id in "" | *[!0-9]*) refuse ambiguous ;; esac
    case $array_id$throttle_now in *[!0-9]*) refuse ambiguous ;; esac
    count=$((count + 1))
    [ "$actual_id" != "$job_id" ] || own=yes
    [ -z "$array_id" ] || arrays=$((arrays + 1))
    [ "$array_id" != "$job_id" ] || members=$((members + 1))
done <<EOF
$records
EOF
# N names one job (one record) or a whole array (every record has ArrayJobId=N), never the own
# id of a split array task.
[ -n "$own" ] || refuse id
if [ "$arrays" -gt 0 ]; then
    [ "$members" -eq "$count" ] || refuse id
else
    [ "$count" -eq 1 ] || refuse id
fi
case $field in
partition) requeue=$6 gpus_ok=$7 same_class=$8 ;;
features) features=$5 prefer=$6 ;;
throttle) borrowed=$6 ;;
time) ;;
*) refuse field ;;
esac
# Per record, in this order: identity, the pending state, then the checks of the field. A
# record that already shows the request leaves changed empty.
changed=""
check() {
    case $record in "$lead JobName=$expected_name UserId="*) ;; *) refuse name ;; esac
    word UserId "$head"
    got=${got##*(} got=${got%)}
    [ "$got" = "$uid" ] || refuse uid
    if [ -n "$expected_token" ]; then
        word Comment "$rest"
        [ "$got" = "$expected_token" ] || refuse token
    fi
    if [ "$field" != throttle ]; then
        word JobState "$head"
        [ "$got" = PENDING ] || refuse state
    fi
    case $field in
    time)
        word TimeLimit "$head"
        minutes "$got"
        [ -n "$mins" ] && [ "$value" -le "$mins" ] || refuse time
        [ "$value" -eq "$mins" ] || changed=yes
        word TimeMin "$head"
        if [ "$got" = N/A ]; then mins=0; else minutes "$got"; fi
        [ -n "$mins" ] && [ "$value" -ge "$mins" ] || refuse timemin
        ;;
    partition)
        word Requeue "$head"
        [ "$got" = "$requeue" ] || refuse requeue
        word Partition "$head"
        subset "$got" "$same_class" || refuse preemptible
        if ! subset "$value" "$got" || ! subset "$got" "$value"; then changed=yes; fi
        # Any GPU request word counts: free text can only add a refusal here, never hide one.
        if [ "$gpus_ok" != 1 ] && printf "%s\n" "$rest" | awk "{ for (i = 1; i <= NF; i++) if (\$i ~ /^(ReqTRES|TresPerJob|TresPerNode|TresPerSocket|TresPerTask)=/ && index(\$i, \"gres/gpu\")) found = 1 } END { exit !found }"; then
            refuse gpus
        fi
        ;;
    features)
        word Features "$head"
        [ "$got" != "(null)" ] || got=""
        [ -z "$features" ] || [ "$got" = "$features" ] || changed=yes
        word Prefer "$head"
        [ -z "$prefer" ] || [ "$got" = "$prefer" ] || changed=yes
        ;;
    throttle)
        [ "$arrays" -gt 0 ] || refuse array
        word Partition "$head"
        # An absent throttle is no limit: any value lowers it.
        if [ -n "$throttle_now" ] && [ "$value" -gt "$throttle_now" ] && meets "$got" "$borrowed"; then
            refuse borrowed
        fi
        [ "$throttle_now" = "$value" ] || changed=yes
        ;;
    esac
}
while IFS= read -r record; do
    parse "$record"
    check
done <<EOF
$records
EOF
# Each requested partition is read once; the time limit of every record must fit its MaxTime.
list=""
[ "$field" != partition ] || list=$value,
while [ -n "$list" ]; do
    member=${list%%,*} list=${list#*,}
    info=$(scontrol show partition "$member" --oneliner 2>&1) || refuse partition_query "$info"
    word MaxTime " $(printf "%s\n" "$info" | grep "^PartitionName=$member ")"
    [ "$got" != UNLIMITED ] || continue
    minutes "$got"
    [ -n "$mins" ] || refuse partition_query "$info"
    limit=$mins
    while IFS= read -r record; do
        parse "$record"
        word TimeLimit "$head"
        minutes "$got"
        [ -n "$mins" ] && [ "$mins" -le "$limit" ] || refuse maxtime
    done <<EOF
$records
EOF
done
if [ -z "$changed" ]; then
    printf "SHK_UNCHANGED\n"
    exit 0
fi
case $field in
time) set -- "TimeLimit=$value" ;;
partition) set -- "Partition=$value" ;;
throttle) set -- "ArrayTaskThrottle=$value" ;;
features)
    set --
    [ -z "$features" ] || set -- "Features=$features"
    [ -z "$prefer" ] || set -- "$@" "Prefer=$prefer"
    ;;
esac
# After dispatch nothing is retried. The inner capture takes the stderr of scontrol, the outer
# one (through fd 4) its stdout; fd 3 is the stdout of this program.
exec 3>&1
out=$(
    {
        err=$(scontrol update "JobId=$job_id" "$@" </dev/null 2>&1 >&4 4>&- 3>&-)
        printf "SHK_DISPATCHED %s\n" "$?" >&3
        [ -z "$err" ] || printf "%.1999s\n" "$err" | sed "s/^/SHK_ERR|/" >&3
    } 4>&1
)
[ -z "$out" ] || printf "%.1999s\n" "$out" | sed "s/^/SHK_OUT|/"
after=$(scontrol show job "$job_id" --oneliner 2>&1)
rc=$?
if [ "$rc" -ne 0 ]; then
    printf "SHK_AFTER_FAILED %s\n" "$rc"
    printf "%.1999s\n" "$after" | sed "s/^/SHK_ERR|/"
else
    printf "%s\n" "$after" | grep "^JobId=" | sed "s/^/SHK_AFTER|/"
fi
exit 0
