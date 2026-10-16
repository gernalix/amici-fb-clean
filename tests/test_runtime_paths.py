import os
import unittest
from pathlib import Path
from unittest.mock import patch

import amici_fb


class RuntimePathTests(unittest.TestCase):
    def test_output_defaults_to_private_app_data_tree(self):
        with patch.dict(
            os.environ,
            {
                "AMICI_FB_DATA_HOME": "/tmp/amici-fb-test-home",
                "AMICI_FB_OUTPUT_DIR": "",
            },
        ):
            self.assertEqual(
                amici_fb.output_dir_path(),
                Path("/tmp/amici-fb-test-home/data"),
            )

    def test_explicit_output_directory_wins(self):
        with patch.dict(
            os.environ,
            {"AMICI_FB_OUTPUT_DIR": "/tmp/amici-fb-test-output"},
        ):
            self.assertEqual(
                amici_fb.output_dir_path(),
                Path("/tmp/amici-fb-test-output"),
            )


if __name__ == "__main__":
    unittest.main()
