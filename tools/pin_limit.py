r"""Pin the EC charge-limit gate open so the configured limit is enforced.

Self-contained: talks to UWACPIDriver (\\.\ACPIDriver, installed by OpenRevo)
via IOCTLs -- no admin needed, no external deps beyond Python itself.

The EC firmware only runs the charge-limit control loop when
    xram[0x07C3] == 4/5  OR  xram[0x0770] == 4/5   (state_is(), per w568w RE).
Machines where neither holds (e.g. ROMID[0]=0xFF unprogrammed and
platform=7) never enforce the live limit in xram[0x07B9].
NOTE: the {4,5} set is per-firmware - on the reference unit (JIAOLONG
EC 1.32), 0x07C3 accepts 4 ONLY (writing 5 did not open the gate).

TWO gate operands exist -- "two keys for the same door":

  --key romid     poke xram[0x0770] (ROMID[0], product-line id).
                  Safe only when the byte is 0xFF (unprogrammed);
                  refusing to overwrite a real ROMID is enforced.
                  WARNING: ROMID is product identity - a wrong value
                  makes the EC take wrong product-table branches and
                  may affect unrelated features.
  --key platform  poke xram[0x07C3] (platform byte). Same gate, but it
                  may NOT feed product-line tables - if that holds, this
                  opens the gate WITHOUT swapping product identity.
                  Original value is saved to pin_limit_state.json and
                  restored by --off (or any reboot: xram is volatile).
                  VERIFIED on JIAOLONG EC 1.32: val=4 opens the gate,
                  val=5 does not; no side effects observed there.

Before poking anything, run --dump - its output separates the three
failure modes: "write didn't stick" / "gate didn't open" /
"gate open but the limit source differs".

  pin_limit.exe                          # bare run = read-only dump (safe)
  pin_limit.exe --watch 10               # pin platform=4 + limit 60%, re-assert
  pin_limit.exe --key romid --val 5      # poke ROMID instead (only if 0x0770==0xFF)
  pin_limit.exe --dump                   # read-only diagnostic
  pin_limit.exe --status                 # quick status of the 4 key registers
  pin_limit.exe --limit 80 --key platform --val 4
  pin_limit.exe --off                    # restore poked bytes from state file

Verified on MECHREVO JIAOLONG 16 Pro 2025 (EC 1.32): both keys open the
gate (0x0742 bit2 sets, 0x07B9 bit7 REACHED asserts, charge stops at the
configured limit). Upstream (tuxedo-drivers) declined to touch this:
"ROMID is a magic value set by the OEM and manipulating it will open
untested codepaths" - treat every poke as an experiment, watch temps,
fans and Fn keys on first use; --off or a reboot reverts everything.
"""
import ctypes
import json
import os
import struct
import sys
import time
import winreg

# ---------- UWACPIDriver plumbing (was ec_probe.py) ----------

IOCTL_EC_READ = 0x9C40A488    # in: u32 addr            out: u32 (low byte = value)
IOCTL_EC_WRITE = 0x9C40A48C   # in: u32 addr, u32 val   out: u32 (unused)
IOCTL_IORD = 0x9C40A4C0       # in: u32 port            out: 4 port bytes
IOCTL_WIOP = 0x9C40A4CC       # in: u32 base,ind,dat    -> outb(base,ind); outb(base+1,dat)
DEVICE_PATHS = ["\\\\.\\ACPIDriver", "\\\\.\\ACPIH"]

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
kernel32.CreateFileW.restype = ctypes.c_void_p
kernel32.DeviceIoControl.restype = ctypes.c_bool
kernel32.CloseHandle.restype = ctypes.c_bool


def open_driver():
    for path in DEVICE_PATHS:
        h = kernel32.CreateFileW(
            path, 0x80000000 | 0x40000000, 0x3, None, 3, 0, None
        )
        if h and h != ctypes.c_void_p(-1).value:
            print(f"opened {path}")
            return h
    raise OSError(f"cannot open ACPIDriver device, err={ctypes.get_last_error()}")


