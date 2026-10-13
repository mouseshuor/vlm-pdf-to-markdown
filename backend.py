"""Local folder conversion, publication and resume safety."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PureWindowsPath
import re
import shutil
import subprocess
import threading
import tempfile
import sys
from urllib.parse import quote, unquote, urlsplit
from runtime_paths import APP_ROOT, STATE_ROOT

ENGINE_ROOT = APP_ROOT
TOOL_ROOT = APP_ROOT
IMAGE_LINK = re.compile(r'(!\[[^\]]*\]\()(<[^>]+>|[^\s)]+)(\))')


def discover_pdfs(source, recursive=False):
    source = Path(source).expanduser().resolve()
    if not source.is_dir():
        raise ValueError('所选文件夹不存在。')
    if recursive:
        files = []
        def onerror(exc):
            raise exc
        for folder, dirs, names in os.walk(source, followlinks=False, onerror=onerror):
            dirs[:] = [name for name in dirs if name != '.vlm-work' and not (Path(folder) / name).is_symlink()]
            files.extend(Path(folder) / name for name in names if Path(name).suffix.lower() == '.pdf')
    else:
        files = [file for file in source.iterdir() if file.is_file() and file.suffix.lower() == '.pdf']
    return sorted(files, key=lambda p: str(p).casefold())


def file_hash(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def write_json(path, data):
    tmp = path.with_name(path.name + '.tmp')
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    tmp.replace(path)


def load_record(path):
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding='utf-8'))


def resolve_runtime():
    config_path = STATE_ROOT / 'config.json'
    config = load_record(config_path)
    if not isinstance(config, dict):
        raise ValueError('运行配置必须是 JSON 对象。')
    scripts = '.venv/Scripts' if os.name == 'nt' else '.venv/bin'
    python_name = 'python.exe' if os.name == 'nt' else 'python'
    cli_name = 'mineru-kit.exe' if os.name == 'nt' else 'mineru-kit'
    python = os.environ.get('VLM_MINERU_PYTHON') or config.get('mineru_python')
    cli = os.environ.get('VLM_MINERU_CLI') or config.get('mineru_cli')
    python_default = ENGINE_ROOT / scripts / python_name
    cli_default = ENGINE_ROOT / scripts / cli_name
    python = Path(python or (python_default if python_default.is_file() else sys.executable)).expanduser().resolve()
    cli = Path(cli or (cli_default if cli_default.is_file() else shutil.which('mineru-kit') or cli_default)).expanduser().resolve()
    model_config = Path(os.environ.get('VLM_MINERU_CONFIG') or config.get('mineru_config') or TOOL_ROOT / 'config.yaml').expanduser().resolve()
    return {'python': python, 'cli': cli, 'config': model_config}


def model_env(runtime=None):
    runtime = runtime or resolve_runtime()
    env = os.environ.copy()
    values = {
        'PYTHONIOENCODING': 'utf-8', 'PYTHONUTF8': '1',
        'MINERU_HOME': str(STATE_ROOT / 'mineru'),
        'MINERU_CONFIG': str(runtime['config']),
        'HF_HOME': str(STATE_ROOT / 'cache/huggingface'),
        'MODELSCOPE_CACHE': str(STATE_ROOT / 'cache/modelscope'),
        'MODELSCOPE_HOME': str(STATE_ROOT / 'mineru/modelscope'),
        'UV_CACHE_DIR': str(STATE_ROOT / 'cache/uv'),
        'HF_HUB_DISABLE_TELEMETRY': '1', 'HF_HUB_DISABLE_XET': '1',
    }
    env.update(values)
    return env


def _run_parser(pdf, cache, retry_failed, emit):
    runtime = resolve_runtime()
    argv = [str(runtime['python']), str(TOOL_ROOT / 'batch_mineru.py'),
            '--mineru-kit', str(runtime['cli']),
            '--output-root', str(cache), '--pdf', str(pdf)]
    if retry_failed:
        argv.append('--retry-failed')
    process = subprocess.Popen(argv, cwd=str(TOOL_ROOT), env=model_env(runtime),
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               text=True, encoding='utf-8', errors='replace', shell=False,
                               creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
    with (cache / 'conversion.log').open('w', encoding='utf-8') as log:
        for line in process.stdout:
            log.write(line)
            log.flush()
            if line.strip():
                emit({'kind': 'activity', 'text': line.strip()})
    code = process.wait()
    progress_path = cache / 'progress.json'
    if not progress_path.is_file():
        raise RuntimeError(f'解析器未生成结果（退出码 {code}）。详情：{cache / "conversion.log"}')
    progress = load_record(progress_path)
    records = [item for item in progress.get('files', {}).values()
               if Path(item.get('source', '')).resolve() == pdf.resolve()]
    if len(records) != 1 or records[0].get('status') != 'completed' or code != 0:
        error = records[0].get('error') if records else None
        raise RuntimeError(error or '上次解析失败或配置不一致。可勾选重试失败文件；详情见转换日志。')
    markdown = Path(records[0].get('md_actual') or records[0].get('md_expected') or '')
    if not markdown.is_file() or not markdown.resolve().is_relative_to(cache.resolve()):
        raise RuntimeError('解析产物路径无效。')
    if not markdown.read_text(encoding='utf-8').strip():
        raise RuntimeError('解析结果为空。')
    return markdown


def _publication_guard(target, assets, record):
    if target.is_symlink() or assets.is_symlink():
        raise RuntimeError('输出位置包含同名目录链接，请选择其他输出位置。')
    if target.exists():
        if not record or record.get('target') != str(target) or file_hash(target) != record.get('md_hash'):
            raise RuntimeError(f'已有同名 Markdown 或文件已被修改，未覆盖：{target.name}。请另选输出目录。')
    if assets.exists():
        if not record or record.get('assets') != str(assets):
            raise RuntimeError(f'已有同名配图目录，未覆盖：{assets.name}。请另选输出目录。')
        expected = record.get('asset_hashes', {})
        for file in assets.rglob('*'):
            if file.is_symlink():
                raise RuntimeError('配图目录包含目录链接，请另选输出位置。')
            if file.is_file():
                name = file.relative_to(assets).as_posix()
                if name not in expected or file_hash(file) != expected[name]:
                    raise RuntimeError('已有配图被修改或添加，未覆盖。请另选输出目录。')


def _publish(markdown, target, assets, metadata, source_hash, config_hash):
    text = markdown.read_text(encoding='utf-8')
    references = []
    def replace(match):
        original = match.group(2).strip('<>')
        if Path(original).is_absolute() or PureWindowsPath(original).is_absolute():
            raise RuntimeError('图片路径必须为解析产物内的相对路径。')
        if urlsplit(original).scheme:
            raise RuntimeError('解析结果含外部图片地址，未发布。')
        relative = unquote(original).replace('\\', '/')
        file = (markdown.parent / relative).resolve()
        if not file.is_relative_to(markdown.parent.resolve()) or not file.is_file():
            raise RuntimeError(f'Markdown 配图不存在或路径越界：{original}')
        references.append(file)
        uri = quote(assets.name + '/' + file.relative_to(markdown.parent.resolve()).as_posix(), safe='/')
        return match.group(1) + uri + match.group(3)
    converted = IMAGE_LINK.sub(replace, text)
    # Stage assets and write a recovery journal before touching published files.
    stage = metadata.parent / 'publish-stage'
    stage.mkdir(parents=True, exist_ok=True)
    old = load_record(metadata)
    hashes = dict(old.get('asset_hashes', {}))
    staged_files = {}
    image_suffixes = {'.png', '.jpg', '.jpeg', '.gif', '.webp', '.svg', '.bmp', '.tif', '.tiff'}
    for file in markdown.parent.rglob('*'):
        if file.is_symlink():
            raise RuntimeError('解析产物包含目录链接，未发布。')
        if file.is_file() and file != markdown and (file in references or file.suffix.lower() in image_suffixes):
            relative = file.relative_to(markdown.parent)
            destination = stage / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(file, destination)
            hashes[relative.as_posix()] = file_hash(destination)
            staged_files[relative.as_posix()] = str(destination)
    staged_md = stage / '_markdown.txt'
    staged_md.write_text(converted, encoding='utf-8')
    final = {'target': str(target), 'assets': str(assets), 'source_hash': source_hash,
             'config_hash': config_hash, 'md_hash': file_hash(staged_md), 'asset_hashes': hashes}
    write_json(metadata.with_name('publication.pending.json'),
               {'final': final, 'old': old, 'staged_files': staged_files, 'staged_md': str(staged_md)})
    _complete_pending(target, assets, metadata)


def _atomic_install(source, destination, cache):
    with tempfile.NamedTemporaryFile(dir=cache, prefix='install-', delete=False) as stream:
        temp = Path(stream.name)
    try:
        shutil.copyfile(source, temp)
        temp.replace(destination)
    finally:
        if temp.exists():
            temp.unlink()


def _complete_pending(target, assets, metadata):
    journal = metadata.with_name('publication.pending.json')
    if not journal.exists():
        return
    pending = load_record(journal)
    final, old = pending['final'], pending['old']
    if final['target'] != str(target) or final['assets'] != str(assets):
        raise RuntimeError('发布恢复记录与输出位置不一致。')
    if target.is_symlink() or assets.is_symlink():
        raise RuntimeError('输出位置包含目录链接，无法恢复发布。')
    if target.exists() and file_hash(target) not in {final['md_hash'], old.get('md_hash')}:
        raise RuntimeError('中断后 Markdown 被修改，未覆盖。请另选输出目录。')
    if assets.exists():
        for file in assets.rglob('*'):
            if file.is_symlink():
                raise RuntimeError('配图目录包含目录链接，无法恢复发布。')
            if file.is_file():
                name = file.relative_to(assets).as_posix()
                allowed = {final['asset_hashes'].get(name), old.get('asset_hashes', {}).get(name)}
                if file_hash(file) not in allowed:
                    raise RuntimeError('中断后配图被修改或添加，未覆盖。请另选输出目录。')
    for name, staged in pending['staged_files'].items():
        source = Path(staged)
        destination = assets / name
        if (not source.resolve().is_relative_to(metadata.parent.resolve())
                or not destination.resolve().is_relative_to(assets.resolve())
                or file_hash(source) != final['asset_hashes'][name]):
            raise RuntimeError('待恢复的配图校验失败。')
    staged_md = Path(pending['staged_md'])
    if not staged_md.resolve().is_relative_to(metadata.parent.resolve()) or file_hash(staged_md) != final['md_hash']:
        raise RuntimeError('待恢复的 Markdown 校验失败。')
    assets.mkdir(parents=True, exist_ok=True)
    for name, source in pending['staged_files'].items():
        destination = assets / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        _atomic_install(source, destination, metadata.parent)
    _atomic_install(staged_md, target, metadata.parent)
    write_json(metadata, final)
    journal.unlink()


def run_conversion(source, output, recursive, retry_failed, emit, stop_event):
    source = Path(source).resolve()
    pdfs = discover_pdfs(source, recursive)
    if not pdfs:
        raise ValueError('所选文件夹没有 PDF。')
    runtime = resolve_runtime()
    for required in [runtime['python'], runtime['cli'], TOOL_ROOT / 'batch_mineru.py', runtime['config']]:
        if not required.is_file():
            raise RuntimeError(f'本地 MinerU 组件缺失：{required}。请先按 README 安装或配置运行环境。')
    output_root = Path(output).expanduser().resolve() if output is not None else source
    output_root.mkdir(parents=True, exist_ok=True)
    summary = {'total': len(pdfs), 'completed': 0, 'skipped': 0, 'failed': 0,
               'stopped': False, 'output_root': str(output_root)}
    emit({'kind': 'scan', 'total': len(pdfs)})
    claimed = set()
    runtime_fingerprint = json.dumps({key: str(value) for key, value in runtime.items()}, sort_keys=True)
    cli_stat = runtime['cli'].stat()
    config_hash = (file_hash(runtime['config']) + file_hash(TOOL_ROOT / 'batch_mineru.py')
                   + hashlib.sha256((runtime_fingerprint + str(cli_stat.st_size) + str(cli_stat.st_mtime_ns)).encode()).hexdigest())
    for index, pdf in enumerate(pdfs, 1):
        if stop_event.is_set():
            summary['stopped'] = True
            break
        emit({'kind': 'file_start', 'index': index, 'total': len(pdfs), 'path': str(pdf)})
        target = None
        error = None
        status = 'failed'
        try:
            folder = pdf.parent if output is None else output_root / pdf.parent.relative_to(source)
            folder.mkdir(parents=True, exist_ok=True)
            target = folder / (pdf.stem + '.md')
            assets = folder / (pdf.stem + '_assets')
            key = str(target).casefold()
            if key in claimed:
                raise RuntimeError('同目录有同名 PDF，无法使用同一个输出名。请将它们分别存放。')
            claimed.add(key)
            cache = folder / '.vlm-work' / hashlib.sha256(str(pdf.resolve()).encode('utf-8')).hexdigest()[:20]
            cache.mkdir(parents=True, exist_ok=True)
            metadata = cache / 'publication.json'
            _complete_pending(target, assets, metadata)
            record = load_record(metadata)
            _publication_guard(target, assets, record)
            source_hash = file_hash(pdf)
            complete_assets = assets.is_dir() and all((assets / name).is_file() for name in record.get('asset_hashes', {}))
            if (record and target.is_file() and complete_assets and record.get('source_hash') == source_hash
                    and record.get('config_hash') == config_hash):
                status = 'skipped'
            else:
                emit({'kind': 'log', 'text': f'加载本地模型并解析：{pdf.name}'})
                markdown = _run_parser(pdf, cache, retry_failed, emit)
                _publish(markdown, target, assets, metadata, source_hash, config_hash)
                status = 'completed'
        except Exception as exc:
            error = str(exc)
        summary[status] += 1
        emit({'kind': 'file_done', 'index': index, 'total': len(pdfs), 'path': str(pdf),
              'status': status, 'markdown': str(target) if status != 'failed' else None, 'error': error})
    summary['stopped'] = summary['stopped'] or stop_event.is_set()
    return summary
