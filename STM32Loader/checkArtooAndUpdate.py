#!/usr/bin/env python3

import glob
import json
import tempfile
import serial
import slip
import os
import re
import subprocess
import sys
import time
import logging
import logging.config
import param_stored_vals_msg  # Used for reading stick calibration values
import struct  # Used for writing stick calibration values

sololink_conf = "/etc/sololink.conf"
logger = logging.getLogger("stm32")
UPDATE_DIR = "/log/updates"


class UpdateError(RuntimeError):
    """An attempted MCU update did not pass its completion checks."""


ARTOO_SYSINFO_ID = chr(0x3)
ARTOO_UPDATE_ID = chr(0x12)
ARTOO_LOCKOUT_ID = chr(0x13)
ARTOO_CALIBRATE_ID = chr(0x2)

ARTOO_UPDATE_SUCCESS = chr(1)
ARTOO_UPDATE_FAILED = chr(2)

ARTOO_LOCKOUT_FALSE = chr(0)
ARTOO_LOCKOUT_TRUE = chr(1)

ARTOO_BAUD = 115200

def _send_artoo(payload):
    serialport = serial.Serial("/dev/ttymxc1", ARTOO_BAUD, timeout=1)
    try:
        slip.SlipDevice(serialport).write(payload)
    finally:
        serialport.close()


# update_result should be either ARTOO_UPDATE_SUCCESS or ARTOO_UPDATE_FAILED
def setArtooUpdateComplete(update_result):
    _send_artoo(ARTOO_UPDATE_ID + update_result)


def setArtooLockout(lockout):
    _send_artoo(ARTOO_LOCKOUT_ID + lockout)

def mkdir_p(path):
    try:
        os.makedirs(path)
    except OSError:
        pass # already exists

def doUpdateComplete():
    setArtooLockout(ARTOO_LOCKOUT_FALSE)
    if not os.path.exists("/log/updates/READY"):
        if os.path.exists("/log/updates/UPDATEFAILED"):
            f = open("/log/updates/UPDATEFAILED", "r")
            # file should be one line; read it all
            r = f.read(1000)
            f.close()
            r = r.strip("\r\n\t\0 ")
            logger.info("request \"update failed\" screen (%s)", r)
            setArtooUpdateComplete(ARTOO_UPDATE_FAILED)
        else:
            logger.info("request \"update success\" screen")
            setArtooUpdateComplete(ARTOO_UPDATE_SUCCESS)
        mkdir_p("/log/updates")
        open("/log/updates/READY", "w").close() # "touch"
    else:
        logger.info("no screen update (READY exists)")

# return tuple (filename, version), or None
def getFirmwareInfo():
    files = glob.glob("/firmware/artoo_*.bin")
    if not files:
        return None
    candidates = [(filename, os.path.basename(filename)[len("artoo_"):-4].removeprefix("v"))
                  for filename in files]
    return max(candidates, key=lambda item: (_parse_semver(item[1]) or (-1, -1, -1), item[0]))

_SEMVER_RE = re.compile(r"(\d+)\.(\d+)\.(\d+)")


def _parse_semver(value):
    if not value:
        return None
    match = _SEMVER_RE.search(value)
    if not match:
        return None
    try:
        return tuple(int(part) for part in match.groups())
    except ValueError:
        return None


def _force_update_enabled():
    return os.getenv("ARTOO_FORCE_UPDATE") == "1" or os.path.exists("/log/updates/FORCE_UPDATE")

def _update_enabled():
    return os.getenv("ARTOO_ENABLE_UPDATE") == "1" or os.path.exists("/log/updates/ENABLE_UPDATE")


