#!/bin/sh
# 检查管理入口实际引用的模块；认证可能位于Vite拆分的共享块中。
set -eu

admin_root=$(readlink -f "${1:?需要管理端产物目录}")
entry=$(sed -n 's/.*src="\/kb\/admin\/assets\/\(main-[^"]*\.js\)".*/\1/p' "$admin_root/index.html" | head -n 1)
test -n "$entry"
test -f "$admin_root/assets/$entry"
scratch=$(mktemp -d)
trap 'rm -rf "$scratch"' EXIT HUP INT TERM
printf '%s\n' "$admin_root/assets/$entry" > "$scratch/pending"
: > "$scratch/visited"
login_found=false
settings_found=false

while test -s "$scratch/pending"; do
    module=$(head -n 1 "$scratch/pending")
    tail -n +2 "$scratch/pending" > "$scratch/next"
    mv "$scratch/next" "$scratch/pending"
    if grep -qxF "$module" "$scratch/visited"; then
        continue
    fi
    printf '%s\n' "$module" >> "$scratch/visited"
    test "$(wc -l < "$scratch/visited")" -le 512
    if grep -qF '/kb/admin/login' "$module"; then login_found=true; fi
    if grep -qF '/public-settings' "$module"; then settings_found=true; fi
    # 只跟随本地产物的JS路径，不将同目录里未被引用的旧文件计入检查。
    tr '"\047' '\n\n' < "$module" | grep -E '^[/A-Za-z0-9_.-]+\.js$' > "$scratch/references" || :
    while IFS= read -r reference; do
        case "$reference" in
            /kb/admin/*) candidate="$admin_root/${reference#/kb/admin/}" ;;
            assets/*) candidate="$admin_root/$reference" ;;
            *) candidate="$(dirname "$module")/$reference" ;;
        esac
        if test ! -f "$candidate"; then continue; fi
        resolved=$(readlink -f "$candidate")
        case "$resolved" in
            "$admin_root"/assets/*.js) printf '%s\n' "$resolved" >> "$scratch/pending" ;;
        esac
    done < "$scratch/references"
done

test "$login_found" = true
test "$settings_found" = true
