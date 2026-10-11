# shk pull: read DIR/SHA256SUMS (read-only), SHK_ lines only.
# SHK_ERROR no_directory|no_manifest|too_large, or SHK_COUNT <regular files a complete
# manifest lists: none named SHA256SUMS, nothing below or named .shk*> and one SHK_M|<line>
# per manifest line (a blank line follows; the reader ignores it); always SHK_END last.
# The count stops at 10001: a manifest lists at most 10000 files.
dir=${1:?usage: manifest.sh DIR}
sums="$dir/SHA256SUMS"
if [ ! -d "$dir" ]; then
    printf "SHK_ERROR no_directory\n"
elif [ -L "$sums" ] || [ ! -f "$sums" ] || [ ! -r "$sums" ]; then
    printf "SHK_ERROR no_manifest\n"
elif [ "$(wc -c < "$sums")" -gt 4194304 ]; then
    printf "SHK_ERROR too_large\n"
else
    count=$(find "$dir/." -name ".shk*" -prune -o -type f ! -name SHA256SUMS -print | head -n 10001 | wc -l)
    printf "SHK_COUNT %d\n" "$((count))"
    sed "s/^/SHK_M|/" "$sums" || exit 1
    printf "\n"
fi
printf "SHK_END\n"
