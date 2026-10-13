import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.parse import unquote

import backend


class BackendTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.root = Path(self.temp.name)
        self.source = self.root / '中文源目录'
        self.source.mkdir()
        self.pdf = self.source / '参考 (1).PDF'
        self.pdf.write_bytes(b'%PDF fixture')
        self.engine = self.root / 'engine'
        for name in ['.venv/Scripts/python.exe', '.venv/Scripts/mineru-kit.exe']:
            file = self.engine / name
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_bytes(b'fixture')
        self.tools = self.root / 'tools'
        self.tools.mkdir()
        (self.tools / 'config.yaml').write_text('fixture', encoding='utf-8')
        (self.tools / 'batch_mineru.py').write_text('fixture', encoding='utf-8')
        self.patches = [patch.object(backend, 'ENGINE_ROOT', self.engine),
                        patch.object(backend, 'TOOL_ROOT', self.tools),
                        patch.object(backend, 'STATE_ROOT', self.root / 'user-state'),
                        patch.object(backend, '_run_parser', side_effect=self.parse)]
        for item in self.patches:
            item.start()
        self.calls = 0
        self.events = []
        self.stop = threading.Event()

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.temp.cleanup()

    def parse(self, pdf, cache, retry_failed, emit):
        self.calls += 1
        folder = cache / 'P01/artifacts'
        (folder / 'images').mkdir(parents=True, exist_ok=True)
        (folder / 'images/图 1.jpg').write_bytes(b'fixture image')
        (folder / 'runtime.json').write_text('{"metadata": "not a public asset"}', encoding='utf-8')
        md = folder / 'markdown.md'
        md.write_text('# 中文测试\n\n![](images/%E5%9B%BE%201.jpg)\n', encoding='utf-8')
        return md

    def run_backend(self, output=None, recursive=False, emit=None):
        return backend.run_conversion(self.source, output, recursive, False,
                                      emit or self.events.append, self.stop)

    def test_default_publication_and_repeat_skip(self):
        result = self.run_backend()
        target = self.pdf.with_suffix('.md')
        self.assertEqual(result['completed'], 1)
        self.assertTrue(target.is_file())
        link = backend.IMAGE_LINK.search(target.read_text(encoding='utf-8')).group(2)
        self.assertTrue((target.parent / unquote(link)).is_file())
        self.assertNotIn('.vlm-work', link)
        self.assertEqual(self.run_backend()['skipped'], 1)
        self.assertEqual(self.calls, 1)
        self.assertEqual(self.pdf.read_bytes(), b'%PDF fixture')
        self.assertFalse((self.source / '参考 (1)_assets/runtime.json').exists())

    def test_interrupted_publication_recovers_without_inference(self):
        install = backend._atomic_install
        def interrupt(source, destination, cache):
            install(source, destination, cache)
            if destination.suffix == '.md':
                raise OSError('模拟写入中断')
        with patch.object(backend, '_atomic_install', side_effect=interrupt):
            self.assertEqual(self.run_backend()['failed'], 1)
        self.assertTrue(self.pdf.with_suffix('.md').exists())
        self.assertEqual(self.run_backend()['skipped'], 1)
        self.assertEqual(self.calls, 1)

    def test_interrupted_publication_preserves_user_changes(self):
        with patch.object(backend, '_atomic_install', side_effect=OSError('模拟写入中断')):
            self.assertEqual(self.run_backend()['failed'], 1)
        self.pdf.with_suffix('.md').write_text('用户中断后修改', encoding='utf-8')
        self.assertEqual(self.run_backend()['failed'], 1)
        self.assertEqual(self.pdf.with_suffix('.md').read_text(encoding='utf-8'), '用户中断后修改')

    def test_recursive_custom_mapping_and_cache_exclusion(self):
        nested = self.source / '子目录'
        nested.mkdir()
        (nested / '文章.pdf').write_bytes(b'pdf')
        hidden = self.source / '.vlm-work'
        hidden.mkdir()
        (hidden / 'exclude.pdf').write_bytes(b'pdf')
        output = self.root / '自选输出'
        result = self.run_backend(output, True)
        self.assertEqual(result['total'], 2)
        self.assertTrue((output / '子目录/文章.md').is_file())
        self.assertFalse((nested / '文章.md').exists())

    def test_existing_markdown_is_not_overwritten(self):
        target = self.pdf.with_suffix('.md')
        target.write_text('用户原文', encoding='utf-8')
        result = self.run_backend()
        self.assertEqual(result['failed'], 1)
        self.assertEqual(target.read_text(encoding='utf-8'), '用户原文')
        self.assertEqual(self.calls, 0)

    def test_user_modified_result_is_not_overwritten(self):
        self.run_backend()
        target = self.pdf.with_suffix('.md')
        target.write_text('用户修改', encoding='utf-8')
        result = self.run_backend()
        self.assertEqual(result['failed'], 1)
        self.assertEqual(self.calls, 1)

    def test_missing_referenced_image_rejects_publication(self):
        def missing(pdf, cache, retry, emit):
            md = cache / 'markdown.md'
            md.write_text('![](missing.jpg)', encoding='utf-8')
            return md
        with patch.object(backend, '_run_parser', side_effect=missing):
            self.assertEqual(self.run_backend()['failed'], 1)
        self.assertFalse(self.pdf.with_suffix('.md').exists())

    def test_stop_after_current_does_not_start_next(self):
        (self.source / '下一份.pdf').write_bytes(b'pdf')
        def emit(event):
            if event['kind'] == 'file_start':
                self.stop.set()
            self.events.append(event)
        result = self.run_backend(emit=emit)
        self.assertEqual(result['completed'], 1)
        self.assertTrue(result['stopped'])
        self.assertEqual(self.calls, 1)

    def test_changed_source_updates_owned_outputs(self):
        self.run_backend()
        self.pdf.write_bytes(b'updated pdf')
        result = self.run_backend()
        self.assertEqual(result['completed'], 1)
        self.assertEqual(self.calls, 2)

    def test_empty_source_and_missing_engine(self):
        with self.assertRaises(ValueError):
            backend.discover_pdfs(self.root / 'not found')
        with patch.object(backend, 'ENGINE_ROOT', self.root / 'not found'):
            with self.assertRaises(RuntimeError):
                self.run_backend()


if __name__ == '__main__':
    unittest.main()
