import concurrent.futures
import os
from pathlib import Path
import pwd
import shlex
import shutil
import socket
import subprocess
import tempfile
import time
import unittest


ROOT = Path(__file__).resolve().parents[1]


class HostInstallationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.privileged = [] if os.geteuid() == 0 else ["sudo", "-n"]
        if cls.privileged and (
            not shutil.which("sudo") or subprocess.run(
                [*cls.privileged, "true"], capture_output=True
            ).returncode != 0
        ):
            raise unittest.SkipTest("Host installation tests need root or passwordless sudo; CI runs them")
        cls.directory = tempfile.TemporaryDirectory(prefix="deploy-slot-install-")
        cls.addClassCleanup(cls.directory.cleanup)
        cls.root = Path(cls.directory.name)
        cls.root.chmod(0o755)
        cls.prefix = cls.root / "prefix with spaces"
        cls.state = cls.root / "state with $ spaces"
        cls.group = f"deploy-slot-{os.getpid()}"
        cls.other_user = f"deployslot{os.getpid()}"
        cls.first_user = pwd.getpwuid(os.getuid()).pw_name
        cls.root_call(
            "useradd", "--no-create-home", "--shell", "/bin/bash",
            "--password", "*NP*", cls.other_user,
        )
        cls.addClassCleanup(cls.delete_user_and_group)
        cls.install()

    @classmethod
    def root_call(cls, *arguments, check=True):
        return subprocess.run(
            [*cls.privileged, *map(str, arguments)],
            text=True, capture_output=True, check=check,
        )

    @classmethod
    def delete_user_and_group(cls):
        cls.root_call("userdel", cls.other_user, check=False)
        cls.root_call("groupdel", cls.group, check=False)

    @classmethod
    def install(cls):
        return cls.root_call(
            "bash", ROOT / "install.sh", "--prefix", cls.prefix,
            "--state-dir", cls.state, "--group", cls.group,
            "--user", cls.first_user, "--user", cls.other_user,
        )

    def run_as(self, user, *arguments, owner="codex:first"):
        environment = [
            "env", "-u", "CODEX_THREAD_ID", "-u", "CLAUDE_CODE_SESSION_ID",
            "-u", "DEPLOY_SLOT_STATE_DIR",
        ]
        if owner:
            provider, identity = owner.split(":", 1)
            variable = "CODEX_THREAD_ID" if provider == "codex" else "CLAUDE_CODE_SESSION_ID"
            environment.append(f"{variable}={identity}")
        return self.root_call(
            "runuser", "-u", user, "--", *environment,
            self.prefix / "bin/deploy-slot", *arguments, check=False,
        )

    def setUp(self):
        result = self.run_as(self.first_user, "release", "--force", owner=None)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_different_unix_users_share_the_same_record_and_lock(self):
        first = self.run_as(self.first_user, "reserve")
        self.assertEqual(first.returncode, 0, first.stderr)
        other = self.run_as(self.other_user, "status", owner=None)
        self.assertEqual(other.returncode, 0, other.stderr)
        self.assertIn("codex:first", other.stdout)
        other = self.run_as(self.other_user, "reserve", owner="claude:second")
        self.assertEqual(other.returncode, 1, other.stderr)
        other = self.run_as(self.other_user, "release", owner="claude:second")
        self.assertEqual(other.returncode, 1, other.stderr)
        other = self.run_as(self.other_user, "release", "--force", owner=None)
        self.assertEqual(other.returncode, 0, other.stderr)
        other = self.run_as(self.other_user, "reserve", owner="claude:second")
        self.assertEqual(other.returncode, 0, other.stderr)
        first = self.run_as(self.first_user, "status", owner=None)
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertIn("claude:second", first.stdout)

    def test_reinstall_preserves_active_reservation_and_lock_inode(self):
        self.assertEqual(self.run_as(self.first_user, "reserve").returncode, 0)
        record = self.root_call("cat", self.state / "reservation").stdout
        inode = self.root_call("stat", "-c", "%i", self.state / "lock").stdout
        self.install()
        self.assertEqual(self.root_call("cat", self.state / "reservation").stdout, record)
        self.assertEqual(self.root_call("stat", "-c", "%i", self.state / "lock").stdout, inode)
        other = self.run_as(self.other_user, "reserve", owner="claude:second")
        self.assertEqual(other.returncode, 1, other.stderr)

    def test_installed_skill_installer_finds_its_bundled_assets(self):
        repository = self.root / "repo"
        repository.mkdir()
        subprocess.run(["git", "init", "-q", str(repository)], check=True)
        result = subprocess.run(
            [str(self.prefix / "bin/deploy-slot"), "install-skill", str(repository)],
            text=True, capture_output=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        skill = repository / ".claude/skills/deploy-slot/SKILL.md"
        self.assertEqual(skill.read_bytes(), (ROOT / "skills/deploy-slot/SKILL.md").read_bytes())

    def test_two_ssh_users_and_aliases_cannot_reserve_independently(self):
        ssh_root = self.root / "ssh"
        ssh_root.mkdir()
        ssh_root.chmod(0o755)
        for name in ("host-key", "client-key"):
            subprocess.run(
                ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(ssh_root / name)],
                check=True,
            )
        authorized = ssh_root / "authorized_keys"
        authorized.write_bytes((ssh_root / "client-key.pub").read_bytes())
        authorized.chmod(0o644)
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        config = ssh_root / "sshd_config"
        config.write_text(f"""Port {port}
ListenAddress 127.0.0.1
HostKey {ssh_root / 'host-key'}
PidFile {ssh_root / 'sshd.pid'}
AuthorizedKeysFile {authorized}
StrictModes no
UsePAM no
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin yes
AllowUsers {self.first_user} {self.other_user}
AcceptEnv CODEX_THREAD_ID CLAUDE_CODE_SESSION_ID
LogLevel ERROR
""")
        log_path = ssh_root / "sshd.log"
        with log_path.open("w") as log:
            server = subprocess.Popen(
                [*self.privileged, shutil.which("sshd") or "/usr/sbin/sshd", "-D", "-e", "-f", str(config)],
                stderr=log,
            )
            try:
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    if server.poll() is not None:
                        self.fail(log_path.read_text())
                    try:
                        with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                            break
                    except OSError:
                        time.sleep(0.05)
                else:
                    self.fail("Temporary privileged SSH server did not start")
                client = ssh_root / "ssh_config"
                client.write_text(f"""Host first-alias second-alias
    HostName 127.0.0.1
    Port {port}
    IdentityFile {ssh_root / 'client-key'}
    IdentitiesOnly yes
    BatchMode yes
    StrictHostKeyChecking no
    UserKnownHostsFile /dev/null
    GlobalKnownHostsFile /dev/null
    LogLevel ERROR
    SendEnv CODEX_THREAD_ID CLAUDE_CODE_SESSION_ID
Host first-alias
    User {self.first_user}
Host second-alias
    User {self.other_user}
""")

                def reserve(index):
                    environment = os.environ.copy()
                    environment.pop("CLAUDE_CODE_SESSION_ID", None)
                    environment["CODEX_THREAD_ID"] = f"cross-user-{index}"
                    command = shlex.join([str(self.prefix / "bin/deploy-slot"), "reserve"])
                    return subprocess.run(
                        ["ssh", "-F", str(client), "first-alias" if index % 2 else "second-alias", command],
                        env=environment, text=True, capture_output=True, timeout=10,
                    )

                with concurrent.futures.ThreadPoolExecutor(max_workers=6) as executor:
                    results = list(executor.map(reserve, range(6)))
                self.assertEqual(sum(result.returncode == 0 for result in results), 1, results)
                self.assertEqual(sum(result.returncode == 1 for result in results), 5, results)
            finally:
                if server.poll() is None:
                    if self.privileged:
                        self.root_call("kill", "-TERM", (ssh_root / "sshd.pid").read_text().strip())
                    else:
                        server.terminate()
                    server.wait(timeout=5)


if __name__ == "__main__":
    unittest.main()
