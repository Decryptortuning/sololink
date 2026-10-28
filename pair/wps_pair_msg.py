
import struct

"""
Pair Request/Confirm message sent between pairing and stm32 modules.

Start
Byte    Size    Description
0       4       ID (not used)
4       4       Length following (56)
8       4       Flags
12      20      MAC address, ASCII string (e.g. "02:1F:09:03:00:08")
32      32      Friendly name (e.g. "Solo Mynis")
64 (total packet length)
"""

MAC_LENGTH = 20
NAME_LENGTH = 32
BODY_LENGTH = 56
TOTAL_LENGTH = 64

FLAGS_REQUEST = 1
FLAGS_CONFIRM = 2
FLAGS_RESULT = 3

fmt_string = "<III%ds%ds" % (MAC_LENGTH, NAME_LENGTH)

MSG_ID = 0

# struct.pack/unpack use bytes for wire fields
#
# struct.pack will truncate the string to fit in the specified length, or pad
# it with \0 bytes to the specified length, e.g.
#   struct.pack("3s", b"Hello") -> b"Hel"
#   struct.pack("8s", b"Hello") -> b"Hello\0\0\0"
#
# struct.unpack will return a string of exactly the specified length, e.g.
#   struct.unpack("8s", b"Hello\0\0\0") -> (b"Hello\0\0\0",)

def unpack(s):
    if len(s) != TOTAL_LENGTH:
        return None
    hdr_id, hdr_len, flags, mac, name = struct.unpack(fmt_string, s)
    if hdr_id != MSG_ID or hdr_len != BODY_LENGTH:
        return None
    mac = mac.partition(b"\0")[0].decode("ascii", errors="replace")
    name = name.partition(b"\0")[0].decode("utf-8", errors="replace")
    return mac, name, flags


def pack(mac, name, flags=0):
    mac = mac.encode("ascii") if isinstance(mac, str) else mac
    name = name.encode("utf-8") if isinstance(name, str) else name
    # Truncate encoded byte strings so each fixed-width field is NUL terminated.
    return struct.pack(fmt_string, MSG_ID, BODY_LENGTH, flags,
                       mac[:MAC_LENGTH - 1], name[:NAME_LENGTH - 1])
