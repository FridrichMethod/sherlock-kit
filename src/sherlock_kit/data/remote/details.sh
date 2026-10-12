# Read-only full own-UID queue/accounting evidence, plus queued physical job identities.
for v in $(env | sed -n "s/^\([A-Za-z_][A-Za-z0-9_]*\)=.*/\1/p"); do
    case $v in SLURM_CONF) ;; SBATCH_* | SQUEUE_* | SACCT_* | SLURM_*) unset "$v" ;; esac
done
mode=$1 since=$2
shift 2
u=$(id -un) uid=$(id -u)
printf "SHK_USER %s\nSHK_UID %s\nSHK_HOST %s\n" "$u" "$uid" "$(uname -n)"
specs() { # NAME RUN_DIR: count the spec files, print the newest NAME.<jobid>.json
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

selector=
if [ "$mode" = ids ]; then
    for id in "$@"; do selector=${selector:+$selector,}$id; done
    set -- "--jobs=$selector"
else
    while [ $# -ge 2 ]; do
        selector=${selector:+$selector,}$1
        [ "$2" = - ] || specs "$1" "$2"
        shift 2
    done
    if [ -n "$selector" ]; then set -- "--name=$selector"; else set --; fi
fi
out=$(env TZ=UTC LC_ALL=C SLURM_TIME_FORMAT=standard SLURM_BITSTR_LEN=0 squeue -h --local -t all -u "$u" "$@" -o "%i|%A|%T|%P|%M|%C|%V|%S|%e|%N|%U|%k|%j")
rc=$?
[ -z "$out" ] || printf "%s\n" "$out" | sed "s/^/SHK_Q|/"
printf "SHK_RC squeue %s\n" "$rc"
# scontrol supplies held request details absent from the portable squeue format.
qids=$(printf "%s\n" "$out" | cut -d "|" -f2 | sort -u)
jrc=0
for id in $qids; do
    case $id in "" | *[!0-9]*)
        jrc=1
        continue
        ;;
    esac
    job=$(scontrol show job "$id" --oneliner) || jrc=$?
    [ -z "$job" ] || printf "%s\n" "$job" | sed "s/^/SHK_J|/"
done
printf "SHK_RC scontrol %s\n" "$jrc"
out=$(env TZ=UTC LC_ALL=C SLURM_TIME_FORMAT=standard SLURM_BITSTR_LEN=0 sacct -n -P --local --allocations --duplicates -u "$u" "$@" -S "now-${since}days" -E now --format=JobID%8192,JobIDRaw%128,Cluster%256,DBIndex,State%64,Partition%128,ElapsedRaw,AllocCPUS,AllocTRES%4096,Submit,Start,End,Restarts,ExitCode,NodeList%4096,UID,Comment%4096,JobName%4096)
rc=$?
[ -z "$out" ] || printf "%s\n" "$out" | sed "s/^/SHK_A|/"
printf "SHK_RC sacct %s\nSHK_END\n" "$rc"
