r"""Uniwill EC mailbox via UWACPIDriver port IOCTLs (no admin needed).

Implements the same protocol as uniwill_wmi.c uw_ec_read/write_addr_direct:
  EC-space mailbox regs: 0x8A=LDAT 0x8B=HDAT 0x8C=FLAGS 0x8D=CMDL 0x8E=CMDH
  FLAGS bits: 0=RFLG 1=WFLG 2=BFLG 3=CFLG 7=DRDY

Port access via driver IOCTLs (UWACPIDriver.sys, \\.\ACPIDriver):
  0x9C40A4C0 IORD  in: u32 port            -> out u32 = 4 port bytes
  0x9C40A4CC WIOP  in: u32 base,ind,dat    -> outb(base,ind); outb(base+1,dat)

WIOP is used instead of IOWD (which would write 4 consecutive ports and hit
0x64 = legacy KBC command port). Writing 0x63/0x67 is harmless (unclaimed).

Usage:
  python mailbox.py read 0x740        # mailbox read one xram addr
  python mailbox.py dump 0x740 0x760  # mailbox read a range
"""
import ctypes
import struct
import sys
import time

from ec_probe import open_driver, _ioctl

IOCTL_IORD = 0x9C40A4C0
IOCTL_WIOP = 0x9C40A4CC

PORT_CMD = 0x66   # EC command/status port
PORT_DAT = 0x62   # EC data port
STS_OBF = 0x01
STS_IBF = 0x02

MB_LDAT = 0x8A
MB_HDAT = 0x8B
MB_FLAGS = 0x8C
MB_CMDL = 0x8D
MB_CMDH = 0x8E

B_RFLG = 0x01
B_WFLG = 0x02
B_BFLG = 0x04
B_DRDY = 0x80

_h = None


def drv():
    global _h
    if _h is None:
        _h = open_driver()
    return _h


def iord32(port):
    return struct.unpack("<I", _ioctl(drv(), IOCTL_IORD, struct.pack("<I", port), 4))[0]


def iord8(port):
    return iord32(port) & 0xFF


def outb(port, val):
    """Write a single byte to `port` via WIOP (also writes 0 to port+1, unclaimed)."""
    _ioctl(drv(), IOCTL_WIOP, struct.pack("<III", port, val & 0xFF, 0), 4)


def status():
    return iord8(PORT_CMD)


def wait_ibf_clear(timeout=0.5):
    t0 = time.time()
    while status() & STS_IBF:
        if time.time() - t0 > timeout:
            return False
    return True


def wait_obf_set(timeout=0.5):
    t0 = time.time()
    while not (status() & STS_OBF):
        if time.time() - t0 > timeout:
            return False
    return True


def drain_obf():
    """Discard any stale pending EC output byte."""
    if status() & STS_OBF:
        return inb_dat()
    return None


def inb_dat():
    return iord8(PORT_DAT)


def ec_space_read(off):
    if not wait_ibf_clear():
        raise TimeoutError("IBF stuck before read cmd")
    outb(PORT_CMD, 0x80)
    if not wait_ibf_clear():
        raise TimeoutError("IBF stuck after read cmd")
    outb(PORT_DAT, off & 0xFF)
    if not wait_obf_set():
        raise TimeoutError(f"OBF timeout reading EC-space {off:#x}")
    return inb_dat()


def ec_space_write(off, val):
    if not wait_ibf_clear():
        raise TimeoutError("IBF stuck before write cmd")
    outb(PORT_CMD, 0x81)
    if not wait_ibf_clear():
        raise TimeoutError("IBF stuck after write cmd")
    outb(PORT_DAT, off & 0xFF)
    if not wait_ibf_clear():
        raise TimeoutError("IBF stuck after write offset")
    outb(PORT_DAT, val & 0xFF)


def mb_read(addr, retries=3):
    """Mailbox read of 16-bit xram address. Returns (value, data_high) or None."""
    for attempt in range(retries):
        try:
            return _mb_read_once(addr)
        except TimeoutError as e:
            print(f"  mb_read {addr:#06x} attempt {attempt+1}: {e}", file=sys.stderr)
            time.sleep(0.05)
    return None


