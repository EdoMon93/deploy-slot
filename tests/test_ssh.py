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


class SSHTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sshd = shutil.which("sshd") or "/usr/sbin/sshd"
        if not Path(cls.sshd).is_file():
            raise unittest.SkipTest("OpenSSH server is required for SSH integration tests")
        cls.directory = tempfile.TemporaryDirectory(prefix="deploy-slot-ssh-")
        cls.addClassCleanup(cls.directory.cleanup)
        cls.root = Path(cls.directory.name)
        cls.state = cls.root / "state"
        cls.state.mkdir()
        cls.username = pwd.getpwuid(os.getuid()).pw_name
        for name in ("host-key", "client-key"):
            subprocess.run(
                ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(cls.root / name)],
                check=True,
            )
        (cls.root / "authorized_keys").write_bytes((cls.root / "client-key.pub").read_bytes())
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            cls.port = listener.getsockname()[1]
        config = cls.root / "sshd_config"
        config.write_text(f"""Port {cls.port}
ListenAddress 127.0.0.1
HostKey {cls.root / 'host-key'}
PidFile {cls.root / 'sshd.pid'}
AuthorizedKeysFile {cls.root / 'authorized_keys'}
StrictModes no
UsePAM no
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin yes
AllowUsers {cls.username}
AcceptEnv CODEX_THREAD_ID CLAUDE_CODE_SESSION_ID
SetEnv DEPLOY_SLOT_STATE_DIR={cls.state}
LogLevel ERROR
""")
        cls.log = (cls.root / "sshd.log").open("w")
        cls.addClassCleanup(cls.log.close)
        cls.server = subprocess.Popen([cls.sshd, "-D", "-e", "-f", str(config)], stderr=cls.log)
        cls.addClassCleanup(cls.stop_server)
        cls.client_config = cls.root / "ssh_config"
        cls.client_config.write_text(f"""Host first-alias second-alias no-identity
    HostName 127.0.0.1
    Port {cls.port}
    User {cls.username}
    IdentityFile {cls.root / 'client-key'}
    IdentitiesOnly yes
    BatchMode yes
    StrictHostKeyChecking no
    UserKnownHostsFile /dev/null
    GlobalKnownHostsFile /dev/null
    LogLevel ERROR
Host first-alias second-alias
    SendEnv CODEX_THREAD_ID CLAUDE_CODE_SESSION_ID
""")
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if cls.server.poll() is not None:
                raise RuntimeError((cls.root / "sshd.log").read_text())
            try:
                with socket.create_connection(("127.0.0.1", cls.port), timeout=0.1):
                    break
            except OSError:
                time.sleep(0.05)
        else:
            raise RuntimeError("Temporary SSH server did not start")

    @classmethod
    def stop_server(cls):
        cls.server.terminate()
        cls.server.wait(timeout=5)

    def setUp(self):
        result = self.remote("first-alias", "release", "--force", owner=None)
        self.assertEqual(result.returncode, 0, result.stderr)

    def remote(self, host, *arguments, owner="codex:ssh-thread"):
        environment = os.environ.copy()
        for name in ("CODEX_THREAD_ID", "CLAUDE_CODE_SESSION_ID", "DEPLOY_SLOT_STATE_DIR"):
            environment.pop(name, None)
        if owner:
            provider, identity = owner.split(":", 1)
            variable = "CODEX_THREAD_ID" if provider == "codex" else "CLAUDE_CODE_SESSION_ID"
            environment[variable] = identity
        command = shlex.join([str(ROOT / "bin/deploy-slot"), *arguments])
        return subprocess.run(
            ["ssh", "-F", str(self.client_config), host, command],
            env=environment, text=True, capture_output=True, timeout=10,
        )

    def test_aliases_and_provider_identities_share_one_reservation(self):
        result = self.remote("first-alias", "reserve")
        self.assertEqual(result.returncode, 0, result.stderr)
        result = self.remote("second-alias", "reserve", owner="claude:another-session")
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("codex:ssh-thread", result.stderr)
        result = self.remote("second-alias", "check")
        self.assertEqual(result.returncode, 0, result.stderr)
        result = self.remote("second-alias", "check", owner="claude:another-session")
        self.assertEqual(result.returncode, 1, result.stderr)
        result = self.remote("second-alias", "release")
        self.assertEqual(result.returncode, 0, result.stderr)
        result = self.remote("second-alias", "reserve", owner="claude:another-session")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("claude:another-session", result.stdout)

    def test_missing_forwarding_fails_without_reserving(self):
        result = self.remote("no-identity", "reserve")
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("Missing session identity", result.stderr)
        result = self.remote("no-identity", "status", owner=None)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Available", result.stdout)

    def test_simultaneous_ssh_requests_have_one_winner(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as executor:
            results = list(executor.map(
                lambda index: self.remote(
                    "first-alias" if index % 2 else "second-alias",
                    "reserve", owner=f"codex:parallel-{index}",
                ),
                range(6),
            ))
        self.assertEqual(sum(result.returncode == 0 for result in results), 1, results)
        self.assertEqual(sum(result.returncode == 1 for result in results), 5, results)


if __name__ == "__main__":
    unittest.main()
