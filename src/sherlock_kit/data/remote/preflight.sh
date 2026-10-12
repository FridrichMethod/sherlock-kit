# Read-only Slurm validation. Original script is stdin; no run directory or provenance.
for v in $(env | sed -n "s/^\([A-Za-z_][A-Za-z0-9_]*\)=.*/\1/p"); do
    case $v in SLURM_CONF) ;; SBATCH_* | SQUEUE_* | SACCT_* | SLURM_*) unset "$v" ;; esac
done
tmp=$(mktemp -d) || exit 1
trap "rm -f \"\$tmp/out\" \"\$tmp/err\"; rmdir \"\$tmp\"" EXIT HUP INT TERM
"$@" --test-only >"$tmp/out" 2>"$tmp/err"
rc=$?
od -An -v -tx1 "$tmp/out" | sed "s/^/SHK_OUTHEX|/"
od -An -v -tx1 "$tmp/err" | sed "s/^/SHK_ERRHEX|/"
printf "SHK_RC sbatch %s\nSHK_END\n" "$rc"
