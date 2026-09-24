# 验证记录：充电精确停止于 60%

设备：MECHREVO JIAOLONG 16 Pro 2025（BIOS `N.1.21MRO34`，EC `1.32`）
电池：80,080 mWh 设计容量
修复状态：`xram[0x0770]=0x04` 钉扎 + watcher 常驻（方案 A）

## 数据来源

Windows `powercfg /batteryreport` 的 *Recent usage* 表（OS 侧自带时间戳记录，非自研脚本输出），已移除计算机名、序列号、完整 build 字符串等标识字段。

## 修复前基线（bug 现象）

设置"工作站模式 60%"后的数日里，电池始终停在 100%——`0x07B9=0x3C` 已写入但固件未执行：

```
2026-09-23 01:16:33 | Active | AC | 100 % | 80,080 mWh
2026-09-23 10:07:35 | Active | AC | 100 % | 80,080 mWh
2026-09-23 18:13:23 | Active | AC | 100 % | 80,080 mWh
2026-09-23 22:47:05 | Active | AC | 100 % | 80,080 mWh
```

## 验证过程（修复生效后）

```
START TIME            | STATE            | SOURCE   | CAPACITY REMAINING
2026-09-23 23:48:57   | Active           | Battery  | 100 % | 80,080 mWh   ← 拔电，放电测试开始
2026-09-24 00:16:06   | Active           | AC       |  55 % | 44,044 mWh   ← 插回电源（55% < 60%，开始充电）
2026-09-24 01:03:15   | Report generated | AC       |  60 % | 48,048 mWh   ← 停在精确的 60.0%
```

充电期间的实时测量（Windows `BatteryStatus` WMI 类）：

```
00:16 前后   Charging=True   ChargeRate=29,253 mW   RemainingCapacity=44,044 mWh
01:03        Charging=False  ChargeRate=0           RemainingCapacity=48,048 mWh  (60.0%)
```

## EC 寄存器状态（停充时点）

| 地址 | 值 | 解读 |
|---|---|---|
| `0x0770` | `0x04` | ROMID[0] 保持钉扎 |
| `0x07B9` | `0xBC` | live limit=60，**bit7 REACHED 置位**——EC 主动判定"已达上限" |
| `0x0742` | `0x26` | bit2=1，门控/控制环路处于激活状态 |

## 补充观察

- 回差行为：充至上限停充后不会因微小自放电立即重启充电（55% → 插电才恢复充电），符合正常充电控制逻辑
- 放电测试中 `0x07B9` 的 REACHED 位随状态实时翻转（满电 `0xBC` ↔ 放电中 `0x3C`），证明控制环路在以 ~1s 周期实际运行，而非一次性标志
- 全程温度、风扇转速、CPU/GPU 性能无异常
