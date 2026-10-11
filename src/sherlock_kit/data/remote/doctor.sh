# shk doctor --remote: read-only facts about the login environment, SHK_ lines only.
printf "SHK_HOST %s\n" "$(uname -n)"
printf "SHK_USER %s\n" "$(id -un)"
for tool in sbatch squeue sacct sha256sum; do
    printf "SHK_TOOL %s %s\n" "$tool" "$(command -v "$tool" || printf -)"
done
if [ -r /etc/agents/AGENTS.md ]; then site=readable; else site=missing; fi
printf "SHK_SITE %s\n" "$site"
printf "SHK_END\n"
