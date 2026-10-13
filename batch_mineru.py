#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""MinerU 4.0.12 CLI 顺序批量测试脚本（本机 VLM 推理，可断点续跑）。

用途
    依次调用官方 MinerU CLI，把一批本地 PDF 转换为 Markdown，并记录可复算的机器指标、
    日志和进度。脚本不联网解析、不拆分大文件、不自动重试、不安装依赖。

设计要点
    - 顺序执行：按文件大小升序逐个处理，同一时刻只运行一个解析进程。
    - 可断点续跑：progress.json 在启动前写入一次，每个文件结束后再写入一次，写入采用临时文件加替换。
    - 可复算：每个文件记录绝对路径、字节数、SHA256、页数、耗时、产物校验指标和日志路径。
    - 只依赖标准库与 pypdf；不导入任何 GPU Python 包。

命令示例
    python batch_mineru.py --mineru-kit <absolute-cli-path> --output-root <absolute-output-path> --pdf <absolute-pdf-path>

退出码
    0  全部文件完成，或跳过了已完成的相同输入
    1  存在失败或拒绝处理的文件
    2  参数、运行环境或运行参数签名错误，未开始解析
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import threading
import time
import unicodedata
import urllib.parse
import zipfile
from datetime import datetime, timezone
from pathlib import Path

HARNESS_VERSION = "1.1.0"
SCHEMA_VERSION = 2

# 单个 PDF 的上限。超限直接判为失败并给出明确原因，不做拆分。
MAX_PDF_BYTES = 200 * 1024 * 1024

# 解析命令的固定部分。--tier standard 对应本机 llama.cpp Vulkan VLM 标准档。
# 参数与官方文档不一致时，先跑一次并检查 cli_probe.json 中的帮助文本，再只改这一处。
PARSE_SUBCOMMAND = "parse"
PARSE_TIER = "standard"
PARSE_EXTRA_ARGS: tuple[str, ...] = ("--format", "zip", "--pages", "all", "--ocr-mode", "ocr")
MD_OUTPUT_NAME = "artifacts/markdown.md"

GPU_POLL_SECONDS = 2.0
GPU_QUERY = [
    "nvidia-smi",
    "--query-gpu=memory.used,utilization.gpu",
    "--format=csv,noheader,nounits",
]

TABLE_DELIMITER_RE = re.compile(r"^\s{0,3}\|?\s*:?-{2,}:?\s*(?:\|\s*:?-{2,}:?\s*)+\|?\s*$")
IMAGE_LINK_RE = re.compile(r"!\[[^\]]*\]\(\s*(<[^>]*>|[^)\s]+)")
DOLLAR_DELIMITER_RE = re.compile(r"\$\$|\$")
CJK_CHAR_RE = re.compile(r"[\u3040-\u30ff\u31f0-\u31ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uff66-\uff9f]")
LATIN_UNIT_RE = re.compile(r"[0-9a-z][0-9a-z_.\-]{1,}")

SKIP_REASON_COMPLETED = "completed_unchanged"
SKIP_REASON_FAILED = "previous_failure_use_retry_failed"


# ---------------------------------------------------------------- 基础工具

def utc_now() -> str:
    """返回带时区的 UTC 时间戳，便于跨机器比对。"""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def log(message: str) -> None:
    """控制台输出。个别字符无法用当前控制台代码页编码时退化为转义写法，不影响运行。"""
    line = "[batch] " + message
    try:
        print(line, flush=True)
    except UnicodeEncodeError:
        print(line.encode("ascii", "backslashreplace").decode("ascii"), flush=True)


def abort(message: str, code: int = 2) -> None:
    """参数或运行环境错误：写日志与标准错误，并以固定退出码结束，不开始解析。"""
    log(message)
    try:
        print(message, file=sys.stderr, flush=True)
    except UnicodeEncodeError:
        print(message.encode("ascii", "backslashreplace").decode("ascii"), file=sys.stderr, flush=True)
    raise SystemExit(code)


