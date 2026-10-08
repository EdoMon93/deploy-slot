#!/usr/bin/env bash
set -euo pipefail

fail() {
    printf 'install.sh: %s\n' "$*" >&2
    exit 2
}

prefix=/usr/local
state_dir=/var/lib/deploy-slot
group=deploy-slot
users=()
while (($#)); do
    case "$1" in
        --prefix|--state-dir|--group|--user)
            (($# >= 2)) || fail "Missing value for $1"
            case "$1" in
                --prefix) prefix="$2" ;;
                --state-dir) state_dir="$2" ;;
                --group) group="$2" ;;
                --user) users+=("$2") ;;
            esac
            shift 2
            ;;
        --help|-h)
            printf 'Usage: sudo bash install.sh [--user SSH_USER]... [--prefix PATH] [--state-dir PATH] [--group GROUP]\n'
            exit 0
            ;;
        *) fail "Unknown argument: $1" ;;
    esac
done
((EUID == 0)) || fail 'Run this installer as root on the destination box.'
[[ "$prefix" == /* && "$state_dir" == /* ]] || fail 'Prefix and state directory must be absolute paths.'
[[ "$group" =~ ^[a-z_][a-z0-9_-]*$ ]] || fail 'Invalid group name.'
for command in flock install getent groupadd usermod readlink mktemp; do
    command -v "$command" >/dev/null || fail "Missing required command: $command"
done
for user in "${users[@]}"; do
    if [[ "$user" == -* ]] || ! id "$user" >/dev/null 2>&1; then
        fail "Unknown user: $user"
    fi
done
source_dir="$(dirname -- "$(readlink -f -- "${BASH_SOURCE[0]}")")"
getent group "$group" >/dev/null || groupadd --system "$group"
install -d -o root -g "$group" -m 2770 -- "$state_dir"
# Existing reservations and the lock inode must survive reinstalls.
exec {install_lock}>>"$state_dir/lock"
flock --exclusive "$install_lock"
chown root:"$group" "$state_dir/lock"
chmod 0660 "$state_dir/lock"
install -d -m 0755 -- "$prefix/bin" "$prefix/share/deploy-slot/skills/deploy-slot"
temporary="$(mktemp "$prefix/bin/.deploy-slot.XXXXXXXX")"
trap 'rm -f -- "$temporary"' EXIT
while IFS= read -r line || [[ -n "$line" ]]; do
    if [[ "$line" == default_state_dir=* ]]; then
        printf 'default_state_dir=%q\n' "$state_dir"
    else
        printf '%s\n' "$line"
    fi
done < "$source_dir/bin/deploy-slot" > "$temporary"
chmod 0755 "$temporary"
mv -f -- "$temporary" "$prefix/bin/deploy-slot"
install -m 0644 -- "$source_dir/libexec/install-skill.sh" "$prefix/share/deploy-slot/install-skill.sh"
install -m 0644 -- "$source_dir/skills/deploy-slot/SKILL.md" "$prefix/share/deploy-slot/skills/deploy-slot/SKILL.md"
for user in "${users[@]}"; do
    usermod -a -G "$group" "$user"
done
printf 'Installed %s/bin/deploy-slot; shared state: %s.\n' "$prefix" "$state_dir"
printf 'Users added to %s need a new login. SSH environment forwarding is a separate setup step; see README.md.\n' "$group"
