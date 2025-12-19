#!/usr/bin/env python3

import re
import sys
import time
#sys.path.append("/usr/bin")
#import clock


# This takes ~20 msec
# (VO):  qnum: 3 qdepth:  0 ampdu-depth:  0 pending:   0 stopped: 0
r = re.compile(
    r"\((?P<queue>.+)\):"
    r"\s+(?P<label1>[a-z-]+):\s+(?P<val1>[0-9]+)"
    r"\s+(?P<label2>[a-z-]+):\s+(?P<val2>[0-9]+)"
    r"\s+(?P<label3>[a-z-]+):\s+(?P<val3>[0-9]+)"
    r"\s+(?P<label4>[a-z-]+):\s+(?P<val4>[0-9]+)"
    r"\s+(?P<label5>[a-z-]+):\s+(?P<val5>[0-9]+)"
)


def get_queues():
    try:
        f = open("/sys/kernel/debug/ieee80211/phy0/ath9k/queues")
    except OSError:
        return {}
    s = f.read()
    f.close()
    s = s.splitlines()
    d = {}
    for line in s:
        m = r.match(line)
        if not m:
            continue
        v = {
            m.group("label1"): int(m.group("val1")),
            m.group("label2"): int(m.group("val2")),
            m.group("label3"): int(m.group("val3")),
            m.group("label4"): int(m.group("val4")),
            m.group("label5"): int(m.group("val5")),
        }
        d[m.group("queue")] = v
    return d


graph = True

if __name__ == "__main__":
    last = {}
    count = 0
    while True:
        qs = get_queues()
        if not qs:
            time.sleep(0.1)
            continue
        if graph:
            if count == 0:
                line = ['-'] * 128
                count = 9
            else:
                line = [' '] * 128
                count -= 1
            for c in range(7):
                line[c * 20] = '|'
            for q in qs:
                val = qs[q]['pending']
                if val > 127:
                    val = 127
                #print q, qs[q], str(qs[q]), str(qs[q])[1]
                line[val] = q[1]
            print("".join(line))
        else:
            for q in qs:
                # if too many packets pending twice in a row, print message
                if q in last and last[q] > 120 and qs[q]['pending'] > 120:
                    print(q, last[q], qs[q]['pending'])
                last[q] = qs[q]['pending']
        time.sleep(0.1)
