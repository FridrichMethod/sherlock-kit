# shk submit NAME RUN_DIR STAMP SHA256 SPEC_JSON -- sbatch ...   (job script on stdin)
# Refuses a live NAME, stages RUN_DIR/.shk/NAME.STAMP.{sh,json}, runs sbatch once on the staged
# script and renames both to NAME.<jobid>.*; prints exactly one of SHK_SUBMITTED <jobid>,
# SHK_LIVE <ids>, SHK_REJECTED <rc> (+ SHK_ERR| lines, files removed), SHK_UNPARSED (files
# kept) or SHK_REFUSED <why> (nothing ran); always exits 0.
for v in $(env | sed -n "s/^\([A-Za-z_][A-Za-z0-9_]*\)=.*/\1/p"); do
    case $v in SLURM_CONF) ;; SBATCH_* | SQUEUE_* | SACCT_* | SLURM_*) unset "$v" ;; esac
done
name=$1 run=$2 stamp=$3 sum=$4 spec=$5
base=$run/.shk/$name.$stamp
refuse() {
    rm -f "$base.sh" "$base.json"
    printf "SHK_REFUSED %s\n" "$1"
    exit 0
}
case $6:$7 in --:sbatch) shift 6 ;; *) refuse usage ;; esac
u=$(id -un)
live=$(squeue -h -u "$u" --name="$name" -o "%i" </dev/null) || refuse squeue
[ -z "$live" ] || {
    printf "SHK_LIVE %s\n" "$(printf "%s" "$live" | tr "\n" " ")"
    exit 0
}
mkdir -p "$run/.shk" || refuse mkdir
cat >"$base.sh" || refuse write
got=$(sha256sum <"$base.sh") || refuse digest
[ "${got%% *}" = "$sum" ] || refuse digest
printf "%s\n" "$spec" >"$base.json" || refuse write
out=$("$@" "$base.sh" 2>"$base.err" </dev/null)
rc=$?
if [ "$rc" -ne 0 ]; then
    printf "SHK_REJECTED %s\n" "$rc"
    head -c 2000 "$base.err" | sed "s/^/SHK_ERR|/"
    rm -f "$base.sh" "$base.json" "$base.err"
    exit 0
fi
rm -f "$base.err"
job=${out%%;*} cluster=${out#*;}
[ "$cluster" != "$out" ] || cluster=
case $job in "" | *[!0-9]*) job= ;; esac
case $cluster in *[!A-Za-z0-9_.-]*) job= ;; esac
if [ -z "$job" ]; then
    printf "SHK_UNPARSED\n"
    exit 0
fi
mv "$base.sh" "$run/.shk/$name.$job.sh"
mv "$base.json" "$run/.shk/$name.$job.json"
printf "SHK_SUBMITTED %s\n" "$job"
