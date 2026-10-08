"""Tests for the hardware scan on PCs other than the one Aero was built on: NVIDIA cards through nvidia-smi, AMD and
Intel cards through the Windows display driver registry, integrated GPUs left out, several GPUs pooled, and the
catalog's per-PC recommendation for each. nvidia-smi and the registry are faked; nothing is read from this machine.
Run from source/:
    python -m unittest discover -s tests -v
"""
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

if "aero.config" not in sys.modules:
    os.environ["AERO_HOME"] = tempfile.mkdtemp(prefix="aero-test-")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aero import catalog, hardware  # noqa: E402

GB = 2**30
CLASS = r"SYSTEM\CurrentControlSet\Control\Class\{4d36e968-e325-11ce-bfc1-08002be10318}"


def fake_winreg(adapters):
    """adapters: (DriverDesc, qwMemorySize bytes or int) per display adapter, as Windows stores them."""
    keys = {f"{i:04d}": {"DriverDesc": n, "HardwareInformation.qwMemorySize": m, "DriverVersion": "32.0.1"}
            for i, (n, m) in enumerate(adapters)}
    order = list(keys) + ["Configuration"]
    wr = types.ModuleType("winreg")
    wr.HKEY_LOCAL_MACHINE = "HKLM"

    def open_key(parent, name):
        if parent == "HKLM" and name == CLASS:
            return "root"
        if parent == "root" and name in keys:
            return keys[name]
        raise OSError(name)

    def enum_key(key, i):
        if key != "root" or i >= len(order):
            raise OSError("no more")
        return order[i]

    def query(key, name):
        if not isinstance(key, dict) or name not in key:
            raise OSError(name)
        return key[name], 3

    wr.OpenKey, wr.EnumKey, wr.QueryValueEx = open_key, enum_key, query
    return wr


def smi_rows(*cards):
    """nvidia-smi --query-gpu=index,name,memory.total,memory.used,memory.free,driver_version (MiB)."""
    return "\n".join(f"{i}, {n}, {t}, {u}, {t - u}, 616.92" for i, (n, t, u) in enumerate(cards))


