# shk query: read-only; prints SHK_ lines only and always exits 0.
#   query.sh names SINCE OCC NAME RUN_DIR [NAME RUN_DIR]...   (RUN_DIR or OCC "-" when unused)
#   query.sh all SINCE -   |   query.sh occupancy - - PARTITION...
# The caller trusts the read only with SHK_END and SHK_RC 0 from every tool it needs.
for v in $(env | sed -n "s/^\([A-Za-z_][A-Za-z0-9_]*\)=.*/\1/p"); do
    case $v in SLURM_CONF) ;; SBATCH_*|SQUEUE_*|SACCT_*|SLURM_*) unset "$v" ;; esac
done
mode=$1 since=$2 occ=$3
shift 3
u=$(id -un)
printf "SHK_USER %s\nSHK_HOST %s\nSHK_NOW %s\n" "$u" "$(uname -n)" "$(date -u +%s)"

specs() {  # NAME RUN_DIR: count the spec files, print the newest NAME.<jobid>.json
    n=0 best=0
    for f in "$2/.shk/$1".*.json; do
        [ -f "$f" ] || continue
        n=$((n + 1)) id=${f%.json}
        id=${id##*.}
        case $id in "" | *[!0-9]*) ;; *) [ "$id" -le "$best" ] || best=$id ;; esac
    done
    printf "SHK_S|%s|%s\n" "$1" "$n"
    [ "$best" -eq 0 ] || printf "SHK_SPEC|%s|%s|%s\n" "$1" "$best" "$(head -c 65536 "$2/.shk/$1.$best.json")"
}

occupancy() {  # PARTITION...: fixed-width squeue -O rows of every user
    orc=0
    for p in "$@"; do
        out=$(env LC_ALL=C squeue -h -p "$p" -O "UserName:64,State:24,tres-alloc:128,TimeUsed:24,TimeLimit:24") || orc=$?
        [ -z "$out" ] || printf "%s\n" "$out" | sed "s/^/SHK_O|$p|/"
    done
    printf "SHK_RC occupancy %s\n" "$orc"
}

if [ "$mode" = occupancy ]; then
    occupancy "$@"
    printf "SHK_END\n"
    exit 0
fi
names=
while [ $# -ge 2 ]; do
    names=${names:+$names,}$1
    [ "$2" = - ] || specs "$1" "$2"
    shift 2
done
out=$(squeue -h -t all -u "$u" ${names:+"--name=$names"} -o "%i|%T|%r|%P|%j"); rc=$?
[ -z "$out" ] || printf "%s\n" "$out" | sed "s/^/SHK_Q|/"
printf "SHK_RC squeue %s\n" "$rc"
out=$(env TZ=UTC LC_ALL=C SLURM_TIME_FORMAT=standard sacct -n -P --local --allocations --duplicates -u "$u" \
    ${names:+"--name=$names"} -S "now-${since}days" -E now \
    --format=JobID,State,Partition,ElapsedRaw,AllocCPUS,AllocTRES,Submit,Restarts,ExitCode,JobName%128); rc=$?
[ -z "$out" ] || printf "%s\n" "$out" | head -n 50001 | sed "s/^/SHK_A|/"
printf "SHK_RC sacct %s\n" "$rc"
[ "$occ" = - ] || occupancy "$occ"
printf "SHK_END\n"
