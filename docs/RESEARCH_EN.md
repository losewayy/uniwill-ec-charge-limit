# Uniwill EC Charge-Limit Fix — Full Reverse-Engineering Notes

> Device under test: MECHREVO JIAOLONG 16 Pro 2025 (JIAOLONG Series-X6xR55xK, Ryzen 9 8945HX + RTX 5070 Ti)
> Firmware: BIOS `N.1.21MRO34` (2026-01-29), EC `1.32`, project_id `0x1A` / bios_project_id `TRX1`
> Platform: Windows 11, OpenRevo installed (provides `UWACPIDriver.sys`)
> Result: **fixed and verified on real hardware** — from 55%, charging stops at exactly 60.0% (48,048/80,080 mWh).
>
> 中文版：[RESEARCH.md](RESEARCH.md)

This document records the full analysis: channel reverse engineering, protocol reconstruction, root-cause, the two fixes, and the dead ends (so future researchers don't have to walk them again).

---

## 1. Problem statement

User-visible: "a charge limit was configured (Workstation mode, 60%) but the battery charges to 100% anyway".

Register level: the limit `xram[0x07B9]` really is written as `0x3C` (both OpenRevo and the vendor console manage this), but the EC never acts on it. `powercfg /batteryreport` shows full charge capacity = design capacity (80,080 mWh), ruling out "limited but displayed as 100%".

## 2. EC access channels

Three theoretically usable paths exist on this machine:

| Channel | Privilege | Reachable range | Status |
|---|---|---|---|
| `\\.\ACPIDriver` IOCTL (H2RAM window) | **no admin** | xram `0x000-0x3FF` read-only, `0x400-0x4FF` RW, `0x700-0x7FF` RW, `0xC00-0xFFF` RW; **`0x800-0xBFF` dead zone** | ✅ primary channel |
| WMI `AcpiTest_MULong::GetSetULong` (EC mailbox) | admin required | **entire 16-bit xram**, incl. `0x087F` | ✅ used by Method B |
| Ports `0x62/0x66`, hand-rolled EC-space protocol | no admin (via driver IOCTL) | same in theory | ❌ firmware never responds on this unit (§8 dead ends) |

### 2.1 `UWACPIDriver.sys` base read/write IOCTLs (known, from NUCtool)

```
Device node: \\.\ACPIDriver   (PnP device ACPI\INOU0000, opens without admin)

IOCTL_EC_READ  = 0x9C40A488   in: u32 addr          out: u32 (low byte = value)
IOCTL_EC_WRITE = 0x9C40A48C   in: u32 addr, u32 val out: ignored
```

### 2.2 Full driver dispatch table (decoded in this work)

Internally each IOCTL calls an ACPI method under `_SB.INOU`. The four-char tags in `.rdata` are **byte-reversed** AML method names (e.g. `"DROI"` → `IORD`), because they are reassembled into a big-endian u32 and stored little-endian.

| IOCTL | .rdata tag | actual ACPI method | meaning | params |
|---|---|---|---|---|
| 0x9C40A488 | — | (direct H2RAM) | EC xram read | u32 addr |
| 0x9C40A48C | — | (direct H2RAM) | EC xram write | addr+val |
| 0x9C40A480 | SMCR | RCMS | CMOS read | idx |
| 0x9C40A484 | SMCW | WCMS | CMOS write | idx+?+val |
| 0x9C40A490 | BRMM | MMRB | phys-mem read 8-bit | u32 addr |
| 0x9C40A494 | DRMM | MMRD | **phys-mem read 32-bit** | u32 addr |
| 0x9C40A498 | BWMM | MMWB | phys-mem write 8-bit | addr+val |
| 0x9C40A49C | DWMM | MMWD | phys-mem write 32-bit | addr+val |
| 0x9C40A4A0 | DRCP | PCRD | PCI config read | addr |
| 0x9C40A4A4 | DWCP | PCWD | PCI config write | addr+val |
| 0x9C40A4C0 | DROI | **IORD port read** | in 32-bit @ port | u32 port |
| 0x9C40A4C4 | DWOI | **IOWD port write** | out 32-bit @ port | port+val |
| 0x9C40A4C8 | POIR | RIOP | indexed port read (base/base+1) | base+idx |
| 0x9C40A4CC | POIW | **WIOP indexed port write** | outb(base,a1); outb(base+1,a2) | base+a1+a2 |
| 0x9C40A4D0-D8 | DRxT | T1RD/T2RD/T3RD | (empty stubs) | — |
| 0x9C40A4DC-E4 | RWxT | T1WR/T2WR/T3WR | (same) | — |
| 0x9C40A500 | — | SMRW | 40-byte buffer R/W | — |
| 0x9C40A504 | dynamic | **generic single-arg method evaluation** | method name also byte-reversed | name+u32 arg |

**Verified on hardware** (all read-only, no admin):

```
MMRD(0xFED50740)      -> 1a 81 22 07   ← identical to window reads of 0x740/741/742
A504 "RRCE"(0x740)    -> 1a            ← generic evaluation works ("ECRR" reversed)
IORD(0x66)            -> 08            ← EC status port
IORD(0x681..0x683)    -> DF E8 9E      ← ports respond (later shown NOT to be I2EC)
```

Note `IORD`/`IOWD` are `ByteAcc` **32-bit** accesses → they touch **4 consecutive ports**. Writing `0x62` also writes `0x63-0x65` (`0x64` is the legacy KBC command port — dangerous). For single-byte writes use `WIOP(base, val, 0)` (writes base and base+1; `0x63`/`0x67` are unclaimed, harmless).

**Bonus finding**: `MMRD`/`MMWD`/`IORD`/`IOWD` amount to arbitrary physical-memory and port access *without admin* — useful primitives for anyone researching these machines (and a reminder that the driver is effectively a kernel-level backdoor; uninstall it if that bothers you).

### 2.3 WMI mailbox protocol (the official channel)

WMI device `AMW0`'s `_WDG` map in the DSDT (decompiled with `iasl`):

| GUID suffix | object | type |
|---|---|---|
| `ABBC0F6F-8EA1-11D1-00A0-C90629100000` | BC | method → `WMBC` |

`WMBC(Arg0, Arg1, Arg2)` dispatches on `Arg1`; `Arg1=4` (i.e. WMI method `GetSetULong`) → `OEMG(u64 data)` → routes by data's high 32 bits to the EC mailbox:

- `SAC1 = 0`/`1` → `WKBC` mailbox write
- `SAC1 = 0x0100` → `RKBC` mailbox read
- `SAC1 = 0x0200` → `SCMD` EC command

Payload format (matches tuxedo `uniwill_wmi.c` and NUCtool `uniwillwmi.rs`; input is a 40-byte buffer, `bytes[0:4]`=arg u32, `byte[5]`=function):

```
read : Data(u64) = (0x0100 << 32) | addr
write: Data(u64) = (data_hi << 24) | (data_lo << 16) | addr      # function byte=0 → write
return: Return u32, low byte = value; 0xFEFEFEFE = mailbox timeout/not implemented

e.g. read 0x087F  → Data = 0x010000000000087F
     write 0x087F=60 → Data = 0x00000000003C087F
```

Mailbox registers in EC space (DSDT `Field(ECMP)`):

```
0x8A LDAT  0x8B HDAT  0x8C FLAGS(bit0=RFLG bit1=WFLG bit2=BFLG bit3=CFLG bit7=DRDY)
0x8D CMDL  0x8E CMDH
```

Sequence (`RKBC`/`WKBC`, holding the `ECMX` mutex): clear DRDY → write address[/data] → set RFLG/WFLG → poll DRDY (20×50 ms) → read CMDL/CMDH.

### 2.4 H2RAM window model (why 0x087F is unreachable)

Host-visible EC memory is not the full 64 KB xram but a 4 KB window with four EC-configured mappings:

| Window | EC xram range | host read | host write |
|---|---|---|---|
| 0 | 0x000–0x3FF | ✅ | ❌ |
| 1 | 0x400–0x5FF | ✅ | only 0x400–0x4FF |
| 2 | 0x600–0x7FF | ✅ | only 0x700–0x7FF |
| 3 | 0xC00–0xFFF | ✅ | ✅ |

`0x800–0xBFF` is entirely outside the window → `0x087F` always reads `0xFF` and cannot be written. Remapping the window would work, but reconfiguring H2RAM on a running system is uncontrollable-risk; not recommended.

## 3. Root-cause chain

w568w's RE of the WUJIE 14XA EC firmware produced the charge-control loop (runs every second):

```c
bool state_is(uint8_t v) { return xram[0x07C3] == v || xram[0x0770] == v; }
bool limit_enabled = state_is(4) || state_is(5);

if (!limit_enabled) {
    uint8_t stored_limit = xram[0x087F] & 0x7f;
    if (stored_limit == 0 || stored_limit > 100) {
        disable_control();   // this unit always stops here
        return;
    }
}
enable_control();   // enforce live limit xram[0x07B9]
store_limit();      // live → stored (xram[0x087F])
```

Plugging in this unit's measured values: `0x07C3=7`, `0x0770=0xFF` → `limit_enabled` always false; `0x087F` in the dead zone always reads `0xFF` → `stored_limit=127` invalid → `disable_control()` forever.

**Writes to `0x07B9` do nothing not because software is buggy — the firmware simply never reads it.**

Important detail: in this firmware a valid `0x087F` alone suffices to enable limiting (in the `!limit_enabled` branch, a legal stored_limit falls through to `enable_control()`) — this is the theoretical basis of Method B.

## 4. Fix A: ROMID[0] pin (verified on hardware)

### Principle

`state_is()` checks two registers: `0x07C3` (real platform ID, =7 here) and `0x0770` — the latter being the Linux driver's `EC_ADDR_ROMID_START`, i.e. **the first byte of the 14-byte ROM ID region** (Uniwill's product-line identifier). This unit's full ROMID reads `FF FF 01 FF FF...` (first byte unprogrammed); TUXEDO devices read `0C xx 01 ...`. The gate's true semantics: "charge limiting is enabled only for product lines with ROMID[0] or platform value 4/5".

