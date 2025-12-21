#!/usr/bin/env python3

class SlipDevice:

    END     = chr(0o300) # indicates end of packet
    ESC     = chr(0o333) # indicates byte stuffing
    ESC_END = chr(0o334) # ESC ESC_END means END data byte
    ESC_ESC = chr(0o335) # ESC ESC_ESC means ESC data byte

    def __init__(self, serial_dev):
        self.ser = serial_dev
        self.insync = False

    def sync(self):
        attempts = 0
        bytes_read = 0
        max_bytes = 4096
        def _as_char(val):
            if isinstance(val, bytes):
                return val.decode("latin1")
            if isinstance(val, int):
                return chr(val)
            return val
        while 1:
            raw = self.ser.read()
            if not raw:
                attempts += 1
                if(attempts >= 3):
                    return False
                continue
            bytes_read += len(raw)
            if bytes_read >= max_bytes:
                return False
            dat = _as_char(raw)
            if not dat:
                continue

            if dat == self.END:
                self.insync = True
                return True

    def read(self):
        """
        read a SLIP packet from artoo.
        """
        pkt = []
        max_packet_bytes = 4096
        def _as_char(val):
            if isinstance(val, bytes):
                return val.decode("latin1")
            if isinstance(val, int):
                return chr(val)
            return val

        if not self.insync:
            if(self.sync() == False):
                return pkt;

        while True:
            raw = self.ser.read()
            if not raw:
                # Timeout/no data. Treat as "no packet" so callers can retry.
                # Also force a resync on next read to avoid getting stuck
                # in an "insync but no data" state forever.
                self.insync = False
                return []
            b = _as_char(raw)
            if b == self.END:
                if len(pkt) > 0:
                    return pkt
            elif b == self.ESC:
                raw = self.ser.read()
                if not raw:
                    self.insync = False
                    return []
                b = _as_char(raw)
                if b == self.ESC_END:
                    pkt.append(self.END)
                elif b == self.ESC_ESC:
                    pkt.append(self.ESC)
                else:
                    pkt.append(b)
            else:
                pkt.append(b)
            if len(pkt) >= max_packet_bytes:
                self.insync = False
                return []

    def write(self, pkt):
        """
        write a SLIP message to artoo
        """

        slip_bytes = [self.END]
        for b in pkt:
            if isinstance(b, bytes):
                b = b.decode("latin1")
            elif isinstance(b, int):
                b = chr(b)
            if b == self.END:
                slip_bytes.append(self.ESC)
                slip_bytes.append(self.ESC_END)
            elif b == self.ESC:
                slip_bytes.append(self.ESC)
                slip_bytes.append(self.ESC_ESC)
            else:
                slip_bytes.append(b)

        slip_bytes.append(self.END)
        self.ser.write("".join(slip_bytes).encode("latin1"))
