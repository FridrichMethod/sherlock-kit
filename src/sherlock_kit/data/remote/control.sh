# Identity guard immediately before one exact Slurm mutation. No arbitrary self target.
action=$1 target=$2 expected_name=$3 expected_token=$4
self_id=${SLURM_JOB_ID:-}
for v in $(env | sed -n "s/^\([A-Za-z_][A-Za-z0-9_]*\)=.*/\1/p"); do
    case $v in SLURM_CONF) ;; SBATCH_* | SQUEUE_* | SACCT_* | SLURM_*) unset "$v" ;; esac
done
refuse() {
    printf "SHK_REFUSED %s\n" "$1"
    exit 0
}
uid=$(id -u)
if [ "$action" = requeue ]; then
    [ "$target" = "$self_id" ] || refuse allocation
    pids=$(scontrol listpids "$target" 2>/dev/null) || refuse listpids
    printf "%s\n" "$pids" | awk -v pid="$$" -v job="$target" "\$1 == pid && \$2 == job { found=1 } END { exit !found }" || refuse process
fi
job=$(scontrol show job "$target" --oneliner 2>/dev/null) || refuse query
[ "$(printf "%s\n" "$job" | wc -l)" -eq 1 ] || refuse ambiguous
field() { printf "%s\n" "$job" | sed -n "s/.*\(^\| \)$1=\([^ ]*\).*/\2/p"; }
actual_uid=$(field UserId)
actual_uid=${actual_uid##*(} actual_uid=${actual_uid%)}
[ "$actual_uid" = "$uid" ] || refuse uid
[ "$(field JobName)" = "$expected_name" ] || refuse name
[ "$(field Comment)" = "$expected_token" ] || refuse token
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
    [ "$(field Partition)" = owners ] || refuse partition
    [ "$(field Requeue)" = 1 ] || refuse policy
    [ "$(field JobState)" = RUNNING ] || refuse state
    [ "$(field JobId)" = "$target" ] || refuse physical
fi
# After dispatch every nonzero or missing reply is ambiguous, never retry automatically.
scontrol "$action" "$target" >/dev/null 2>&1 || {
    printf "SHK_UNPARSED\n"
    exit 0
}
printf "SHK_CHANGED %s\n" "$target"
