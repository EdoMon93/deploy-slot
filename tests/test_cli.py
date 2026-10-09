import concurrent.futures
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "bin/deploy-slot"
IDENTITY_VARIABLES = ("CODEX_THREAD_ID", "CLAUDE_CODE_SESSION_ID")


class ReservationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.state = Path(self.directory.name)
        self.environment = os.environ.copy()
        for variable in IDENTITY_VARIABLES:
            self.environment.pop(variable, None)
        self.environment["DEPLOY_SLOT_STATE_DIR"] = str(self.state)

    def run_cli(self, *arguments, owner="codex:thread-a"):
        environment = self.environment.copy()
        if owner:
            provider, identity = owner.split(":", 1)
            variable = "CODEX_THREAD_ID" if provider == "codex" else "CLAUDE_CODE_SESSION_ID"
            environment[variable] = identity
        return subprocess.run(
            ["bash", str(CLI), *arguments], env=environment, text=True, capture_output=True
        )

    def test_lifecycle_with_both_providers(self):
        for owner in ("codex:thread-a", "claude:thread-b"):
            with self.subTest(owner=owner):
                result = self.run_cli("reserve", owner=owner)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn(owner, result.stdout)
                status = self.run_cli("status", owner=None)
                self.assertEqual(status.returncode, 0, status.stderr)
                self.assertIn(owner, status.stdout)
                self.assertIn("since", status.stdout)
                release = self.run_cli("release", owner=owner)
                self.assertEqual(release.returncode, 0, release.stderr)
                self.assertIn("Available", self.run_cli("status", owner=None).stdout)

    def test_busy_reservation_and_wrong_owner_release_preserve_state(self):
        self.assertEqual(self.run_cli("reserve").returncode, 0)
        before = (self.state / "reservation").read_bytes()
        for command in ("reserve", "release"):
            result = self.run_cli(command, owner="claude:other-thread")
            self.assertEqual(result.returncode, 1, result.stderr)
            self.assertIn("codex:thread-a", result.stderr)
            self.assertEqual((self.state / "reservation").read_bytes(), before)

    def test_repeated_reservation_preserves_original_record(self):
        self.assertEqual(self.run_cli("reserve").returncode, 0)
        before = (self.state / "reservation").read_bytes()
        again = self.run_cli("reserve")
        self.assertEqual(again.returncode, 0, again.stderr)
        self.assertEqual((self.state / "reservation").read_bytes(), before)

    def test_force_release_needs_no_agent_identity(self):
        self.assertEqual(self.run_cli("reserve").returncode, 0)
        self.assertEqual(self.run_cli("release", "--force", owner=None).returncode, 0)
        self.assertEqual(self.run_cli("reserve", owner="claude:new-owner").returncode, 0)
        self.assertEqual(self.run_cli("release").returncode, 1)
        self.assertIn("claude:new-owner", self.run_cli("status", owner=None).stdout)

    def test_check_passes_only_for_the_owner_and_never_reserves(self):
        result = self.run_cli("check")
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("Not reserved", result.stderr)
        self.assertFalse((self.state / "reservation").exists())
        self.assertEqual(self.run_cli("reserve").returncode, 0)
        before = (self.state / "reservation").read_bytes()
        result = self.run_cli("check")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("codex:thread-a", result.stdout)
        result = self.run_cli("check", owner="claude:other-thread")
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("codex:thread-a", result.stderr)
        self.assertEqual((self.state / "reservation").read_bytes(), before)

    def test_check_needs_an_identity(self):
        self.assertEqual(self.run_cli("reserve").returncode, 0)
        self.assertEqual(self.run_cli("check", owner=None).returncode, 2)

    def test_empty_release_is_idempotent(self):
        for _ in range(2):
            result = self.run_cli("release")
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_missing_or_ambiguous_identity_cannot_reserve(self):
        self.assertEqual(self.run_cli("reserve", owner=None).returncode, 2)
        environment = self.environment | {
            "CODEX_THREAD_ID": "one", "CLAUDE_CODE_SESSION_ID": "two"
        }
        result = subprocess.run(
            ["bash", str(CLI), "reserve"], env=environment, capture_output=True, text=True
        )
        self.assertEqual(result.returncode, 2)
        self.assertFalse((self.state / "reservation").exists())

    def test_invalid_identity_cannot_write_a_record(self):
        for identity in ("bad\nidentity", "bad identity", "x" * 257):
            result = self.run_cli("reserve", owner=f"codex:{identity}")
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertFalse((self.state / "reservation").exists())

    def test_simultaneous_reservations_have_exactly_one_winner(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=16) as executor:
            results = list(executor.map(
                lambda index: self.run_cli("reserve", owner=f"codex:thread-{index}"),
                range(24),
            ))
        self.assertEqual(sum(result.returncode == 0 for result in results), 1)
        self.assertEqual(sum(result.returncode == 1 for result in results), 23)
        winner = next(index for index, result in enumerate(results) if result.returncode == 0)
        self.assertIn(f"codex:thread-{winner} ", self.run_cli("status", owner=None).stdout)

    def test_corrupt_state_fails_closed(self):
        (self.state / "reservation").write_text("garbage\n")
        for command in ("reserve", "status", "check", "release"):
            result = self.run_cli(command)
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertNotIn("Available", result.stdout)
        self.assertEqual(self.run_cli("release", "--force", owner=None).returncode, 0)

    def test_unknown_arguments_do_not_change_state(self):
        for arguments in (("reserve", "some-box"), ("release", "--oops"), ("status", "--force"), ("check", "--force")):
            result = self.run_cli(*arguments)
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertFalse((self.state / "reservation").exists())

    def test_no_installation_fails_instead_of_reporting_available(self):
        self.environment["DEPLOY_SLOT_STATE_DIR"] = str(self.state / "missing")
        result = self.run_cli("status", owner=None)
        self.assertEqual(result.returncode, 2)
        self.assertNotIn("Available", result.stdout)

    def test_read_only_state_fails_instead_of_reporting_available(self):
        self.state.chmod(0o500)
        self.addCleanup(self.state.chmod, 0o700)
        result = self.run_cli("status", owner=None)
        self.assertEqual(result.returncode, 2)
        self.assertNotIn("Available", result.stdout)


class SkillInstallationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.repository = Path(self.directory.name) / "repo with spaces"
        self.repository.mkdir()
        subprocess.run(["git", "init", "-q", str(self.repository)], check=True)

    def install(self, path=None):
        return subprocess.run(
            ["bash", str(CLI), "install-skill", str(path or self.repository)],
            text=True, capture_output=True,
        )

    def test_installs_both_providers_and_preserves_existing_guidance(self):
        (self.repository / "AGENTS.md").write_text("Existing guidance.\n")
        result = self.install()
        self.assertEqual(result.returncode, 0, result.stderr)
        codex = self.repository / ".agents/skills/deploy-slot/SKILL.md"
        claude = self.repository / ".claude/skills/deploy-slot"
        self.assertEqual(codex.read_bytes(), (ROOT / "skills/deploy-slot/SKILL.md").read_bytes())
        self.assertTrue(claude.is_symlink())
        self.assertEqual((claude / "SKILL.md").read_bytes(), codex.read_bytes())
        self.assertTrue((self.repository / "AGENTS.md").read_text().startswith("Existing guidance.\n"))
        for name in ("AGENTS.md", "CLAUDE.md"):
            self.assertIn("deploy-slot", (self.repository / name).read_text())
        before = {name: (self.repository / name).read_bytes() for name in ("AGENTS.md", "CLAUDE.md")}
        self.assertEqual(self.install().returncode, 0)
        for name, content in before.items():
            self.assertEqual((self.repository / name).read_bytes(), content)

    def test_installation_from_a_subdirectory_uses_repo_root(self):
        child = self.repository / "src"
        child.mkdir()
        result = self.install(child)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.repository / ".agents/skills/deploy-slot/SKILL.md").exists())
        self.assertFalse((child / ".agents").exists())

    def test_conflicting_skill_is_not_overwritten_or_partially_installed(self):
        skill = self.repository / ".agents/skills/deploy-slot"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text("An unrelated skill.\n")
        result = self.install()
        self.assertEqual(result.returncode, 2)
        self.assertEqual((skill / "SKILL.md").read_text(), "An unrelated skill.\n")
        self.assertFalse((self.repository / ".claude").exists())
        self.assertFalse((self.repository / "AGENTS.md").exists())

    def test_external_skill_directory_symlink_is_not_followed(self):
        outside = Path(self.directory.name) / "outside"
        outside.mkdir()
        (self.repository / ".agents").symlink_to(outside, target_is_directory=True)
        result = self.install()
        self.assertEqual(result.returncode, 2)
        self.assertEqual(list(outside.iterdir()), [])
        self.assertFalse((self.repository / ".claude").exists())

    def test_external_instruction_symlink_is_not_modified(self):
        outside = Path(self.directory.name) / "outside.md"
        outside.write_text("Shared instructions.\n")
        (self.repository / "CLAUDE.md").symlink_to(outside)
        result = self.install()
        self.assertEqual(result.returncode, 2)
        self.assertEqual(outside.read_text(), "Shared instructions.\n")
        self.assertFalse((self.repository / ".agents").exists())

    def test_internal_instruction_symlink_is_preserved(self):
        (self.repository / "AGENTS.md").write_text("Existing.\n")
        (self.repository / "CLAUDE.md").symlink_to("AGENTS.md")
        result = self.install()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.repository / "CLAUDE.md").is_symlink())
        self.assertEqual((self.repository / "AGENTS.md").read_text().count("Before deploying"), 1)

    def test_non_git_directory_is_rejected_without_changes(self):
        outside = Path(self.directory.name) / "not a repo"
        outside.mkdir()
        result = self.install(outside)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(list(outside.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
