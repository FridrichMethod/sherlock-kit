# shk resources PARTITION...: read-only Slurm node capacity; prints SHK_ lines and exits 0.
# OS FreeMem is preserved raw but never used for admission.
for v in $(env | sed -n "s/^\([A-Za-z_][A-Za-z0-9_]*\)=.*/\1/p"); do
    case $v in SLURM_CONF) ;; SBATCH_* | SQUEUE_* | SACCT_* | SLURM_*) unset "$v" ;; esac
done
out=$(env LC_ALL=C scontrol show nodes --oneliner)
rc=$?
wanted=
for p in "$@"; do wanted=${wanted:+$wanted|}$p; done
# Ship only the nodes whose Partitions= list names a requested partition, each once.
[ -z "$out" ] || printf "%s\n" "$out" | grep -E " Partitions=([^ ]*,)?($wanted)(,| |\$)" | sed "s/^/SHK_N|/"
printf "SHK_RC scontrol %s\nSHK_END\n" "$rc"
