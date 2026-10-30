# Artoo read-only diagnostics schema 1

The existing stm32 bridge owns UART1. Its new endpoint binds only
127.0.0.1:5027/UDP and forwards host packet ID27. A local request is exactly
three bytes: schema1, token uint16 little endian. Run
`python3 flightcode/stm32/artoo_diagnostics.py` on the controller (or the installed
copy when integrated). It never opens the UART and does not reset, clear faults,
change calibration, hold power or program firmware. No CLI destination option
allows accidental network exposure.

The endpoint allows at most one UART request per second and one pending reply
for one second. It validates schema, exact lengths and matching token; invalid,
unsolicited, duplicate and expired replies are discarded. The CLI chooses a
random16-bit token, makes one request with a1.25-second timeout and never retries.
Tokens are correlation aids, not authentication or a proof against collision.
There is no remote listener or vehicle-routing path. Port conflicts disable
this endpoint with a log message; ordinary bridge operation continues. The
existing UART downstream writer performs the small SLIP write, with no replay
on a partial write. This retains the bridge's existing serial blocking semantics.

Reply payload is204 bytes, excluding ID27. All multibyte integers are little
endian. Offsets: schema0(u8), flags1(u8), length2(u16=204), token4(u16),
reserved6(u16=0), RCC reset flags8(u32), retained fault12(16u32),
metrics76(32u32). Flags: bit0 fault valid, bit1 bench metrics enabled.

Fault words: magic, format, IPSR, EXC_RETURN, rawSP, PC, LR, xPSR, CFSR, HFSR,
MMFAR, BFAR, currentTask, flags, reserved, checksum. Invalid records are zero.
Metrics0–8: txBytes,rxBytes,malformedBytes,overruns,dropped,lineErrors,
rejectedFrames,txRejected,txErrors.9–16: stackUsed,heapUsed,heapPeak,OOM,
heartbeatPosted,heartbeatServed,coalesced,maxLoopGapTicks.17–23 task counts;
24–30 task maximum ticks;31 reserved. Timing ticks are9MHz; durations saturate
at32bits and counters wrap. Bench-only values are zero in production.
SysInfo/version bytes and the172-byte calibration format are unchanged.

Native regression `python3 -m unittest discover -s tests -p test_artoo_diagnostics.py`
uses actual Diagnostics/Telem/SLIP/PacketHandler sources, loopback sockets and
pipes. It exercises strict request lengths, escaping, correlation, malformed
replies, rate limiting, duplicate suppression, bind conflict and the actual
telemetry ID filter. LOG_DATA120 is filtered;97 and134 are forwarded. Test
frames deliberately isolate forwarding behavior and are not MAVLink CRC tests.
Integration must also run the established independent MAVLink wire suite,
all relevant ARM image checks and Renode UART cases after the combined freeze.
Host tests do not establish physical UART or fault-retention acceptance.
