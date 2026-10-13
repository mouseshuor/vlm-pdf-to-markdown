"""Runtime data stays outside the source checkout."""
import os
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parent
STATE_ROOT = Path(os.environ.get('VLM_APP_DATA') or (
    Path(os.environ.get('LOCALAPPDATA', Path.home() / '.local/share')) / 'VlmPdfToMarkdown'
)).expanduser().resolve()
