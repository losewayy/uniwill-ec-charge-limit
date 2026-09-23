r"""Uniwill EC probe via UWACPIDriver (\\.\ACPIDriver), protocol from cyear/NUCtool.

  IOCTL_EC_READ  = 0x9C40A488   in: u32 addr            out: u32 (low byte = value)
  IOCTL_EC_WRITE = 0x9C40A48C   in: u32 addr, u32 val   out: u32 (unused)

Usage:
  python ec_probe.py             # recon dump
  python ec_probe.py read 0x07b9 # single read
"""
import ctypes
import struct
import sys

IOCTL_EC_READ = 0x9C40A488
IOCTL_EC_WRITE = 0x9C40A48C
DEVICE_PATHS = ["\\\\.\\ACPIDriver", "\\\\.\\ACPIH"]

GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
FILE_SHARE_RW = 0x3
OPEN_EXISTING = 3

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
kernel32.CreateFileW.restype = ctypes.c_void_p
kernel32.DeviceIoControl.restype = ctypes.c_bool
kernel32.CloseHandle.restype = ctypes.c_bool


def open_driver():
    for path in DEVICE_PATHS:
        h = kernel32.CreateFileW(
            path, GENERIC_READ | GENERIC_WRITE, FILE_SHARE_RW, None, OPEN_EXISTING, 0, None
        )
        if h and h != ctypes.c_void_p(-1).value:
            print(f"opened {path}")
            return h
    raise OSError(f"cannot open ACPIDriver device, err={ctypes.get_last_error()}")


def _ioctl(h, code, inbuf, out_size=4):
    inb = (ctypes.c_ubyte * len(inbuf)).from_buffer_copy(bytes(inbuf))
    outb = (ctypes.c_ubyte * out_size)()
    ret = ctypes.c_ulong(0)
    ok = kernel32.DeviceIoControl(h, code, inb, len(inb), outb, out_size, ctypes.byref(ret), None)
    if not ok:
        raise OSError(f"DeviceIoControl {code:#x} err={ctypes.get_last_error()}")
    return bytes(outb)[: ret.value]


def ec_read(h, addr):
    return struct.unpack("<I", _ioctl(h, IOCTL_EC_READ, struct.pack("<I", addr)))[0] & 0xFF


def ec_write(h, addr, val):
    _ioctl(h, IOCTL_EC_WRITE, struct.pack("<II", addr, val))


def main():
    h = open_driver()
    try:
        if len(sys.argv) >= 3 and sys.argv[1] == "read":
            addr = int(sys.argv[2], 16)
            print(f"ec[{addr:#06x}] = {ec_read(h, addr):#04x}")
            return

        targets = {
            0x0740: "PROJECT_ID",
            0x0741: "AP_OEM",
            0x0742: "SUPPORT_5 (bit4 fan-turbo, bit5 fan-support)",
            0x0765: "SUPPORT_1",
            0x0766: "SUPPORT_2",
            0x07A6: "OEM_4 charging_profile bits[5:4] (0=high,1=bal,2=station)",
            0x07B9: "CHARGE_CTRL bits[6:0]=limit%, bit7=reached",
            0x07C3: "w568w: limit state / enable gate?",
            0x07C6: "AP_OEM_6 (bit3=full>24h, bit4=erm reached)",
            0x07CC: "USB_C_POWER_PRIORITY bit7",
            0x078E: "charging-profile support flag (tuxedo bit3)",
            0x0434: "BAT_CURRENT_1",
            0x0435: "BAT_CURRENT_2",
            0x0436: "BAT_REMAIN_CAP_1",
            0x0437: "BAT_REMAIN_CAP_2",
            0x0404: "BAT_FULL_CAP_1",
            0x0405: "BAT_FULL_CAP_2",
            0x043E: "CPU_TEMP",
            0x044F: "GPU_TEMP",
            0x0464: "FAN1_RPM_HI",
            0x0465: "FAN1_RPM_LO",
            0x0751: "MANUAL_FAN_CTRL / FAN_MODE",
        }
        for addr, desc in targets.items():
            try:
                v = ec_read(h, addr)
                print(f"ec[{addr:#06x}] = {v:#04x} ({v:08b}b)  {desc}")
            except Exception as e:
                print(f"ec[{addr:#06x}] = ERR {e}  {desc}")
    finally:
        kernel32.CloseHandle(h)


if __name__ == "__main__":
    main()