def _mb_read_once(addr):
    flags = ec_space_read(MB_FLAGS)
    if flags & B_BFLG:
        time.sleep(0.02)
        flags = ec_space_read(MB_FLAGS)
        if flags & B_BFLG:
            raise TimeoutError("BFLG busy")
    ec_space_write(MB_FLAGS, flags | B_BFLG)
    ec_space_write(MB_LDAT, addr & 0xFF)
    ec_space_write(MB_HDAT, (addr >> 8) & 0xFF)
    ec_space_write(MB_FLAGS, ((flags | B_BFLG) & ~B_DRDY) | B_RFLG)
    f = 0
    for _ in range(30):
        f = ec_space_read(MB_FLAGS)
        if f & B_DRDY:
            break
        time.sleep(0.015)
    if not (f & B_DRDY):
        ec_space_write(MB_FLAGS, 0x00)
        raise TimeoutError("DRDY timeout")
    lo = ec_space_read(MB_CMDL)
    hi = ec_space_read(MB_CMDH)
    ec_space_write(MB_FLAGS, 0x00)
    return (lo, hi)


def mb_write(addr, val, data_hi=0, retries=3):
    for attempt in range(retries):
        try:
            return _mb_write_once(addr, val, data_hi)
        except TimeoutError as e:
            print(f"  mb_write {addr:#06x} attempt {attempt+1}: {e}", file=sys.stderr)
            time.sleep(0.05)
    return False


def _mb_write_once(addr, val, data_hi):
    flags = ec_space_read(MB_FLAGS)
    if flags & B_BFLG:
        time.sleep(0.02)
        flags = ec_space_read(MB_FLAGS)
        if flags & B_BFLG:
            raise TimeoutError("BFLG busy")
    ec_space_write(MB_FLAGS, flags | B_BFLG)
    ec_space_write(MB_LDAT, addr & 0xFF)
    ec_space_write(MB_HDAT, (addr >> 8) & 0xFF)
    ec_space_write(MB_CMDL, val & 0xFF)
    ec_space_write(MB_CMDH, data_hi & 0xFF)
    ec_space_write(MB_FLAGS, ((flags | B_BFLG) & ~B_DRDY) | B_WFLG)
    f = 0
    for _ in range(30):
        f = ec_space_read(MB_FLAGS)
        if f & B_DRDY:
            break
        time.sleep(0.015)
    ec_space_write(MB_FLAGS, 0x00)
    if not (f & B_DRDY):
        raise TimeoutError("DRDY timeout")
    return True


def main():
    h = drv()
    try:
        print(f"EC status: {status():#04x}")
        stale = drain_obf()
        if stale is not None:
            print(f"drained stale OBF byte: {stale:#04x}")

        if len(sys.argv) >= 3 and sys.argv[1] == "read":
            addr = int(sys.argv[2], 16)
            r = mb_read(addr)
            print(f"mailbox[{addr:#06x}] = {r[0]:#04x} (hi {r[1]:#04x})" if r else "READ FAILED")
        elif len(sys.argv) >= 4 and sys.argv[1] == "dump":
            a0, a1 = int(sys.argv[2], 16), int(sys.argv[3], 16)
            for a in range(a0, a1 + 1):
                r = mb_read(a)
                print(f"  [{a:#06x}] = {r[0]:#04x}" if r else f"  [{a:#06x}] FAIL")
        else:
            # self-test: mailbox read of known regs vs window values
            print("self-test: mailbox vs window reads")
            from ec_probe import ec_read
            for a in (0x740, 0x741, 0x742, 0x7B9, 0x7C3):
                r = mb_read(a)
                w = ec_read(h, a)
                mark = "OK" if r and r[0] == w else "MISMATCH"
                print(f"  [{a:#06x}] mailbox={r[0] if r else 'FAIL'} window={w:#04x} {mark}")
    finally:
        ctypes.WinDLL("kernel32").CloseHandle(h)


if __name__ == "__main__":
    main()
