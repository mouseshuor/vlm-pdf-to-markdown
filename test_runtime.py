import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import backend


class RuntimeTests(unittest.TestCase):
    def test_configuration_and_environment_precedence(self):
        with tempfile.TemporaryDirectory() as folder:
            state = Path(folder)
            (state / 'config.json').write_text(json.dumps({
                'mineru_python': str(state / 'configured-python'),
                'mineru_cli': str(state / 'configured-cli'),
                'mineru_config': str(state / 'configured.yaml')}), encoding='utf-8')
            with patch.object(backend, 'STATE_ROOT', state), patch.dict(os.environ, {
                'VLM_MINERU_PYTHON': str(state / 'override-python'),
                'VLM_MINERU_CLI': '', 'VLM_MINERU_CONFIG': ''}):
                runtime = backend.resolve_runtime()
                self.assertEqual(runtime['python'], state / 'override-python')
                self.assertEqual(runtime['cli'], state / 'configured-cli')
                self.assertEqual(runtime['config'], state / 'configured.yaml')
                env = backend.model_env(runtime)
                self.assertEqual(Path(env['MINERU_HOME']), state / 'mineru')

    def test_invalid_configuration_fails_clearly(self):
        with tempfile.TemporaryDirectory() as folder:
            state = Path(folder)
            (state / 'config.json').write_text('[]', encoding='utf-8')
            with patch.object(backend, 'STATE_ROOT', state):
                with self.assertRaises(ValueError):
                    backend.resolve_runtime()


if __name__ == '__main__':
    unittest.main()
