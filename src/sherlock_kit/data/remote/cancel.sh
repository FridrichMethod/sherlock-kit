# shk cancel NAME: list the live jobs of NAME, then scancel -u USER --name=NAME once.
# Prints SHK_JOB|<id> lines and exactly one of SHK_CANCELLED, SHK_NONE, SHK_REJECTED <rc>
# (+ SHK_ERR| lines) or SHK_REFUSED <why>; always exits 0.
for v in $(env | sed -n "s/^\([A-Za-z_][A-Za-z0-9_]*\)=.*/\1/p"); do
    case $v in SLURM_CONF) ;; SBATCH_*|SQUEUE_*|SACCT_*|SLURM_*) unset "$v" ;; esac
done
u=$(id -un)
live=$(squeue -h -u "$u" --name="$1" -o "%i" </dev/null) || { printf "SHK_REFUSED squeue\n"; exit 0; }
[ -n "$live" ] || { printf "SHK_NONE\n"; exit 0; }
printf "%s\n" "$live" | sed "s/^/SHK_JOB|/"
err=$(scancel -u "$u" --name="$1" 2>&1 >/dev/null </dev/null); rc=$?
if [ "$rc" -eq 0 ]; then
    printf "SHK_CANCELLED\n"
else
    printf "SHK_REJECTED %s\n" "$rc"
    printf "%s\n" "$err" | head -c 2000 | sed "s/^/SHK_ERR|/"
fi
