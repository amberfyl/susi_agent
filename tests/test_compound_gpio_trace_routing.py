import unittest

import susi_gen


class CompoundGpioTraceRoutingTests(unittest.TestCase):
    def test_eio300_uses_nct6694b_for_gpio_trace_gate(self):
        self.assertEqual(
            susi_gen._resolve_gpio_trace_chip_name("EIO-300"),
            "NCT6694B",
        )

    def test_non_compound_chip_keeps_original_identity(self):
        self.assertEqual(
            susi_gen._resolve_gpio_trace_chip_name("NCT6126D"),
            "NCT6126D",
        )

    def test_nct6694b_scope_accepts_all_ec_ports(self):
        self.assertTrue(susi_gen._is_nct6694b_gpio_signal("EC_P1_GPIO0"))
        self.assertTrue(susi_gen._is_nct6694b_gpio_signal("EC_P2_GPIO7"))
        self.assertTrue(susi_gen._is_nct6694b_gpio_signal("EC_P12_GPIO3"))

    def test_nct6694b_scope_rejects_non_target_gpio_names(self):
        self.assertFalse(susi_gen._is_nct6694b_gpio_signal("SIO_GPIO0"))
        self.assertFalse(susi_gen._is_nct6694b_gpio_signal("EC_GPIO0"))
        self.assertFalse(susi_gen._is_nct6694b_gpio_signal("GPIO90"))


if __name__ == "__main__":
    unittest.main()
