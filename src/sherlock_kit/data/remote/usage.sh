# shk usage: read-only Slurm accounting of one account; prints SHK_ lines only and always exits 0.
#   usage.sh ACCOUNT B0 B1 ... Bn   (n >= 1; each B is YYYY-MM-DD, the last may be now)
# Segment i (1..n) is [B(i-1), Bi). Records: SHK_USER, SHK_ASSOC (the associations of the user
# with ACCOUNT, 0 for an account that is not theirs), SHK_RC sacctmgr; per segment the sreport
# banner (SHK_W|i|) and rows (SHK_R|i|), then one SHK_RC sreport with the first nonzero rc;
# the runs of the user over [B0, Bn) (SHK_A|), SHK_RC sacct; SHK_END.
for v in $(env | sed -n "s/^\([A-Za-z_][A-Za-z0-9_]*\)=.*/\1/p"); do
    case $v in SLURM_CONF) ;; SBATCH_* | SQUEUE_* | SACCT_* | SLURM_*) unset "$v" ;; esac
done
LC_ALL=C
export LC_ALL
account=$1 first=$2
shift 2
u=$(id -un)
printf "SHK_USER %s\n" "$u"
out=$(sacctmgr -nP show assoc where user="$u" account="$account" format=account)
rc=$?
printf "SHK_ASSOC %s\nSHK_RC sacctmgr %s\n" "$(printf "%s\n" "$out" | grep -c .)" "$rc"
i=0 start=$first src=0
for end in "$@"; do
    i=$((i + 1))
    out=$(sreport -P -t seconds -T cpu,gres/gpu cluster AccountUtilizationByUser \
        "Accounts=$account" "Start=$start" "End=$end")
    rc=$?
    [ "$src" -ne 0 ] || src=$rc
    # The banner names the window sreport reported; every other line holding | but the header
    # is a row: one per TRES for the account (empty Login), then one per TRES for each user.
    [ -z "$out" ] || printf "%s\n" "$out" | while IFS= read -r line; do
        case $line in
        "Cluster|Account|"*) ;;
        *"|"*) printf "SHK_R|%s|%s\n" "$i" "$line" ;;
        *Utilization*) printf "SHK_W|%s|%s\n" "$i" "$line" ;;
        esac
    done
    start=$end
done
printf "SHK_RC sreport %s\n" "$src"
out=$(sacct -X -D -n -P -u "$u" -A "$account" -S "$first" -E "$end" \
    --format=JobIDRaw,State,AllocTRES,ElapsedRaw,Start,End)
rc=$?
[ -z "$out" ] || printf "%s\n" "$out" | sed "s/^/SHK_A|/"
printf "SHK_RC sacct %s\nSHK_END\n" "$rc"