def _ioctl(h, code, inbuf, out_size=4):
    inb = (ctypes.c_ubyte * len(inbuf)).from_buffer_copy(bytes(inbuf))
    outb_ = (ctypes.c_ubyte * out_size)()
    ret = ctypes.c_ulong(0)
    ok = kernel32.DeviceIoControl(h, code, inb, len(inb), outb_, out_size,
                                  ctypes.byref(ret), None)
    if not ok:
        raise OSError(f"DeviceIoControl {code:#x} err={ctypes.get_last_error()}")
    return bytes(outb_)[: ret.value]


def ec_read(h, addr):
    return struct.unpack("<I", _ioctl(h, IOCTL_EC_READ, struct.pack("<I", addr)))[0] & 0xFF


def ec_write(h, addr, val):
    _ioctl(h, IOCTL_EC_WRITE, struct.pack("<II", addr, val))


def iord8(h, port):
    return struct.unpack("<I", _ioctl(h, IOCTL_IORD, struct.pack("<I", port), 4))[0] & 0xFF


def outb(h, port, val):
    """Single-byte port write via WIOP (also writes 0 to port+1, unclaimed)."""
    _ioctl(h, IOCTL_WIOP, struct.pack("<III", port, val & 0xFF, 0), 4)


# ---------- EC mailbox, read path only (was mailbox.py) ----------
# EC-space mailbox regs via legacy ports 0x66 (cmd) / 0x62 (data):
#   0x8A=LDAT 0x8B=HDAT 0x8C=FLAGS 0x8D=CMDL 0x8E=CMDH
#   FLAGS: bit0=RFLG bit1=WFLG bit2=BFLG bit7=DRDY

PORT_CMD, PORT_DAT = 0x66, 0x62
STS_OBF, STS_IBF = 0x01, 0x02
MB_LDAT, MB_HDAT, MB_FLAGS, MB_CMDL, MB_CMDH = 0x8A, 0x8B, 0x8C, 0x8D, 0x8E
B_RFLG, B_BFLG, B_DRDY = 0x01, 0x04, 0x80


def _wait_ibf(h, timeout=0.5):
    t0 = time.time()
    while iord8(h, PORT_CMD) & STS_IBF:
        if time.time() - t0 > timeout:
            return False
    return True


def _wait_obf(h, timeout=0.5):
    t0 = time.time()
    while not (iord8(h, PORT_CMD) & STS_OBF):
        if time.time() - t0 > timeout:
            return False
    return True


def _ec_space_read(h, off):
    if not _wait_ibf(h):
        raise TimeoutError("IBF stuck before read cmd")
    outb(h, PORT_CMD, 0x80)
    if not _wait_ibf(h):
        raise TimeoutError("IBF stuck after read cmd")
    outb(h, PORT_DAT, off & 0xFF)
    if not _wait_obf(h):
        raise TimeoutError(f"OBF timeout reading EC-space {off:#x}")
    return iord8(h, PORT_DAT)


def _ec_space_write(h, off, val):
    if not _wait_ibf(h):
        raise TimeoutError("IBF stuck before write cmd")
    outb(h, PORT_CMD, 0x81)
    if not _wait_ibf(h):
        raise TimeoutError("IBF stuck after write cmd")
    outb(h, PORT_DAT, off & 0xFF)
    if not _wait_ibf(h):
        raise TimeoutError("IBF stuck after write offset")
    outb(h, PORT_DAT, val & 0xFF)


def mb_read(h, addr, retries=3):
    """Mailbox read of a 16-bit xram address -> (value, data_high) or None."""
    for attempt in range(retries):
        try:
            return _mb_read_once(h, addr)
        except TimeoutError:
            time.sleep(0.05)
    return None


