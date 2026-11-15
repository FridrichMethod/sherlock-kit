# shk submit NAME RUN_DIR STAMP SHA256 SPEC_JSON -- sbatch ...   (job script on stdin)
# Refuses a live NAME, stages RUN_DIR/.shk/NAME.STAMP.{sh,json} and runs sbatch once on the staged
# script; prints exactly one marker and always exits 0. Before sbatch: SHK_LIVE <ids> or
# SHK_REFUSED <why> (nothing ran). After sbatch its stdout is the only evidence, whatever the exit
# code (with --wait that is the exit code of the job): a stdout that is, trailing newlines aside,
# the one line N or N;cluster with N positive and no leading zero prints SHK_SUBMITTED <jobid> and
# renames both files to NAME.<jobid>.*; anything else prints SHK_UNPARSED sbatch exit <rc>
# (+ SHK_ERR| lines) and keeps NAME.STAMP.{sh,json,err}, because the job may or may not exist.
for v in $(env | sed -n "s/^\([A-Za-z_][A-Za-z0-9_]*\)=.*/\1/p"); do
    case $v in SLURM_CONF) ;; SBATCH_* | SQUEUE_* | SACCT_* | SLURM_* | SHK_SUBMISSION_TOKEN) unset "$v" ;; esac
done
name=$1 run=$2 stamp=$3 sum=$4 spec=$5
base=$run/.shk/$name.$stamp
refuse() {
    rm -f "$base.sh" "$base.json"
    printf "SHK_REFUSED %s\n" "$1"
    exit 0
}
shift 6 # past "--": the rest is the sbatch command
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
job=${out%%;*} cluster=${out#"$job"}
case $job in "" | 0* | *[!0-9]*) job="" ;; esac
case $cluster in ";" | ";"*[!A-Za-z0-9_.-]*) job="" ;; esac
if [ -z "$job" ]; then
    printf "SHK_UNPARSED sbatch exit %s\n" "$rc"
    head -c 2000 "$base.err" | sed "s/^/SHK_ERR|/"
    exit 0
fi
rm -f "$base.err"
mv "$base.sh" "$run/.shk/$name.$job.sh"
mv "$base.json" "$run/.shk/$name.$job.json"
printf "SHK_SUBMITTED %s\n" "$job"
