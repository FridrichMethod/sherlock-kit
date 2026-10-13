# Fixed read-only local scheduler query; the caller supplies validated arguments.
for v in $(env | sed -n "s/^\([A-Za-z_][A-Za-z0-9_]*\)=.*/\1/p"); do
    case $v in SLURM_CONF) ;; SBATCH_* | SQUEUE_* | SACCT_* | SLURM_*) unset "$v" ;; esac
done
LC_ALL=C
export LC_ALL
case $1 in
job) output=$(scontrol show job "$2" --oneliner) ;;
step) output=$(scontrol show step "$2" --oneliner) ;;
node) output=$(scontrol show node "$2" --oneliner) ;;
hosts) output=$(scontrol show hostnames "$2") ;;
pidinfo) output=$(scontrol pidinfo "$2") ;;
listpids) output=$(scontrol listpids "$2") ;;
*) exit 1 ;;
esac
rc=$?
printf "%s\n" "$output" | sed "s/^/SHK_DATA|/"
printf "SHK_RC %s\nSHK_END\n" "$rc"