def _mb_read_once(h, addr):
    flags = _ec_space_read(h, MB_FLAGS)
    if flags & B_BFLG:
        time.sleep(0.02)
        flags = _ec_space_read(h, MB_FLAGS)
        if flags & B_BFLG:
            raise TimeoutError("BFLG busy")
    _ec_space_write(h, MB_FLAGS, flags | B_BFLG)
    _ec_space_write(h, MB_LDAT, addr & 0xFF)
    _ec_space_write(h, MB_HDAT, (addr >> 8) & 0xFF)
    _ec_space_write(h, MB_FLAGS, ((flags | B_BFLG) & ~B_DRDY) | B_RFLG)
    f = 0
    for _ in range(30):
        f = _ec_space_read(h, MB_FLAGS)
        if f & B_DRDY:
            break
        time.sleep(0.015)
    if not (f & B_DRDY):
        _ec_space_write(h, MB_FLAGS, 0x00)
        raise TimeoutError("DRDY timeout")
    lo = _ec_space_read(h, MB_CMDL)
    hi = _ec_space_read(h, MB_CMDH)
    _ec_space_write(h, MB_FLAGS, 0x00)
    return (lo, hi)


# ---------- gate pin logic ----------

REG_ROMID = 0x0770       # ROMID[0] (0xFF = unprogrammed on the reference machine)
REG_PLATFORM = 0x07C3    # platform byte - the second gate operand
REG_LIVE_LIMIT = 0x07B9  # live charge limit, bits[6:0]; bit7 = REACHED
REG_GATE = 0x0742        # bit2 = gate open indicator (observability only)

KEYS = {"romid": REG_ROMID, "platform": REG_PLATFORM}
STATE_FILE = os.path.join(
    os.path.dirname(sys.executable if getattr(sys, "frozen", False)
                    else os.path.abspath(__file__)),
    "pin_limit_state.json")


def parse_args(argv):
    args = dict(key="platform", val=None, limit=60, watch=None,
                off=False, status_only=False, dump=False)
    i = 0

    def take(name):
        nonlocal i
        i += 1
        if i >= len(argv):
            raise SystemExit(f"{name} requires a value\n{__doc__}")
        return argv[i]

    while i < len(argv):
        a = argv[i]
        if a in ("-h", "--help"):
            print(__doc__)
            raise SystemExit(0)
        if a == "--off":
            args["off"] = True
        elif a == "--status":
            args["status_only"] = True
        elif a == "--dump":
            args["dump"] = True
        elif a == "--key":
            args["key"] = take("--key")
            if args["key"] not in KEYS:
                raise SystemExit(f"--key must be one of {sorted(KEYS)}")
        elif a == "--val":
            try:
                args["val"] = int(take("--val"), 0)
            except ValueError:
                raise SystemExit("--val must be a number (dec or 0x..)")
            if not 0 <= args["val"] <= 0xFF:
                raise SystemExit("--val must be a byte")
        elif a == "--watch":
            i += 1
            args["watch"] = float(argv[i]) if i < len(argv) else 10.0
        elif a == "--limit":
            try:
                args["limit"] = int(take("--limit"))
            except ValueError:
                raise SystemExit("--limit must be an integer")
            if not 1 <= args["limit"] <= 100:
                raise SystemExit("--limit must be 1..100")
        else:
            raise SystemExit(f"unknown arg: {a}\n{__doc__}")
        i += 1
    if args["val"] is None:
        args["val"] = 0x04
    return args


def load_state():
    try:
        return json.load(open(STATE_FILE))
    except (OSError, ValueError):
        return {}


def save_state(orig):
    json.dump(orig, open(STATE_FILE, "w"), indent=2)


def pin(h, args, orig):
    reg = KEYS[args["key"]]
    cur = ec_read(h, reg)
    if cur != args["val"]:
        if args["key"] == "romid" and cur != 0xFF:
            raise SystemExit(
                f"REFUSING to write: ROMID[0] is {cur:#04x}, not 0xFF. "
                "This machine has a programmed ROM ID - overwriting byte 0 "
                "would change its product-line identity. Aborting."
            )
        key = f"{reg:#06x}"
        if key not in orig:          # record only the FIRST seen value as original
            orig[key] = cur
            save_state(orig)
        ec_write(h, reg, args["val"])
    if ec_read(h, REG_LIVE_LIMIT) & 0x7F != args["limit"]:
        ec_write(h, REG_LIVE_LIMIT, args["limit"])


