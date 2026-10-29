"""Offline contract tests. Requires ssh-keygen, RRSYNC_SOURCE and matching RSYNC_BINARY.

Run only after the combined source review gate. RRSYNC_SOURCE must be the
support/rrsync from the exact Yocto rsync source, not an invented receiver.
RSYNC_BINARY is the native3.5.1 binary: newer rrsync options require that version.
Only its executable-path constant is relocated in a disposable script copy.
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
from unittest import mock
from types import SimpleNamespace
import stat

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
        upstream = Path(os.environ["RRSYNC_SOURCE"]).resolve()
        binary = Path(os.environ["RSYNC_BINARY"]).resolve()
        self.assertTrue(upstream.is_file(), "use exact Yocto support/rrsync")
        self.assertTrue(binary.is_file(), "build the matching rsync-native first")
        version = subprocess.check_output([str(binary), "--version"], text=True)
        self.assertIn("version 3.5.1", version.splitlines()[0])
        # Preserve the upstream source; relocate just its installed executable
        # path for the native host fixture. All receiver logic/options are real.
        text = upstream.read_text()
        self.assertEqual(text.count("RSYNC = '/usr/bin/rsync'"), 1)
        self.rrsync = self.root / "rrsync"
        self.rrsync.write_text(text.replace("RSYNC = '/usr/bin/rsync'", "RSYNC = " + repr(str(binary)), 1))
        # Local transport shim receives the real producer's rsync SSH argv.
        # It passes the generated remote command to the real upstream receiver.
        self.bin = self.root / "bin"
        self.bin.mkdir()
        (self.bin / "rsync").symlink_to(binary)
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

    def test_cycle_real_exec_chain_fd_binding_and_invalid_source_preservation(self):
        log = self.root / "log"
        log.mkdir()
        self.src = log / "dataflash"
        self.src.mkdir()
        (self.src / "9.BIN").write_bytes(b"growing-flight")
        (self.dest / "old.BIN").write_bytes(b"stale")
        command = sender.transfer_command(self.src, self.root / "key", self.root / "known")
        env = dict(self.env, EXPECTED_SSH_COMMAND=command[command.index("-e") + 1])
        with mock.patch.dict(os.environ, env):
            with mock.patch.object(sender, "transfer_credentials", return_value=(self.root / "key", self.root / "known")):
                # Real ionice -> nice -> rsync -> local SSH shim -> upstream rrsync.
                sender.transfer_cycle(log, lambda _: ("mounted", log.stat().st_dev))
                self.assertEqual((self.dest / "9.BIN").read_bytes(), b"growing-flight")
                self.assertFalse((self.dest / "old.BIN").exists())
                before = {p.name: p.read_bytes() for p in self.dest.iterdir()}
                with self.assertRaises(RuntimeError):
                    sender.transfer_cycle(log, mock.Mock(side_effect=RuntimeError("mount lost")))
                self.assertEqual(before, {p.name: p.read_bytes() for p in self.dest.iterdir()})
                with (self.src / "9.BIN").open("ab") as stream:
                    stream.write(b"-completed")
                sender.transfer_cycle(log, lambda _: ("mounted", log.stat().st_dev))
                self.assertEqual((self.dest / "9.BIN").read_bytes(), b"growing-flight-completed")

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


class MountedSourceTests(unittest.TestCase):
    """Exercise the actual cycle; only the kernel mount observation is injected."""
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.log = Path(self.temp.name) / "log"
        self.log.mkdir()
        self.src = self.log / "dataflash"
        self.src.mkdir()
        for i in range(55):
            (self.src / (str(i) + ".BIN")).write_bytes(b"record")
        self.mount = ("mount-id", self.log.stat().st_dev)

    def test_mountinfo_parser_accepts_only_expected_whole_writable_filesystem(self):
        device = self.log.parent / "fake-device"
        info = self.log.parent / "mountinfo"
        dev = self.log.stat().st_dev
        major_minor = "%d:%d" % (os.major(dev), os.minor(dev))
        actual_stat = Path.stat
        def kernel_stat(path, *args, **kwargs):
            if path == device:
                return SimpleNamespace(st_mode=stat.S_IFBLK | 0o600, st_rdev=dev)
            return actual_stat(path, *args, **kwargs)
        good = "21 1 %s / %s rw,relatime - ext3 /dev/alias rw\n" % (major_minor, self.log)
        with mock.patch.object(Path, "stat", kernel_stat):
            info.write_text(good)
            self.assertEqual(sender.mounted_log(self.log, device, info), ("21", dev))
            for bad in ("", good + good, good.replace(major_minor, "999:999"),
                        good.replace(" - ext3 ", " - tmpfs "), good.replace(" / ", " /subdir "),
                        good.replace(" rw,relatime ", " ro,relatime ")):
                with self.subTest(mountinfo=bad):
                    info.write_text(bad)
                    with self.assertRaises(RuntimeError):
                        sender.mounted_log(self.log, device, info)

    def test_absent_mount_does_not_prune_initialize_or_transfer(self):
        missing = self.log / "absent"
        with mock.patch.object(sender, "transfer_credentials") as credentials:
            with self.assertRaises(RuntimeError):
                sender.transfer_cycle(missing, mock.Mock(side_effect=RuntimeError("unmounted")))
            credentials.assert_not_called()
        self.assertFalse(missing.exists())
        self.assertEqual(len(list(self.src.glob("*.BIN"))), 55)

    def test_symlink_paths_fail_before_mount_device_lookup(self):
        for path in (self.log / "alias", self.src):
            if path == self.src:
                self.src.rename(self.log / "real-source")
            path.symlink_to(self.log / "real-source")
            with self.assertRaises(RuntimeError):
                sender.mounted_log(path if path.name == "alias" else self.log)
            path.unlink()

    def test_mount_lost_before_prune_preserves_logs(self):
        check = mock.Mock(side_effect=[self.mount, self.mount, RuntimeError("unmounted")])
        with mock.patch.object(sender.subprocess, "run") as run:
            with self.assertRaises(RuntimeError):
                sender.transfer_cycle(self.log, check)
            run.assert_not_called()
        self.assertEqual(len(list(self.src.glob("*.BIN"))), 55)

    def test_mount_lost_before_transfer_never_calls_rsync(self):
        check = mock.Mock(side_effect=[self.mount, self.mount, self.mount, RuntimeError("unmounted")])
        with mock.patch.object(sender, "transfer_credentials", return_value=(Path("key"), Path("host"))):
            with mock.patch.object(sender.subprocess, "run") as run:
                with self.assertRaises(RuntimeError):
                    sender.transfer_cycle(self.log, check)
                run.assert_not_called()
        self.assertFalse(list(self.src.glob(".transfer-*")))

    def test_valid_cycle_prunes_stale_snapshot_and_binds_transfer(self):
        stale = self.src / ".transfer-abandoned"
        stale.mkdir(mode=0o700)
        (stale / "old").write_text("old")
        def inspect(command, **kwargs):
            source = Path(command[-2])
            self.assertEqual(len(list(source.glob("[0-9]*.BIN"))), 50)
            self.assertEqual((source / "RECENT.BIN").read_bytes(), b"record")
            self.assertTrue(kwargs["pass_fds"])
            self.assertIn("--delete", command)
        with mock.patch.object(sender, "transfer_credentials", return_value=(Path("key"), Path("host"))):
            with mock.patch.object(sender.subprocess, "run", side_effect=inspect):
                sender.transfer_cycle(self.log, lambda _: self.mount)
        self.assertFalse(stale.exists())
        self.assertEqual(os.readlink(self.log / "RECENT.BIN"), "dataflash/54.BIN")
        self.assertFalse(list(self.src.glob(".transfer-*")))

    def test_valid_empty_mount_still_transfers_and_interruption_cleans_snapshot(self):
        for path in self.src.iterdir():
            path.unlink()
        with mock.patch.object(sender, "transfer_credentials", return_value=(Path("key"), Path("host"))):
            with mock.patch.object(sender.subprocess, "run", side_effect=subprocess.TimeoutExpired("rsync", 180)) as run:
                with self.assertRaises(subprocess.TimeoutExpired):
                    sender.transfer_cycle(self.log, lambda _: self.mount)
                run.assert_called_once()
        self.assertFalse(list(self.src.iterdir()))


if __name__ == "__main__":
    unittest.main()