Writing `xram[0x0770] = 0x04` → `state_is(4)` holds → the control loop runs. Note the kernel/tuxedo drivers write ROMID via a formal handshake (`0x077E=0xA5`, `0x077F=0x78` unlock, then byte-wise write); on this firmware a plain write to `0x0770` takes effect, so the address has no extra write protection.

### Observed timing

```
write 0x0770=0x04
t+0s : 770=04 7B9=3c(bit7=0) 742=22(bit2=0)
t+5s : 770=04 7B9=bc(bit7=1) 742=26(bit2=1)   ← gate open, REACHED set
temps/fans uneventful over 40 s
```

Restoring `0x0770=0xFF` closes the gate within ~5 s → **this firmware does NOT write back `0x087F` while the gate is open** (unlike w568w's machine; on EC 1.32 the store_limit path isn't triggered or needs other conditions).

### End-to-end verification

Discharge to 55% → plug in → `BatteryStatus.Charging=True, ChargeRate≈29 W` → capacity climbs → stops at **48,048 mWh (exactly 60.0%)**: `Charging=False, ChargeRate=0`, `0x07B9` bit7 (REACHED) set. Full timestamped record: [verification-log.md](verification-log.md).

### Implementation

`pin_limit.py`: `--watch N` re-asserts `0x0770=4` + `0x07B9=<limit>` every N s; `--limit` changes the target (default 60); `--off` restores `0xFF`. Autostart via a Startup-folder launcher (no admin).

