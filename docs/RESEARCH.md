# Uniwill EC 充电限制修复 — 完整逆向研究记录

> 研究对象：机械革命蛟龙 16 Pro 2025（JIAOLONG Series-X6xR55xK，Ryzen 9 8945HX + RTX 5070 Ti）
> 固件：BIOS `N.1.21MRO34`（2026-01-29），EC `1.32`，project_id `0x1A` / bios_project_id `TRX1`
> 平台：Windows 11，OpenRevo 已安装（提供 `UWACPIDriver.sys`）
> 结论：**问题已修复并实机验证**——55% 插电充到精确 60.0%（48,048/80,080 mWh）自动停充。

本文记录完整的分析过程：通道逆向、协议还原、根因定位、两种修复方案、以及走过的死路（供后续研究者避坑）。

---

## 1. 问题定义

用户层面："设置了充电上限（工作站模式 60%）但电池照样充到 100%"。

寄存器层面：上限值 `xram[0x07B9]` 确实被写成了 `0x3C`（OpenRevo 和官方控制台都能写进去），但 EC 不执行。`powercfg /batteryreport` 显示满充容量 = 设计容量（80,080 mWh），排除"已限充但显示 100%"的可能。

## 2. EC 访问通道全景

这台机器上有三条理论可用的 EC 访问路径：

| 通道 | 权限 | 可达范围 | 状态 |
|---|---|---|---|
| `\\.\ACPIDriver` IOCTL（H2RAM 窗口） | **免管理员** | xram `0x000-0x3FF` 只读、`0x400-0x4FF` 可写、`0x700-0x7FF` 可写、`0xC00-0xFFF` 可读写；**`0x800-0xBFF` 盲区** | ✅ 主用通道 |
| WMI `AcpiTest_MULong::GetSetULong`（EC mailbox） | 需管理员 | **全 16-bit xram**，含 `0x087F` | ✅ 方案 B 用 |
| 端口 `0x62/0x66` 手工 EC-space 协议 | 免管理员（经驱动 IOCTL） | 理论同上 | ❌ 本机固件不应答（§8 死路） |

### 2.1 `UWACPIDriver.sys` 的基础读写 IOCTL（已知，来自 NUCtool）

```
设备节点：\\.\ACPIDriver   （PnP 设备 ACPI\INOU0000，无需管理员打开）

IOCTL_EC_READ  = 0x9C40A488   in: u32 addr          out: u32（低字节=值）
IOCTL_EC_WRITE = 0x9C40A48C   in: u32 addr, u32 val out: 忽略
```

### 2.2 驱动 dispatch 全表（本次逆向解出）

驱动内部为每个 IOCTL 调用一个 `_SB.INOU` 下的 ACPI 方法。`.rdata` 里的四字符 tag 是**字节倒序**的 AML 方法名（如 `"DROI"` → `IORD`），因为它们被重新组装成大端 u32 后按小端存回。

| IOCTL | .rdata tag | 实际 ACPI 方法 | 语义 | 参数 |
|---|---|---|---|---|
| 0x9C40A488 | — | （直接 H2RAM） | EC xram 读 | u32 addr |
| 0x9C40A48C | — | （直接 H2RAM） | EC xram 写 | addr+val |
| 0x9C40A480 | SMCR | RCMS | CMOS 读 | idx |
| 0x9C40A484 | SMCW | WCMS | CMOS 写 | idx+?+val |
| 0x9C40A490 | BRMM | MMRB | 物理内存读 8-bit | u32 addr |
| 0x9C40A494 | DRMM | MMRD | **物理内存读 32-bit** | u32 addr |
| 0x9C40A498 | BWMM | MMWB | 物理内存写 8-bit | addr+val |
| 0x9C40A49C | DWMM | MMWD | 物理内存写 32-bit | addr+val |
| 0x9C40A4A0 | DRCP | PCRD | PCI 配置读 | addr |
| 0x9C40A4A4 | DWCP | PCWD | PCI 配置写 | addr+val |
| 0x9C40A4C0 | DROI | **IORD 端口读** | in 32-bit @ port | u32 port |
| 0x9C40A4C4 | DWOI | **IOWD 端口写** | out 32-bit @ port | port+val |
| 0x9C40A4C8 | POIR | RIOP | 索引端口读（base/base+1） | base+idx |
| 0x9C40A4CC | POIW | **WIOP 索引端口写** | outb(base,a1); outb(base+1,a2) | base+a1+a2 |
| 0x9C40A4D0-D8 | DRxT | T1RD/T2RD/T3RD | （空方法，占位） | — |
| 0x9C40A4DC-E4 | RWxT | T1WR/T2WR/T3WR | （同上） | — |
| 0x9C40A500 | — | SMRW | 40B buffer 读写 | — |
| 0x9C40A504 | 动态 | **通用单参数方法评估** | 输入方法名也要字节倒序 | name+u32 arg |

