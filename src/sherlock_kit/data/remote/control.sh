# Identity guard immediately before one exact Slurm mutation. No arbitrary self target: requeue
# acts only on this physical allocation, and on an array element as the ArrayJobId_ArrayTaskId
# in the Slurm record of this physical job (the bare id of the element that keeps the meta record
# of the array would act on every element; inherited SLURM_ARRAY_* variables can name another job).
action=$1 target=$2 expected_name=$3 expected_token=$4 requeue_partitions=${5:-}
self_id=${SLURM_JOB_ID:-}
for v in $(env | sed -n "s/^\([A-Za-z_][A-Za-z0-9_]*\)=.*/\1/p"); do
    case $v in SLURM_CONF) ;; SBATCH_* | SQUEUE_* | SACCT_* | SLURM_*) unset "$v" ;; esac
done
refuse() {
    printf "SHK_REFUSED %s\n" "$1"
    [ -z "${2:-}" ] || printf "%s\n" "$2" | head -c 2000 | sed "s/^/SHK_ERR|/"
    exit 0
}
uid=$(id -u)
if [ "$action" = requeue ]; then
    [ "$target" = "$self_id" ] || refuse allocation
    # listpids takes only the physical id and prints it as JOBID.
    pids=$(scontrol listpids "$self_id" 2>&1) || refuse listpids "$pids"
    printf "%s\n" "$pids" | awk -v pid="$$" -v job="$self_id" "\$1 == pid && \$2 == job { found=1 } END { exit !found }" || refuse process
fi
job=$(scontrol show job "$target" --oneliner 2>&1) || refuse query "$job"
# Only record lines count (stderr warnings are not records). The meta physical id of an array
# also answers with every sibling: requeue keeps only the record of this job.
if [ "$action" = requeue ]; then
    job=$(printf "%s\n" "$job" | awk -v id="JobId=$target" "\$1 == id")
else
    job=$(printf "%s\n" "$job" | grep "^JobId=")
fi
[ -n "$job" ] && [ "$(printf "%s\n" "$job" | wc -l)" -eq 1 ] || refuse ambiguous
# Slurm prints JobId, the array identity and JobName first and free text (SubmitLine, WorkDir,
# Comment, paths) last. Pin the leading fields, then read each key at its first occurrence, so
# a comment such as "rerun of ArrayJobId=400 ArrayTaskId=7" cannot shadow a real field.
lead=${job%%" JobName="*}
case $lead in
JobId=*" ArrayJobId="*" ArrayTaskId="*)
    actual_id=${lead%%" "*} actual_id=${actual_id#JobId=}
    array_id=${lead#*" ArrayJobId="} array_id=${array_id%%" "*}
    array_task=${lead##*" ArrayTaskId="}
    ;;
*) actual_id=${lead#JobId=} array_id="" array_task="" ;;
esac
case $actual_id$array_id in "" | *[!0-9]*) refuse ambiguous ;; esac
case $job in "$lead JobName=$expected_name UserId="*) ;; *) refuse name ;; esac
rest=${job#"$lead JobName=$expected_name "}
field() { printf "%s\n" "$rest" | awk -v key="$1=" "{ for (i = 1; i <= NF; i++) if (index(\$i, key) == 1) { print substr(\$i, length(key) + 1); exit } }"; }
actual_uid=$(field UserId)
actual_uid=${actual_uid##*(} actual_uid=${actual_uid%)}
[ "$actual_uid" = "$uid" ] || refuse uid
if [ "$action" = release ] || [ -n "$expected_token" ]; then
    [ "$(field Comment)" = "$expected_token" ] || refuse token
fi
if [ "$actual_id" != "$target" ]; then
    [ "${array_id}_${array_task}" = "$target" ] || refuse id
fi
if [ "$action" = release ]; then
    [ "$(field JobState)" = PENDING ] || refuse state
    [ "$(field Reason)" = JobHeldUser ] || refuse hold
    [ "$(field Priority)" = 0 ] || refuse priority
else
    [ "$action" = requeue ] || refuse action
    case ",$requeue_partitions," in *",$(field Partition),"*) ;; *) refuse partition ;; esac
    [ "$(field Requeue)" = 1 ] || refuse policy
    [ "$(field JobState)" = RUNNING ] || refuse state
    if [ -n "$array_id" ]; then
        case $array_task in "" | *[!0-9]*) refuse ambiguous ;; esac
        target=${array_id}_$array_task
    fi
fi
# After dispatch every nonzero or missing reply is ambiguous, never retry automatically.
err=$(scontrol "$action" "$target" 2>&1 >/dev/null)
rc=$?
if [ "$rc" -ne 0 ]; then
    printf "SHK_UNPARSED scontrol %s exit %s\n" "$action" "$rc"
    printf "%s\n" "$err" | head -c 2000 | sed "s/^/SHK_ERR|/"
    exit 0
fi
printf "SHK_CHANGED %s\n" "$target"
