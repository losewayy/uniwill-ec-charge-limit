# uniwill-ec-charge-limit

Fix for the "charge limit is set but never enforced" EC firmware issue on Uniwill/Tongfang-ODM laptops.

Tested on: MECHREVO JIAOLONG 16 Pro 2025 (8945HX + RTX 5070 Ti, BIOS `N.1.21MRO34`, EC `1.32`).
In principle applies to any Uniwill-EC machine using `UWACPIDriver.sys` / the `AcpiTest_MULong` WMI interface (MECHREVO, XMG, TUXEDO, Eluktronics and other Tongfang-family devices) — **but EC behavior varies per model/firmware; run read-only probes first**.

> 中文文档：[README.md](README.md)

---

## Symptom

- Setting a charge limit (e.g. "Workstation mode, 60%") in the vendor console / OpenRevo does write `0x3C` to EC register `0x07B9`
- Yet the battery still charges to 100%

## Root cause

The EC firmware runs a charge-control loop once per second, gated at the top by a platform check (from [w568w's firmware reverse engineering](https://gist.github.com/w568w/957976b59906e0ce5d6c13ad342e1593)):

```c
state_is(v) = (xram[0x07C3] == v) || (xram[0x0770] == v);
limit_enabled = state_is(4) || state_is(5);

if (!limit_enabled) {
    stored_limit = xram[0x087F] & 0x7f;
    if (stored_limit == 0 || stored_limit > 100) {
        disable_control();   // this unit always lands here
        return;
    }
}
enable_control();  // uses live limit xram[0x07B9]
store_limit();
```

`0x0770` is **`EC_ADDR_ROMID_START`** in the Linux driver: the first byte of a 14-byte ROM ID region (`0x0770–0x077D`) that Uniwill uses to distinguish product lines. The firmware only offers the charge limit to product lines whose ROMID[0] or platform value is 4/5 — a **per-product-line entitlement gate**.

On this unit `0x07C3=7` and ROMID[0]=`0xFF` (unprogrammed) → `limit_enabled` is always false; and `0x087F` (the stored limit) sits in a host-invisible H2RAM dead zone (`0x800–0xBFF`) so it always reads `0xFF` → `disable_control()` forever, writes to `0x07B9` have no effect.

## Fixes

### Method A: ROMID[0] pin (recommended, verified on real hardware, no admin)

Write `xram[0x0770] = 0x04` — the first ROM ID byte. It shipped as `0xFF` (unprogrammed) on this unit; writing it satisfies the gate and the control loop immediately starts enforcing the limit.

- ✅ One writable register; no firmware changes, no hidden addresses, no admin needed
- ✅ Verified: gate opens within ~5 s (`0x0742` bit2, `0x07B9` bit7 set); 55% → stops at exactly **60.0%** (48,048/80,080 mWh)
- ⚠️ **Prerequisite: ROMID[0] must be `0xFF`** (check with `python tools\ec_probe.py read 0x0770` first). On machines with a programmed ROMID (e.g. `0x0C` on TUXEDO devices) writing it **changes product-line identification** and may confuse SKU detection — do NOT use this method there
- ⚠️ Side-effect surface: ROMID/platform values feed all `state_is()` branches (in theory including power/thermal tables); temps, fans and performance measured normal; **always reversible via `pin_limit.py --off`**
- ⚠️ Old-model warning: the Linux driver docs note that on some ~2020 models the charge-limit interface can **permanently damage the battery** — this tool is only recommended for newer devices that shipped with a charge-limit option whose firmware fails to execute it
- ⚠️ Volatile: lost on EC reset/reboot; replayed at logon (watcher + startup launcher included)

```powershell
# Read-only status (writes nothing)
python tools\pin_limit.py --status

# One-shot pin (default limit 60%; --limit to change)
python tools\pin_limit.py --limit 60

# Resident watcher (re-asserts every 10 s, survives resets by other software)
pythonw tools\pin_limit.py --watch 10

# Revert
python tools\pin_limit.py --off
```

Autostart: `tools\install_startup.bat` creates a launcher in the current user's Startup folder (no admin needed). Delete that file to remove autostart.

### Method B: write stored limit `0x087F` (the "official" route, needs admin, NOT yet verified on this unit)

Per the firmware pseudocode: even when the platform gate fails, a valid `xram[0x087F]` (1–100) makes the loop enforce the limit anyway. `0x087F` is above the host-writable H2RAM window (dead zone beyond `0x07FF`), but the firmware's own **WMI EC mailbox** (`AcpiTest_MULong::GetSetULong` → `WMBC` → `OEMG` → `RKBC/WKBC`) can reach it — the official channel used by the vendor console.

```powershell
# Run elevated PowerShell
.\tools\wmi_stored_limit.ps1            # probe only: mailbox read of 0x0740 should return ..1A, then reads 0x087F
.\tools\wmi_stored_limit.ps1 -Write     # after probe looks sane, write 0x087F = 60
```

- If the write verifies → the stored limit is effective and Method A can be removed (`pin_limit.py --off`); the gate stays open via the stored value — **zero side effects**
- If the write doesn't stick or isn't persisted by firmware (xram is volatile), fall back to Method A
- Two unknowns: whether this EC implements the mailbox backend, and whether the value survives reboot (w568w's machine persists it; this unit unverified)

## How to verify

The only reliable test: discharge below the limit (e.g. 55%), reconnect AC, watch whether charging stops at 60%.

```powershell
# At 60% you should see:
Get-CimInstance -Namespace root\wmi -ClassName BatteryStatus |
  Select Charging, ChargeRate, RemainingCapacity
# Charging=False, ChargeRate=0, RemainingCapacity ≈ 48048 (60% × 80080)

# EC-side confirmation (no admin):
python tools\ec_probe.py read 0x07b9   # 0xbc = bit7 REACHED set
python tools\ec_probe.py read 0x0742   # bit2=1 = gate open
```

Full timestamped record (Windows battery report + live measurements): [docs/verification-log.md](docs/verification-log.md)

## Tools

| File | Purpose |
|---|---|
| `tools/ec_probe.py` | EC xram read/write probe (`read 0xADDR` / full dump), no admin |
| `tools/pin_limit.py` | Method A: `--watch N` resident / `--off` revert |
| `tools/wmi_stored_limit.ps1` | Method B: WMI mailbox read/write of `0x087F` (admin) |
| `tools/mailbox.py` | Reference implementation of the port-0x62/0x66 mailbox (firmware did not respond on this unit; kept for other models) |
| `tools/install_startup.bat` | Installs the watcher into the user's Startup folder |

Dependencies: Python 3 stdlib only + an installed `UWACPIDriver.sys` (installed by OpenRevo / the vendor console; device node `\\.\ACPIDriver`).

## Safety & disclaimer

- All writes target **volatile** EC registers; a power cycle restores factory state — nothing is flashed
- Record the original value before writing; do not poke registers whose semantics you don't know
- Provided "as is"; the author is not liable for hardware damage or data loss. The EC is the power-management core of the laptop — proceed at your own risk

## Credits & references

- **[w568w's WUJIE 14XA fix gist](https://gist.github.com/w568w/957976b59906e0ce5d6c13ad342e1593)** — the foundational EC firmware RE; this project's root-cause analysis builds entirely on it
- [tuxedo-drivers `uniwill_wmi.c`](https://github.com/tuxedocomputers/tuxedo-drivers) — WMI/direct mailbox protocol reference
- [Linux `uniwill-acpi.c` / `uniwill-laptop` driver](https://docs.kernel.org/wmi/devices/uniwill-laptop.html) — authoritative register table
- [cyear/NUCtool](https://github.com/cyear/NUCtool) — source of the `UWACPIDriver` IOCTL protocol
- [faintonce/open-revo](https://github.com/faintonce/open-revo) — open-source console whose installed driver provides the no-admin EC channel

Full reverse-engineering notes (decoded IOCTL dispatch table, H2RAM window model, DSDT/WMI protocol, dead ends) in [docs/RESEARCH_EN.md](docs/RESEARCH_EN.md) (中文原文：[docs/RESEARCH.md](docs/RESEARCH.md)).

## For software developers

To integrate detection/fix into control software (e.g. OpenRevo): [docs/INTEGRATION.md](docs/INTEGRATION.md) — decision flow, the three paths, hard safety rules.

## Related discussions

- OpenRevo issue: https://github.com/faintonce/open-revo/issues/38
- tuxedo-drivers issue (GitLab): https://gitlab.com/tuxedocomputers/development/packages/tuxedo-drivers/-/work_items/392

## License

MIT — see [LICENSE](LICENSE).
