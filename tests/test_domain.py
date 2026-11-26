import unittest

from astrbot_plugin_immich_xwuid.core import classify_image, parse_sync_command, summarize_results


class DomainTests(unittest.TestCase):
    def test_square_photo_goes_to_panel_album(self):
        self.assertEqual(classify_image(1000, 1000), "panel")

    def test_landscape_photo_goes_to_background_album(self):
        self.assertEqual(classify_image(1600, 900), "background")

    def test_portrait_photo_goes_to_panel_album(self):
        self.assertEqual(classify_image(900, 1600), "panel")

    def test_sync_command_parses_multiple_roles_and_kind(self):
        self.assertEqual(parse_sync_command("同步背景图相册", "折枝，椿"), (["折枝", "椿"], "background"))

    def test_result_summary_counts_success_duplicate_and_failure(self):
        result = summarize_results([
            {"status": "success"}, {"status": "duplicate"}, {"status": "failed", "reason": "ratio"}
        ])
        self.assertEqual(result, {"success": 1, "duplicate": 1, "failed": 1,
                                  "failures": ["图片比例不符合目标规格"]})


if __name__ == "__main__":
    unittest.main()
