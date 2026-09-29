# 集成指南 / Integration Guide

> 给想把充电限制修复做进控制台类软件（OpenRevo 等）的开发者。
> For developers integrating the charge-limit fix into control software.

## TL;DR

**只写 `0x07B9` 不够。** 固件先查门控再读阈值。正确顺序：**检测 → 判断 → 再决定写不写**。
Writing `0x07B9` alone is not enough — the firmware checks a gate before ever reading the threshold. Correct order: **detect → classify → decide whether to write**.

## 检测流程 / Detection flow

全部寄存器只读即可判定，零风险：

```
1. 写 0x07B9 = <limit>          （正常设置上限）
2. 等 ~5 秒，读 0x0742
   bit2 = 1  → 门控开着，功能原生生效，什么都不用做 ✅ DONE
   bit2 = 0  → 门控关着，继续 ↓
3. 读 0x0770 (ROMID[0])
   = 0xFF    → 未编程空槽，优先试写 0x07C3=0x04（platform 字节，
               不碰产品身份）→ 跳转"启用路径"
   ≠ 0xFF    → ROMID 已编程（如 0x0C），禁止写入 → 仍可先试 0x07C3=0x04，
               或直接走"存储值路径"
4. 读 0x07C3 记原值 → 写 0x07C3=0x04 → 等 ~5s 读 0x0742 bit2 验证门开
   （注：参考机型固件上 0x07C3 只认 4，写 5 不开门；其他固件常数可能不同）
```

```
1. Write 0x07B9 = <limit>       (set the limit normally)
2. Wait ~5 s, read 0x0742
   bit2 = 1  → gate already open, feature works natively, done ✅
   bit2 = 0  → gate closed, continue ↓
3. Read 0x0770 (ROMID[0])
   = 0xFF    → unprogrammed slot, prefer writing 0x07C3=0x04 (platform
               byte — does not touch product identity) → "enable path"
   ≠ 0xFF    → programmed ROMID (e.g. 0x0C), DO NOT write → still try
               0x07C3=0x04, or go straight to the stored-limit path
4. Read 0x07C3 to save original → write 0x07C3=0x04 → wait ~5 s, verify
   0x0742 bit2 (on tested firmware 0x07C3 accepts 4 ONLY; 5 does nothing)
```

## 三条路径 / Three paths

### 路径 1：门控已开（gate already open）

`0x07C3` 或 `0x0770` 出厂即为 4/5 的机型——写 `0x07B9` 就生效。无需任何动作。

### 路径 2：ROMID 空槽钉扎（ROMID[0] pin）

仅当 `0x0770 == 0xFF`：

```
写 0x0770 = 0x04  →  ~5s 内 0x0742 bit2 置位 → 限充环路开始执行
恢复 0x0770 = 0xFF →  ~5s 内门控回落（回滚）
```

注意 / Notes:

- **易失**：EC 复位/重启丢，需要周期性重放或启动时重放（本项目 watcher 每 10s 一次）
- **副作用面**：ROMID/平台值影响所有 `state_is()` 分支（含功耗/温控表的可能性）。已实测无异常，但应给用户知情提示
- EC 复位期间若被其他软件改写，重放会覆盖回来——OpenRevo 集成后天然具备这个能力（服务常驻）

### 路径 3：WMI 写存储上限（stored-limit write）

仅当 ROMID 已编程、不能写 `0x0770` 时考虑。需要管理员权限，走官方 mailbox 通道：

```
WMI AcpiTest_MULong.GetSetULong：
  读：Data(u64) = (0x0100 << 32) | addr
  写：Data(u64) = (value << 16) | addr
写 0x087F = <limit>，读回验证；0xFEFEFEFE = mailbox 未实现
```

- 有效则门控由存储值维持（`!limit_enabled` 分支 fallthrough），零平台副作用
- 持久化与否取决于固件（w568w 的机器掉电保留；JIAOLONG EC 1.32 未验证）
- 参考实现：`tools/wmi_stored_limit.ps1`

## 硬性安全规则 / Hard safety rules

| 规则 | 原因 |
|---|---|
| `0x0770` 只在 == 0xFF 时写 | 已编程 ROMID 是产品身份，覆盖会破坏 SKU 检测 |
| `0x07C3` 优先写 `0x04`，写前留存原值 | 第二门控操作数，参考机型实测认 4 不认 5；不改产品身份，但仍是 `state_is()` 判据，可能喂其他产品线表——须逐项验证副作用 |
| ~2020 前后老机型不提供此功能 | 内核 CVE-2026-64143：那些机型上限充可**永久损坏电池**——门控可能是保护性的 |
| 任何写前读原值并留存 | 回滚需要 |
| 门控关闭 ≠ 接口坏了 | 是产品线授权——用户提示语别说"故障"，说"固件未对该产品线启用" |

## 自检建议 / Suggested self-check

软件设置上限后读 `0x0742` bit2：

- 置位 = 固件真的会执行
- 未置位 = 写了也是摆设——**这是区分"设置成功"和"实际生效"的唯一可靠信号**

CR1 的 MechrevoBatteryManager 就是卡在这：逻辑写了 `0x07B9`/`0x07D0` 且读回了 `0x0742`，但没把 bit2 当作"未生效"信号处理，也没尝试开门——所以在门控关闭的机器上注定无效。

## 相关定义 / Register reference

| 地址 | 语义 |
|---|---|
| `0x0742` bit2 | 门控/控制环路激活状态（只读观察） |
| `0x0770` | ROMID[0]，`EC_ADDR_ROMID_START`，产品线 ID 首字节 |
| `0x077E/0x077F` | ROMID 写入握手（官方写序：0xA5/0x78 解锁；本机实测直接写也生效） |
| `0x07A6` bits[5:4] | 充电档位：0=high_capacity 1=balanced 2=stationary |
| `0x07B9` | live limit bits[6:0] + bit7=REACHED |
| `0x07C3` | platform 字节，第二门控操作数（优先用 4 试开，勿写其他值） |
| `0x087F` | stored limit（H2RAM 盲区 0x800-0xBFF 内，只能经 mailbox） |

更完整的协议/逆向细节：[RESEARCH.md](RESEARCH.md) / [RESEARCH_EN.md](RESEARCH_EN.md)
