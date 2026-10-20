# Bluetooth Dual Boot Sync

[English](README.en.md)

让 Windows 和 Linux 双系统共用一份经典蓝牙配对密钥，减少切换系统后重新配对的麻烦。

这个项目来自一次 Logitech K480 的实际排错：同一台电脑，键盘在 Ubuntu 配对后能用，进入 Windows 重新配对，再回到 Ubuntu 就连不上。将 Windows 的配对密钥同步给 Ubuntu 后，键盘在原来的档位恢复了输入。

仓库包含方法说明，以及一个默认只预览、支持备份和恢复的 Python 命令行工具。

## 它解决什么问题

双系统通常使用同一个物理蓝牙适配器，也就是同一个适配器地址，但两个系统分别保存配对密钥。重新配对后，键盘保留的新密钥可能与另一个系统中的旧密钥不一致。

解决思路是：**先在 Linux 配对，再在 Windows 配对，最后把 Windows 的密钥同步到 Linux。** 两个系统使用相同的密钥和适配器地址，设备才有条件在切换系统后继续连接。这个方法已有 [LinkWinBT](https://github.com/vvoland/linkwinbt) 等项目实现，并非本项目原创。

对于 K480，效果是让同一台双系统电脑共用一个档位。**它不会扩充 K480 的三个档位，也不会让键盘直接连接任意数量的独立电脑。** 三设备切换是键盘本身的设计，见[罗技产品说明](https://www.logitech.com/en-gb/shop/p/k480-multi-device-wireless.920-006362)。

## 支持范围和验证状态

| 项目 | 当前范围 |
| --- | --- |
| 蓝牙类型 | 经典蓝牙 BR/EDR，BlueZ 配置中已有 `[LinkKey]` |
| Linux | 使用 systemd、BlueZ 和默认 `/var/lib/bluetooth` 存储目录 |
| Windows 数据 | 可读的离线 `SYSTEM` 注册表文件，且含支持的经典蓝牙密钥记录 |
| 物理适配器 | 两个系统使用同一个适配器，地址一致 |
| BLE | 暂不支持 `LongTermKey`、IRK 等 BLE 配对信息 |
| 其他用途 | 不处理网络键鼠共享、固件扩容或不同电脑共用蓝牙身份 |

实际案例确认：**Ubuntu 24.04.4 LTS + Logitech K480，同步后在 Ubuntu 恢复输入。** 当时 Windows 版本由用户推测为 Windows 11，未实际读取版本；同步后再次切回 Windows 的验证仍待确认。

自动化测试检查的是解析、选定目标、文件更新、备份及错误处理逻辑。测试通过不等于其他设备、适配器、发行版或 Windows 版本已经实测。较新的 Windows 版本若改变密钥存储方式，本工具可能无法读取。

## 开始前

- 准备另一种输入方式，例如笔记本自带键盘或 USB 键盘。实际写入时蓝牙服务会短暂停止。
- 确认两个系统使用同一个蓝牙适配器。不要把内置适配器和 USB 适配器混用。
- 在 Linux 已经有这个设备的配对记录。工具只更新现有记录，不创建配对。
- 能读取 Windows 的 `Windows/System32/config/SYSTEM` 文件。
- 使用 Python 3.10 或更新版本。Ubuntu 上创建虚拟环境通常需要 `python3-venv`。

密钥、原始 BlueZ `info` 文件、`SYSTEM` 文件和本地备份都可能包含敏感信息。请留在本机，不要上传到 GitHub 或贴进 issue。

## 操作步骤

### 1. 按顺序配对

以 K480 的档位 2 为例：

1. 在 Ubuntu，把键盘切到档位 2，完成配对并确认能输入。
2. 重启进入 Windows，仍使用档位 2，重新配对并确认能输入。
3. 回到 Ubuntu。此时键盘可能暂时无法连接，继续下面的同步步骤。

完成同步后不要再删除设备或长按配对键，否则会重新生成密钥。确实需要重新配对时，重新完成以上顺序并同步。

### 2. 安装工具

```bash
git clone https://github.com/wzxsph/bluetooth-dualboot-sync.git
cd bluetooth-dualboot-sync
python3 -m venv .venv
.venv/bin/python -m pip install .
```

后面的命令都在仓库目录执行。创建虚拟环境和安装包不需要 `sudo`。

### 3. 只读访问 Windows 数据

先查看分区，自己确认哪个是 Windows 分区：

```bash
lsblk -f
```

下面的 `/dev/你的Windows分区` 是占位符，**必须替换，不能直接复制执行**：

```bash
sudo mkdir -p /mnt/windows
sudo mount -o ro /dev/你的Windows分区 /mnt/windows
```

工具读取的路径为：

```text
/mnt/windows/Windows/System32/config/SYSTEM
```

如果分区因休眠、快速启动或加密无法读取，请不要强制以读写方式挂载。可以在 Windows 完整关机后重试，或在 Windows 导出 `SYSTEM` hive 到自己可读的介质，再把命令中的路径改为导出文件。BitLocker 分区需要先用自己的解锁方式访问；使用本方法无需普遍关闭磁盘加密。

工具也可以读取该 hive 的本地只读副本。请保管好副本，用完后按自己的数据保留需求清理。

### 4. 确认适配器和设备

在 Ubuntu 查看：

```bash
bluetoothctl list
bluetoothctl devices
```

前者列出本机适配器，后者列出 BlueZ 已知设备。请确认设备名称和地址，尤其是机器上有多个适配器时。

再查看 Windows hive 中支持的经典蓝牙记录：

```bash
.venv/bin/bt-dualboot-sync list \
  --system-hive /mnt/windows/Windows/System32/config/SYSTEM
```

`list` 只列出适配器和设备地址，不打印密钥。文件可读时无需 `sudo`；遇到访问权限不足时可使用 `sudo` 读取，**不要修改系统文件权限**。如果没有列出目标设备，请先看[排错](#排错)，不要直接重新配对。

以下示例中 `AA:BB:CC:DD:EE:FF` 是本机适配器地址，`11:22:33:44:55:66` 是键盘地址，都是占位示例，必须替换成自己的值。

### 5. 先预览，再同步

先预览计划更新的那一条记录：

```bash
sudo .venv/bin/bt-dualboot-sync sync \
  --system-hive /mnt/windows/Windows/System32/config/SYSTEM \
  --adapter AA:BB:CC:DD:EE:FF \
  --device 11:22:33:44:55:66
```

默认预览不会写文件，也不会停止或重启蓝牙服务。确认选定的适配器和设备记录正确，再加上 `--apply`：

```bash
sudo .venv/bin/bt-dualboot-sync sync \
  --system-hive /mnt/windows/Windows/System32/config/SYSTEM \
  --adapter AA:BB:CC:DD:EE:FF \
  --device 11:22:33:44:55:66 \
  --apply
```

实际写入需要 root。工具会备份目标原文件，并显示备份位置，请保留这个路径。它会恢复蓝牙服务原先的运行状态：操作前运行，结束后重新启动；操作前未运行，结束后仍保持未运行。后一种情况下，要连接设备需自行执行：

```bash
sudo systemctl start bluetooth.service
```

### 6. 唤醒并验证

1. 保持原档位，把键盘电源关闭再打开，按几下按键。
2. 在 Ubuntu 的输入框里试着打字。
3. 重启进入 Windows，保持同一档位，确认还能输入。
4. 再回到 Ubuntu 测试一次。

**不要长按配对键，也不要删除设备重新配对。** 本次 K480 案例中，密钥更新后还需要关闭再打开键盘，才恢复连接。

## 工具具体做了什么

- 以只读方式解析 Windows `SYSTEM` hive，通过 `Select\Current` 找到当前控制集，不固定假设为 `ControlSet001`。
- 解析前检查 hive 的 REGF 校验和及两份序列号；需要注册表恢复的 hive 会被拒绝，不自动重放事务日志。
- 根据明确指定的适配器和设备地址，查找支持的经典蓝牙密钥。
- 对应 Linux 的 `/var/lib/bluetooth/<adapter>/<device>/info`，只替换 `[LinkKey]` 中的 `Key`，保留原有 `Type`、`PINLength` 和其他内容。
- 实际写入前停止 `bluetooth.service`，停止后重新读取目标文件，备份原文件，再通过原子替换写入。
- 将备份放在仅 root 可访问的 `/var/backups/bluetooth-dualboot-sync`，恢复时检查备份记录和文件摘要。
- 操作结束时恢复服务原先的运行状态；遇到写入或服务恢复错误时尝试恢复原文件并报告结果。

BlueZ 上游[存储格式说明](https://github.com/bluez/bluez/blob/master/doc/settings-storage.txt)记录了目录结构及密钥字段，也明确不鼓励外部直接修改存储。因此本工具在修改前停止服务，不在 `bluetoothd` 运行时直接写入。

断电、进程被强制终止或磁盘故障等情况，可能阻止自动恢复。请根据工具输出检查状态，必要时使用保留下来的备份。工具不修改 Windows hive，不自动挂载磁盘，不更改系统文件的访问权限。

## 恢复原记录

将 `<snapshot>` 替换成同步命令显示的备份目录名。先预览：

```bash
sudo .venv/bin/bt-dualboot-sync restore \
  --backup "/var/backups/bluetooth-dualboot-sync/<snapshot>"
```

确认后恢复：

```bash
sudo .venv/bin/bt-dualboot-sync restore \
  --backup "/var/backups/bluetooth-dualboot-sync/<snapshot>" \
  --apply
```

恢复操作只针对该备份记录的原目标文件，并验证备份内容。它同样会临时停止蓝牙服务并恢复此前的运行状态。恢复旧 Linux 密钥后，键盘若仍保存 Windows 的新密钥，连接可能仍失败；恢复文件不等于让键盘也恢复到旧配对状态。

## 排错

| 情况 | 建议 |
| --- | --- |
| `Host is down`、键盘没有响应 | 保持原档位，关闭再打开键盘，按键唤醒，检查电量和蓝牙服务状态。这个错误本身不能证明密钥错误。 |
| Linux 配对记录不存在或没有 `[LinkKey]` | 核对适配器和设备地址；可能选错适配器，或设备使用 BLE。不要为了补文件直接重配；需要重新配对时，应按 Linux → Windows → 同步的完整顺序进行。 |
| Windows 中找不到目标设备 | 确认读取的是本次 Windows 安装的 `SYSTEM` 文件，同档位已在 Windows 配对，适配器地址一致。Windows 存储变化也可能导致不支持。 |
| `hive needs registry recovery` | 该 hive 尚不一致。让 Windows 正常完整启动后完整关机，再读取；也可以在 Windows 导出一致的 `SYSTEM` hive。不要强制解析或手动忽略事务日志。 |
| 权限不足 | 需要读取受限文件时使用 `sudo`，不要对 `/var/lib/bluetooth`、hive 或备份执行开放权限的 `chmod`。 |
| 同步后仍连接失败 | 核对档位、地址、服务状态，以及同步后是否又重新配对。保留备份，查看错误输出后再决定是否恢复。 |
| 蓝牙服务恢复失败或操作被中断 | 查看 `systemctl status bluetooth.service`；保留错误信息，按需运行 `restore`，再检查或启动服务。不要假定所有失败都已自动回滚。 |
| BIOS、UEFI 或 GRUB 里不能用 | 预启动环境是否支持蓝牙键盘取决于固件。本工具只处理操作系统中的配对记录。 |

提交 issue 时请提供发行版、BlueZ 版本、Windows 版本、设备型号、执行的子命令和已脱敏的错误信息。不要提交真实密钥、完整 hive、BlueZ 配置或备份文件；地址也请替换为占位符。

## 开发与贡献

欢迎修正文档、补充测试和报告已经完成双向重启验证的设备组合。硬件报告请说明两个系统、同一适配器、蓝牙类型，以及两边输入是否都已确认。

```bash
.venv/bin/python -m unittest discover -s tests -v
```

测试使用合成数据，不应依赖或提交个人蓝牙记录。

## 参考与致谢

- [LinkWinBT](https://github.com/vvoland/linkwinbt)：已有的 Windows → Linux 蓝牙配对密钥同步项目，帮助解释配对顺序和问题原理。
- [BlueZ settings storage](https://github.com/bluez/bluez/blob/master/doc/settings-storage.txt)：Linux 配置目录及 `LinkKey` 字段的上游说明。
- [Logitech K480 官方说明](https://www.logitech.com/en-gb/shop/p/k480-multi-device-wireless.920-006362)：键盘的三设备切换设计。

## 许可证

[MIT](LICENSE)。本项目与 Logitech、Microsoft、Ubuntu 或 BlueZ 无隶属关系。
