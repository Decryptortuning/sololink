#!/usr/bin/python3
"""Wi-Fi configuration lifecycle shared by offline provisioning and boot/setters.

No default credential or WPS enrollment. Secrets are local mode-0600 files.
A checksum is a crash-recovery marker, not an authentication boundary.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile

KINDS = ('hostapd', 'wpa_supplicant')


def safe_path(root, relative):
    root = Path(root).absolute()
    path = Path(root.anchor)
    for part in root.parts[1:] + Path(relative).parts:
        if part in ('..', '/'):
            raise ValueError('invalid configuration path')
        path /= part
        if path.is_symlink():
            raise ValueError('symlink configuration path refused')
    return path


def atomic(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix='.wifi-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
        fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def write_config(root, name, text):
    path = safe_path(root, 'etc/' + name)
    marker = safe_path(root, 'etc/' + name + '.md5')
    atomic(path, text)
    # Target path, not the workstation's overlay mount path.
    atomic(marker, hashlib.md5(text.encode()).hexdigest() + '  /etc/' + name + '\n')


def valid(root, name):
    path = safe_path(root, 'etc/' + name)
    marker = safe_path(root, 'etc/' + name + '.md5')
    if not path.is_file() or not marker.is_file():
        return False
    words = marker.read_text().split()
    return len(words) == 2 and words[0] == hashlib.md5(path.read_bytes()).hexdigest()


def assignments(text):
    result = {}
    for line in text.splitlines():
        match = re.fullmatch(r'\s*([a-zA-Z0-9_]+)\s*=\s*(.*?)\s*', line)
        if match:
            if match[1] in result:
                raise ValueError('duplicate Wi-Fi assignment')
            result[match[1]] = match[2]
    return result


def value(text, kind, key):
    if kind == 'wpa_supplicant' and key == 'password':
        encoded = re.findall(r'^# opensolo-passphrase-hex=([0-9a-f]+)$', text, re.M)
        if len(encoded) == 1:
            return bytes.fromhex(encoded[0]).decode('ascii')
    values = assignments(text)
    name = 'wpa_passphrase' if kind == 'hostapd' and key == 'password' else ('psk' if key == 'password' else 'ssid')
    result = values.get(name, '')
    if kind == 'wpa_supplicant' and result.startswith('"'):
        # wpa_supplicant 2.10 parse_psk treats quoted passphrases as raw bytes.
        result = result[1:-1] if key == 'password' else json.loads(result)
    elif kind == 'wpa_supplicant' and key == 'ssid' and result:
        result = bytes.fromhex(result).decode('utf-8')
    return result


def check_value(key, value):
    if key == 'ssid':
        if not 1 <= len(value.encode()) <= 32:
            raise ValueError('SSID must fit 1..32 bytes')
    elif not 8 <= len(value) <= 63:
        raise ValueError('password must be 8..63 printable ASCII characters')
    if value != value.strip():
        raise ValueError('leading/trailing whitespace is not supported')
    if any(ord(c) < 32 or ord(c) > 126 for c in value):
        raise ValueError('Wi-Fi value must be printable ASCII')


def replace_assignment(text, name, value):
    pattern = re.compile(r'^\s*' + re.escape(name) + r'\s*=.*$', re.M)
    # A callback avoids replacement-string/backslash interpretation.
    lines = pattern.sub('', text).rstrip() + '\n'
    return lines + name + '=' + value + '\n'


def template(root, kind):
    path = safe_path(root, 'mnt/rootfs.ro/etc/' + kind + '.orig')
    if not path.is_file():
        path = safe_path(root, 'etc/' + kind + '.orig')
    text = path.read_text()
    if kind == 'hostapd':
        text = re.sub(r'^\s*(wpa_passphrase|wpa_psk|wpa_psk_file|ap_pin|wps_state)\s*=.*\n?', '', text, flags=re.M)
        text = replace_assignment(text, 'wps_state', '0')
    else:
        # A golden station template must never carry a saved network.
        text = re.sub(r'(?m)^[ \t]*network[ \t]*=[ \t]*\{[ \t]*\n.*?^[ \t]*\}[ \t]*$', '', text, flags=re.S)
        text = re.sub(r'^# opensolo-passphrase-hex=.*\n?', '', text, flags=re.M)
        text = replace_assignment(text, 'update_config', '0')
    return text


def set_value(text, kind, key, new_value):
    check_value(key, new_value)
    if kind == 'hostapd':
        name = 'wpa_passphrase' if key == 'password' else 'ssid'
        return replace_assignment(text, name, new_value)
    # Derive the station PSK rather than relying on wpa_supplicant's raw quote
    # parser. The private companion comment permits explicit get/rotation/SSID
    # changes without another state file. Hex is encoding, not encryption.
    ssid = new_value if key == 'ssid' else value(text, kind, 'ssid')
    password = new_value if key == 'password' else value(text, kind, 'password')
    networks = list(re.finditer(r'(?m)^[ \t]*network[ \t]*=[ \t]*\{[ \t]*\n(.*?)^[ \t]*\}[ \t]*$', text, re.S))
    if len(networks) > 1:
        raise ValueError('select one station network before changing pairing')
    block = networks[0][1] if networks else '\nkey_mgmt=WPA-PSK\nproto=RSN\npairwise=CCMP TKIP\nauth_alg=OPEN\n'
    block = re.sub(r'^# opensolo-passphrase-hex=.*\n?', '', block, flags=re.M)
    block = re.sub(r'^\s*(ssid|psk)\s*=.*\n?', '', block, flags=re.M)
    if ssid:
        block += 'ssid=' + ssid.encode().hex() + '\n'
    if password:
        block += '# opensolo-passphrase-hex=' + password.encode().hex() + '\n'
    if ssid and password:
        block += 'psk=' + hashlib.pbkdf2_hmac('sha1', password.encode(), ssid.encode(), 4096, 32).hex() + '\n'
    if networks:
        match = networks[0]
        return text[:match.start()] + 'network={\n' + block.lstrip('\n') + '}\n' + text[match.end():]
    return text.rstrip() + '\nnetwork={\n' + block.lstrip('\n') + '}\n'


def save(root, kind, text):
    if valid(root, kind + '.conf'):
        write_config(root, kind + '.back', safe_path(root, 'etc/' + kind + '.conf').read_text())
    write_config(root, kind + '.conf', text)


def initialize(root, kind):
    """Apply current templates, preserving only validated personalized settings.

    Wi-Fi avoids shell diff/patch entirely: failed/missing bases cannot restore a
    shared golden credential. Old personalized passwords differ from old base.
    """
    golden = template(root, kind)
    current = None
    for suffix in ('.conf', '.back'):
        if valid(root, kind + suffix):
            current = safe_path(root, 'etc/' + kind + suffix).read_text()
            break
    result = golden
    if current is not None:
        try:
            prior = safe_path(root, 'etc/' + kind + '.base')
            base = prior.read_text() if valid(root, kind + '.base') else ''
            password = value(current, kind, 'password')
            # Reject the inherited golden credential; retain deliberate rotation.
            if password and (base and password != value(base, kind, 'password')):
                ssid = value(current, kind, 'ssid')
                check_value('password', password)
                check_value('ssid', ssid)
                result = set_value(set_value(golden, kind, 'ssid', ssid), kind, 'password', password)
                if kind == 'hostapd':
                    old = assignments(current)
                    for name in ('country_code', 'channel', 'chanlist'):
                        if name in old:
                            result = replace_assignment(result, name, old[name])
                else:
                    # Keep station diagnostics such as disable_ht from the selected network.
                    old = assignments(current)
                    for name in ('disable_ht', 'disable_ht40', 'scan_ssid', 'bssid'):
                        if name in old:
                            result = result.replace('}\n', name + '=' + old[name] + '\n}\n')
        except (ValueError, UnicodeError):
            # Malformed settings stay unavailable; no fallback credential.
            result = golden
    write_config(root, kind + '.conf', result)
    # The old backup belongs to the old baseline. Never let a later corruption
    # reinterpret a factory/unknown password against the new secret-free base.
    for suffix in ('.back', '.back.md5'):
        safe_path(root, 'etc/' + kind + suffix).unlink(missing_ok=True)
    write_config(root, kind + '.base', golden)
    # Record actual canonical orig bytes; .orig remains owned by the image.
    orig = safe_path(root, 'etc/' + kind + '.orig')
    if orig.is_file():
        atomic(safe_path(root, 'etc/' + kind + '.orig.md5'), hashlib.md5(orig.read_bytes()).hexdigest() + '  /etc/' + kind + '.orig\n')


def ready(root, kind):
    if not valid(root, kind + '.conf'):
        raise ValueError('Wi-Fi configuration missing or incomplete')
    path = safe_path(root, 'etc/' + kind + '.conf')
    metadata = path.stat()
    if metadata.st_mode & 0o077 or metadata.st_uid != os.geteuid():
        raise ValueError('Wi-Fi configuration ownership or permissions unsafe')
    text = path.read_text()
    check_value('ssid', value(text, kind, 'ssid'))
    check_value('password', value(text, kind, 'password'))
    if kind == 'wpa_supplicant':
        expected = hashlib.pbkdf2_hmac('sha1', value(text, kind, 'password').encode(), value(text, kind, 'ssid').encode(), 4096, 32).hex()
        if assignments(text).get('psk') != expected:
            raise ValueError('station PSK differs from paired credentials')
    if kind == 'hostapd':
        values = assignments(text)
        if values.get('wpa') != '2' or values.get('wps_state') != '0':
            raise ValueError('AP requires WPA2 and disabled WPS')
    return text


def pair(controller, drone, recovery, ssid, password):
    """Explicitly selected offline roots only; operator file is durable first."""
    if Path(controller).resolve() == Path(drone).resolve():
        raise ValueError('pair roots must differ')
    check_value('ssid', ssid)
    if not ssid.startswith('SoloLink_'):
        raise ValueError('pair SSID must start SoloLink_')
    check_value('password', password)
    recovery = Path(recovery).absolute()
    safe_path(recovery.parent, recovery.name)
    if any(recovery.is_relative_to(Path(root).resolve()) for root in (controller, drone)):
        raise ValueError('operator recovery file must be outside both device roots')
    if recovery.exists():
        raise ValueError('choose a new operator recovery file for each rotation')
    # Preflight all paths and templates before writing either half.
    configs = []
    for root, kind in ((controller, 'hostapd'), (drone, 'wpa_supplicant')):
        for suffix in ('.conf', '.conf.md5', '.back', '.back.md5', '.base', '.base.md5', '.orig.md5'):
            safe_path(root, 'etc/' + kind + suffix)
        golden = template(root, kind)
        configs.append((root, kind, golden, set_value(set_value(golden, kind, 'ssid', ssid), kind, 'password', password)))
    atomic(recovery, json.dumps({'ssid': ssid, 'password': password, 'policy': 'offline-pair-no-wps'}, indent=2) + '\n')
    for root, kind, golden, text in configs:
        for suffix in ('.back', '.back.md5'):
            safe_path(root, 'etc/' + kind + suffix).unlink(missing_ok=True)
        write_config(root, kind + '.base', golden)
        write_config(root, kind + '.conf', text)
    # An interruption can leave only one root updated. Both remain offline until
    # the operator reruns with the recovery values and checks the matched pair.


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', default='/')
    parser.add_argument('--kind', choices=KINDS, default='hostapd')
    parser.add_argument('command', choices=('initialize', 'ready', 'get', 'set', 'reset', 'pair'))
    parser.add_argument('--key', choices=('ssid', 'password'))
    parser.add_argument('--controller-root')
    parser.add_argument('--drone-root')
    parser.add_argument('--recovery-file')
    args = parser.parse_args()
    try:
        if args.command == 'pair':
            if not all((args.controller_root, args.drone_root, args.recovery_file)):
                raise ValueError('pair requires both roots and a recovery file')
            # JSON via stdin keeps credentials out of argv, logs and receipts.
            import sys
            values = json.load(sys.stdin)
            pair(args.controller_root, args.drone_root, args.recovery_file, values['ssid'], values['password'])
        elif args.command == 'initialize':
            initialize(args.root, args.kind)
        elif args.command == 'ready':
            ready(args.root, args.kind)
        elif args.command == 'reset':
            write_config(args.root, args.kind + '.conf', template(args.root, args.kind))
            for suffix in ('.back', '.back.md5'):
                safe_path(args.root, 'etc/' + args.kind + suffix).unlink(missing_ok=True)
        else:
            if not args.key:
                raise ValueError('key required')
            path = safe_path(args.root, 'etc/' + args.kind + '.conf')
            text = path.read_text() if path.exists() else template(args.root, args.kind)
            if args.command == 'get':
                result = value(text, args.kind, args.key)
                check_value(args.key, result)
                print(result)  # Explicit operator getter only.
            else:
                import sys
                new_value = sys.stdin.read().removesuffix('\n')
                save(args.root, args.kind, set_value(text, args.kind, args.key, new_value))
    except (OSError, ValueError, KeyError):
        # Values and file contents must never be part of an error/receipt.
        parser.exit(1, 'Wi-Fi operation unavailable; check paths, configuration and provisioning inputs.\n')


if __name__ == '__main__':
    main()