**实测验证**（全部只读、免管理员）：

```
MMRD(0xFED50740)      -> 1a 81 22 07   ← 与窗口读 0x740/741/742 完全一致
A504 "RRCE"(0x740)    -> 1a            ← 通用评估可用（"ECRR" 的倒序）
IORD(0x66)            -> 08            ← EC 状态口
IORD(0x681..0x683)    -> DF E8 9E      ← 端口有响应（后证实非 I2EC）
```

注意 `IORD`/`IOWD` 是 `ByteAcc` 的 **32 位**访问 → 一次读写 **4 个连续端口**。写 `0x62` 会连带写 `0x63-0x65`（`0x64` 是 legacy KBC 命令口，危险）。单字节写用 `WIOP(base, val, 0)`（写 base 与 base+1，`0x63`/`0x67` 无设备，无害）。

**附赠价值**：`MMRD`/`MMWD`/`IORD`/`IOWD` 意味着免管理员的任意物理内存与端口访问——对研究这些机器的人这是很有用的原语（也提醒：该驱动等于一个内核级后门，介意的用户可以卸载）。

### 2.3 WMI mailbox 协议（官方通道）

DSDT（`iasl` 反编译）中 WMI 设备 `AMW0` 的 `_WDG` 映射：

| GUID 后缀 | 对象 | 类型 |
|---|---|---|
| `ABBC0F6F-8EA1-11D1-00A0-C90629100000` | BC | 方法 → `WMBC` |

`WMBC(Arg0, Arg1, Arg2)` 按 `Arg1` 分发；`Arg1=4`（即 WMI 方法 `GetSetULong`）→ `OEMG(u64 data)` → 按 data 高 32 位分发到 EC mailbox：

- `SAC1 = 0`/`1` → `WKBC` mailbox 写
- `SAC1 = 0x0100` → `RKBC` mailbox 读
- `SAC1 = 0x0200` → `SCMD` EC 命令

载荷格式（与 tuxedo `uniwill_wmi.c`、NUCtool `uniwillwmi.rs` 一致；输入是 40 字节 buffer，`bytes[0:4]`=arg u32，`byte[5]`=function）：

```
读：Data(u64) = (0x0100 << 32) | addr
写：Data(u64) = (data_hi << 24) | (data_lo << 16) | addr        # function byte=0 → 写
返回：Return u32，低字节 = 值；0xFEFEFEFE = mailbox 超时/未实现

例：读 0x087F → Data = 0x010000000000087F
    写 0x087F=60 → Data = 0x00000000003C087F
```

mailbox 在 EC-space 的寄存器（DSDT `Field(ECMP)`）：

```
0x8A LDAT  0x8B HDAT  0x8C FLAGS(bit0=RFLG bit1=WFLG bit2=BFLG bit3=CFLG bit7=DRDY)
0x8D CMDL  0x8E CMDH
```

时序（`RKBC`/`WKBC`，持 `ECMX` 互斥锁）：清 DRDY → 写地址[/数据] → 置 RFLG/WFLG → 轮询 DRDY（20×50ms）→ 读 CMDL/CMDH。

### 2.4 H2RAM 窗口模型（为什么 0x087F 够不着）

主机可见的 EC 内存不是完整 64KB xram，而是一个 4KB 窗口，由 EC 侧配置四段映射：

| 窗口 | EC xram 范围 | 主机读 | 主机写 |
|---|---|---|---|
| 0 | 0x000–0x3FF | ✅ | ❌ |
| 1 | 0x400–0x5FF | ✅ | 仅 0x400–0x4FF |
| 2 | 0x600–0x7FF | ✅ | 仅 0x700–0x7FF |
| 3 | 0xC00–0xFFF | ✅ | ✅ |

