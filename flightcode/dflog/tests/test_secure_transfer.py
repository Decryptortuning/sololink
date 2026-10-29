"""Offline contract tests. Requires rsync, ssh-keygen and RRSYNC_SOURCE.

Run only after the combined source review gate. RRSYNC_SOURCE must be the
support/rrsync from the exact Yocto rsync source, not an invented receiver.
No ssh connections, target devices or hardware are used.
"""
import importlib.machinery
import importlib.util
import os
from pathlib import Path
import shlex
import subprocess
import tempfile
import unittest

HERE = Path(__file__).resolve().parents[1]


def load(name, filename):
    loader = importlib.machinery.SourceFileLoader(name, str(HERE / filename))
    spec = importlib.util.spec_from_loader(name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


sender = load("sender", "dataFlashMAVLink-to-artoo.py")
provision = load("provision", "opensolo-log-provision")


class TransferTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.src = self.root / "source"
        self.dest = self.root / "receiver"
        self.src.mkdir()
        self.dest.mkdir()
        self.rrsync = Path(os.environ["RRSYNC_SOURCE"]).resolve()
        self.assertTrue(self.rrsync.is_file(), "use exact Yocto support/rrsync")
        # Local transport shim receives the real producer's rsync SSH argv.
        # It passes the generated remote command to the real upstream receiver.
        self.bin = self.root / "bin"
        self.bin.mkdir()
        ssh = self.bin / "ssh"
        ssh.write_text('''#!/usr/bin/python3
import os, shlex, subprocess, sys
args = sys.argv[1:]
# Match the complete transport option prefix emitted by transfer_command,
# followed by rsync's explicit login user/host. Reject unrelated invocations.
expected = shlex.split(os.environ["EXPECTED_SSH_COMMAND"])[1:]
expected += ["-l", "sololog", "10.1.1.1"]
if args[:len(expected)] != expected:
    raise SystemExit("unexpected local transport argv")
remote = args[len(expected):]
if remote[:2] != ["rsync", "--server"]:
    raise SystemExit("expected rsync receiver command")
os.environ["SSH_ORIGINAL_COMMAND"] = " ".join(remote)
sys.exit(subprocess.call([sys.executable, os.environ["RRSYNC_SOURCE"], "-wo", "-munge", os.environ["RECEIVER_ROOT"]]))
''')
        ssh.chmod(0o755)
        self.env = dict(os.environ, PATH=str(self.bin) + ":" + os.environ["PATH"],
                        RECEIVER_ROOT=str(self.dest), RRSYNC_SOURCE=str(self.rrsync))

    def sync(self, source):
        command = sender.transfer_command(source, self.root / "key", self.root / "known")
        env = dict(self.env, EXPECTED_SSH_COMMAND=command[command.index("-e") + 1])
        return subprocess.run(command[4:], env=env, capture_output=True, timeout=15)

    def test_actual_producer_partial_delete_and_recent_files(self):
        (self.src / "1.BIN").write_bytes(b"flight-one")
        (self.src / "2.BIN").write_bytes(b"flight-two")
        (self.dest / "stale.BIN").write_bytes(b"delete-in-scope")
        sentinel = self.root / "outside"
        sentinel.write_bytes(b"preserve")
        (self.src / "3.BIN").symlink_to(sentinel)
        with sender.snapshot(self.src) as staging:
            result = self.sync(staging)
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertFalse((self.dest / "stale.BIN").exists())
        self.assertFalse((self.dest / "3.BIN").exists())
        self.assertEqual((self.dest / "RECENT.BIN").read_bytes(), b"flight-two")
        self.assertFalse((self.dest / "RECENT.BIN").is_symlink())
        self.assertEqual(sentinel.read_bytes(), b"preserve")
        # A partial destination is replaced by the complete source next pass.
        (self.dest / "2.BIN").write_bytes(b"fli")
        with sender.snapshot(self.src) as staging:
            result = self.sync(staging)
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertEqual((self.dest / "2.BIN").read_bytes(), b"flight-two")

    def test_receiver_rejects_remote_shell_sender_escape_and_unsafe_options(self):
        for command in ("sh -c id", "rsync --server --sender -r . .",
                        "rsync --server -r --delete . ../outside",
                        "rsync --server -r --partial-dir=../../outside . .",
                        "rsync --server -r --no-munge-links . .",
                        "rsync --server -r . .;touch /tmp/opensolo-bad"):
            with self.subTest(command=command):
                result = subprocess.run(["python3", str(self.rrsync), "-wo", "-munge", str(self.dest)],
                                        env=dict(self.env, SSH_ORIGINAL_COMMAND=command),
                                        input=b"", capture_output=True, timeout=10)
                self.assertNotEqual(result.returncode, 0)

    def test_receiver_munges_uploaded_symlink(self):
        outside = self.root / "secret"
        outside.write_text("private")
        (self.src / "link").symlink_to(outside)
        command = sender.transfer_command(self.src, self.root / "key", self.root / "known")[4:]
        command[1] = "-rlt"
        env = dict(self.env, EXPECTED_SSH_COMMAND=command[command.index("-e") + 1])
        result = subprocess.run(command, env=env, capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertTrue((self.dest / "link").is_symlink())
        self.assertTrue(os.readlink(self.dest / "link").startswith("/rsyncd-munged/"))
        self.assertFalse((self.dest / "link").exists())
        self.assertEqual(outside.read_text(), "private")

    def test_unpaired_and_unsafe_credentials_fail_closed(self):
        with self.assertRaises(FileNotFoundError):
            sender.transfer_credentials(self.root / "missing")
        state = self.root / "state"
        state.symlink_to(self.src, target_is_directory=True)
        with self.assertRaises(RuntimeError):
            sender.transfer_credentials(state)

    def test_pinned_host_and_identity_options(self):
        command = sender.transfer_command(self.src, Path("/private/key"), Path("/trust/known"))
        ssh = shlex.split(command[command.index("-e") + 1])
        for option in ("StrictHostKeyChecking=yes", "IdentitiesOnly=yes", "BatchMode=yes",
                       "GlobalKnownHostsFile=/dev/null", "UpdateHostKeys=no"):
            self.assertIn(option, ssh)
        self.assertIn("/private/key", ssh)
        self.assertIn("UserKnownHostsFile=/trust/known", ssh)
        self.assertIn("--partial", command)
        self.assertIn("--delete", command)
        self.assertEqual(command[-1], "sololog@10.1.1.1:.")

    def test_pair_exact_identities_mismatch_and_symlink_rejection(self):
        drone, controller = self.root / "drone", self.root / "controller"
        drone.mkdir()
        controller.mkdir()
        drone_key = provision.identity(drone, "drone")
        host_key = provision.identity(controller, "controller")
        provision.pair(drone, controller)
        self.assertEqual((controller / "etc/opensolo/log-receiver/authorized_keys").read_text(),
                         "restrict " + drone_key + "\n")
        self.assertEqual((drone / "etc/opensolo/log-transfer/known_hosts").read_text(),
                         "10.1.1.1 " + host_key + "\n")
        public = controller / "etc/ssh/ssh_host_ed25519_key.pub"
        public.write_text(drone_key + "\n")
        with self.assertRaisesRegex(ValueError, "mismatch"):
            provision.pair(drone, controller)
        public.unlink()
        public.symlink_to(self.root / "outside")
        with self.assertRaisesRegex(ValueError, "symlink"):
            provision.pair(drone, controller)
        with self.assertRaises(ValueError):
            provision.pair(drone, drone)


if __name__ == "__main__":
    unittest.main()