def unpin(h):
    orig = load_state()
    restored = []
    for hexaddr, val in orig.items():
        reg = int(hexaddr, 16)
        if ec_read(h, reg) != val:
            ec_write(h, reg, val)
            restored.append(f"{hexaddr}<-{val:#04x}")
    if not restored:
        print("nothing recorded to restore - if a poke is still active, "
              "a reboot reverts it (xram is volatile)")
    else:
        print("restored:", " ".join(restored))
    try:
        os.remove(STATE_FILE)
    except OSError:
        pass


def state(h):
    r770, r7c3, r7b9, r742 = (ec_read(h, a) for a in
                             (REG_ROMID, REG_PLATFORM, REG_LIVE_LIMIT, REG_GATE))
    gate_ok = r770 in (4, 5) or r7c3 in (4, 5)
    return (f"770={r770:#04x} 7C3={r7c3:#04x} gate_op={'OPEN' if gate_ok else 'closed'} "
            f"7B9={r7b9:#04x}(reached={r7b9 >> 7}) 742={r742:#04x}(gate={(r742 >> 2) & 1})")


def dump(h):
    try:
        rk = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                            r"HARDWARE\DESCRIPTION\System\BIOS")
        model = winreg.QueryValueEx(rk, "SystemProductName")[0]
        bios = winreg.QueryValueEx(rk, "BIOSVersion")[0]
    except OSError:
        model, bios = "?", "?"
    print(f"machine: {model}   BIOS: {bios}")
    named = [(0x0740, "PROJECT_ID"), (0x0741, "AP_OEM"),
             (0x0742, "GATE_IND bit2"), (0x0765, "SUPPORT_1"),
             (0x0766, "SUPPORT_2"), (0x07A6, "charge profile"),
             (0x07B9, "LIVE_LIMIT bit7=reached"), (0x07C3, "PLATFORM key"),
             (0x07C6, "AP_OEM_6"), (0x07CC, "USBC_PPS bit7"),
             (0x078E, "profile flags")]
    for a, name in named:
        print(f"  xram[{a:#06x}] = {ec_read(h, a):#04x}   {name}")
    romid = [ec_read(h, 0x0770 + i) for i in range(16)]
    print("  ROMID block 0x0770-0x077F:",
          " ".join(f"{b:02X}" for b in romid))
    r = mb_read(h, 0x087F)
    if r is not None:
        print(f"  xram[0x087F] = {r[0]:#04x} (hi {r[1]:#04x})   STORED_LIMIT (mailbox)")
    else:
        print("  xram[0x087F] = mailbox timeout (port path dead on some firmware)")
    print("  " + state(h))
    print("dump wrote no xram bytes (mailbox probe touches EC regs only).")


def main():
    if len(sys.argv) == 1:
        # bare run (e.g. double-clicked exe): dump only, write nothing
        try:
            h = open_driver()
            try:
                dump(h)
            finally:
                kernel32.CloseHandle(h)
        except OSError as e:
            print(e)
        try:
            input("\n-- Press Enter to close --")
        except EOFError:
            pass
        return
    args = parse_args(sys.argv[1:])
    h = open_driver()
    try:
        if args["dump"]:
            dump(h)
            return
        if args["status_only"]:
            print("status:", state(h))
            return
        if args["off"]:
            unpin(h)
            print("unpinned:", state(h))
            return
        orig = load_state()
        if args["watch"] is not None:
            print(f"watch mode: pin {KEYS[args['key']]:#06x}={args['val']:#04x} "
                  f"+ limit={args['limit']}%, re-assert every {args['watch']}s")
            while True:
                pin(h, args, orig)
                time.sleep(args["watch"])
        pin(h, args, orig)
        time.sleep(2)
        print("pinned:", state(h))
    finally:
        kernel32.CloseHandle(h)


if __name__ == "__main__":
    main()
