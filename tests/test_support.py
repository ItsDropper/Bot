import unittest

from core.support import build_technical_report


class TechnicalSupportIntakeTests(unittest.TestCase):
    def test_formats_environment_and_log_metadata(self):
        report, fields = build_technical_report(
            minecraft_version="1.21.11",
            loader="Fabric 0.18",
            launcher_version="2.4.0",
            summary="The instance crashes while loading the modpack.",
            log_url="https://example.com/latest.log",
        )
        self.assertIn("Minecraft version: 1.21.11", report)
        self.assertIn("Loader: Fabric 0.18", report)
        self.assertEqual(len(fields), 4)

    def test_optional_environment_fields_can_be_blank(self):
        report, fields = build_technical_report(
            summary="The launcher does not open after updating.",
        )
        self.assertEqual(report, "Issue: The launcher does not open after updating.")
        self.assertEqual(fields, [])

    def test_rejects_non_http_log_links(self):
        with self.assertRaisesRegex(ValueError, "http:// or https://"):
            build_technical_report(
                summary="The game crashes on startup.",
                log_url="file:///C:/Users/example/latest.log",
            )

    def test_rejects_reports_over_database_limit(self):
        with self.assertRaisesRegex(ValueError, "report is too long"):
            build_technical_report(
                summary="x" * 300,
                minecraft_version="1.21.11",
                loader="Fabric " + "x" * 80,
                launcher_version="Vanta " + "x" * 40,
                log_url="https://example.com/" + "x" * 300,
            )


if __name__ == "__main__":
    unittest.main()