## 5. Fix B: WMI mailbox write of `0x087F` (cleaner in theory, unverified)

If `xram[0x087F]` holds a valid value (1–100), no platform override is needed — the `!limit_enabled` branch falls through to `enable_control()` upon seeing a legal stored_limit — **zero platform side effects**.

The only channel is admin WMI `GetSetULong` (`tools/wmi_stored_limit.ps1`):

```
probe : MB-Read 0x0740  → expect ..1A (proves the mailbox backend exists)
        MB-Read 0x087F  → inspect current stored value
write : MB-Write 0x087F = 0x3C → read back to verify
```

Two unverified points (someone has to run it to find out):

1. Whether this EC 1.32 firmware implements the mailbox backend handler (the AML side in the DSDT is complete, but the EC-side handler's existence can't be confirmed statically from the host)
2. Whether the written value is persisted to flash (it is on w568w's machine; unverified here. If not persisted, it dies on reboot and Method A is better anyway)

## 6. Register map (measured on this unit)

| Address | Value | Meaning |
|---|---|---|
| 0x0740 | 0x1A | PROJECT_ID=26 |
| 0x0741 | 0x81 | AP_OEM / manual-mode flag |
| 0x0742 | 0x22/0x26 | SUPPORT_5; **bit2 = charge-limit gate state** |
| 0x0765/0x0766 | 0xA1/0x94 | SUPPORT_1/2 |
| 0x0770 | 0xFF→**0x04** | **ROMID[0]** (`EC_ADDR_ROMID_START`, first byte of the 14-byte ROM ID region 0x0770–0x077D; unprogrammed 0xFF on this unit, the write point of this fix) |
| 0x077E/0x077F | 0x55/0xAA | ROMID write-handshake bytes (`ROMID_SPECIAL_1/2`; tuxedo writes 0xA5/0x78 to unlock before programming ROMID) |
| 0x07A6 | 0x21 | charge-profile bits[5:4] (0=high capacity 1=balanced 2=stationary) |
| 0x07B9 | 0x3C/0xBC | **live limit** bits[6:0]; **bit7 = REACHED** |
| 0x07BA | 0x00 | semantics unconfirmed; used here as a harmless mailbox write probe (write had no effect) |
| 0x07C3 | 0x07 | platform/state value (real platform ID=7) |
| 0x087F | 0xFF | **stored limit** — inside the H2RAM dead zone, reachable only via the mailbox |

## 7. Dead ends (don't retry these)

| Attempt | Result |
|---|---|
| Hand-rolled EC-space R/W over ports 0x62/0x66 (cmd 0x80/0x81) | Command bytes are consumed by the EC (IBF pulses visible) but **OBF never sets, zero response**; blind mailbox writes also ineffective. Presumably Windows acpi.sys's EC tasker wins every response byte, or the channel doesn't speak the standard command set |
| I2EC debug port (0x681-0x683) | Ports respond (DF/E8/9E) but the data port doesn't track the address latch → I2EC not enabled on this firmware |
| Direct `inpoutx64.sys` | Device symlink exists but won't open, and the port path was dead anyway → abandoned |
| Driver IOCTL for arbitrary multi-arg method eval | Only A504 single-arg dynamic eval + the fixed method table; `ECRW` (2 args) has no matching IOCTL |
| Official BIOS/EC update package | Vendor download page only ships driver bundles, no BIOS/EC downloads; direct directory access 403 |
| MMRD to read EC firmware | MMRD only reaches host physical memory; EC code lives in its own SPI flash, invisible to the host |

## 8. Risks & notes

- **Platform-override side effects**: `0x0770=4` affects every `state_is()` branch (possibly including power/thermal tables). Temps, fans, performance measured normal, but not all firmware paths are enumerated; on any anomaly run `pin_limit.py --off` to revert instantly
- **Volatility**: Method A lives in EC RAM and is lost on EC reset/reboot → depends on startup replay; that's a feature not a bug (leaves no trace, zero brick risk)
- **Percentage precision**: Windows' displayed charge and the EC's internal SOC can differ by ±1–2%
- **Read before write**: record original values before any write; don't write registers of unknown semantics
- **OpenRevo compatibility**: OpenRevo continuously writes `0x07B9`/`0x07A6` — not a conflict (it writes exactly 60); if you change its preset, the watcher pins `0x07B9` back to the configured limit

## 9. References

- w568w, "My Battery Charging Limit Fix for MECHREVO Wujie 14XA": https://gist.github.com/w568w/957976b59906e0ce5d6c13ad342e1593 — original source of the charge-logic pseudocode and the 0x07C3/0x0770/0x087F semantics
- tuxedo-drivers `uniwill_wmi.c` — direct/WMI mailbox reference implementation
- Linux `drivers/platform/x86/uniwill/uniwill-acpi.c` + `Documentation/wmi/devices/uniwill-laptop.rst` — register table; official record of the ECRR/ECRW 0xFFF limit
- cyear/NUCtool — `UWACPIDriver` IOCTL protocol
- ACPICA `iasl` — DSDT decompilation
- faintonce/open-revo — the open-source console whose driver provides the no-admin EC channel