`0x800–0xBFF` 完全不在窗口里 → `0x087F` 直接读恒 `0xFF`、写不入。改窗口配置能绕开，但在运行中的系统上改 H2RAM 配置风险不可控，不推荐。

## 3. 根因链

w568w 对无界 14XA EC 固件的逆向给出充电控制循环（每秒执行）：

```c
bool state_is(uint8_t v) { return xram[0x07C3] == v || xram[0x0770] == v; }
bool limit_enabled = state_is(4) || state_is(5);

if (!limit_enabled) {
    uint8_t stored_limit = xram[0x087F] & 0x7f;
    if (stored_limit == 0 || stored_limit > 100) {
        disable_control();   // 本机永远停在这里
        return;
    }
}
enable_control();   // 按 live limit xram[0x07B9] 执行
store_limit();      // live → stored (xram[0x087F])
```

本机实测值代入：`0x07C3=7`、`0x0770=0xFF` → `limit_enabled` 恒假；`0x087F` 在盲区恒读 `0xFF` → `stored_limit=127` 无效 → 永远 `disable_control()`。

**写 `0x07B9` 无效不是软件 bug，是固件根本没去读它。**

重要细节：这套固件里 `0x087F` 有效本身就能打开限充（`!limit_enabled` 分支里只要 stored_limit 合法就会 fallthrough 到 `enable_control()`）——这是方案 B 的理论基础。

## 4. 修复方案 A：平台覆盖槽（已实机验证）

### 原理

`state_is()` 检查两个寄存器：`0x07C3`（真实平台 ID，本机=7）和 `0x0770`（**覆盖槽**，出厂 `0xFF`）。后者就在可写窗口 `0x700-0x7FF` 内——不需要碰隐藏地址。

写 `xram[0x0770] = 0x04` → `state_is(4)` 成立 → 控制循环开始执行。

### 实测时序

```
写 0x0770=0x04
t+0s : 770=04 7B9=3c(bit7=0) 742=22(bit2=0)
t+5s : 770=04 7B9=bc(bit7=1) 742=26(bit2=1)   ← 门控打开，REACHED 置位
温度/风扇 40s 无异常
```

恢复 `0x0770=0xFF` 后 ~5s 门控回落 → **本机固件在门控开启期间不会自动写回 `0x087F`**（与 w568w 机器行为不同，EC 1.32 上 store_limit 路径未被触发或需要其他条件）。

### 端到端验证

电池放电至 55% → 插电 → `BatteryStatus.Charging=True, ChargeRate≈29W` → 容量爬升 → 在 **48,048 mWh（精确 60.0%）停止**：`Charging=False, ChargeRate=0`，`0x07B9 bit7`（REACHED）置位。

### 实现

`pin_limit.py`：`--watch N` 每 N 秒重新断言 `0x0770=4` + `0x07B9=<limit>`；`--limit` 改目标百分比（默认 60）；`--off` 恢复 `0xFF`。开机自启用启动文件夹 launcher（免管理员）。

## 5. 修复方案 B：WMI mailbox 写 `0x087F`（理论更优，待验证）

如果 `xram[0x087F]` 存着合法值（1-100），则无需任何平台覆盖——`!limit_enabled` 分支看到有效 stored_limit 后自然 fallthrough 到 `enable_control()`，**零平台副作用**。

唯一通道是管理员态 WMI `GetSetULong`（`tools/wmi_stored_limit.ps1`）：

```
探测：MB-Read 0x0740  → 应回 ...1A（验证 mailbox 后端存在）
      MB-Read 0x087F  → 看当前存储值
写入：MB-Write 0x087F = 0x3C → 再读验证
```

两个未验证点（需要有人跑一下才知道）：

1. 本机 EC 1.32 固件是否实现了 mailbox 后端处理（DSDT 的 AML 侧是齐的，但 EC 侧 handler 存在与否无法从主机侧静态确认）
2. 写入是否被固件持久化到 flash（w568w 的机器上会；本机未验证。若不持久化，重启丢，不如方案 A）

