#!/usr/bin/python3
"""Mirror bounded DataFlash snapshots to an explicitly provisioned controller."""
import contextlib
import json
import logging
import logging.handlers
import os
from pathlib import Path
import re
import shlex
import shutil
import stat
import subprocess
import tempfile
import time

LOG = logging.getLogger("dataflash-transfer")
STATE = Path("/etc/opensolo/log-transfer")
SOURCE = Path("/log/dataflash")
DESTINATION = "sololog@10.1.1.1:."


def log_files(directory):
    """Never follow a source symlink, including a numeric-looking log name."""
    return sorted((p for p in Path(directory).iterdir()
                   if re.fullmatch(r"[0-9]+\.BIN", p.name)
                   and stat.S_ISREG(p.lstat().st_mode)),
                  key=lambda p: int(p.stem))


def update_links(directory, link_directory):
    files = log_files(directory)
    for i in range(10):
        name = "RECENT.BIN" if i == 0 else "RECENT-%d.BIN" % i
        link = Path(link_directory) / name
        if link.is_symlink():
            link.unlink()
        elif link.exists():
            raise RuntimeError("refusing to replace non-link %s" % link)
        if i < len(files):
            link.symlink_to(os.path.relpath(files[-1-i], link.parent))


def transfer_credentials(state=STATE):
    """A provisioning marker alone never bypasses credential/path checks."""
    state = Path(state)
    for path in (state, state / "pair.json", state / "id_ed25519", state / "known_hosts"):
        mode = path.lstat()
        if path.is_symlink() or mode.st_uid != 0 or mode.st_mode & 0o022:
            raise RuntimeError("unsafe credential ownership or permissions: %s" % path)
    if (state / "id_ed25519").stat().st_mode & 0o077:
        raise RuntimeError("private key must be mode 0600")
    pair = json.loads((state / "pair.json").read_text())
    if pair != {"enabled": True, "destination": DESTINATION}:
        raise RuntimeError("invalid transfer pairing configuration")
    known = (state / "known_hosts").read_text().split()
    if len(known) != 3 or known[:2] != ["10.1.1.1", "ssh-ed25519"]:
        raise RuntimeError("controller host identity is missing or invalid")
    return state / "id_ed25519", state / "known_hosts"


@contextlib.contextmanager
def snapshot(directory):
    """Same-filesystem hardlinks keep pruning and source symlinks out of rsync.

    RECENT files are hardlinks in this temporary view, so the receiver never
    needs to accept navigable symlinks. File contents may grow while logging.
    """
    directory = Path(directory)
    staging = Path(tempfile.mkdtemp(prefix=".transfer-", dir=directory))
    try:
        files = log_files(directory)[-50:]
        for source in files:
            target = staging / source.name
            os.link(source, target, follow_symlinks=False)
            if not stat.S_ISREG(target.lstat().st_mode):
                raise RuntimeError("log changed to a symlink during snapshot")
        for i, source in enumerate(reversed(files[-10:])):
            name = "RECENT.BIN" if i == 0 else "RECENT-%d.BIN" % i
            os.link(staging / source.name, staging / name, follow_symlinks=False)
        yield staging
    finally:
        shutil.rmtree(staging)


def transfer_command(source, identity, known_hosts):
    ssh = shlex.join(["ssh", "-F", "/dev/null", "-T", "-o", "BatchMode=yes",
                      "-o", "StrictHostKeyChecking=yes", "-o", "IdentitiesOnly=yes",
                      "-o", "PasswordAuthentication=no", "-o", "KbdInteractiveAuthentication=no",
                      "-o", "GlobalKnownHostsFile=/dev/null", "-o", "UpdateHostKeys=no",
                      "-o", "ConnectTimeout=10", "-o", "ServerAliveInterval=15",
                      "-o", "ServerAliveCountMax=2", "-o", "UserKnownHostsFile=" + str(known_hosts),
                      "-i", str(identity)])
    return ["ionice", "-c", "3", "nice", "rsync", "-rt", "--size-only",
            "--partial", "--delete", "--chmod=Du=rwx,Dgo=rx,Fu=rw,Fgo=r",
            "--bwlimit=100", "--timeout=60", "-e", ssh, str(source) + "/", DESTINATION]


def main():
    logging.basicConfig(level=logging.INFO,
                        handlers=[logging.handlers.SysLogHandler(address="/dev/log")])
    SOURCE.mkdir(parents=True, exist_ok=True)
    # A power loss can leave a hardlink view behind. Only our private real
    # directories qualify for cleanup; other paths are never followed.
    for stale in SOURCE.glob(".transfer-*"):
        metadata = stale.lstat()
        if (stat.S_ISDIR(metadata.st_mode) and metadata.st_uid == os.getuid()
                and stat.S_IMODE(metadata.st_mode) == 0o700):
            shutil.rmtree(stale)
    last_error = None
    while True:
        try:
            for old in log_files(SOURCE)[:-50]:
                old.unlink()
            update_links(SOURCE, SOURCE.parent)
            identity, known_hosts = transfer_credentials()
            with snapshot(SOURCE) as staging:
                subprocess.run(transfer_command(staging, identity, known_hosts), check=True,
                               stdout=subprocess.DEVNULL, timeout=180)
            if last_error is not None:
                LOG.info("controller log transfer established")
            last_error = None
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
            message = str(error)
            if message != last_error:
                LOG.error("controller log transfer unavailable: %s", message)
            last_error = message
        time.sleep(10)


if __name__ == "__main__":
    main()
