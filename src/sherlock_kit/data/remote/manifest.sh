# shk pull: read DIR/SHA256SUMS (read-only), SHK_ lines only.
# SHK_ERROR no_directory|no_manifest, or SHK_COUNT <regular files a complete manifest lists:
# none named SHA256SUMS, nothing below or named .shk*> and one SHK_M|<line> per manifest line
# (a blank line follows; the reader ignores it); always SHK_END last.
dir=$1
sums="$dir/SHA256SUMS"
if [ ! -d "$dir" ]; then
    printf "SHK_ERROR no_directory\n"
elif [ ! -f "$sums" ] || [ ! -r "$sums" ]; then
    printf "SHK_ERROR no_manifest\n"
else
    count=$(find "$dir/." -name ".shk*" -prune -o -type f ! -name SHA256SUMS -print | wc -l)
    printf "SHK_COUNT %d\n" "$((count))"
    sed "s/^/SHK_M|/" "$sums" || exit 1
    printf "\n"
fi
printf "SHK_END\n"