# return version as string ("unknown" if can't get version)
def getArtooVersion():
    #Check the version of the stm32 firmware over serial
    #The STM32 might be emitting packets already, so we try a few times to get the
    #version string. This has been observed to get the version with one retry on
    #several occasions.
    logger.info("requesting stm32 version")
    version = "unknown"
    ser = serial.Serial("/dev/ttymxc1", 115200, timeout=1)
    slipdev = slip.SlipDevice(ser)
    try:
        for i in range(5):
            slipdev.write("".join([ARTOO_SYSINFO_ID]))
            pkt = slipdev.read()
            if not pkt:
                logger.info("no data received from stm32, retrying")
                continue
            pkt = "".join(pkt)
            if pkt[0] == ARTOO_SYSINFO_ID and len(pkt) > 15:
                # SysInfo packet is:          artoo/src/hostprotocol.cpp
                # start size
                #   0     1  ARTOO_SYSINFO_ID artoo/src/hostprotocol.h
                #   1    12  UniqueId         artoo/src/stm32/sys.h
                #  13     2  hwversion        artoo/src/hostprotocol.cpp
                #  15   var  Version          artoo/src/version.h
                # Version may start with an initial 'v', e.g. v0.6.10,
                # but we want it starting with the numeric part.
                if pkt[15] != 'v':
                    version = pkt[15:]
                else:
                    version = pkt[16:]
                break
            logger.info("got %s/%d, retrying", str(hex(ord(pkt[0]))), len(pkt))
    finally:
        ser.close()
    return version

def validate_stick_cal(values):
    """Accept six ordered uint16 min/trim/max triples, never erased flash."""
    if not isinstance(values, (list, tuple)) or len(values) != 18:
        raise UpdateError("Missing or malformed stick calibration")
    if any(type(value) is not int or not 0 <= value < 0xffff for value in values):
        raise UpdateError("Invalid stick calibration values")
    for index in range(0, 18, 3):
        low, trim, high = values[index:index + 3]
        if not low <= trim <= high or low >= high:
            raise UpdateError("Invalid stick calibration range")
    return list(values)


