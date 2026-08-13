# ============================================================
#  Which language the .sh half speaks. Sets LC to "en" or "ru".
#
#  **Sourced, not run** - `. ./lang.sh` - because the whole point is
#  to set a variable in the caller. That is why there is no shebang
#  and no `set -e`: both belong to the caller's shell here, and
#  turning on -e for somebody else's script is not this file's call.
#
#  Same three sources as lang.bat and i18n.detect(), in the same
#  order of how deliberate each one is:
#    1. COMFYUI_LANG in the environment
#    2. COMFYUI_LANG in .env, where the settings windows write it
#    3. the usual locale variables
#  Anything unrecognised, and anything missing, means English.
#
#  There is no registry step, which is the one real difference from
#  lang.bat: on Unix the locale variables *are* the deliberate answer,
#  where on Windows they are unusual and the UI language is the truth.
# ============================================================

_comfyui_pick_lang() {
    local here raw
    here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)

    raw="${COMFYUI_LANG:-}"

    if [ -z "$raw" ] && [ -f "$here/.env" ]; then
        raw=$(sed -n 's/^[[:space:]]*COMFYUI_LANG[[:space:]]*=[[:space:]]*//p' "$here/.env" | tail -n 1)
        raw="${raw%$'\r'}"
        raw="${raw%\"}"; raw="${raw#\"}"
        raw="${raw%\'}"; raw="${raw#\'}"
    fi

    if [ -z "$raw" ]; then
        raw="${LANGUAGE:-${LC_ALL:-${LC_MESSAGES:-${LANG:-}}}}"
    fi

    raw="${raw%%:*}"
    raw="${raw%%.*}"
    raw=$(printf '%s' "$raw" | tr '[:upper:]' '[:lower:]')

    case "$raw" in
        ru|ru[-_]*|russian*) LC=ru ;;
        *) LC=en ;;
    esac
}

_comfyui_pick_lang
unset -f _comfyui_pick_lang