def sha256_of_file(path: Path, chunk_bytes: int = 1 << 20) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(chunk_bytes), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_of_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def atomic_write_json(path: Path, payload: dict) -> None:
    """先写临时文件再替换，避免进程中断留下半个 JSON。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(path.name + ".tmp")
    with tmp_path.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp_path, path)


def load_json(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        with path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        log("progress.json 无法解析，按空进度继续：%s" % exc)
        return {}
    return data if isinstance(data, dict) else {}


def build_child_env() -> dict:
    """子进程环境。强制标准输出走 UTF-8，否则 Windows 控制台代码页会让日志出现替换字符。"""
    env = dict(os.environ)
    env.setdefault("PYTHONUTF8", "1")
    env.setdefault("PYTHONIOENCODING", "utf-8")
    return env


def runtime_signature(mineru_kit: Path) -> dict:
    """解析器身份与解析参数指纹。续跑只允许在同一指纹下进行。"""
    stat = mineru_kit.stat()
    config_file = os.environ.get("MINERU_CONFIG", "")
    config_path = Path(config_file).resolve() if config_file else None
    option_env = {name: value for name, value in os.environ.items() if name.startswith("MINERU_MODEL_")}
    return {
        "mineru_kit": str(mineru_kit),
        "mineru_kit_size_bytes": stat.st_size,
        "mineru_kit_mtime_ns": stat.st_mtime_ns,
        "parse_argv": [PARSE_SUBCOMMAND, "--tier", PARSE_TIER, *PARSE_EXTRA_ARGS],
        "md_output_name": MD_OUTPUT_NAME,
        "harness_version": HARNESS_VERSION,
        "schema_version": SCHEMA_VERSION,
        "config_file": str(config_path) if config_path else None,
        "config_sha256": sha256_of_file(config_path) if config_path and config_path.is_file() else None,
        "model_option_env_sha256": sha256_of_text(json.dumps(option_env, sort_keys=True)),
    }


def signature_id(signature: dict) -> str:
    canonical = json.dumps(signature, ensure_ascii=False, sort_keys=True)
    return "sha256:" + sha256_of_text(canonical)


# ---------------------------------------------------------------- 输入与页数

class PdfInput:
    __slots__ = ("path", "size_bytes", "key")

    def __init__(self, path: Path) -> None:
        self.path = path
        self.size_bytes = path.stat().st_size
        self.key = str(path).casefold()


def require_pypdf() -> str:
    try:
        import pypdf
    except ImportError as exc:  # 环境问题，直接停止，不降级运行
        abort("错误：未安装 pypdf，无法在执行前记录页数。请先在本机虚拟环境安装 pypdf。原始错误：%s" % exc)
    return getattr(pypdf, "__version__", "unknown")


def collect_pdf_inputs(raw_paths: list[str]) -> list[PdfInput]:
    inputs: list[PdfInput] = []
    seen: set[str] = set()
    for raw in raw_paths:
        candidate = Path(raw)
        if not candidate.is_absolute():
            abort("错误：--pdf 只接受绝对路径：%s" % raw)
        path = candidate.resolve()
        if not path.is_file():
            abort("错误：--pdf 指向的文件不存在：%s" % path)
        if path.suffix.lower() != ".pdf":
            log("注意：%s 的扩展名不是 .pdf，仍按 PDF 处理" % path.name)
        item = PdfInput(path)
        if item.key in seen:
            log("注意：重复输入的 PDF 已忽略：%s" % path)
            continue
        seen.add(item.key)
        inputs.append(item)
    if not inputs:
        abort("错误：至少需要一个 --pdf 输入")
    inputs.sort(key=lambda item: (item.size_bytes, item.key))
    return inputs


def read_page_count(pdf: Path) -> dict:
    """使用 pypdf 读取页数。必须在启动解析进程之前调用。"""
    import pypdf

    try:
        reader = pypdf.PdfReader(str(pdf))
        return {"pages": len(reader.pages), "error": None}
    except Exception as exc:  # pypdf 对损坏文件会抛出多种异常
        return {"pages": None, "error": "%s: %s" % (type(exc).__name__, exc)}


def extract_native_text(pdf: Path) -> tuple[str, int, int]:
    """抽取 PDF 原生文本层，用作覆盖率代理，不做 OCR。"""
    import pypdf

    reader = pypdf.PdfReader(str(pdf))
    chunks: list[str] = []
    pages_with_text = 0
    for page in reader.pages:
        try:
            text = page.extract_text() or ""
        except Exception:
            text = ""
        if text.strip():
            pages_with_text += 1
        chunks.append(text)
    return "\n".join(chunks), pages_with_text, len(reader.pages)


# ---------------------------------------------------------------- 产物校验

def locate_markdown(declared: Path, work_dir: Path) -> tuple[Path | None, str, int]:
    """定位 Markdown 产物。优先使用 -o 指定路径，找不到时在本次输出目录内做一次有界搜索。"""
    if declared.is_file() and declared.stat().st_size > 0:
        return declared, "declared", 1
    found = [path for path in work_dir.rglob("*.md") if path.is_file() and path.stat().st_size > 0]
    if found:
        best = max(found, key=lambda path: (path.stat().st_size, str(path)))
        return best, "discovered", len(found)
    if declared.is_file():
        return declared, "declared_empty", 0
    return None, "missing", 0


def resolve_image_targets(md_path: Path, targets: list[str]) -> tuple[list[str], list[str], int]:
    """解析本地图片引用。远端地址与锚点只计数，不做本地存在性检查。"""
    resolved: list[str] = []
    missing: list[str] = []
    remote = 0
    for raw in targets:
        target = raw.strip()
        if target.startswith("<") and target.endswith(">"):
            target = target[1:-1].strip()
        lowered = target.lower()
        if lowered.startswith(("http://", "https://", "data:")) or target.startswith("#"):
            remote += 1
            continue
        candidate = Path(urllib.parse.unquote(target))
        full = candidate if candidate.is_absolute() else md_path.parent / candidate
        if full.exists():
            resolved.append(str(full))
        else:
            missing.append(target)
    return resolved, missing, remote


def analyze_markdown(md_path: Path) -> dict:
    """统计 Markdown 产物指标。计数对象是分隔符与链接，不是经过解析的结构数量。"""
    text = md_path.read_text(encoding="utf-8", errors="replace")
    dollar_tokens = DOLLAR_DELIMITER_RE.findall(text)
    image_targets = IMAGE_LINK_RE.findall(text)
    resolved, missing, remote = resolve_image_targets(md_path, image_targets)
    return {
        "md_chars": len(text),
        "md_lines": len(text.splitlines()),
        "md_tables": sum(1 for line in text.splitlines() if TABLE_DELIMITER_RE.match(line)) + len(re.findall(r"<table\b", text, re.I)),
        "formula_delimiters": {
            "dollar_block": sum(1 for token in dollar_tokens if token == "$$"),
            "dollar_inline": sum(1 for token in dollar_tokens if token == "$"),
            "bracket_open": text.count("\\["),
            "bracket_close": text.count("\\]"),
            "paren_open": text.count("\\("),
            "paren_close": text.count("\\)"),
        },
        "image_links": len(image_targets),
        "image_links_remote": remote,
        "image_links_resolved": len(resolved),
        "image_links_missing_total": len(missing),
        "image_links_missing_sample": missing[:10],
        "md_sha256": sha256_of_text(text),
    }


def coverage_units(text: str) -> dict:
    """切成可比较的最小单元：拉丁词与数字、单个 CJK 字符；保留首次出现顺序。"""
    normalized = unicodedata.normalize("NFKC", text).lower()
    units: dict[str, None] = {}
    for match in LATIN_UNIT_RE.finditer(normalized):
        units.setdefault(match.group(0), None)
    for match in CJK_CHAR_RE.finditer(normalized):
        units.setdefault(match.group(0), None)
    chars = {char for char in normalized if not char.isspace()}
    return {"units": units, "chars": chars}


def compute_coverage(pdf_path: Path, md_path: Path | None, page_count: int | None) -> dict:
    """以 pypdf 原生文本为代理计算覆盖率，只用于发现整页或整段缺失。"""
    base = {
        "method": "normalized_unit_coverage",
        "is_coverage_proxy": True,
        "is_ocr_accuracy": False,
        "ground_truth": "none",
        "doc_text_source": "pypdf native text extraction",
        "note": "只表示原生文本在 Markdown 中的覆盖程度，不代表 OCR 或版面准确率，也不是权威真值",
    }
    try:
        doc_text, pages_with_text, pages_total = extract_native_text(pdf_path)
    except Exception as exc:
        base.update({
            "extractable": False,
            "error": "%s: %s" % (type(exc).__name__, exc),
            "char_coverage": None,
            "unit_coverage": None,
        })
        return base
    if not doc_text.strip():
        base.update({
            "extractable": False,
            "pages_total": pages_total,
            "pages_with_text": pages_with_text,
            "char_coverage": None,
            "unit_coverage": None,
            "note": "PDF 没有可用的原生文本层，覆盖率无法计算；此项为空不代表转换失败",
        })
        return base
    doc = coverage_units(doc_text)
    md_text = md_path.read_text(encoding="utf-8", errors="replace") if md_path else ""
    md = coverage_units(md_text)
    missing_units = [unit for unit in doc["units"] if unit not in md["units"]]
    doc_units_total = len(doc["units"])
    doc_chars_total = len(doc["chars"])
    base.update({
        "extractable": True,
        "pages_total": pages_total,
        "pages_with_text": pages_with_text,
        "page_count_from_metadata": page_count,
        "doc_text_chars": len(doc_text),
        "doc_units_total": doc_units_total,
        "md_units_total": len(md["units"]),
        "unit_coverage": round(1 - len(missing_units) / doc_units_total, 4) if doc_units_total else None,
        "char_coverage": round(len(doc["chars"] & md["chars"]) / doc_chars_total, 4) if doc_chars_total else None,
        "pages_with_text_ratio": round(pages_with_text / pages_total, 4) if pages_total else None,
        "missing_units_total": len(missing_units),
        "missing_units_sample": missing_units[:15],
    })
    return base


# ---------------------------------------------------------------- GPU 采样

class GpuSampler(threading.Thread):
    """按固定间隔采样整卡显存与利用率。任何失败只记录，不中断解析。"""

    def __init__(self, interval_seconds: float) -> None:
        super().__init__(name="gpu-sampler", daemon=True)
        self.interval_seconds = interval_seconds
        self.samples: list[dict] = []
        self.errors: list[str] = []
        self.consecutive_failures = 0
        self.stopped_cleanly: bool | None = None
        self._stop_event = threading.Event()

    def _record_error(self, message: str) -> None:
        self.consecutive_failures += 1
        if len(self.errors) < 5:
            self.errors.append(message)

    def sample_once(self) -> None:
        try:
            completed = subprocess.run(
                GPU_QUERY,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                shell=False,
            )
        except OSError as exc:
            self._record_error("nvidia-smi 无法启动：%s" % exc)
            return
        if completed.returncode != 0:
            self._record_error("nvidia-smi 退出码 %s" % completed.returncode)
            return
        memory_total = 0
        utilization_max = 0.0
        device_count = 0
        for line in (completed.stdout or "").splitlines():
            parts = [part.strip() for part in line.split(",")]
            if len(parts) < 2:
                continue
            try:
                memory_total += int(float(parts[0]))
                utilization_max = max(utilization_max, float(parts[1]))
            except ValueError:
                continue
            device_count += 1
        if device_count == 0:
            self._record_error("nvidia-smi 没有返回可用数据行")
            return
        self.consecutive_failures = 0
        self.samples.append({
            "at_seconds": time.monotonic(),
            "gpu_count": device_count,
            "memory_used_mib": memory_total,
            "utilization_pct": utilization_max,
        })

    def run(self) -> None:
        while True:
            self.sample_once()
            if self.consecutive_failures >= 3:
                return
            if self._stop_event.wait(self.interval_seconds):
                return

    def stop(self) -> None:
        """解析进程退出后立即停止采样。"""
        self._stop_event.set()
        self.join(5)
        self.stopped_cleanly = not self.is_alive()

    def summarize(self, started_at: float) -> dict:
        memories = [sample["memory_used_mib"] for sample in self.samples]
        utils = [sample["utilization_pct"] for sample in self.samples]
        return {
            "enabled": True,
            "sampler": " ".join(GPU_QUERY),
            "interval_seconds": self.interval_seconds,
            "sample_count": len(self.samples),
            "samples": [dict(sample, at_seconds=round(sample["at_seconds"] - started_at, 3))
                        for sample in self.samples],
            "memory_used_mib_max": max(memories) if memories else None,
            "memory_used_mib_min": min(memories) if memories else None,
            "utilization_pct_max": max(utils) if utils else None,
            "utilization_pct_mean": round(sum(utils) / len(utils), 2) if utils else None,
            "is_process_specific": False,
            "is_exact_peak": False,
            "note": "整卡累计采样值，含其他进程占用，且受采样间隔限制，不是本进程的精确峰值",
            "errors": self.errors,
            "stopped_cleanly": self.stopped_cleanly,
        }


def disabled_gpu_summary() -> dict:
    return {
        "enabled": False,
        "sampler": " ".join(GPU_QUERY),
        "sample_count": 0,
        "is_process_specific": False,
        "is_exact_peak": False,
        "note": "本次运行使用 --no-gpu-monitor，未采样",
    }


# ---------------------------------------------------------------- 进度与记录

class RunContext:
    def __init__(self, mineru_kit: Path, output_root: Path, signature: dict, pypdf_version: str,
                 gpu_monitor: bool, retry_failed: bool) -> None:
        self.mineru_kit = mineru_kit
        self.output_root = output_root
        self.signature = signature
        self.signature_id = signature_id(signature)
        self.pypdf_version = pypdf_version
        self.gpu_monitor = gpu_monitor
        self.retry_failed = retry_failed
        self.progress_path = output_root / "progress.json"
        self.summary_path = output_root / "summary.json"
        self.probe_path = output_root / "cli_probe.json"


def new_record(pdf: PdfInput, file_id: str) -> dict:
    return {
        "id": file_id,
        "source": str(pdf.path),
        "size_bytes": pdf.size_bytes,
        "sha256": None,
        "page_count": None,
        "status": "pending",
        "last_action": "pending",
        "last_action_reason": None,
        "decision_reason": None,
        "attempt": 0,
        "freshly_timed": False,
        "duration_seconds": None,
    }


def build_progress(ctx: RunContext, records: dict[str, dict], inputs: dict[str, PdfInput]) -> dict:
    """progress.json 是输出根目录的登记表：保留所有已登记文件，避免子集运行丢失历史。"""
    ordered = {file_id: records[file_id] for file_id in sorted(records)}
    completed = [fid for fid, rec in ordered.items() if rec.get("status") == "completed"]
    failed = [fid for fid, rec in ordered.items()
              if rec.get("status") in ("failed", "rejected_oversize", "interrupted")]
    return {
        "schema_version": SCHEMA_VERSION,
        "harness_version": HARNESS_VERSION,
        "updated_at": utc_now(),
        "signature_id": ctx.signature_id,
        "runtime": dict(ctx.signature, output_root=str(ctx.output_root), pypdf_version=ctx.pypdf_version,
                        python_version=sys.version.split()[0], gpu_monitor=ctx.gpu_monitor),
        "in_this_run": sorted(inputs),
        "counts": {"registered": len(ordered), "in_this_run": len(inputs),
                   "completed": len(completed), "failed": len(failed)},
        "completed": completed,
        "failed": failed,
        "files": ordered,
    }


def write_progress(ctx: RunContext, records: dict[str, dict], inputs: dict[str, PdfInput]) -> None:
    atomic_write_json(ctx.progress_path, build_progress(ctx, records, inputs))


def allocate_ids(previous_records: dict[str, dict], inputs: list[PdfInput]) -> dict[str, PdfInput]:
    """已登记的文件沿用原编号，新文件按大小顺序取下一个空号，保证编号跨批次稳定。"""
    known: dict[str, str] = {}
    used: set[int] = set()
    for file_id, record in previous_records.items():
        source = record.get("source") if isinstance(record, dict) else None
        if not isinstance(source, str):
            continue
        known[source.casefold()] = file_id
        digits = re.fullmatch(r"P(\d+)", file_id)
        if digits:
            used.add(int(digits.group(1)))
    mapping: dict[str, PdfInput] = {}
    next_number = 1
    for item in inputs:
        file_id = known.get(item.key)
        if file_id is None:
            while next_number in used:
                next_number += 1
            file_id = "P%02d" % next_number
            used.add(next_number)
        mapping[file_id] = item
    return mapping


# ---------------------------------------------------------------- 执行单个文件

def decide_action(record: dict, current_sha: str, retry_failed: bool) -> tuple[str, str]:
    """决定本次对该文件做什么。只跳过可验证的缓存结果，其余情况重新解析。"""
    status = record.get("status")
    if status == "completed":
        if record.get("sha256") != current_sha:
            return "parse", "source_changed"
        md_path = record.get("md_actual")
        if isinstance(md_path, str) and Path(md_path).is_file() and Path(md_path).stat().st_size > 0:
            if analyze_markdown(Path(md_path))["image_links_missing_total"] == 0:
                return "skip_completed", SKIP_REASON_COMPLETED
            return "parse", "image_asset_missing"
        return "parse", "md_missing_or_empty"
    if status in ("failed", "rejected_oversize"):
        return ("parse", "retry_failed_requested") if retry_failed else ("skip_failed", SKIP_REASON_FAILED)
    if status in ("running", "interrupted"):
        return "parse", "unfinished_previous_run"
    return "parse", "new_input"


def execute_parse(pdf: PdfInput, file_id: str, ctx: RunContext, record: dict, gpu_started: dict,
                  decision_reason: str) -> None:
    """启动一次解析并写入日志与指标。不含超时，不做重试。"""
    work_dir = ctx.output_root / file_id
    work_dir.mkdir(parents=True, exist_ok=True)
    md_path = work_dir / MD_OUTPUT_NAME
    archive_path = work_dir / "package.zip"
    stdout_log = work_dir / "stdout.log"
    stderr_log = work_dir / "stderr.log"
    argv = [
        str(ctx.mineru_kit), PARSE_SUBCOMMAND, str(pdf.path), "-o", str(archive_path),
        "--tier", PARSE_TIER, *PARSE_EXTRA_ARGS,
    ]
    record.update({
        "status": "running",
        "last_action": "parsing",
        "decision_reason": decision_reason,
        "attempt": int(record.get("attempt") or 0) + 1,
        "freshly_timed": False,
        "started_at": utc_now(),
        "argv": argv,
        "cwd": str(work_dir),
        "output_dir": str(work_dir),
        "md_expected": str(md_path),
        "stdout_log": str(stdout_log),
        "stderr_log": str(stderr_log),
    })

    sampler = GpuSampler(GPU_POLL_SECONDS) if ctx.gpu_monitor else None
    process: subprocess.Popen | None = None
    started = time.monotonic()
    gpu_started["value"] = started
    if sampler is not None:
        sampler.start()
    try:
        process = subprocess.Popen(
            argv,
            cwd=str(work_dir),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            shell=False,
            env=build_child_env(),
        )
        stdout_text, stderr_text = process.communicate()
        return_code = process.returncode
    except OSError as exc:
        # 无法启动 CLI（路径不可执行、权限不足等）：记录为失败，继续下一个文件。
        return_code = None
        stdout_text = ""
        stderr_text = "%s: %s" % (type(exc).__name__, exc)
    except BaseException:
        if process is not None and process.poll() is None:
            try:
                process.kill()
                process.communicate()
            except Exception:
                pass
        raise
    finally:
        if sampler is not None:
            sampler.stop()
    duration_seconds = time.monotonic() - started

    stdout_log.write_text(stdout_text or "", encoding="utf-8", errors="replace", newline="\n")
    stderr_log.write_text(stderr_text or "", encoding="utf-8", errors="replace", newline="\n")

    package_error = None
    if return_code == 0:
        try:
            output_assets = work_dir / "artifacts"
            output_assets.mkdir(parents=True, exist_ok=True)
            with zipfile.ZipFile(archive_path) as archive:
                for member in archive.infolist():
                    if not (output_assets / member.filename).resolve().is_relative_to(output_assets.resolve()):
                        raise ValueError("ZIP 文件含有越界路径")
                archive.extractall(output_assets)
        except (OSError, ValueError, zipfile.BadZipFile) as exc:
            package_error = "%s: %s" % (type(exc).__name__, exc)
    found_md, md_source, md_candidates = locate_markdown(md_path, work_dir)
    metrics = analyze_markdown(found_md) if found_md is not None and found_md.stat().st_size > 0 else None
    if return_code == 0 and not package_error and metrics is not None and metrics["md_chars"] > 0 and metrics["image_links_missing_total"] == 0:
        status = "completed"
        error = None
    elif return_code is None:
        status = "failed"
        error = "无法启动 CLI：%s" % stderr_text.strip()
    elif return_code == 0 and package_error:
        status = "failed"
        error = "ZIP 产物校验失败：%s" % package_error
    elif return_code == 0 and metrics is not None and metrics["image_links_missing_total"]:
        status = "failed"
        error = "Markdown 配图缺失，详见 image_links_missing_sample"
    elif return_code == 0:
        status = "failed"
        error = "CLI 退出码为 0，但没有找到非空 Markdown 产物"
    else:
        status = "failed"
        error = "CLI 退出码为 %s，详见 stderr.log" % return_code

    record.update({
        "status": status,
        "last_action": "parsed",
        "last_action_reason": ("cli_exit_%s" % return_code) if return_code is not None else "cli_start_failed",
        "freshly_timed": True,
        "finished_at": utc_now(),
        "duration_seconds": round(duration_seconds, 3),
        "last_measured_duration_seconds": round(duration_seconds, 3),
        "exit_code": return_code,
        "md_actual": str(found_md) if found_md is not None else None,
        "md_path_source": md_source,
        "md_candidates_found": md_candidates,
        "error": error,
        "zip_path": str(archive_path),
    })
    if metrics is not None:
        record.update(metrics)
    page_count = int(record.get("page_count") or 0)
    if page_count:
        record["seconds_per_page"] = round(duration_seconds / page_count, 3)
        record["pages_per_minute"] = round(page_count / (duration_seconds / 60), 3) if duration_seconds > 0 else None
    else:
        record["seconds_per_page"] = None
        record["pages_per_minute"] = None
    record["coverage_proxy"] = compute_coverage(pdf.path, found_md, record.get("page_count"))
    record["gpu"] = sampler.summarize(gpu_started["value"]) if sampler is not None else disabled_gpu_summary()
    record["artifacts"] = {
        "output_dir": str(work_dir),
        "markdown": str(found_md) if found_md is not None else None,
        "stdout_log": str(stdout_log),
        "stderr_log": str(stderr_log),
    }


def process_one(pdf: PdfInput, file_id: str, ctx: RunContext, record: dict, gpu_started: dict) -> dict:
    """单个文件的完整责任：预检、缓存判断、解析、产物校验和指标记录。"""
    current_sha = sha256_of_file(pdf.path)
    action, reason = decide_action(record, current_sha, ctx.retry_failed)
    record["sha256"] = current_sha
    record["size_bytes"] = pdf.size_bytes

    if action == "skip_completed":
        record.update({"last_action": "skipped_completed", "last_action_reason": reason,
                       "decision_reason": reason,
                       "freshly_timed": False, "skipped_at": utc_now()})
        return record
    if action == "skip_failed":
        record.update({"last_action": "skipped_failed", "last_action_reason": reason,
                       "decision_reason": reason,
                       "freshly_timed": False, "skipped_at": utc_now()})
        return record

    if pdf.size_bytes > MAX_PDF_BYTES:
        record.update({
            "status": "rejected_oversize",
            "last_action": "rejected",
            "last_action_reason": "oversize_%d_bytes_over_limit_%d" % (pdf.size_bytes, MAX_PDF_BYTES),
            "decision_reason": reason,
            "freshly_timed": False,
            "error": "文件超过 %d 字节上限；脚本不做拆分，需人工处理" % MAX_PDF_BYTES,
        })
        return record

    page_info = read_page_count(pdf.path)
    record["page_count"] = page_info["pages"]
    record["page_count_error"] = page_info["error"]
    record["page_count_source"] = "pypdf %s" % ctx.pypdf_version
    if not page_info["pages"]:
        record.update({
            "status": "failed",
            "last_action": "failed_before_parse",
            "last_action_reason": "page_count_unreadable",
            "decision_reason": reason,
            "freshly_timed": False,
            "error": "无法读取页数或页数为 0，未启动解析",
        })
        return record

    execute_parse(pdf, file_id, ctx, record, gpu_started, reason)
    return record


# ---------------------------------------------------------------- CLI 探测与汇总

def probe_cli(mineru_kit: Path, ctx: RunContext) -> dict:
    """记录本机 CLI 的帮助文本，用于核对子命令与参数；探测失败不影响批量。"""
    probes: dict[str, dict] = {}
    for name, argv in (("top_level_help", [str(mineru_kit), "--help"]),
                       ("parse_help", [str(mineru_kit), PARSE_SUBCOMMAND, "--help"])):
        entry = {"argv": argv, "returncode": None, "stdout_head": "", "stderr_head": "", "error": None}
        try:
            completed = subprocess.run(
                argv, capture_output=True, text=True, encoding="utf-8", errors="replace",
                shell=False, env=build_child_env(), cwd=str(ctx.output_root),
            )
            entry["returncode"] = completed.returncode
            entry["stdout_head"] = (completed.stdout or "")[:4000]
            entry["stderr_head"] = (completed.stderr or "")[:2000]
        except OSError as exc:
            entry["error"] = "%s: %s" % (type(exc).__name__, exc)
        probes[name] = entry
    probes["checked_at"] = utc_now()
    probes["note"] = "只记录帮助输出，用于核对 parse 子命令与 --tier 参数；探测失败不阻止批量"
    atomic_write_json(ctx.probe_path, probes)
    if probes["parse_help"]["returncode"] != 0:
        log("注意：%s parse --help 退出码为 %s，请先查看 cli_probe.json 核对参数"
            % (mineru_kit.name, probes["parse_help"]["returncode"]))
    return probes


def build_summary(ctx: RunContext, records: dict[str, dict], inputs: dict[str, PdfInput],
                  run_started_at: str, run_seconds: float) -> dict:
    """summary.json 描述本次运行；files 只含本次传入的文件，registry 给出登记表概览。"""
    ordered = [records[file_id] for file_id in sorted(inputs)]
    registry = [records[file_id] for file_id in sorted(records)]
    timed = [rec for rec in ordered if rec.get("freshly_timed")]
    timed_completed = [rec for rec in timed if rec.get("status") == "completed"]
    timed_failed = [rec for rec in timed if rec.get("status") != "completed"]
    total_pages = sum(int(rec.get("page_count") or 0) for rec in ordered)
    measured_seconds = round(sum(float(rec.get("duration_seconds") or 0) for rec in timed_completed), 3)
    measured_pages = sum(int(rec.get("page_count") or 0) for rec in timed_completed)
    coverage_values = [rec["coverage_proxy"].get("unit_coverage") for rec in timed_completed
                       if isinstance(rec.get("coverage_proxy"), dict)
                       and rec["coverage_proxy"].get("unit_coverage") is not None]
    return {
        "schema_version": SCHEMA_VERSION,
        "harness_version": HARNESS_VERSION,
        "generated_at": utc_now(),
        "run_started_at": run_started_at,
        "run_wall_seconds": round(run_seconds, 3),
        "signature_id": ctx.signature_id,
        "runtime": dict(ctx.signature, output_root=str(ctx.output_root), pypdf_version=ctx.pypdf_version,
                        python_version=sys.version.split()[0], gpu_monitor=ctx.gpu_monitor,
                        retry_failed=ctx.retry_failed),
        "batch": {
            "file_count": len(inputs),
            "total_bytes": sum(item.size_bytes for item in inputs.values()),
            "total_pages": total_pages,
            "sort": "size_ascending",
            "ids": sorted(inputs),
        },
        "registry_overview": {
            rec["id"]: {
                "status": rec.get("status"),
                "source": rec.get("source"),
                "size_bytes": rec.get("size_bytes"),
                "page_count": rec.get("page_count"),
                "unit_coverage": (rec.get("coverage_proxy") or {}).get("unit_coverage"),
            } for rec in registry
        },
        "results": {
            "completed": [rec["id"] for rec in ordered if rec.get("status") == "completed"],
            "failed": [rec["id"] for rec in ordered if rec.get("status") in ("failed", "rejected_oversize")],
            "skipped_completed": [rec["id"] for rec in ordered if rec.get("last_action") == "skipped_completed"],
            "skipped_failed": [rec["id"] for rec in ordered if rec.get("last_action") == "skipped_failed"],
            "freshly_timed": [rec["id"] for rec in timed],
            "timed_failed": [rec["id"] for rec in timed_failed],
        },
        "timing": {
            "measured_duration_seconds_total": measured_seconds,
            "measured_files": [rec["id"] for rec in timed_completed],
            "measured_pages": measured_pages,
            "seconds_per_page_overall": round(measured_seconds / measured_pages, 3) if measured_pages else None,
            "note": "只统计本次成功完成的文件；耗时含 CLI 启动与模型加载，不是纯推理时间",
        },
        "coverage_proxy_summary": {
            "files_with_coverage": len(coverage_values),
            "unit_coverage_min": min(coverage_values) if coverage_values else None,
            "unit_coverage_mean": round(sum(coverage_values) / len(coverage_values), 4) if coverage_values else None,
            "note": "以 pypdf 原生文本为代理，只用于发现整页或整段缺失，不代表 OCR 或版面准确率",
        },
        "cost": {"cloud_calls": 0, "note": "本机 VLM 推理，无云端调用，未产生按次费用"},
        "paths": {
            "output_root": str(ctx.output_root),
            "progress": str(ctx.progress_path),
            "summary": str(ctx.summary_path),
            "cli_probe": str(ctx.probe_path),
            "per_file": {
                rec["id"]: {
                    "output_dir": rec.get("output_dir"),
                    "markdown": rec.get("md_actual"),
                    "stdout_log": rec.get("stdout_log"),
                    "stderr_log": rec.get("stderr_log"),
                } for rec in ordered
            },
        },
        "files": ordered,
    }


# ---------------------------------------------------------------- 主流程

def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="MinerU CLI 顺序批量测试脚本：本机 VLM 推理，可断点续跑，记录可复算指标。",
    )
    parser.add_argument("--mineru-kit", required=True, help="官方 MinerU CLI 可执行文件的绝对路径")
    parser.add_argument("--output-root", required=True, help="输出根目录的绝对路径，进度与产物写入此处")
    parser.add_argument("--pdf", required=True, action="append", default=[], help="输入 PDF 的绝对路径，可重复传入")
    parser.add_argument("--retry-failed", action="store_true", help="显式重跑此前失败的文件；不加此参数时跳过")
    parser.add_argument("--no-gpu-monitor", action="store_true", help="关闭 nvidia-smi 采样")
    return parser.parse_args(argv)


def report_batch(summary: dict) -> None:
    results = summary["results"]
    timing = summary["timing"]
    log("batch finished")
    log("  output_root       : %s" % summary["paths"]["output_root"])
    log("  signature_id      : %s" % summary["signature_id"])
    log("  completed         : %s" % (", ".join(results["completed"]) or "-"))
    log("  failed            : %s" % (", ".join(results["failed"]) or "-"))
    log("  skipped_completed : %s" % (", ".join(results["skipped_completed"]) or "-"))
    log("  skipped_failed    : %s" % (", ".join(results["skipped_failed"]) or "-"))
    log("  freshly timed     : %s (%s s)" % (", ".join(timing["measured_files"]) or "-",
                                              timing["measured_duration_seconds_total"]))
    log("  summary_json      : %s" % summary["paths"]["summary"])


def run_batch(args: argparse.Namespace) -> int:
    mineru_kit_raw = Path(args.mineru_kit)
    if not mineru_kit_raw.is_absolute():
        abort("错误：--mineru-kit 需要绝对路径：%s" % args.mineru_kit)
    mineru_kit = mineru_kit_raw.resolve()
    if not mineru_kit.is_file():
        abort("错误：找不到 MinerU CLI 可执行文件：%s" % mineru_kit)
    output_root_raw = Path(args.output_root)
    if not output_root_raw.is_absolute():
        abort("错误：--output-root 需要绝对路径：%s" % args.output_root)
    output_root = output_root_raw.resolve()
    output_root.mkdir(parents=True, exist_ok=True)

    pypdf_version = require_pypdf()
    signature = runtime_signature(mineru_kit)
    ctx = RunContext(mineru_kit, output_root, signature, pypdf_version,
                     gpu_monitor=not args.no_gpu_monitor, retry_failed=args.retry_failed)
    inputs_list = collect_pdf_inputs(args.pdf)

    previous = load_json(ctx.progress_path)
    if previous.get("signature_id") and previous.get("signature_id") != ctx.signature_id:
        log("运行参数或解析器身份与既有进度不一致，为避免混用不同条件的产物，已停止")
        log("既有签名：%s" % previous.get("signature_id"))
        log("当前签名：%s" % ctx.signature_id)
        log("处理方式：改用新的 --output-root，或人工确认后再决定是否复用旧目录")
        return 2

    previous_records = previous.get("files") if isinstance(previous.get("files"), dict) else {}
    inputs = allocate_ids(previous_records, inputs_list)
    records = {file_id: dict(record) for file_id, record in previous_records.items()
               if isinstance(record, dict)}
    for file_id, item in inputs.items():
        records[file_id] = dict(previous_records.get(file_id) or new_record(item, file_id))
    write_progress(ctx, records, inputs)

    run_started_at = utc_now()
    run_started = time.monotonic()
    probe_cli(mineru_kit, ctx)
    gpu_started = {"value": run_started}
    for file_id in sorted(inputs):
        item = inputs[file_id]
        write_progress(ctx, records, inputs)
        try:
            process_one(item, file_id, ctx, records[file_id], gpu_started)
        except KeyboardInterrupt:
            records[file_id].update({"status": "interrupted", "last_action": "interrupted",
                                     "last_action_reason": "keyboard_interrupt", "freshly_timed": False})
            write_progress(ctx, records, inputs)
            raise
        write_progress(ctx, records, inputs)
        record = records[file_id]
        log("%s %-18s %-24s duration=%s" % (
            file_id, record.get("status"), record.get("last_action"),
            record.get("duration_seconds") if record.get("freshly_timed") else "n/a",
        ))

    run_seconds = time.monotonic() - run_started
    summary = build_summary(ctx, records, inputs, run_started_at, run_seconds)
    atomic_write_json(ctx.summary_path, summary)
    report_batch(summary)
    return 1 if summary["results"]["failed"] else 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        return run_batch(args)
    except KeyboardInterrupt:
        log("收到中断，当前进度已写入 progress.json；请用同一条命令续跑")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