## 6. 寄存器地图（本机实测）

| 地址 | 值 | 含义 |
|---|---|---|
| 0x0740 | 0x1A | PROJECT_ID=26 |
| 0x0741 | 0x81 | AP_OEM / manual mode 标志 |
| 0x0742 | 0x22/0x26 | SUPPORT_5；**bit2 = 限充门控状态** |
| 0x0765/0x0766 | 0xA1/0x94 | SUPPORT_1/2 |
| 0x0770 | 0xFF→**0x04** | **平台覆盖槽（本次修复写入点）** |
| 0x077E/0x077F | 0x55/0xAA | 疑似持久化提交握手（源自其他机型固件分析，本机未验证） |
| 0x07A6 | 0x21 | 充电档位 bits[5:4]（0=长效 1=均衡 2=工作站） |
| 0x07B9 | 0x3C/0xBC | **Live limit** bits[6:0]；**bit7 = REACHED** |
| 0x07BA | 0x00 | 语义未确认；本次用作无害的 mailbox 写探针（写入未生效，无影响） |
| 0x07C3 | 0x07 | 平台/状态值（真实平台 ID=7） |
| 0x087F | 0xFF | **Stored limit**——H2RAM 盲区，只能经 mailbox 访问 |

## 7. 走过的死路（别再试）

| 尝试 | 结果 |
|---|---|
| 端口 0x62/0x66 手工 EC-space 读写（cmd 0x80/0x81） | 命令字节被 EC 消费（IBF 脉冲可见）但 **OBF 永不置位、零响应**；盲发 mailbox 写无效。推测 Windows acpi.sys 的 EC tasker 抢答了所有响应字节，或该通道不跑标准命令集 |
| I2EC 调试口（0x681-0x683） | 端口有响应（DF/E8/9E）但写地址锁存后数据口不跟踪 → 本固件未启用 I2EC |
| `inpoutx64.sys` 直连 | 设备符号链接存在但打不开，且端口路径本身就不通 → 废弃 |
| 驱动 IOCTL 评估任意多参数方法 | 只有 A504 单参数动态评估 + 固定方法表；`ECRW`（2 参数）无对应 IOCTL |
| 官方 BIOS/EC 更新包 | 官网下载页只有驱动合集，无 BIOS/EC 下载；目录直连 403 |
| MMRD 读 EC 固件 | MMRD 只能读主机物理内存；EC 代码在自己的 SPI flash 里，host 不可见 |

## 8. 风险与注意事项

- **平台覆盖副作用**：`0x0770=4` 影响所有 `state_is()` 分支（可能含功耗/温控表）。实测温度、风扇、性能正常，但未穷举所有固件路径；出现任何异常执行 `pin_limit.py --off` 即可立即恢复
- **易失性**：方案 A 的写在 EC RAM 中，EC 复位/重启后丢失 → 依赖启动重放；这不是缺陷而是特性（不留痕、零变砖风险）
- **百分比精度**：Windows 显示电量与 EC 内部 SOC 可能有 ±1-2% 出入
- **写前读原值**：所有写操作前先记录原值；不要写语义不明的寄存器
- **OpenRevo 兼容性**：OpenRevo 会持续写 `0x07B9`/`0x07A6`，与 pin 不冲突（它写的正是 60）；若改了它的档位设置，watcher 会把 `0x07B9` 顶回 `LIMIT_PCT`

## 9. 参考资料

- w568w，《My Battery Charging Limit Fix for MECHREVO Wujie 14XA》：https://gist.github.com/w568w/957976b59906e0ce5d6c13ad342e1593 —— 充电逻辑伪代码、0x07C3/0x0770/0x087F 语义的原始出处
- tuxedo-drivers `uniwill_wmi.c` —— mailbox 直连/WMI 双实现参考
- Linux `drivers/platform/x86/uniwill/uniwill-acpi.c` + `Documentation/wmi/devices/uniwill-laptop.rst` —— 寄存器表、ECRR/ECRW 上限 0xFFF 的官方记载
- cyear/NUCtool —— `UWACPIDriver` IOCTL 协议
- ACPICA `iasl` —— DSDT 反编译
- faintonce/open-revo —— 提供免管理员 EC 通道的开源控制台
