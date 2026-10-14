import sys
import unittest
from pathlib import Path


LIB = Path(__file__).resolve().parents[1] / "usr/lib/ed-config"
sys.path.insert(0, str(LIB))

from edconfig.catalog import Catalog  # noqa: E402
from edconfig.system import BEGIN, END, _config_content  # noqa: E402


class ConfigPreservationTests(unittest.TestCase):
    def test_existing_lines_outside_managed_block_are_preserved(self):
        original = (
            "# customer comment\n"
            "[cm4]\n"
            "otg_mode=1\n"
            "dtoverlay=uart2\n"
            "dtoverlay=jedec-spi-nor,spi1-0\n"
            "[cm5]\n"
            "dtoverlay=dwc2,dr_mode=host\n"
            "[all]\n"
            "gpu_mem=128\n"
        )
        updated = _config_content(
            original, ["otg_mode=1", "dtoverlay=uart2", "gpu_mem=128"], "CM4"
        )
        self.assertTrue(updated.startswith(original))
        self.assertEqual(updated.count("otg_mode=1"), 1)
        self.assertEqual(updated.count("dtoverlay=uart2"), 1)
        self.assertEqual(updated.count("dtoverlay=jedec-spi-nor,spi1-0"), 1)
        self.assertEqual(updated.count("gpu_mem=128"), 1)
        self.assertEqual(
            _config_content(updated, ["otg_mode=1", "dtoverlay=uart2", "gpu_mem=128"], "CM4"),
            updated,
        )

    def test_switching_to_ipc2632_replaces_only_the_old_managed_block(self):
        original = (
            "# factory setting\n"
            "[cm4]\n"
            "otg_mode=1\n"
            "[all]\n"
            "gpu_mem=128\n\n"
            f"{BEGIN}\n"
            "dtoverlay=jedec-spi-nor,spi1-0\n"
            f"{END}\n"
            "# customer setting after the managed block\n"
        )
        catalog = Catalog(LIB / "catalog")
        resolution = catalog.resolve("IPC2632", [])
        desired = [item["value"] for item in resolution["config_lines"]]
        updated = _config_content(original, desired, "CM4")
        self.assertTrue(
            updated.startswith("# factory setting\n[cm4]\notg_mode=1\n[all]\ngpu_mem=128\n\n")
        )
        self.assertTrue(updated.endswith("# customer setting after the managed block\n"))
        self.assertEqual(updated.count("otg_mode=1"), 1)
        self.assertNotIn("dtoverlay=jedec-spi-nor,spi1-0", updated)
        self.assertIn("dtoverlay=ed-ipc,ipc2100,exp8x8y", updated)
        self.assertEqual(_config_content(updated, desired, "CM4"), updated)

    def test_setting_for_other_platform_does_not_hide_needed_line(self):
        original = "[cm5]\notg_mode=1\n[all]\n"
        updated = _config_content(original, ["otg_mode=1"], "CM4")
        self.assertTrue(updated.startswith(original))
        self.assertEqual(updated.count("otg_mode=1"), 2)


if __name__ == "__main__":
    unittest.main()
