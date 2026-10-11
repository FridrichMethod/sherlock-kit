# shk pull: read DIR/SHA256SUMS (read-only), SHK_ lines only.
# SHK_ERROR no_directory|no_manifest|too_large, or SHK_SHA256 <file digest>, SHK_COUNT <regular
# files a complete manifest lists: no SHA256SUMS, nothing named .shk*>, one SHK_M|<line> per
# manifest line (a blank line follows; the reader ignores it); always SHK_END last.
dir=${1:?usage: manifest.sh DIR}
sums="$dir/SHA256SUMS"
if [ ! -d "$dir" ]; then
    printf "SHK_ERROR no_directory\n"
elif [ -L "$sums" ] || [ ! -f "$sums" ] || [ ! -r "$sums" ]; then
    printf "SHK_ERROR no_manifest\n"
elif [ "$(wc -c < "$sums")" -gt 4194304 ]; then
    printf "SHK_ERROR too_large\n"
else
    digest=$(sha256sum < "$sums") || exit 1
    printf "SHK_SHA256 %s\n" "${digest%% *}"
    count=$(find "$dir/." -name ".shk*" -prune -o -type f ! -name SHA256SUMS -print | wc -l)
    printf "SHK_COUNT %d\n" "$((count))"
    sed "s/^/SHK_M|/" "$sums" || exit 1
    printf "\n"
fi
printf "SHK_END\n"
