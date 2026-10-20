"""Read Windows pairing records and update one BlueZ LinkKey.

No function writes a Windows registry hive. Pairing values must never be logged.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
import configparser
import fcntl
import hashlib
import io
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import tempfile
from typing import Callable

BLUEZ_ROOT = Path("/var/lib/bluetooth")
BACKUP_ROOT = Path("/var/backups/bluetooth-dualboot-sync")
LOCK_PATH = Path("/run/lock/bluetooth-dualboot-sync.lock")


class SyncError(Exception):
    """An actionable error whose message is safe to display."""


def normalize_mac(value: str) -> str:
    if re.fullmatch(r"[0-9a-fA-F]{12}", value):
        compact = value
    elif re.fullmatch(r"(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}", value):
        compact = value.replace(":", "")
    elif re.fullmatch(r"(?:[0-9a-fA-F]{2}-){5}[0-9a-fA-F]{2}", value):
        compact = value.replace("-", "")
    else:
        raise SyncError("Invalid Bluetooth address; use AA:BB:CC:DD:EE:FF.")
    return ":".join(compact[i:i + 2] for i in range(0, 12, 2)).upper()


def read_windows_pairings(hive_path: Path) -> dict[str, dict[str, bytes]]:
    """Return only classic REG_BINARY / 16-byte keys in Select\\Current."""
    from Registry import Registry, RegistryParse

    try:
        contents = hive_path.read_bytes()
        header = RegistryParse.REGFBlock(contents, 0, None)
        recovery = header.recovery_required()
        if recovery.recover_header or recovery.recover_data:
            raise SyncError(
                "The SYSTEM hive needs registry recovery. Fully boot and shut down Windows, "
                "or export a consistent SYSTEM hive there; transaction-log replay is not supported."
            )
        hive = Registry.Registry(io.BytesIO(contents))
        selected = hive.open("Select").value("Current")
        current = selected.value()
        if selected.value_type() != Registry.RegDWord or type(current) is not int or not 1 <= current <= 999:
            raise SyncError("Windows Select\\Current is not a valid ControlSet number.")
        path = f"ControlSet{current:03d}\\Services\\BTHPORT\\Parameters\\Keys"
        try:
            adapters = hive.open(path).subkeys()
        except Registry.RegistryKeyNotFoundException:
            return {}
        result = {}
        for adapter in adapters:
            if not re.fullmatch(r"[0-9a-fA-F]{12}", adapter.name()):
                continue
            devices = {}
            for value in adapter.values():
                if not re.fullmatch(r"[0-9a-fA-F]{12}", value.name()):
                    continue
                if value.value_type() != Registry.RegBin:
                    continue
                key = value.value()
                if isinstance(key, bytes) and len(key) == 16:
                    devices[normalize_mac(value.name())] = key
            if devices:
                result[normalize_mac(adapter.name())] = devices
        return result
    except SyncError:
        raise
    except PermissionError:
        raise SyncError("Cannot read SYSTEM. Use a readable offline copy or run with sudo.") from None
    except FileNotFoundError:
        raise SyncError("SYSTEM was not found. Mount Windows read only or select an offline copy.") from None
    except Exception:
        # Registry exceptions may contain binary key values. Do not echo them.
        raise SyncError("Cannot parse the offline SYSTEM hive; its format or saved state may be unsupported.") from None


def replace_link_key(original: bytes, new_key: bytes) -> tuple[bytes, bool]:
    """Preserve every byte outside the single [LinkKey] Key value."""
    if not isinstance(new_key, bytes) or len(new_key) != 16:
        raise SyncError("A classic Bluetooth link key must contain exactly 16 bytes.")
    try:
        text = original.decode("utf-8")
        config = configparser.ConfigParser(interpolation=None, strict=True)
        config.optionxform = str
        config.read_string(text)
        # Never accept an inherited DEFAULT value as a device's key.
        if config.defaults():
            raise SyncError("Unexpected DEFAULT values in the BlueZ record.")
        old_key = config.get("LinkKey", "Key", fallback="")
        if not re.fullmatch(r"[0-9a-fA-F]{32}", old_key):
            raise SyncError("No valid [LinkKey] in this BlueZ record. BLE pairing is not supported.")
        section = re.search(r"(?ms)^\[LinkKey\]\r?\n.*?(?=^\[|\Z)", text)
        if section is None:
            raise SyncError("The BlueZ LinkKey section has an unsupported layout.")
        replacement = new_key.hex().upper()
        patched, count = re.subn(
            r"(?m)^(Key[ \t]*=[ \t]*)[0-9a-fA-F]{32}([ \t]*\r?)$",
            lambda match: match[1] + replacement + match[2], section[0],
        )
        if count != 1:
            raise SyncError("The BlueZ LinkKey value has an unsupported layout.")
        if old_key.upper() == replacement:
            return original, True
        return (text[:section.start()] + patched + text[section.end():]).encode("utf-8"), False
    except (UnicodeError, configparser.Error):
        raise SyncError("The BlueZ record is invalid or contains duplicate sections/values.") from None


def safe_regular(path: Path) -> os.stat_result:
    if path.absolute() != path.resolve():
        raise SyncError("Refusing a path containing symbolic links or parent traversal.")
    metadata = path.lstat()
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        raise SyncError("Expected a regular file with a single hard link.")
    return metadata


def private_directory(path: Path, *, create: bool = False) -> None:
    if path.absolute() != path.resolve():
        raise SyncError("Refusing a backup path containing symbolic links or parent traversal.")
    if create:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
    metadata = path.lstat()
    if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.geteuid() or stat.S_IMODE(metadata.st_mode) & 0o077:
        raise SyncError("Backup directories must be owned by the current user and accessible only to that user.")


def fsync_directory(path: Path) -> None:
    directory = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def atomic_write(path: Path, content: bytes, metadata: os.stat_result) -> None:
    fd, temporary = tempfile.mkstemp(prefix=".bt-sync-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            os.fchown(stream.fileno(), metadata.st_uid, metadata.st_gid)
            os.fchmod(stream.fileno(), stat.S_IMODE(metadata.st_mode))
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        fsync_directory(path.parent)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


@contextmanager
def exclusive_lock(lock_path: Path):
    fd = os.open(lock_path, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        metadata = os.fstat(fd)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.geteuid() or metadata.st_nlink != 1:
            raise SyncError("The transaction lock has unsafe ownership or file type.")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SyncError("Another synchronization is running.") from None
        yield
    finally:
        os.close(fd)


def create_backup(info: Path, original: bytes, metadata: os.stat_result, adapter: str, device: str, backup_root: Path) -> Path:
    private_directory(backup_root, create=True)
    snapshot = Path(tempfile.mkdtemp(
        prefix=datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-"), dir=backup_root,
    ))
    manifest = {
        "format": 1, "adapter": normalize_mac(adapter), "device": normalize_mac(device),
        "sha256": hashlib.sha256(original).hexdigest(),
        "mode": stat.S_IMODE(metadata.st_mode), "uid": metadata.st_uid, "gid": metadata.st_gid,
    }
    for name, contents in (
        ("info", original),
        ("manifest.json", (json.dumps(manifest, indent=2) + "\n").encode()),
    ):
        fd = os.open(snapshot / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(contents)
            stream.flush()
            os.fsync(stream.fileno())
    # Make backup directory entries durable before replacing the live record.
    fsync_directory(snapshot)
    fsync_directory(backup_root)
    fsync_directory(backup_root.parent)
    return snapshot


@dataclass(frozen=True)
class Backup:
    adapter: str
    device: str
    content: bytes = field(repr=False)


def load_backup(snapshot: Path, *, backup_root: Path = BACKUP_ROOT) -> Backup:
    private_directory(backup_root)
    private_directory(snapshot)
    if snapshot.parent.resolve() != backup_root.resolve():
        raise SyncError("Choose a direct snapshot directory inside the configured backup directory.")
    for name in ("info", "manifest.json"):
        metadata = safe_regular(snapshot / name)
        if metadata.st_uid != os.geteuid() or stat.S_IMODE(metadata.st_mode) & 0o077:
            raise SyncError("Backup files have unsafe ownership or permissions.")
    try:
        manifest = json.loads((snapshot / "manifest.json").read_text("utf-8"))
        if not isinstance(manifest, dict) or manifest.get("format") != 1:
            raise ValueError
        adapter, device = normalize_mac(manifest["adapter"]), normalize_mac(manifest["device"])
        content = (snapshot / "info").read_bytes()
        if hashlib.sha256(content).hexdigest() != manifest["sha256"]:
            raise SyncError("Backup checksum mismatch; restoration was refused.")
        # Reject non-BlueZ or BLE-only data before touching the live service.
        replace_link_key(content, bytes(16))
        return Backup(adapter, device, content)
    except SyncError:
        raise
    except (ValueError, TypeError, KeyError, UnicodeError):
        raise SyncError("Backup metadata is invalid.") from None


class ServiceController:
    def _run(self, *arguments: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["/usr/bin/systemctl", *arguments, "bluetooth.service"],
            capture_output=True, env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C.UTF-8"},
        )

    def is_active(self) -> bool:
        result = self._run("is-active")
        state = result.stdout.decode("utf-8", errors="replace").strip()
        if result.returncode == 0 and state == "active":
            return True
        if state in {"inactive", "failed"}:
            return False
        raise SyncError("bluetooth.service is unavailable or is changing state. Try again after it settles.")

    def stop(self) -> None:
        if self._run("stop").returncode:
            raise SyncError("Could not stop bluetooth.service.")

    def start(self) -> None:
        if self._run("start").returncode:
            raise SyncError("Could not start bluetooth.service.")


def apply_change(
    info: Path, adapter: str, device: str, transform: Callable[[bytes], bytes], *,
    backup_root: Path = BACKUP_ROOT, controller=None, lock_path: Path = LOCK_PATH,
) -> Path | None:
    """Back up, replace, verify, and recover the service's prior running state.

    Validation and equality checks after writing happen while BlueZ is stopped.
    Tests may inject a service controller and temporary paths; the CLI does not
    expose alternate system paths.
    """
    controller = controller or ServiceController()
    with exclusive_lock(lock_path):
        safe_regular(info)
        initial = info.read_bytes()
        if transform(initial) == initial:
            return None
        was_active = controller.is_active()
        stop_attempted = False
        write_attempted = False
        snapshot = None
        original = None
        try:
            if was_active:
                stop_attempted = True
                controller.stop()
            metadata = safe_regular(info)
            original = info.read_bytes()
            updated = transform(original)
            if updated != original:
                snapshot = create_backup(info, original, metadata, adapter, device, backup_root)
                write_attempted = True
                atomic_write(info, updated, metadata)
                if info.read_bytes() != updated:
                    raise SyncError("Verification failed while bluetooth.service was stopped.")
            if was_active:
                controller.start()
            return snapshot
        except BaseException:
            recovery_failed = False
            if write_attempted:
                try:
                    if was_active:
                        controller.stop()
                    atomic_write(info, original, metadata)
                except BaseException:
                    recovery_failed = True
            if was_active and stop_attempted:
                try:
                    controller.start()
                except BaseException:
                    recovery_failed = True
            if recovery_failed:
                raise SyncError(
                    "Automatic recovery was incomplete. Check bluetooth.service and restore the backup: "
                    + (str(snapshot) if snapshot else "no backup was created")
                ) from None
            raise
