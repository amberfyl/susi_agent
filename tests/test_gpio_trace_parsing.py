import importlib.util
import unittest
from unittest.mock import patch
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
GENERATOR_PATH = ROOT / "susi_gen.py"


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


generator = load_module("susi_generator_gpio_trace", GENERATOR_PATH)


class GpioFunctionLabelTests(unittest.TestCase):
    def test_decimal_gpio_label(self):
        self.assertEqual(generator._parse_gpio_function_label("GPIO34"), (3, 4))

    def test_hex_group_gpio_labels(self):
        self.assertEqual(generator._parse_gpio_function_label("GPIOB0"), (11, 0))
        self.assertEqual(generator._parse_gpio_function_label("GPIOA5"), (10, 5))
        self.assertEqual(generator._parse_gpio_function_label("GPIOD0"), (13, 0))

    def test_label_embedded_in_pin_mux_text(self):
        label = "SHD_CS#/CLKRUN#/ESPI_CS2#/GPIOB1"
        self.assertEqual(generator._parse_gpio_function_label(label), (11, 1))

    def test_gp_label(self):
        self.assertEqual(generator._parse_gpio_function_label("GP50"), (5, 0))

    def test_unparseable_label(self):
        self.assertIsNone(generator._parse_gpio_function_label("SIO_LED1"))
        self.assertIsNone(generator._parse_gpio_function_label(""))


class GpioTargetSignalTests(unittest.TestCase):
    def test_nct6694b_traces_all_ec_ports(self):
        hint, pattern = generator._gpio_target_signal_pattern("NCT6694B")
        self.assertEqual(hint, "EC_P*_GPIO*")
        for signal in ("EC_P1_GPIO0", "EC_P2_GPIO5", "EC_P3_GPIO7"):
            self.assertTrue(pattern.match(signal), signal)
        self.assertFalse(pattern.match("SIO_GPIO0"))

    def test_nct61xxd_traces_sio_gpio(self):
        hint, pattern = generator._gpio_target_signal_pattern("NCT6126D")
        self.assertEqual(hint, "SIO_GPIO*")
        self.assertTrue(pattern.match("SIO_GPIO3"))
        self.assertFalse(pattern.match("EC_P1_GPIO0"))


class GpioKeyNumberingTests(unittest.TestCase):
    """INI keys are GPIO00, GPIO01, ... in signal order; never the chip function label."""

    DEFAULTS = {"base_addr": "0", "io_port": "0", "options": "0xA0000003"}

    def _rows(self, product, chip, items, is_ec):
        raw = {"topology_status": "GPIO_TRACE_CONFIRMED", "items": items}
        trace = generator._normalize_gpio_trace_result(raw)
        with patch.object(generator, "_load_gpio_defaults", return_value=self.DEFAULTS):
            result, _ = generator._build_gpio_query_result(
                ROOT / "config_new.db", product, chip, {}, gpio_trace=trace, is_ec=is_ec
            )
        return [(r["item_name"], r["group"], r["bit"]) for r in result["rows"]]

    def test_nct6694b_keys_follow_signal_order_not_function_label(self):
        items = [
            # Deliberately shuffled, with report_name set to the chip function label.
            {"report_name": "GPIO130", "signal": "EC_P2_GPIO0", "function_label": "GPIOD0", "group": 13, "bit": 0, "status": "CONFIRMED"},
            {"report_name": "GPIO35", "signal": "EC_P1_GPIO1", "function_label": "GPIO35", "group": 3, "bit": 5, "status": "CONFIRMED"},
            {"report_name": "GPIO34", "signal": "EC_P1_GPIO0", "function_label": "GPIO34", "group": 3, "bit": 4, "status": "CONFIRMED"},
            {"report_name": "GPIO105", "signal": "EC_P2_GPIO5", "function_label": "GPIOA5", "group": 10, "bit": 5, "status": "CONFIRMED"},
        ]
        self.assertEqual(self._rows("MIO", "NCT6694B", items, True), [
            ("GPIO00", 3, 4),    # EC_P1_GPIO0
            ("GPIO01", 3, 5),    # EC_P1_GPIO1
            ("GPIO02", 13, 0),   # EC_P2_GPIO0
            ("GPIO03", 10, 5),   # EC_P2_GPIO5
        ])

    def test_sio_keys_follow_signal_order(self):
        items = [
            {"report_name": "GPIO42", "signal": "SIO_GPIO2", "function_label": "GP42", "group": 4, "bit": 2, "status": "CONFIRMED"},
            {"report_name": "GPIO50", "signal": "SIO_GPIO0", "function_label": "GP50", "group": 5, "bit": 0, "status": "CONFIRMED"},
            {"report_name": "GPIO51", "signal": "SIO_GPIO1", "function_label": "GP51", "group": 5, "bit": 1, "status": "CONFIRMED"},
        ]
        self.assertEqual(self._rows("AIMB", "NCT6126D", items, False), [
            ("GPIO00", 5, 0), ("GPIO01", 5, 1), ("GPIO02", 4, 2),
        ])


if __name__ == "__main__":
    unittest.main()