def backup_stick_cal(filename, values):
    """Keep an immutable, durable calibration receipt before any programming."""
    values = validate_stick_cal(values)
    os.makedirs(UPDATE_DIR, exist_ok=True)
    fd, path = tempfile.mkstemp(prefix="artoo-calibration-", suffix=".json", dir=UPDATE_DIR)
    with os.fdopen(fd, "w") as stream:
        json.dump({"firmware": filename, "stick_calibration": values}, stream)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    directory_fd = os.open(UPDATE_DIR, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
    logger.info("Calibration backup saved to %s", path)
    return path


def updateStm32(filename):
    # Normal updates erase pages 0..126 and preserve the parameter page. Never
    # infer readout protection from a generic failure and escalate to mass erase.
    # Destructive recovery requires a separate, explicitly reviewed procedure.
    if not 0 < os.path.getsize(filename) <= 0x3f800:
        raise UpdateError("Firmware is empty or overlaps the reserved parameter page")
    stick_cal = validate_stick_cal(read_stick_cal())
    backup_stick_cal(filename, stick_cal)
    for attempt in range(4):
        logger.info("updating without readout protection disable (attempt %d)", attempt + 1)
        if call_stm32loader(filename):
            break
    else:
        raise UpdateError("STM32 loader failed after four attempts; no unprotect attempted")

    for attempt in range(3):
        try:
            new_cal = validate_stick_cal(read_stick_cal())
        except UpdateError:
            new_cal = None
        if new_cal == stick_cal:
            logger.info("Update complete; stick calibration verified")
            return True
        logger.warning("Stick calibration changed; restoring saved values")
        write_stick_cal(stick_cal)
        time.sleep(1)
    if validate_stick_cal(read_stick_cal()) != stick_cal:
        raise UpdateError("Stick calibration restoration/readback failed")
    logger.info("Update complete; restored stick calibration verified")
    return True

# Initiate the stm32loader process to actually update the STM32 chip. Return True if successful.
def call_stm32loader(filename):
    # Firmware/parameter-page preservation is mandatory in this boot path.
    flags = "-wvq"

    try:
        s = subprocess.check_output(["stm32loader.py", flags, "-s", "127",
                                     "-b", "115200", "-p", "/dev/ttymxc1", filename],
                                    stderr=subprocess.STDOUT)
        logger.info("stm32loader.py returned normally; output:")
        success = True
    except subprocess.CalledProcessError as cpe:
        s = cpe.output
        logger.info("stm32loader.py returned error; output:")
        success = False

    # this might be ugly, but it gets the output in the log
    if isinstance(s, bytes):
        s = s.decode("utf-8", errors="replace")
    s = (s or "").strip("\r\n\t\0 ")
    logger.info(s)
    # Wait a second for the STM32 to come back up before we send it a message later
    time.sleep(1)
    return success

def writeVersionFile(version):
    f = open("/STM_VERSION", 'w')
    f.write(version + '\n')
    f.close()

# return version from /STM_VERSION
def getVersionFile():
    try:
        f = open("/STM_VERSION", 'r')
        version = f.readline()
        f.close()
        version = version.strip()
        if version == "":
            version = "unknown"
    except:
        version = "unknown"
    return version


def read_stick_cal():
    # Updates run in runlevel 2, where the normal bridge is stopped. Own this
    # child directly so cleanup cannot leave a shell-owned bridge on the UART.
    process = subprocess.Popen(["stm32"])
    try:
        time.sleep(3)
        message = param_stored_vals_msg.fetch()
        if not message:
            logger.error("Could not read stick calibration")
            return None
        try:
            params = param_stored_vals_msg.unpack(message)
            return validate_stick_cal([value for axis in params["stickCals"] for value in axis])
        except (KeyError, TypeError, ValueError, struct.error, UpdateError) as error:
            logger.error("Invalid calibration response: %s", error)
            return None
    finally:
        process.terminate()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        time.sleep(1)


def write_stick_cal(values):
    values = validate_stick_cal(values)
    serialport = serial.Serial('/dev/ttymxc1', ARTOO_BAUD, timeout=1)
    try:
        slipdev = slip.SlipDevice(serialport)
        slipdev.write(bytes([ord(ARTOO_CALIBRATE_ID)]) + struct.pack("<18H", *values))
    finally:
        serialport.close()


def run_update():
    firmware = getFirmwareInfo()
    artoo_version = getArtooVersion()
    logger.info("running version: %s", artoo_version)
    if not artoo_version or artoo_version == "unknown":
        raise UpdateError("Could not identify the running STM32 firmware")
    if firmware is None:
        logger.info("not updating (no new firmware)")
    else:
        filename, candidate_version = firmware
        running_semver = _parse_semver(artoo_version)
        candidate_semver = _parse_semver(candidate_version)
        if not (_update_enabled() or _force_update_enabled()):
            logger.info("not updating (artoo updates disabled); retaining candidate")
        elif not _force_update_enabled() and (
                candidate_semver is None or running_semver is None or candidate_semver <= running_semver):
            logger.info("not updating (version is not a recognized upgrade); retaining candidate")
        else:
            if not updateStm32(filename):
                raise UpdateError("STM32 update did not report success")
            artoo_version = getArtooVersion()
            # Verify the actual version, including development/build suffixes.
            if artoo_version.removeprefix("v") != candidate_version.removeprefix("v"):
                raise UpdateError("Running STM32 version does not match the programmed candidate")
            loaded = os.path.join(os.path.dirname(filename), "loaded")
            os.makedirs(loaded, exist_ok=True)
            os.rename(filename, os.path.join(loaded, os.path.basename(filename)))
            failed_marker = os.path.join(UPDATE_DIR, "UPDATEFAILED")
            if os.path.exists(failed_marker):
                os.unlink(failed_marker)
    writeVersionFile(artoo_version)
    doUpdateComplete()
    if os.path.exists("/log/.factory"):
        os.unlink("/log/.factory")
    return 0


def main():
    logging.config.fileConfig(sololink_conf)
    logger.info("stm32 update starting")
    try:
        return run_update()
    except Exception as error:
        logger.exception("STM32 update failed")
        os.makedirs(UPDATE_DIR, exist_ok=True)
        with open(os.path.join(UPDATE_DIR, "UPDATEFAILED"), "w") as stream:
            stream.write(str(error) + "\n")
        try:
            # An existing READY marker must not hide this attempted update's
            # failure. Do not create a new success/READY marker here.
            setArtooUpdateComplete(ARTOO_UPDATE_FAILED)
        except Exception:
            logger.exception("Could not report the STM32 failure to the display")
        return 1


if __name__ == "__main__":
    sys.exit(main())
