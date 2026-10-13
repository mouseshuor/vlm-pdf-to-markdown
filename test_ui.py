"""UI contract checks without starting an inference model."""
import json
from pathlib import Path
import sys
import tempfile
import tkinter as tk
import types
import unittest
from unittest.mock import patch

import launcher


class UITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=launcher.APP_ROOT)
        self.source = Path(self.temp.name) / '中文 PDF'
        self.source.mkdir()
        self.pdf = self.source / '示例.pdf'
        self.pdf.write_bytes(b'%PDF-1.4 fixture')
        self.backend = types.SimpleNamespace(discover_pdfs=lambda *args: [self.pdf])
        self.mock = patch.dict(sys.modules, {'backend': self.backend})
        self.mock.start()
        self.root = tk.Tk()
        self.root.withdraw()
        with patch.object(launcher, 'SETTINGS', Path(self.temp.name) / 'settings.json'):
            self.app = launcher.Launcher(self.root)
        self.app.source.set(str(self.source))
        self.app.scan()
        self.root.update()

    def tearDown(self):
        self.app.cancel_timers()
        self.root.destroy()
        self.mock.stop()
        self.temp.cleanup()

    def test_source_output_default_and_custom_switch(self):
        self.assertTrue(self.app.same_folder.get())
        self.assertEqual(self.app.destination.get(), str(self.source))
        self.assertEqual(str(self.app.dest_entry.cget('state')), 'disabled')
        self.app.same_folder.set(False)
        custom = str(Path(self.temp.name) / '自选目录')
        self.app.destination.set(custom)
        self.app.same_folder.set(True)
        self.assertEqual(self.app.destination.get(), str(self.source))
        self.app.same_folder.set(False)
        self.assertEqual(self.app.destination.get(), custom)

    def test_empty_source_disables_start(self):
        self.backend.discover_pdfs = lambda *args: []
        self.app.scan()
        self.assertEqual(str(self.app.start_button.cget('state')), 'disabled')
        self.assertIn('没有 PDF', self.app.status.get())

    def test_progress_result_and_restore_controls(self):
        self.app.active = True
        self.app.handle_event({'kind': 'file_start', 'index': 1, 'total': 1, 'path': str(self.pdf)})
        row = self.app.rows[str(self.pdf.resolve())]
        self.assertEqual(self.app.tree.set(row, 'status'), '正在转换')
        self.app.handle_event({'kind': 'file_done', 'index': 1, 'total': 1,
                              'path': str(self.pdf), 'status': 'completed',
                              'markdown': str(self.pdf.with_suffix('.md')), 'error': None})
        self.app.handle_event({'kind': 'finished', 'summary': {
            'total': 1, 'completed': 1, 'skipped': 0, 'failed': 0,
            'stopped': False, 'output_root': str(self.source)}})
        self.assertFalse(self.app.active)
        self.assertEqual(self.app.tree.set(row, 'status'), '已完成')
        self.assertEqual(str(self.app.open_button.cget('state')), 'normal')
        self.assertEqual(str(self.app.dest_entry.cget('state')), 'disabled')

    def test_stop_waits_for_current_and_close_keeps_window(self):
        self.app.active = True
        self.app.request_stop()
        self.assertTrue(self.app.stop_event.is_set())
        self.assertEqual(str(self.app.stop_button.cget('state')), 'disabled')
        with patch.object(launcher.messagebox, 'askyesno', return_value=True):
            self.app.close()
        self.assertTrue(self.root.winfo_exists())


def screenshot():
    import ctypes
    from PIL import ImageGrab
    root = tk.Tk()
    app = launcher.Launcher(root)
    root.geometry('960x760+30+30')
    root.update()
    root.after(350, root.quit)
    root.mainloop()
    handle = ctypes.windll.user32.GetAncestor(root.winfo_id(), 2)
    ImageGrab.grab(window=handle).save(launcher.APP_ROOT / 'qa-window.png')
    visible = {'window': [root.winfo_width(), root.winfo_height()],
               'default_same_folder': app.same_folder.get(), 'recursive': app.recursive.get(),
               'file_count': len(app.files), 'start_state': str(app.start_button.cget('state'))}
    (launcher.APP_ROOT / 'qa-ui.json').write_text(json.dumps(visible, ensure_ascii=False, indent=2), encoding='utf-8')
    app.cancel_timers()
    root.destroy()


if __name__ == '__main__':
    if '--screenshot' in sys.argv:
        screenshot()
    else:
        unittest.main()
