r"""Pin the EC charge-limit gate open so the configured limit is enforced.

The EC firmware only runs the charge-limit control loop when
    xram[0x07C3] == 4/5  OR  xram[0x0770] == 4/5   (state_is()).
Machines reporting a different platform value skip the loop -> 0x07B9 ignored.

Fix used here: write 0x0770 = 0x04 (the dedicated override slot, normally 0xFF).
The control loop then enforces the live limit in xram[0x07B9].

Both registers live in the writable H2RAM window (0x700-0x7FF), plain
IOCTL_EC_WRITE via \\.\ACPIDriver -- no admin, no ports, no mailbox.

  python pin_limit.py                  # one-shot pin, limit = 60%
  python pin_limit.py --limit 80       # one-shot pin, limit = 80%
  python pin_limit.py --watch 10       # re-assert every 10s (survives EC resets)
  python pin_limit.py --limit 80 --watch 10
  python pin_limit.py --status         # read-only status, writes nothing
  python pin_limit.py --off            # restore 0x0770=0xFF (disable)

Verified on MECHREVO JIAOLONG 16 Pro 2025 (EC 1.32). The override slot may
feed other firmware branches - watch temps/fans on first use; --off reverts.
"""
import ctypes
import sys
import time

from ec_probe import open_driver, ec_read, ec_write

REG_OVERRIDE = 0x0770   # platform override slot (0xFF = empty)
REG_LIVE_LIMIT = 0x07B9  # live charge limit, bits[6:0]
REG_GATE = 0x0742        # bit2 = gate open indicator (observability only)

OVERRIDE_VAL = 0x04


def parse_args(argv):
    limit, watch, off, status_only = 60, None, False, False
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--off":
            off = True
        elif a == "--status":
            status_only = True
        elif a == "--watch":
            i += 1
            watch = float(argv[i]) if i < len(argv) else 10.0
        elif a == "--limit":
            i += 1
            limit = int(argv[i])
            if not 1 <= limit <= 100:
                raise SystemExit("--limit must be 1..100")
        else:
            raise SystemExit(f"unknown arg: {a}\n{__doc__}")
        i += 1
    return limit, watch, off, status_only


def pin(h, limit):
    if ec_read(h, REG_OVERRIDE) != OVERRIDE_VAL:
        ec_write(h, REG_OVERRIDE, OVERRIDE_VAL)
    if ec_read(h, REG_LIVE_LIMIT) & 0x7F != limit:
        ec_write(h, REG_LIVE_LIMIT, limit)


def unpin(h):
    if ec_read(h, REG_OVERRIDE) != 0xFF:
        ec_write(h, REG_OVERRIDE, 0xFF)


def state(h):
    r770, r7b9, r742 = (ec_read(h, a) for a in (REG_OVERRIDE, REG_LIVE_LIMIT, REG_GATE))
    return f"770={r770:#04x} 7B9={r7b9:#04x}(reached={r7b9>>7}) 742={r742:#04x}(gate={(r742>>2)&1})"


def main():
    limit, watch, off, status_only = parse_args(sys.argv[1:])
    h = open_driver()
    try:
        if status_only:
            print("status:", state(h))
            return
        if off:
            unpin(h)
            print("unpinned:", state(h))
            return
        if watch is not None:
            print(f"watch mode: pin 0x0770=4 + limit={limit}%, re-assert every {watch}s")
            while True:
                pin(h, limit)
                time.sleep(watch)
        pin(h, limit)
        time.sleep(2)
        print("pinned:", state(h))
    finally:
        ctypes.WinDLL("kernel32").CloseHandle(h)


if __name__ == "__main__":
    main()
