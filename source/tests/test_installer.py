"""Tests for the installer's per-PC llama.cpp build choice: CUDA matched to the NVIDIA driver (12.8+ on RTX 50-series),
Vulkan for AMD and Intel cards, the CPU build on a PC without a dedicated GPU, and the registry scan that finds AMD and
Intel cards while leaving integrated graphics out. Nothing is downloaded and no real registry is read.
Run from source/:
    python -m unittest discover -s tests -v
"""
import importlib.util
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

_spec = importlib.util.spec_from_file_location("aero_installer_setup",
                                               Path(__file__).resolve().parent.parent / "installer" / "setup.py")
setup = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(setup)

TAG = "b9100"


def release(*kinds):
    names = [f"llama-{TAG}-bin-win-{k}-x64.zip" for k in kinds]
    names += [f"cudart-llama-bin-win-{k}-x64.zip" for k in kinds if k.startswith("cuda-")]
    return {"tag_name": TAG, "assets": [{"name": n, "browser_download_url": "https://example.invalid/" + n} for n in names]}


REL = release("cuda-12.4", "cuda-12.8", "cuda-13.1", "vulkan", "cpu")


def nvidia(cc, cuda):
    return {"name": "NVIDIA test card", "mem": "16303 MiB", "cc": cc, "driver": "600.00", "cuda": cuda}


class PickAssetsTests(unittest.TestCase):
    def pick(self, rel, gpu, others=()):
        with mock.patch.object(setup, "say"):
            kind, urls = setup.pick_assets(rel, gpu, others)
        return kind, [u.rsplit("/", 1)[1] for u in urls]

    def test_rtx50_new_driver_gets_newest_cuda_it_supports(self):
        kind, names = self.pick(REL, nvidia("12.0", "13.4"))
        self.assertEqual(kind, "CUDA 13.1")
        self.assertEqual(names, [f"llama-{TAG}-bin-win-cuda-13.1-x64.zip", "cudart-llama-bin-win-cuda-13.1-x64.zip"])

    def test_build_never_newer_than_the_driver(self):
        self.assertEqual(self.pick(REL, nvidia("8.9", "12.6"))[0], "CUDA 12.4")

    def test_rtx50_with_old_driver_falls_back_to_vulkan(self):
        # Blackwell needs a 12.8+ build; a 12.6 driver can't run one, so Vulkan it is
        self.assertEqual(self.pick(REL, nvidia("12.0", "12.6"))[0], "VULKAN")

    def test_amd_or_intel_card_gets_vulkan(self):
        self.assertEqual(self.pick(REL, None, ["AMD Radeon RX 9070 XT (16 GB)"])[0], "VULKAN")

    def test_no_dedicated_gpu_gets_cpu_build(self):
        self.assertEqual(self.pick(REL, None, [])[0], "CPU")

    def test_no_cpu_build_listed_falls_back_to_vulkan(self):
        self.assertEqual(self.pick(release("vulkan"), None, [])[0], "VULKAN")

    def test_no_windows_build_is_an_error(self):
        with self.assertRaises(RuntimeError):
            self.pick({"tag_name": TAG, "assets": []}, None, [])


class FakeKey:
    def __init__(self, values=None, subkeys=()):
        self.values, self.subkeys = values or {}, list(subkeys)


def fake_winreg(cards):
    subs = {f"{i:04d}": FakeKey({"DriverDesc": n, "HardwareInformation.qwMemorySize": m}) for i, (n, m) in enumerate(cards)}
    subs["Properties"] = FakeKey()
    root = FakeKey(subkeys=list(subs))
    wr = types.ModuleType("winreg")
    wr.HKEY_LOCAL_MACHINE = object()

    def open_key(parent, name):
        if parent is wr.HKEY_LOCAL_MACHINE:
            return root
        return subs[name]

    def enum_key(key, i):
        if i >= len(key.subkeys):
            raise OSError("no more")
        return key.subkeys[i]

    def query(key, name):
        if name not in key.values:
            raise OSError(name)
        return key.values[name], 0

    wr.OpenKey, wr.EnumKey, wr.QueryValueEx = open_key, enum_key, query
    return wr


GB = 2**30


class OtherGpuTests(unittest.TestCase):
    def scan(self, cards):
        with mock.patch.dict(sys.modules, {"winreg": fake_winreg(cards)}):
            return setup.other_gpus()

    def test_dedicated_amd_and_intel_found(self):
        found = self.scan([("AMD Radeon RX 9070 XT", (16 * GB).to_bytes(8, "little")),
                           ("Intel(R) Arc(TM) B580 Graphics", 12 * GB)])
        self.assertEqual(found, ["AMD Radeon RX 9070 XT (16 GB)", "Intel(R) Arc(TM) B580 Graphics (12 GB)"])

    def test_integrated_graphics_and_nvidia_left_out(self):
        found = self.scan([("AMD Radeon(TM) Graphics", (4 * GB).to_bytes(8, "little")),
                           ("Intel(R) UHD Graphics 770", 2 * GB),
                           ("Intel(R) Arc(TM) Graphics", 8 * GB),
                           ("NVIDIA GeForce RTX 5080", (16 * GB).to_bytes(8, "little")),
                           ("AMD Radeon test card with 1 GB", 1 * GB)])
        self.assertEqual(found, [])

    def test_not_windows(self):
        with mock.patch.dict(sys.modules, {"winreg": None}):
            self.assertEqual(setup.other_gpus(), [])


