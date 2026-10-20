"""Offline regression tests; every pairing and key below is synthetic."""

from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from bt_dualboot_sync import cli, core  # noqa: E402


ADAPTER = "AA:BB:CC:DD:EE:FF"
DEVICE = "11:22:33:44:55:66"
OLD_KEY = bytes([0x11]) * 16
NEW_KEY = bytes([0x22]) * 16


def bluez_info(key: bytes = OLD_KEY, newline: bytes = b"\n") -> bytes:
    return newline.join(
        [
            b"[General]",
            b"Name=Synthetic Test Keyboard",
            b"Trusted=true",
            b"Blocked=false",
            b"Services=00001124-0000-1000-8000-00805f9b34fb;",
            b"",
            b"[LinkKey]",
            b"Key=" + key.hex().upper().encode("ascii"),
            b"Type=4",
            b"PINLength=0",
            b"",
            b"[DeviceID]",
            b"Source=2",
            b"Vendor=1133",
            b"Product=0",
            b"Version=1",
            b"",
        ]
    )


class MacTests(unittest.TestCase):
    def test_accepts_common_forms_and_canonicalizes(self):
        for value in ("aabbccddeeff", "aa:bb:cc:dd:ee:ff", "aa-bb-cc-dd-ee-ff"):
            with self.subTest(value=value):
                self.assertEqual(core.normalize_mac(value), ADAPTER)

    def test_rejects_invalid_or_ambiguous_addresses(self):
        for value in ("", "AA:BB:CC:DD:EE", "AA:BB:CC:DD:EE:FF:00", "GG:BB:CC:DD:EE:FF", "../../AA:BB:CC:DD:EE:FF"):
            with self.subTest(value=value), self.assertRaises(core.SyncError):
                core.normalize_mac(value)


class LinkKeyTests(unittest.TestCase):
    def test_changes_only_key_and_preserves_other_fields(self):
        original = bluez_info()
        updated, identical = core.replace_link_key(original, NEW_KEY)
        self.assertFalse(identical)
        self.assertEqual(
            updated,
            original.replace(OLD_KEY.hex().upper().encode(), NEW_KEY.hex().upper().encode()),
        )

    def test_preserves_crlf(self):
        original = bluez_info(newline=b"\r\n")
        updated, identical = core.replace_link_key(original, NEW_KEY)
        self.assertFalse(identical)
        self.assertEqual(
            updated,
            original.replace(OLD_KEY.hex().upper().encode(), NEW_KEY.hex().upper().encode()),
        )

    def test_same_key_is_byte_for_byte_noop(self):
        original = bluez_info()
        updated, identical = core.replace_link_key(original, OLD_KEY)
        self.assertTrue(identical)
        self.assertEqual(updated, original)

    def test_does_not_change_other_sections_named_key(self):
        original = bluez_info() + b"\n[Other]\nKey=unchanged\n"
        updated, _ = core.replace_link_key(original, NEW_KEY)
        self.assertTrue(updated.endswith(b"[Other]\nKey=unchanged\n"))

    def test_rejects_duplicate_key_or_section(self):
        for original in (
            bluez_info().replace(b"Type=4", b"Key=" + OLD_KEY.hex().encode() + b"\nType=4"),
            bluez_info() + b"\n[LinkKey]\nKey=" + OLD_KEY.hex().encode() + b"\n",
        ):
            with self.subTest(original=original), self.assertRaises(core.SyncError):
                core.replace_link_key(original, NEW_KEY)

    def test_rejects_invalid_existing_key(self):
        for key in (b"", b"not-a-link-key", b"11" * 15, b"ZZ" * 16):
            original = bluez_info().replace(OLD_KEY.hex().upper().encode(), key)
            with self.subTest(key=key), self.assertRaises(core.SyncError):
                core.replace_link_key(original, NEW_KEY)

    def test_rejects_ble_pairings_without_a_classic_link_key(self):
        original = b"[General]\nName=Synthetic BLE Device\n\n[LongTermKey]\nKey=" + OLD_KEY.hex().encode() + b"\n"
        with self.assertRaises(core.SyncError):
            core.replace_link_key(original, NEW_KEY)

    def test_rejects_wrong_length_new_key(self):
        for key in (b"", bytes(15), bytes(17)):
            with self.subTest(length=len(key)), self.assertRaises(core.SyncError):
                core.replace_link_key(bluez_info(), key)


class RegistryValue:
    def __init__(self, name, value, kind):
        self._name, self._value, self._kind = name, value, kind

    def name(self):
        return self._name

    def value(self):
        return self._value

    def value_type(self):
        return self._kind


class RegistryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.hive = Path(self.temporary.name) / "SYSTEM"
        self.hive.write_bytes(b"Synthetic offline hive; parsed by a fake Registry.")

    def registry_parse(self, *, recover_header=False, recover_data=False):
        recovery = SimpleNamespace(recover_header=recover_header, recover_data=recover_data)
        header = SimpleNamespace(recovery_required=lambda: recovery)
        return SimpleNamespace(REGFBlock=lambda contents, offset, parent: header)

    def registry_module(self, selected=3, selected_kind=4):
        opened = []
        values = [
            RegistryValue("112233445566", NEW_KEY, 3),
            RegistryValue("010203040506", bytes(15), 3),
            RegistryValue("111213141516", bytes(16), 4),
            RegistryValue("212223242526", "22" * 16, 3),
            RegistryValue("not-a-device", bytes(16), 3),
        ]
        adapter = SimpleNamespace(name=lambda: "aabbccddeeff", values=lambda: values)
        ignored = SimpleNamespace(name=lambda: "invalid-adapter", values=lambda: values)

        def open_key(path):
            opened.append(path)
            if path == "Select":
                return SimpleNamespace(value=lambda name: RegistryValue(name, selected, selected_kind))
            if path == "ControlSet003\\Services\\BTHPORT\\Parameters\\Keys":
                return SimpleNamespace(subkeys=lambda: [adapter, ignored])
            raise AssertionError("Unexpected registry path")

        module = SimpleNamespace(
            RegDWord=4,
            RegBin=3,
            RegistryKeyNotFoundException=KeyError,
            Registry=lambda stream: SimpleNamespace(open=open_key),
        )
        return SimpleNamespace(Registry=module, RegistryParse=self.registry_parse()), opened

    def test_uses_selected_control_set_and_only_binary_16_byte_keys(self):
        module, opened = self.registry_module()
        with patch.dict(sys.modules, {"Registry": module}):
            result = core.read_windows_pairings(self.hive)
        self.assertEqual(result, {ADAPTER: {DEVICE: NEW_KEY}})
        self.assertEqual(opened, ["Select", "ControlSet003\\Services\\BTHPORT\\Parameters\\Keys"])
        self.assertEqual(self.hive.read_bytes(), b"Synthetic offline hive; parsed by a fake Registry.")

    def test_rejects_invalid_select_current(self):
        for selected, kind in ((0, 4), (1000, 4), (True, 4), ("3", 4), (3, 3)):
            module, opened = self.registry_module(selected, kind)
            with self.subTest(selected=selected, kind=kind), patch.dict(sys.modules, {"Registry": module}):
                with self.assertRaises(core.SyncError):
                    core.read_windows_pairings(self.hive)
                self.assertEqual(opened, ["Select"])

    def test_rejects_dirty_hive_before_parsing_pairing_records(self):
        for recover_header, recover_data in ((True, False), (False, True)):
            module, opened = self.registry_module()
            module.RegistryParse = self.registry_parse(
                recover_header=recover_header, recover_data=recover_data,
            )
            parser = Mock()
            module.Registry.Registry = parser
            with (
                self.subTest(recover_header=recover_header, recover_data=recover_data),
                patch.dict(sys.modules, {"Registry": module}),
                self.assertRaises(core.SyncError) as caught,
            ):
                core.read_windows_pairings(self.hive)
            parser.assert_not_called()
            self.assertEqual(opened, [])
            self.assertIn("recovery", str(caught.exception))

    def test_parser_errors_do_not_disclose_key_material(self):
        secret = NEW_KEY.hex().upper()

        def failing_parser(stream):
            raise ValueError("Parser detail: " + secret)

        module = SimpleNamespace(
            Registry=SimpleNamespace(Registry=failing_parser),
            RegistryParse=self.registry_parse(),
        )
        with patch.dict(sys.modules, {"Registry": module}), self.assertRaises(core.SyncError) as caught:
            core.read_windows_pairings(self.hive)
        self.assertNotIn(secret, str(caught.exception))


class FakeController:
    def __init__(self, active=True, on_stop=None, on_start=None):
        self.active = active
        self.on_stop = on_stop
        self.on_start = on_start
        self.events = []

    def is_active(self):
        self.events.append("is_active")
        return self.active

    def stop(self):
        self.events.append("stop")
        if self.on_stop:
            self.on_stop()
        self.active = False

    def start(self):
        self.events.append("start")
        if self.on_start:
            self.on_start()
        self.active = True


class TransactionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.info = self.root / "info"
        self.info.write_bytes(bluez_info())
        self.info.chmod(0o600)
        self.backups = self.root / "backups"
        self.lock = self.root / "transaction.lock"

    def apply(self, controller, transform=None):
        if transform is None:
            transform = lambda content: core.replace_link_key(content, NEW_KEY)[0]
        return core.apply_change(
            self.info, ADAPTER, DEVICE, transform,
            backup_root=self.backups, controller=controller, lock_path=self.lock,
        )

    def test_reads_current_record_after_service_stop(self):
        flushed = bluez_info().replace(b"Blocked=false", b"Blocked=true")
        controller = FakeController(on_stop=lambda: self.info.write_bytes(flushed))
        inputs = []

        def transform(content):
            inputs.append((list(controller.events), content))
            return core.replace_link_key(content, NEW_KEY)[0]

        snapshot = self.apply(controller, transform)
        self.assertEqual(inputs[-1][1], flushed)
        self.assertIn("stop", inputs[-1][0])
        self.assertEqual(self.info.read_bytes(), core.replace_link_key(flushed, NEW_KEY)[0])
        self.assertEqual((snapshot / "info").read_bytes(), flushed)
        self.assertEqual(controller.events, ["is_active", "stop", "start"])

    def test_service_metadata_update_after_start_is_not_mistaken_for_failure(self):
        expected = core.replace_link_key(bluez_info(), NEW_KEY)[0]

        def service_start():
            self.assertEqual(self.info.read_bytes(), expected)
            self.info.write_bytes(expected + b"\n# Metadata refreshed by BlueZ\n")

        controller = FakeController(on_start=service_start)
        snapshot = self.apply(controller)
        self.assertIsNotNone(snapshot)
        self.assertTrue(self.info.read_bytes().endswith(b"# Metadata refreshed by BlueZ\n"))
        self.assertEqual(controller.events, ["is_active", "stop", "start"])

    def test_identical_key_does_not_stop_service_or_create_backup(self):
        self.info.write_bytes(bluez_info(NEW_KEY))
        controller = FakeController()
        self.assertIsNone(self.apply(controller))
        self.assertEqual(controller.events, [])
        self.assertFalse(self.backups.exists())

    def test_inactive_service_stays_inactive(self):
        controller = FakeController(active=False)
        self.assertIsNotNone(self.apply(controller))
        self.assertEqual(controller.events, ["is_active"])
        self.assertFalse(controller.active)

    def test_write_failure_restores_record_and_service(self):
        real_atomic_write = core.atomic_write
        calls = []

        def fail_once(path, content, metadata):
            calls.append(content)
            if len(calls) == 1:
                path.write_bytes(b"Simulated failed partial write")
                raise OSError("Synthetic storage failure")
            return real_atomic_write(path, content, metadata)

        controller = FakeController()
        with patch.object(core, "atomic_write", side_effect=fail_once), self.assertRaises(OSError):
            self.apply(controller)
        self.assertEqual(self.info.read_bytes(), bluez_info())
        self.assertEqual(calls[-1], bluez_info())
        self.assertTrue(controller.active)
        self.assertEqual(controller.events, ["is_active", "stop", "stop", "start"])
        self.assertEqual(len(list(self.backups.iterdir())), 1)

    def test_validation_after_stop_failure_restores_service_without_writing(self):
        malformed = b"[General]\nName=Changed by BlueZ\n"
        controller = FakeController(on_stop=lambda: self.info.write_bytes(malformed))
        with self.assertRaises(core.SyncError):
            self.apply(controller)
        self.assertEqual(self.info.read_bytes(), malformed)
        self.assertTrue(controller.active)
        self.assertEqual(controller.events, ["is_active", "stop", "start"])
        self.assertFalse(self.backups.exists())

    def test_start_failure_restores_original_and_retries_prior_service_state(self):
        attempts = []

        def fail_first_start():
            attempts.append(True)
            if len(attempts) == 1:
                raise core.SyncError("Synthetic service startup failure")

        controller = FakeController(on_start=fail_first_start)
        with self.assertRaises(core.SyncError):
            self.apply(controller)
        self.assertEqual(self.info.read_bytes(), bluez_info())
        self.assertTrue(controller.active)
        self.assertEqual(controller.events, ["is_active", "stop", "start", "stop", "start"])

    def test_backup_permissions_roundtrip_and_no_plaintext_key_in_manifest(self):
        snapshot = self.apply(FakeController())
        for directory in (self.backups, snapshot):
            self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o700)
        for name in ("info", "manifest.json"):
            self.assertEqual(stat.S_IMODE((snapshot / name).stat().st_mode), 0o600)
        manifest_text = (snapshot / "manifest.json").read_text()
        manifest = json.loads(manifest_text)
        self.assertEqual(manifest["adapter"], ADAPTER)
        self.assertEqual(manifest["device"], DEVICE)
        self.assertNotIn(OLD_KEY.hex().upper(), manifest_text)
        restored = core.load_backup(snapshot, backup_root=self.backups)
        self.assertEqual((restored.adapter, restored.device, restored.content), (ADAPTER, DEVICE, bluez_info()))
        self.assertNotIn(OLD_KEY.hex(), repr(restored))

    def test_corrupt_backup_is_refused(self):
        snapshot = self.apply(FakeController())
        (snapshot / "info").write_bytes(bluez_info(NEW_KEY))
        with self.assertRaises(core.SyncError):
            core.load_backup(snapshot, backup_root=self.backups)

    def test_atomic_write_preserves_permissions_and_owner(self):
        self.info.chmod(0o640)
        before = self.info.stat()
        core.atomic_write(self.info, b"Synthetic updated contents", before)
        after = self.info.stat()
        self.assertEqual(self.info.read_bytes(), b"Synthetic updated contents")
        self.assertEqual(stat.S_IMODE(after.st_mode), stat.S_IMODE(before.st_mode))
        self.assertEqual((after.st_uid, after.st_gid), (before.st_uid, before.st_gid))
        self.assertEqual(list(self.root.glob(".bt-sync-*")), [])

    def test_symbolic_or_shared_hard_link_record_is_refused(self):
        symbolic = self.root / "symbolic"
        symbolic.symlink_to(self.info)
        with self.assertRaises(core.SyncError):
            core.safe_regular(symbolic)
        hard_link = self.root / "hard-link"
        os.link(self.info, hard_link)
        with self.assertRaises(core.SyncError):
            core.safe_regular(self.info)


class CliTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.info = self.root / ADAPTER / DEVICE / "info"
        self.info.parent.mkdir(parents=True)
        self.info.write_bytes(bluez_info())
        self.arguments = [
            "sync", "--system-hive", str(self.root / "SYSTEM"),
            "--adapter", ADAPTER, "--device", DEVICE,
        ]

    def test_default_preview_writes_nothing_and_does_not_touch_service(self):
        output = io.StringIO()
        before = {path.relative_to(self.root): path.read_bytes() for path in self.root.rglob("*") if path.is_file()}
        with (
            patch.object(cli, "BLUEZ_ROOT", self.root),
            patch.object(cli, "read_windows_pairings", return_value={ADAPTER: {DEVICE: NEW_KEY}}),
            patch.object(cli, "apply_change") as apply,
            patch.object(core, "ServiceController") as controller,
            redirect_stdout(output),
        ):
            result = cli.main(self.arguments)
        after = {path.relative_to(self.root): path.read_bytes() for path in self.root.rglob("*") if path.is_file()}
        self.assertEqual(result, 0)
        self.assertEqual(before, after)
        apply.assert_not_called()
        controller.assert_not_called()
        self.assertIn("Preview only", output.getvalue())
        self.assertNotIn(OLD_KEY.hex().upper(), output.getvalue())
        self.assertNotIn(NEW_KEY.hex().upper(), output.getvalue())

    def test_unexpected_exception_text_is_not_disclosed(self):
        output, errors = io.StringIO(), io.StringIO()
        secret = NEW_KEY.hex().upper()
        with (
            patch.object(cli, "read_windows_pairings", side_effect=RuntimeError("Secret: " + secret)),
            redirect_stdout(output), redirect_stderr(errors),
        ):
            result = cli.main(self.arguments)
        self.assertEqual(result, 1)
        self.assertNotIn(secret, output.getvalue() + errors.getvalue())
        self.assertNotIn("Traceback", errors.getvalue())
        self.assertIn("RuntimeError", errors.getvalue())

    def test_apply_requires_root_before_any_write(self):
        with (
            patch.object(cli, "BLUEZ_ROOT", self.root),
            patch.object(cli, "read_windows_pairings", return_value={ADAPTER: {DEVICE: NEW_KEY}}),
            patch.object(cli.os, "geteuid", return_value=1000),
            patch.object(cli, "apply_change") as apply,
            redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()),
        ):
            result = cli.main(self.arguments + ["--apply"])
        self.assertEqual(result, 1)
        apply.assert_not_called()
        self.assertEqual(self.info.read_bytes(), bluez_info())


if __name__ == "__main__":
    unittest.main()
