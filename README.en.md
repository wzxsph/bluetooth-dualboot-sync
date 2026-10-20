# Bluetooth Dual Boot Sync

[中文](README.md)

Share a classic Bluetooth pairing key between Windows and Linux on a dual boot computer.

This project grew out of troubleshooting a Logitech K480: pairing it again in Windows made its existing Ubuntu pairing stop working. Copying the Windows link key into the Ubuntu record restored typing on the same keyboard slot.

It includes a practical guide and a Python CLI with preview, backup, and restore commands.

## Why it works

Both operating systems can use the same physical Bluetooth adapter and adapter address while storing different pairing keys. After another pairing, the peripheral may retain a key that no longer matches the other operating system.

The method is to **pair in Linux first, pair in Windows second, then copy the Windows key to Linux**. This is an established technique implemented by projects such as [LinkWinBT](https://github.com/vvoland/linkwinbt), not a new discovery by this project.

For a K480, it lets one dual boot computer share one keyboard slot. It does not add slots or enable connections to an unlimited number of independent computers. Logitech documents the keyboard's [three-device switching design](https://www.logitech.com/en-gb/shop/p/k480-multi-device-wireless.920-006362).

## Scope and verification

- Classic Bluetooth BR/EDR only, with an existing BlueZ `[LinkKey]` record. BLE keys such as `LongTermKey` and IRK are not supported.
- Linux with systemd, BlueZ, and the default `/var/lib/bluetooth` storage location.
- A readable offline Windows `SYSTEM` hive containing supported classic Bluetooth records.
- The same physical adapter, with the same address, in both operating systems.

**Hardware evidence:** Ubuntu 24.04.4 LTS with a Logitech K480; Ubuntu typing was restored after synchronization. Windows 11 was the user's estimate, not a verified version. A final reboot back into Windows after synchronization has not yet been confirmed.

Automated tests cover parsing, target selection, file updates, backups, and failure handling. Passing tests does not establish hardware compatibility with other devices, adapters, distributions, or Windows releases. Windows storage changes may make its records unreadable by this tool.

## Prerequisites

- Keep another input method available: applying changes briefly stops Bluetooth.
- Use the same adapter in both systems, rather than switching between internal and USB adapters.
- Pair the device in Linux first. The tool updates an existing record; it does not create a pairing.
- Obtain read access to Windows `Windows/System32/config/SYSTEM`.
- Use Python 3.10 or newer. Ubuntu generally needs `python3-venv` to create the environment.

Pairing keys, BlueZ `info` files, `SYSTEM` hives, and backups contain sensitive information. Keep them local and out of commits and issues.

## Walkthrough

### 1. Pair in order

Using K480 slot 2 as an example:

1. Select slot 2, pair in Ubuntu, and confirm typing works.
2. Reboot into Windows, keep slot 2, pair again, and confirm typing works.
3. Return to Ubuntu and continue below, even if the keyboard cannot connect yet.

After synchronization, avoid removing the device or holding the pairing button. Pairing again generates new keys. If necessary, repeat the full Linux → Windows → sync sequence.

### 2. Install

```bash
git clone https://github.com/wzxsph/bluetooth-dualboot-sync.git
cd bluetooth-dualboot-sync
python3 -m venv .venv
.venv/bin/python -m pip install .
```

Run subsequent commands from the repository directory. Creating the environment and installing the package do not require `sudo`.

### 3. Read Windows data

Identify your Windows partition first:

```bash
lsblk -f
```

In the command below, `/dev/YOUR_WINDOWS_PARTITION` is a placeholder. **Replace it before running the command.**

```bash
sudo mkdir -p /mnt/windows
sudo mount -o ro /dev/YOUR_WINDOWS_PARTITION /mnt/windows
```

The hive should be at `/mnt/windows/Windows/System32/config/SYSTEM`.

If hibernation, Fast Startup, or encryption prevents access, do not force a writable mount. Fully shut down Windows and retry, or export the `SYSTEM` hive from Windows to media you can read and use that file's path. Unlock BitLocker using your own authorized method if needed; this approach does not require generally disabling disk encryption.

A local read-only copy of the hive also works. Protect the copy and remove it when you no longer need it.

### 4. Identify the adapter and device

```bash
bluetoothctl list
bluetoothctl devices
```

The first command lists local adapters; the second lists known remote devices. Check device names and addresses, particularly when several adapters are present.

List supported Windows classic Bluetooth records:

```bash
.venv/bin/bt-dualboot-sync list \
  --system-hive /mnt/windows/Windows/System32/config/SYSTEM
```

This prints adapter and device addresses, never keys. It needs no `sudo` if the hive is readable. If access is denied, use `sudo` to read it rather than changing system file permissions. See troubleshooting if your device is absent; do not immediately pair again.

In the examples below, replace `AA:BB:CC:DD:EE:FF` with your local adapter address and `11:22:33:44:55:66` with your keyboard address. These are placeholders.

### 5. Preview and apply

Preview the selected update:

```bash
sudo .venv/bin/bt-dualboot-sync sync \
  --system-hive /mnt/windows/Windows/System32/config/SYSTEM \
  --adapter AA:BB:CC:DD:EE:FF \
  --device 11:22:33:44:55:66
```

Preview mode does not write files or stop or restart Bluetooth. Confirm the selected adapter and device record, then add `--apply`:

```bash
sudo .venv/bin/bt-dualboot-sync sync \
  --system-hive /mnt/windows/Windows/System32/config/SYSTEM \
  --adapter AA:BB:CC:DD:EE:FF \
  --device 11:22:33:44:55:66 \
  --apply
```

Applying requires root. Keep the backup path printed by the tool. It restores the service's previous running state: an active service is started again, while an initially inactive service remains inactive. In the latter case, start it yourself before connecting:

```bash
sudo systemctl start bluetooth.service
```

### 6. Wake and test

Keep the same slot, switch the keyboard off and on, and press some keys. Test typing in Ubuntu, reboot into Windows and test again, then return to Ubuntu for another check.

**Do not hold the pairing button or remove and re-pair the device.** In the original K480 case, a keyboard power cycle was needed after synchronization.

## Implementation

- Reads the Windows hive without modifying it. Resolves the active control set through `Select\Current`, rather than assuming `ControlSet001`.
- Checks the REGF checksum and both sequence numbers before parsing. Rejects hives requiring registry recovery, without replaying transaction logs.
- Selects one classic key using explicit adapter and device addresses.
- Updates only `[LinkKey]` → `Key` in `/var/lib/bluetooth/<adapter>/<device>/info`, preserving `Type`, `PINLength`, and other content.
- Stops `bluetooth.service` before applying, rereads the target after stopping it, saves a backup, and writes through an atomic replacement.
- Stores root-only backups under `/var/backups/bluetooth-dualboot-sync`. Restore validates the backup record and file digest.
- Restores the previous service running state and attempts to recover the original file after write or service recovery errors, reporting the outcome.

The upstream [BlueZ storage documentation](https://github.com/bluez/bluez/blob/master/doc/settings-storage.txt) describes these files and discourages direct external changes. The tool therefore stops the service before editing its storage.

Power loss, forced termination, or disk failures may prevent automatic recovery. Inspect the output and service state, and use the retained backup when necessary. The tool does not modify Windows, mount disks automatically, or change system access permissions.

## Restore

Replace `<snapshot>` with the backup directory name printed by the sync command. Preview first:

```bash
sudo .venv/bin/bt-dualboot-sync restore \
  --backup "/var/backups/bluetooth-dualboot-sync/<snapshot>"
```

Then restore:

```bash
sudo .venv/bin/bt-dualboot-sync restore \
  --backup "/var/backups/bluetooth-dualboot-sync/<snapshot>" \
  --apply
```

Restore verifies the backup and restores only its recorded original target file, temporarily stopping Bluetooth and restoring its prior running state. Restoring an old Linux key does not change the key stored on the keyboard, so it may not restore connectivity.

## Troubleshooting

| Symptom | Check |
| --- | --- |
| `Host is down` or no keyboard response | Keep the same slot, power cycle and wake the keyboard, check battery and service status. This error alone does not prove a key mismatch. |
| Missing Linux pairing or `[LinkKey]` | Verify both addresses and the chosen adapter. The device may use BLE. If a new pairing is needed, repeat the full Linux → Windows → sync sequence. |
| Missing Windows device | Check that the hive belongs to the correct Windows installation, pairing used the same slot, and adapter addresses match. A changed Windows storage format may be unsupported. |
| `hive needs registry recovery` | The hive is not consistent. Let Windows complete a normal boot and full shutdown before reading it again, or export a consistent `SYSTEM` hive from Windows. Do not force parsing or manually ignore transaction logs. |
| Permission denied | Use `sudo` for restricted reads or applying changes. Do not open permissions on BlueZ files, hives, or backups with `chmod`. |
| Still disconnected after sync | Check slot, addresses, service state, and whether another pairing occurred. Retain the backup and inspect errors before restoring. |
| Service recovery failed or process interrupted | Inspect `systemctl status bluetooth.service`, retain the error, restore if needed, and check or start the service. Do not assume every failure was rolled back. |
| No input in BIOS, UEFI, or GRUB | Pre-boot Bluetooth support depends on firmware. This tool only changes operating system pairing records. |

For issues, provide distribution, BlueZ and Windows versions, device model, subcommand, and sanitized error messages. Replace real addresses with placeholders. Never attach keys, hives, complete BlueZ files, or backups.

## Contributing

Documentation fixes, tests, and hardware reports are welcome. Hardware reports should state both operating systems, the shared adapter, Bluetooth type, and whether typing was verified after rebooting into each system.

```bash
.venv/bin/python -m unittest discover -s tests -v
```

Tests use synthetic data. Do not commit personal pairing records.

## References and acknowledgements

- [LinkWinBT](https://github.com/vvoland/linkwinbt): an existing Windows → Linux pairing key synchronization project explaining the method and pairing order.
- [BlueZ settings storage](https://github.com/bluez/bluez/blob/master/doc/settings-storage.txt): upstream storage layout and key fields.
- [Logitech K480](https://www.logitech.com/en-gb/shop/p/k480-multi-device-wireless.920-006362): official three-device switching information.

## License

[MIT](LICENSE). This project is not affiliated with Logitech, Microsoft, Ubuntu, or BlueZ.
