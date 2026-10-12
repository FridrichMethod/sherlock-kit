# Read-only capacity from Slurm; OS FreeMem is preserved raw but never used for admission.
for v in $(env | sed -n "s/^\([A-Za-z_][A-Za-z0-9_]*\)=.*/\1/p"); do
    case $v in SLURM_CONF) ;; SBATCH_* | SQUEUE_* | SACCT_* | SLURM_*) unset "$v" ;; esac
done
out=$(env LC_ALL=C scontrol show nodes --oneliner)
rc=$?
[ -z "$out" ] || printf "%s\n" "$out" | sed "s/^/SHK_N|/"
printf "SHK_RC scontrol %s\nSHK_END\n" "$rc"
