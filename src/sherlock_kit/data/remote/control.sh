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
if [ "$action" = requeue ]; then
    # The meta physical id of an array also answers with every sibling: keep only this job.
    job=$(printf "%s\n" "$job" | awk -v id="JobId=$target" "\$1 == id")
fi
[ -n "$job" ] && [ "$(printf "%s\n" "$job" | wc -l)" -eq 1 ] || refuse ambiguous
field() { printf "%s\n" "$job" | sed -n "s/.*\(^\| \)$1=\([^ ]*\).*/\2/p"; }
actual_uid=$(field UserId)
actual_uid=${actual_uid##*(} actual_uid=${actual_uid%)}
[ "$actual_uid" = "$uid" ] || refuse uid
[ "$(field JobName)" = "$expected_name" ] || refuse name
if [ "$action" = release ] || [ -n "$expected_token" ]; then
    [ "$(field Comment)" = "$expected_token" ] || refuse token
fi
actual_id=$(field JobId)
array_id=$(field ArrayJobId) array_task=$(field ArrayTaskId)
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
    [ -z "$array_id" ] || target=${array_id}_$array_task
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
