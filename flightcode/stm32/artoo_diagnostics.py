#!/usr/bin/env python3
"""Read a nondestructive Artoo snapshot via the existing local UART bridge."""
import json
import os
import socket
import struct

COUNTERS = ['tx_bytes', 'rx_bytes', 'malformed_bytes', 'overruns', 'dropped',
            'line_errors', 'rejected_frames', 'tx_rejected', 'tx_errors',
            'stack_used', 'heap_used', 'heap_peak', 'oom', 'heartbeat_posted',
            'heartbeat_served', 'heartbeat_coalesced', 'max_loop_gap_ticks']
FAULT = ['magic', 'format', 'ipsr', 'exc_return', 'raw_sp', 'pc', 'lr', 'xpsr',
         'cfsr', 'hfsr', 'mmfar', 'bfar', 'current_task', 'flags', 'stage', 'checksum']

def validate_fault(raw):
    words = struct.unpack('<16I', raw)
    checksum = 2166136261
    for value in raw[4:60]:
        checksum = ((checksum ^ value) * 16777619) & 0xffffffff
    exception = words[14] == 0 and words[13] in (0, 1)
    software = words[13] == 2 and 1 <= words[14] <= 10
    if (words[0] != 0x41525446 or words[1] != 0x00010040 or
            not (exception or software) or words[15] != checksum):
        raise ValueError('Invalid retained fault record')
    return dict(zip(FAULT, words))

def decode(data, token):
    if len(data) != 204:
        raise ValueError('Invalid diagnostics length')
    schema, flags, length, reply_token, reserved = struct.unpack_from('<BBHHH', data)
    if schema != 1 or flags & ~3 or length != 204 or reply_token != token or reserved:
        raise ValueError('Invalid diagnostics header or stale reply')
    reset, = struct.unpack_from('<I', data, 8)
    fault = validate_fault(data[12:76]) if flags & 1 else None
    metrics = struct.unpack_from('<32I', data, 76)
    return {'schema': schema, 'reset_flags': reset,
            'fault_valid': bool(flags & 1), 'fault': fault if flags & 1 else None,
            'bench_metrics': bool(flags & 2),
            'counters': dict(zip(COUNTERS, metrics[:17])),
            'task_counts': metrics[17:24], 'task_max_ticks': metrics[24:31],
            'tick_hz': 1000000000}

def read_snapshot():
    token = int.from_bytes(os.urandom(2), 'little')
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.connect(('127.0.0.1', 5027))
        sock.settimeout(1.25)
        sock.send(struct.pack('<BH', 1, token))
        return decode(sock.recv(205), token)

if __name__ == '__main__':
    try:
        print(json.dumps(read_snapshot(), indent=2))
    except (OSError, ValueError) as exc:
        raise SystemExit('Artoo diagnostics unavailable: ' + str(exc))