class RecordingWinreg:
    """Stands in for winreg: records the key and values register_windows_app writes."""
    HKEY_LOCAL_MACHINE, HKEY_CURRENT_USER = "HKLM", "HKCU"
    KEY_ALL_ACCESS, KEY_WOW64_64KEY = 0xF003F, 0x0100
    REG_SZ, REG_DWORD = 1, 4

    def __init__(self, existing=None):
        self.keys = {}
        self.existing = existing or {}

    def CreateKeyEx(self, hive, path, reserved, access):
        self.access = access
        values = self.keys.setdefault((hive, path), dict(self.existing))
        outer = self

        class Key:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False
        k = Key()
        k.values = values
        outer.last = k
        return k

    def QueryValueEx(self, key, name):
        if name not in key.values:
            raise OSError(2, "not found")
        return key.values[name][1], key.values[name][0]

    def SetValueEx(self, key, name, reserved, kind, value):
        key.values[name] = (kind, value)


class InstalledAppsTests(unittest.TestCase):
    """Aero is listed under Settings > Apps > Installed apps with an UninstallString that runs Uninstall-Aero.ps1."""

    def make_install(self, root: Path):
        (root / "app" / "aero").mkdir(parents=True)
        (root / "app" / "installer").mkdir(parents=True)
        (root / "app" / "aero" / "config.py").write_text('VERSION = "9.8.7"\n')
        src = Path(__file__).resolve().parent.parent / "installer" / "Uninstall-Aero.ps1"
        (root / "app" / "installer" / "Uninstall-Aero.ps1").write_bytes(src.read_bytes())
        (root / "models").mkdir()
        (root / "models" / "m.gguf").write_bytes(b"x" * 4096)
        return root

    def register(self, root, admin=True, existing=None, **kw):
        reg = RecordingWinreg(existing)
        with mock.patch.object(setup, "say"), mock.patch.object(setup, "_is_admin", return_value=admin):
            where = setup.register_windows_app(root, root / "aero-bubble.ico", winreg=reg, **kw)
        return where, reg

    def test_admin_install_registers_for_every_user_with_all_values(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            root = self.make_install(Path(d))
            where, reg = self.register(root)
            self.assertEqual(where, "HKLM")
            (hive, path), = reg.keys
            self.assertEqual(hive, "HKLM")
            self.assertTrue(path.endswith(r"CurrentVersion\Uninstall\Aero"))
            self.assertTrue(reg.access & reg.KEY_WOW64_64KEY, "the 64-bit registry view, which Settings reads")
            v = {k: val for k, (_kind, val) in reg.last.values.items()}
            self.assertEqual(v["DisplayName"], "Aero")
            self.assertEqual(v["DisplayVersion"], "9.8.7")
            self.assertEqual(v["Publisher"], "NermalYT")
            self.assertEqual(v["InstallLocation"], str(root))
            self.assertIn("aero-bubble.ico", v["DisplayIcon"])
            self.assertIn(f'-File "{root / "Uninstall-Aero.ps1"}"', v["UninstallString"])
            self.assertIn("-ExecutionPolicy Bypass", v["UninstallString"])
            self.assertTrue(v["QuietUninstallString"].endswith(" -Quiet"))
            self.assertNotIn("-AppId", v["UninstallString"])
            self.assertEqual((v["NoModify"], v["NoRepair"]), (1, 1))
            self.assertGreaterEqual(v["EstimatedSize"], 4)            # KB, includes the model file
            self.assertEqual(reg.last.values["EstimatedSize"][0], reg.REG_DWORD)
            self.assertTrue((root / "Uninstall-Aero.ps1").exists(), "the uninstaller sits outside app\\, which updates mirror")

    def test_without_admin_rights_registers_for_the_current_user(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            where, reg = self.register(self.make_install(Path(d)), admin=False)
            self.assertEqual(where, "HKCU")

    def test_update_keeps_the_first_install_date(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            _w, reg = self.register(self.make_install(Path(d)), existing={"InstallDate": (1, "20261009")})
            self.assertEqual(reg.last.values["InstallDate"][1], "20261009")

    def test_test_entries_pass_their_id_to_the_uninstaller(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            _w, reg = self.register(self.make_install(Path(d)), app_id="AeroTest", display_name="Aero (test)")
            (_hive, path), = reg.keys
            self.assertTrue(path.endswith(r"\AeroTest"))
            self.assertIn('-AppId "AeroTest"', reg.last.values["UninstallString"][1])

    def test_uninstaller_ships_as_ascii_with_crlf(self):
        # Windows PowerShell 5.1 reads a BOM-less script as the ANSI code page, so the script stays ASCII
        for name in ("Uninstall-Aero.ps1", "Uninstall-Aero.bat"):
            b = (Path(__file__).resolve().parent.parent / "installer" / name).read_bytes()
            self.assertTrue(all(c < 128 for c in b), name)
            self.assertNotIn(b"\n", b.replace(b"\r\n", b""), name)


if __name__ == "__main__":
    unittest.main()
