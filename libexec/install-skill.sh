#!/usr/bin/env bash
set -euo pipefail

fail() {
    printf 'deploy-slot: %s\n' "$*" >&2
    exit 2
}

command -v git >/dev/null || fail 'git is required to install the skill into a repository.'
command -v flock >/dev/null || fail 'flock is required; install util-linux.'
repository="$(git -C "${1:-.}" rev-parse --show-toplevel 2>/dev/null)" || fail 'Choose a Git repository.'
repository="$(readlink -f -- "$repository")"
base="$(dirname -- "$(readlink -f -- "${BASH_SOURCE[0]}")")"
if [[ -f "$base/skills/deploy-slot/SKILL.md" ]]; then
    source_skill="$base/skills/deploy-slot/SKILL.md"
else
    source_skill="$base/../skills/deploy-slot/SKILL.md"
fi
[[ -r "$source_skill" ]] || fail 'Bundled skill is missing; reinstall deploy-slot.'

common_dir="$(git -C "$repository" rev-parse --path-format=absolute --git-common-dir)"
exec {install_lock}>>"$common_dir/deploy-slot-install.lock"
flock --exclusive "$install_lock"

codex_dir="$repository/.agents/skills/deploy-slot"
claude_dir="$repository/.claude/skills/deploy-slot"
link_target='../../.agents/skills/deploy-slot'
instruction='Before deploying, use the deploy-slot skill to reserve the destination box.'

for path in "$repository/.agents" "$repository/.agents/skills" "$codex_dir" \
            "$repository/.claude" "$repository/.claude/skills"; do
    resolved="$(readlink -m -- "$path")"
    [[ "$resolved" == "$repository/"* ]] || fail "Path points outside the repository: $path"
    [[ ! -e "$path" || -d "$path" ]] || fail "Expected a directory: $path"
done
if [[ -e "$codex_dir" || -L "$codex_dir" ]]; then
    [[ ! -L "$codex_dir" && ! -L "$codex_dir/SKILL.md" && -f "$codex_dir/SKILL.md" ]] ||
        fail "Existing skill conflicts: $codex_dir"
    cmp -s -- "$source_skill" "$codex_dir/SKILL.md" || fail "Existing skill differs; review it before replacing: $codex_dir"
fi
if [[ -e "$claude_dir" || -L "$claude_dir" ]]; then
    [[ -L "$claude_dir" && "$(readlink -- "$claude_dir")" == "$link_target" ]] ||
        fail "Existing Claude skill conflicts: $claude_dir"
fi
for name in AGENTS.md CLAUDE.md; do
    path="$repository/$name"
    resolved="$(readlink -m -- "$path")"
    [[ "$resolved" == "$repository/"* ]] || fail "Instruction file points outside the repository: $path"
    if [[ -e "$path" || -L "$path" ]]; then
        [[ -f "$path" && -w "$path" ]] || fail "Instruction file is not writable: $path"
    fi
done

mkdir -p -- "$codex_dir" "$repository/.claude/skills"
if [[ ! -e "$codex_dir/SKILL.md" ]]; then
    cp -- "$source_skill" "$codex_dir/SKILL.md"
fi
if [[ ! -L "$claude_dir" ]]; then
    ln -s -- "$link_target" "$claude_dir"
fi
for name in AGENTS.md CLAUDE.md; do
    path="$repository/$name"
    touch -- "$path"
    if ! grep -Fxq -- "$instruction" "$path"; then
        printf '\n%s\n' "$instruction" >> "$path"
    fi
done
printf 'Installed deploy-slot skill for Codex and Claude in %s.\n' "$repository"
printf 'Added deployment guidance to AGENTS.md and CLAUDE.md. Commit these files for future worktrees.\n'
