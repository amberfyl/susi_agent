import importlib.util
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
GENERATOR_PATH = ROOT / "susi_gen.py"


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


generator = load_module("susi_generator_infer_chip", GENERATOR_PATH)


class InferChipNameTests(unittest.TestCase):
    def test_ec_chip_is_inferred_from_chips(self):
        spec = {"chips": {"hwm": "EIOIS200", "gpio": "EIO-211"}}
        self.assertEqual(generator._infer_chip_name(spec), "EIO-211")

    def test_compound_string_prefers_ec_key(self):
        spec = {"chips": {"hwm": "Nuvoton_NCT6694B(EIO-300)", "gpio": "NCT6694B"}}
        self.assertEqual(generator._infer_chip_name(spec), "EIO-300")

    def test_ec_key_anywhere_wins_over_earlier_nct(self):
        spec = {"chips": {"hwm": "NCT6694B", "gpio": "EIO-300"}}
        self.assertEqual(generator._infer_chip_name(spec), "EIO-300")

    def test_sio_chip_is_inferred_from_chips(self):
        spec = {"chips": {"hwm": "Nuvoton NCT6126D", "gpio": "NCT6126D"}}
        self.assertEqual(generator._infer_chip_name(spec), "NCT6126D")

    def test_sio_chip_with_separator_is_normalized(self):
        spec = {"chips": {"hwm": "nuvoton nct-6116d"}}
        self.assertEqual(generator._infer_chip_name(spec), "NCT6116D")

    def test_sio_chip_is_inferred_from_gpio_pins(self):
        spec = {"gpio": {"pins": [{"chip": "NCT6106D"}]}}
        self.assertEqual(generator._infer_chip_name(spec), "NCT6106D")

    def test_unknown_chip_returns_none(self):
        spec = {"chips": {"hwm": "Fintek F81866"}}
        self.assertIsNone(generator._infer_chip_name(spec))


if __name__ == "__main__":
    unittest.main()
