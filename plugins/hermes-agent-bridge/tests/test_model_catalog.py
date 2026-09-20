import importlib.util
import unittest
from pathlib import Path

spec = importlib.util.spec_from_file_location('model_catalog', Path(__file__).parents[1] / 'src' / 'model_catalog.py')
catalog = importlib.util.module_from_spec(spec)
spec.loader.exec_module(catalog)


class ModelCatalog(unittest.TestCase):
    def test_openrouter_uses_configured_deepseek_without_codex_fallback(self):
        result = catalog.configured_model_catalog({'model': {'default': 'deepseek/deepseek-v4.1-flash', 'provider': 'openrouter'}}, 'now')
        self.assertEqual(result['models'], ['deepseek/deepseek-v4.1-flash'])
        self.assertEqual(result['defaultModel'], result['models'][0])
        self.assertEqual(result['entries'][0]['provider'], 'openrouter')

    def test_unconfigured_runtime_does_not_invent_a_model(self):
        for config in [{}, None, {'model': {'provider': 'openrouter'}}, {'model': 'invalid model'}]:
            self.assertIsNone(catalog.configured_model_catalog(config, 'now'))


if __name__ == '__main__':
    unittest.main()
