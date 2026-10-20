"""Explicit preview/apply commands. No raw registry errors or keys in output."""
import argparse
import os
from pathlib import Path
import sys

from . import __version__
from .core import (
    BLUEZ_ROOT, SyncError, apply_change, load_backup, normalize_mac,
    read_windows_pairings, replace_link_key, safe_regular,
)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        description="Share a classic Bluetooth pairing on one Windows/Linux dual-boot computer.",
    )
    result.add_argument("--version", action="version", version=__version__)
    commands = result.add_subparsers(dest="command", required=True)
    listing = commands.add_parser("list", help="List classic Windows pairing addresses; never print keys")
    listing.add_argument("--system-hive", required=True, type=Path)
    sync = commands.add_parser("sync", help="Preview synchronization, or update one device with --apply")
    sync.add_argument("--system-hive", required=True, type=Path)
    sync.add_argument("--adapter", required=True)
    sync.add_argument("--device", required=True)
    sync.add_argument("--apply", action="store_true", help="Write the BlueZ record (requires root)")
    restore = commands.add_parser("restore", help="Preview or restore a protected snapshot")
    restore.add_argument("--backup", required=True, type=Path)
    restore.add_argument("--apply", action="store_true", help="Restore the record (requires root)")
    return result


def run(args) -> int:
    if args.command == "list":
        records = read_windows_pairings(args.system_hive)
        if not records:
            print("No supported classic Bluetooth records in the active Windows ControlSet. BLE is not supported.")
        for adapter, devices in sorted(records.items()):
            for device in sorted(devices):
                print(f"Adapter {adapter}  Device {device}")
        return 0

    if args.command == "sync":
        adapter, device = normalize_mac(args.adapter), normalize_mac(args.device)
        records = read_windows_pairings(args.system_hive)
        key = records.get(adapter, {}).get(device)
        if key is None:
            raise SyncError("No classic Windows pairing for this adapter/device in Select\\Current. Check the addresses and pairing order.")
        info = BLUEZ_ROOT / adapter / device / "info"
        safe_regular(info)
        _, identical = replace_link_key(info.read_bytes(), key)
        if identical:
            print("Pairing keys already match. No files or services were changed.")
            return 0
        print(f"Target: adapter {adapter}, device {device}")
        print("Planned change: replace only [LinkKey].Key; preserve Type, PINLength and all other fields.")
        transform = lambda content: replace_link_key(content, key)[0]
    else:
        backup = load_backup(args.backup)
        adapter, device = backup.adapter, backup.device
        info = BLUEZ_ROOT / adapter / device / "info"
        safe_regular(info)
        if info.read_bytes() == backup.content:
            print("This backup already matches the live record. No files or services were changed.")
            return 0
        print(f"Target: adapter {adapter}, device {device}")
        print("Planned change: restore the entire device record from the verified backup.")
        transform = lambda content: backup.content

    if not args.apply:
        print("Preview only. Nothing was changed. Add --apply to perform this operation.")
        return 0
    if os.geteuid() != 0:
        raise SyncError("Writing BlueZ records requires root. Re-run the command with sudo.")
    snapshot = apply_change(info, adapter, device, transform)
    if snapshot:
        print(f"Completed. The previous device record is backed up at: {snapshot}")
    else:
        print("The record already matches. Nothing was changed.")
    print("The prior service running state was restored. Keep the same keyboard slot and wake the device; do not pair again.")
    return 0


def main(argv=None) -> int:
    args = parser().parse_args(argv)
    try:
        return run(args)
    except SyncError as error:
        print(f"Error: {error}", file=sys.stderr)
    except PermissionError:
        print("Error: access denied. BlueZ records and backups normally require sudo.", file=sys.stderr)
    except FileNotFoundError:
        print("Error: a required file is missing. Check the hive path and existing Linux pairing.", file=sys.stderr)
    except KeyboardInterrupt:
        print("Interrupted. If writing had begun, recovery was attempted; check the device and service.", file=sys.stderr)
        return 130
    except Exception as error:
        # Parser/library exception text can contain secret key material.
        print(f"Error: operation failed ({type(error).__name__}). Check file access and service status.", file=sys.stderr)
    return 1
