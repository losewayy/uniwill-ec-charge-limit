# uniwill-ec-charge-limit

修复同方（Uniwill/Tongfang）模具笔记本"设置了充电上限却不生效"的 EC 固件问题。

实测机型：机械革命蛟龙 16 Pro 2025（JIAOLONG，8945HX + RTX 5070 Ti，BIOS `N.1.21MRO34`，EC `1.32`）。
原理上适用于所有使用 Uniwill EC + `UWACPIDriver.sys` / WMI `AcpiTest_MULong` 接口的机型（机械革命、XMG、TUXEDO、Eluktronics 等同模具家族），但**不同机型的 EC 固件行为可能不同，请先只读验证**。

> English version: [README_EN.md](README_EN.md) · English research notes: [docs/RESEARCH_EN.md](docs/RESEARCH_EN.md)

---

## 症状

- 在官方控制台 / OpenRevo 里设置了充电上限（如"工作站模式 60%"），EC 寄存器 `0x07B9` 也确实被写成了 `0x3C`
- 但电池照样充到 100%

## 根因

EC 固件每秒执行的充电控制循环开头有一个平台门控（来自 [w568w 的固件逆向](https://gist.github.com/w568w/957976b59906e0ce5d6c13ad342e1593)）：

```c
state_is(v) = (xram[0x07C3] == v) || (xram[0x0770] == v);
limit_enabled = state_is(4) || state_is(5);

if (!limit_enabled) {
    stored_limit = xram[0x087F] & 0x7f;
    if (stored_limit == 0 || stored_limit > 100) {
        disable_control();   // 本机永远走这里
        return;
    }
}
enable_control();  // 使用 live limit xram[0x07B9]
store_limit();
```

`0x0770` 即 Linux 驱动中的 **`EC_ADDR_ROMID_START`**：14 字节 ROM ID 区（`0x0770–0x077D`）的首字节，Uniwill 用 ROMID 区分产品线。固件只对 ROMID[0] 或平台值为 4/5 的产品开放充电限制——这是一条**产品线授权门**。

本机 `0x07C3=7`、ROMID[0]=`0xFF`（未编程）→ `limit_enabled` 恒为假；`0x087F`（存储上限）又位于主机不可写的 H2RAM 盲区（`0x800-0xBFF`），读出恒为 `0xFF` → 永远 `disable_control()`，写 `0x07B9` 毫无效果。

## 修复方案

### 方案 A：ROMID[0] 钉扎（推荐，已实机验证，免管理员）

写 `xram[0x0770] = 0x04`——即 ROM ID 首字节。本机出厂为 `0xFF`（未编程的空槽），写入后门控条件成立，控制循环立刻开始执行限充。

- ✅ 只写一个可写寄存器，不改固件、不碰隐藏地址、无需管理员
- ✅ 已验证：门控 5 秒内打开（`0x0742` bit2、`0x07B9` bit7 置位），55% → 充到精确 **60.0%**（48,048/80,080 mWh）自动停充
- ⚠️ **适用前提：ROMID[0] 必须为 `0xFF` 空值**（先 `python tools\ec_probe.py read 0x0770` 确认）。若你的机器 ROMID 已编程（如 TUXEDO 设备为 `0x0C`），写入会**改变设备产品线识别**，可能导致其他 SKU 检测逻辑错乱——请勿使用本方案
- ⚠️ 副作用面：ROMID/平台值会影响 EC 内所有 `state_is()` 分支（理论上也包括功耗/温控表），实测温度、风扇、性能均正常；**可随时 `pin_limit.py --off` 撤销**
- ⚠️ 老机型特别警告：Linux 驱动文档记载部分 ~2020 机型上充电限制可能**永久损坏电池**——本工具仅建议用于确认出厂带充电限制选项、但固件未执行的新型设备
- ⚠️ 易失：EC 复位/重启后丢，需要启动时重放（已提供 watcher + 开机自启方式）

```powershell
# 只读状态查询（不写任何寄存器）
python tools\pin_limit.py --status

# 一次性钉扎（默认上限 60%，--limit 可改）
python tools\pin_limit.py --limit 60

# 后台常驻（每 10 秒重新断言，防其他软件重置）
pythonw tools\pin_limit.py --watch 10

# 撤销
python tools\pin_limit.py --off
```

开机自启：运行 `tools\install_startup.bat` 会在当前用户的启动文件夹生成一个 launcher（不需要管理员）。删除该文件即取消自启。

### 方案 B：写存储上限 `0x087F`（更"正式"，需要管理员，本机未验证）

由固件伪代码可知：即使平台门控不成立，只要 `xram[0x087F]` 是合法的 1-100，控制循环照样执行。`0x087F` 高于 H2RAM 可写窗口（`0x07FF` 以上盲区），但可以通过固件自带的 **WMI EC mailbox**（`AcpiTest_MULong::GetSetULong` → `WMBC` → `OEMG` → `RKBC/WKBC`）访问，这是厂商控制台使用的官方通道。

```powershell
# 需要以管理员身份运行 PowerShell
.\tools\wmi_stored_limit.ps1            # 先只读探测：mailbox 读 0x0740 应回 0x1A，再读 0x087F 看现状
.\tools\wmi_stored_limit.ps1 -Write     # 确认探测正常后，写 0x087F = 60
```

- 若写入后读回验证通过 → 存储上限有效，可以撤掉方案 A 的覆盖（`pin_limit.py --off`），门控由存储值维持打开，**零副作用**
- 若写入不落或固件不持久化（xram 断电即失），则退回方案 A
- 两个未知数：mailbox 后端是否在本机固件实现；写入是否被固件同步进 flash（w568w 的机器会持久化，本机未验证）

## 验证方法

唯一可靠验证：把电池放到上限以下（如 55%），插回电源，观察是否在 60% 停住。

```powershell
# 充到 60% 时应看到：
Get-CimInstance -Namespace root\wmi -ClassName BatteryStatus |
  Select Charging, ChargeRate, RemainingCapacity
# Charging=False, ChargeRate=0, RemainingCapacity ≈ 48048 (60% × 80080)

# EC 侧确认（免管理员）：
python tools\ec_probe.py read 0x07b9   # 0xbc = bit7 REACHED 置位
python tools\ec_probe.py read 0x0742   # bit2=1 = 门控开
```

实测完整记录（Windows 电池报告时间戳 + 实时测量）：[docs/verification-log.md](docs/verification-log.md)

## 工具一览

| 文件 | 作用 |
|---|---|
| `tools/ec_probe.py` | EC xram 读写探针（`read 0xADDR` / 全量 dump），免管理员 |
| `tools/pin_limit.py` | 方案 A 实现：`--watch N` 常驻 / `--off` 撤销 |
| `tools/wmi_stored_limit.ps1` | 方案 B 实现：WMI mailbox 读写 `0x087F`（需管理员） |
| `tools/mailbox.py` | 端口 0x62/0x66 手工 mailbox 协议的参考实现（本机实测固件不应答，留存供其他机型尝试） |
| `tools/install_startup.bat` | 把 watcher 装进当前用户启动文件夹 |

依赖：仅 Python 3 标准库 + 已安装的 `UWACPIDriver.sys`（OpenRevo / 机械革命官方控制台都会安装它；设备节点 `\\.\ACPIDriver`）。

## 安全与免责

- 所有写入均为**易失** EC 寄存器，断电即恢复，不刷写任何固件/闪存
- 写操作前建议先读原值以便回滚；不要在不了解语义的情况下写其他寄存器
- 本工具按"现状"提供，作者不对任何硬件损坏、数据丢失负责。笔记本 EC 是电源管理核心，操作自负风险

## 致谢与参考

- **[w568w 的 WUJIE 14XA 修复 gist](https://gist.github.com/w568w/957976b59906e0ce5d6c13ad342e1593)** —— EC 固件充电逻辑逆向的奠基工作，本项目的根因判断全部源自此
- [tuxedo-drivers `uniwill_wmi.c`](https://github.com/tuxedocomputers/tuxedo-drivers) —— WMI/直连 mailbox 协议参考实现
- [Linux `uniwill-acpi.c` / `uniwill-laptop` 驱动](https://docs.kernel.org/wmi/devices/uniwill-laptop.html) —— 权威寄存器表
- [cyear/NUCtool](https://github.com/cyear/NUCtool) —— `UWACPIDriver` IOCTL 协议来源
- [faintonce/open-revo](https://github.com/faintonce/open-revo) —— 开源控制台，其安装的驱动提供了免管理员 EC 通道

完整逆向过程（IOCTL dispatch 全表、H2RAM 窗口模型、DSDT/WMI 协议、走过的死路）见 [docs/RESEARCH.md](docs/RESEARCH.md)。

## 相关讨论

- OpenRevo issue：https://github.com/faintonce/open-revo/issues/38
- tuxedo-drivers issue（GitLab）：https://gitlab.com/tuxedocomputers/development/packages/tuxedo-drivers/-/work_items/392

## License

MIT — 见 [LICENSE](LICENSE)。

---

Full English documentation: [README_EN.md](README_EN.md) · [docs/RESEARCH_EN.md](docs/RESEARCH_EN.md)
