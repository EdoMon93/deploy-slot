# deploy-slot

Reserve a deployment box before another agent starts deploying to it.

`deploy-slot` is a small Bash CLI installed on the destination. Run it with ordinary SSH:

```sh
ssh production deploy-slot reserve
ssh production deploy-slot status
ssh production deploy-slot release
```

Codex and Claude Code ownership comes from their existing session environment variables. There is no owner argument or token to retain. All SSH aliases and authorized users reaching the same box share one reservation.

Reservations are advisory. Agents build, test and wait for CI first, reserve when they start preparing the deployment, keep the reservation through verification or rollback, and release afterward. The tool does not wrap SSH, alter deployment scripts, detect running deployments, or expire reservations. It stops a deployment that bypasses it only if the deployment script calls `deploy-slot check` (see [Guard a deploy script](#guard-a-deploy-script)).

## Install on the deployment box

Requires Linux, Bash 4.4+, `flock` from util-linux, and standard GNU utilities. The installer also uses Linux user/group administration commands. Python is needed only for tests.

```sh
git clone https://github.com/EdoMon93/deploy-slot.git
cd deploy-slot
sudo bash install.sh --user deployer
```

The installer puts the executable and bundled skill under `/usr/local`, creates shared state at `/var/lib/deploy-slot`, and grants access through the `deploy-slot` group. Root can always access the state. Repeat `--user` to grant other SSH users access. Users added to the group need a new login; existing SSH multiplexed connections may need reconnecting too.

Reinstalling preserves the reservation and lock file. Use `--prefix`, `--state-dir`, or `--group` to customize installation. Every caller must use the same installed state directory. The installer does not change SSH configuration or restart services.

## Forward session identity

In the SSH client's host configuration, add:

```sshconfig
Host production
    SendEnv CODEX_THREAD_ID CLAUDE_CODE_SESSION_ID
```

On the destination, add to `/etc/ssh/sshd_config` or an included configuration file:

```sshconfig
AcceptEnv CODEX_THREAD_ID CLAUDE_CODE_SESSION_ID
```

Validate with `sudo sshd -t`, then reload the SSH service using the command appropriate for the host. Start a new SSH connection after changing server configuration. If the host uses connection multiplexing, reconnect the master connection too.

Codex provides `CODEX_THREAD_ID` in its shell environment. Current Claude Code supplies `CLAUDE_CODE_SESSION_ID` to Bash tool subprocesses. The destination detects whichever nonempty variable is present and records ownership as `codex:<id>` or `claude:<id>`. If both are present, it refuses an ambiguous identity. If neither is present, `reserve` and ordinary `release` fail. It never falls back to the SSH username or connection ID.

See the [Claude Code environment reference](https://code.claude.com/docs/en/env-vars), [OpenSSH SendEnv reference](https://man.openbsd.org/ssh_config#SendEnv), and [AcceptEnv reference](https://man.openbsd.org/sshd_config#AcceptEnv).

## Commands

| Command | Behavior |
| --- | --- |
| `deploy-slot reserve` | Atomically reserve this box. Repeating it as the owner preserves the original reservation. |
| `deploy-slot status` | Show the owner, reservation time in UTC, and SSH user, or `Available`. No session identity is required. |
| `deploy-slot check` | Succeed only if this session holds the reservation. Never creates one. |
| `deploy-slot release` | Release this session's reservation. Releasing an empty slot succeeds. |
| `deploy-slot release --force` | Clear an abandoned reservation without an owner check. Verify the previous deployment work has stopped first. |
| `deploy-slot install-skill [REPO]` | Install the agent guidance into a Git repository; defaults to the current repository. |

`reserve` and `release` exit with **1** when another session owns the box. `check` exits with **1** unless this session holds the reservation. Invalid input, missing identity, inaccessible state, or corrupt state produce an error and a nonzero exit; the ordinary CLI uses **2** for these errors. SSH transport errors have SSH's own exit status. A failed command does not establish availability. Checking status does not reserve the box.

Ownership follows the provider session ID. A new session or Claude `/clear` can change that ID while a reservation is held. Finish deployment work before changing sessions; otherwise, verify it has stopped before force-releasing the abandoned reservation.

## Guard a deploy script

A deploy script run over the same SSH connection inherits the forwarded session ID. One line at its top makes the reservation required for the step that changes the box:

```sh
deploy-slot check || exit
```

`exit` without an argument keeps the status of `check`: 1 when this session does not hold the reservation, 2 for a missing identity or other error. `sudo` drops the session variables by default. If the script runs under `sudo`, call `check` before it or keep the variables with `sudo --preserve-env=CODEX_THREAD_ID,CLAUDE_CODE_SESSION_ID`.

## Install the skill into a repo

From a checkout of this tool, run:

```sh
./bin/deploy-slot install-skill /path/to/project
```

Or use `deploy-slot install-skill` if the CLI is installed on the machine holding your repository. The installer does not make SSH connections or require reservation state.

It copies the bundled `SKILL.md` to `.agents/skills/deploy-slot`, adds a relative symlink under `.claude/skills`, and appends a one-line deployment instruction to `AGENTS.md` and `CLAUDE.md`. Existing instructions remain intact. Repeated installation is harmless; conflicting skills or paths pointing outside the repo are rejected before making changes. Review an existing differing skill before replacing it.

Commit the installed files so future worktrees and contributors discover the guidance. Existing worktrees need the commit or their own installation. The source skill is in [skills/deploy-slot/SKILL.md](skills/deploy-slot/SKILL.md).

## Development

```sh
bash -n bin/deploy-slot libexec/install-skill.sh install.sh
shellcheck bin/deploy-slot libexec/install-skill.sh install.sh
python3 -m unittest discover -s tests -v
```

Tests cover concurrent ownership, wrong-owner release, force-release, corrupt state, skill installation, and real SSH calls through different aliases. Host installation and cross-user SSH tests require root or passwordless sudo; CI runs them on an isolated runner. SSH tests use temporary keys and a loopback-only server.

`DEPLOY_SLOT_STATE_DIR` overrides the installed directory for isolated tests. Do not forward it through SSH or use per-user overrides on a shared deployment host.