class ScanTests(unittest.TestCase):
    def scan(self, adapters=(), smi=None, ram_gb=32, cores=16):
        hardware._cache.update(t=0.0, other=None)
        vm = types.SimpleNamespace(total=ram_gb * GB, available=ram_gb * GB // 2)
        with mock.patch.object(hardware, "IS_WIN", True), \
                mock.patch.dict(sys.modules, {"winreg": fake_winreg(adapters)}), \
                mock.patch.object(hardware, "_nvidia_smi", return_value="nvidia-smi" if smi else None), \
                mock.patch.object(hardware, "_run", return_value=smi or ""), \
                mock.patch.object(hardware, "cpu_name", return_value="Test CPU"), \
                mock.patch.object(hardware, "cuda_driver_version", return_value="13.0"), \
                mock.patch.object(hardware.psutil, "virtual_memory", return_value=vm), \
                mock.patch.object(hardware.psutil, "cpu_count", side_effect=lambda logical=True: cores * (2 if logical else 1)):
            hw = hardware.snapshot()
        hardware._cache.update(t=0.0, other=None)
        return hw

    def test_nvidia_card_read_live_and_not_listed_twice(self):
        hw = self.scan(adapters=[("NVIDIA GeForce RTX 5080", (16 * GB).to_bytes(8, "little")),
                                 ("AMD Radeon(TM) Graphics", (2 * GB).to_bytes(8, "little"))],
                       smi=smi_rows(("NVIDIA GeForce RTX 5080", 16303, 1200)))
        self.assertEqual([g["name"] for g in hw["gpus"]], ["NVIDIA GeForce RTX 5080"])
        self.assertTrue(hw["vram_measured"])
        self.assertEqual((hw["vram_total_mb"], hw["vram_free_mb"], hw["gpu_vendor"]), (16303, 15103, "nvidia"))

    def test_amd_card_from_registry_with_64bit_size(self):
        hw = self.scan(adapters=[("AMD Radeon RX 9070 XT", (16 * GB).to_bytes(8, "little"))])
        g = hw["gpus"][0]
        self.assertEqual((g["name"], g["vendor"], g["total_mb"], g["measured"]), ("AMD Radeon RX 9070 XT", "amd", 16384, False))
        self.assertEqual(g["used_mb"], hardware.DESKTOP_EST_MB)      # an estimate; Aero measures its own models
        self.assertFalse(hw["vram_measured"])
        self.assertIsNone(hw["cuda"])

    def test_intel_arc_card_found_integrated_left_out(self):
        hw = self.scan(adapters=[("Intel(R) Arc(TM) B580 Graphics", 12 * GB),
                                 ("Intel(R) UHD Graphics 770", 2 * GB),
                                 ("Microsoft Basic Display Adapter", 0)])
        self.assertEqual([(g["name"], g["vendor"]) for g in hw["gpus"]], [("Intel(R) Arc(TM) B580 Graphics", "intel")])

    def test_integrated_only_is_cpu_only(self):
        hw = self.scan(adapters=[("AMD Radeon 780M Graphics", (4 * GB).to_bytes(8, "little")),
                                 ("Intel(R) Arc(TM) Graphics", (8 * GB).to_bytes(8, "little"))])
        self.assertEqual(hw["gpus"], [])
        self.assertIsNone(hw["gpu_name"])
        self.assertIn("no dedicated GPU", hardware.summary(hw))

    def test_two_gpus_pooled(self):
        hw = self.scan(adapters=[("NVIDIA GeForce RTX 5080", (16 * GB).to_bytes(8, "little")),
                                 ("NVIDIA GeForce RTX 5060 Ti", (16 * GB).to_bytes(8, "little"))],
                       smi=smi_rows(("NVIDIA GeForce RTX 5080", 16303, 1000), ("NVIDIA GeForce RTX 5060 Ti", 16311, 300)))
        self.assertEqual(len(hw["gpus"]), 2)
        self.assertEqual(hw["vram_total_mb"], 32614)
        self.assertTrue(hw["gpu_name"].startswith("2 GPUs: "))

    def test_recommendations_fit_each_pc(self):
        pcs = {
            "rtx5080": self.scan(adapters=[("NVIDIA GeForce RTX 5080", 16 * GB)],
                                 smi=smi_rows(("NVIDIA GeForce RTX 5080", 16303, 1200))),
            "rx9070xt": self.scan(adapters=[("AMD Radeon RX 9070 XT", 16 * GB)]),
            "rtx3060-laptop": self.scan(adapters=[("NVIDIA GeForce RTX 3060 Laptop GPU", 6 * GB)],
                                        smi=smi_rows(("NVIDIA GeForce RTX 3060 Laptop GPU", 6144, 400)), ram_gb=16, cores=6),
            "cpu-only": self.scan(adapters=[("Intel(R) UHD Graphics 620", 1 * GB)], ram_gb=16, cores=4),
            "cpu-8gb": self.scan(adapters=[], ram_gb=8, cores=4),
        }
        for name, hw in pcs.items():
            with self.subTest(pc=name):
                entry, plan = catalog.recommend_main(hw)
                self.assertIn(plan["fit"], ("gpu", "moe", "cpu", "slow", "split", "no"))
                if hw["gpus"]:
                    self.assertIn(plan["fit"], ("gpu", "moe"), f"{entry['name']}: {plan}")
                    if plan["fit"] == "gpu":
                        self.assertLessEqual(plan["size_gb"] * 1024, hw["vram_total_mb"])
                else:
                    self.assertIn(plan["fit"], ("cpu", "slow", "no"), f"{entry['name']}: {plan}")
                router = catalog.recommend_router(hw)
                self.assertTrue(router["id"])
        # a smaller PC never gets a bigger download than a bigger one
        size = {k: catalog.recommend_main(v)[1]["size_gb"] for k, v in pcs.items()}
        self.assertLessEqual(size["rtx3060-laptop"], size["rtx5080"])
        self.assertLessEqual(size["cpu-8gb"], size["cpu-only"])
        self.assertEqual(catalog.recommend_router(pcs["cpu-8gb"])["id"], "lfm2.5-1.2b")


if __name__ == "__main__":
    unittest.main()
