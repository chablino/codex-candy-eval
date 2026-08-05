import tomllib
import unittest
from pathlib import Path

import tomli_w


class VendoredTomliWTests(unittest.TestCase):
    def test_nested_plugin_config_round_trips(self):
        repository_root = Path(__file__).resolve().parents[1]
        document = {
            "model": "gpt-test",
            "plugins": {
                "superpowers@openai-api-curated": {"enabled": True},
            },
        }

        rendered = tomli_w.dumps(document)

        self.assertEqual(
            Path(tomli_w.__file__).resolve().parent,
            repository_root / "tomli_w",
        )
        self.assertEqual(tomllib.loads(rendered), document)


if __name__ == "__main__":
    unittest.main()
