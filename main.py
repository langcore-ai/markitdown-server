from __future__ import annotations

import asyncio
import logging
import os
import shutil
import subprocess
import tempfile
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Literal, Mapping, MutableMapping, cast

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from markitdown import MarkItDown
from openai import OpenAI

DEFAULT_OPENAI_BASE_URL = "https://api.openai.com/v1"
DEFAULT_EXIFTOOL_PATH = "/usr/bin/exiftool"
DEFAULT_SOFFICE_PATH = "/usr/bin/soffice"
DEFAULT_MAX_CONCURRENT_JOBS = 4
DEFAULT_CONVERT_TIMEOUT_SEC = 180
DEFAULT_MAX_UPLOAD_SIZE_MB = 100
DEFAULT_THREADPOOL_WORKERS = 4
DEFAULT_UVICORN_WORKERS = 2
DEFAULT_SUBPROCESS_TIMEOUT_SEC = 60
UPLOAD_CHUNK_SIZE = 1024 * 1024
MULTIMODAL_FALLBACK_REASON = "llm_enhanced_conversion_failed"

TEXT_EXTENSIONS = {
    ".csv",
    ".htm",
    ".html",
    ".ini",
    ".js",
    ".json",
    ".log",
    ".markdown",
    ".md",
    ".py",
    ".sh",
    ".text",
    ".toml",
    ".ts",
    ".tsx",
    ".txt",
    ".xml",
    ".yaml",
    ".yml",
}
IMAGE_EXTENSIONS = {
    ".bmp",
    ".gif",
    ".heic",
    ".heif",
    ".jpeg",
    ".jpg",
    ".png",
    ".svg",
    ".tif",
    ".tiff",
    ".webp",
}
TEXT_MIME_TYPES = {
    "application/json",
    "application/ld+json",
    "application/xml",
    "application/x-yaml",
    "text/csv",
    "text/html",
    "text/markdown",
    "text/plain",
    "text/x-markdown",
    "text/xml",
}

DetectedInputType = Literal[
    "doc",
    "docx",
    "ppt",
    "pptx",
    "xls",
    "xlsx",
    "pdf",
    "image",
    "text",
]
EffectiveInputType = Literal["docx", "pptx", "xls", "xlsx", "pdf", "image", "text"]

PREPROCESS_TARGETS: dict[Literal["doc", "ppt"], EffectiveInputType] = {
    "doc": "docx",
    "ppt": "pptx",
}

logger = logging.getLogger("markitdown_server")
logging.basicConfig(
    level=os.getenv("MARKITDOWN_LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)


def read_int_env(name: str, default: int, minimum: int = 1) -> int:
    """读取整数环境变量，非法值回退为默认值。"""

    raw_value = os.getenv(name)
    if raw_value is None or not raw_value.strip():
        return default
    try:
        value = int(raw_value)
    except ValueError:
        logger.warning("invalid integer env, fallback to default", extra={"env": name})
        return default
    return max(minimum, value)


MAX_CONCURRENT_JOBS = read_int_env(
    "MARKITDOWN_MAX_CONCURRENT_JOBS",
    DEFAULT_MAX_CONCURRENT_JOBS,
)
CONVERT_TIMEOUT_SEC = read_int_env(
    "MARKITDOWN_CONVERT_TIMEOUT_SEC",
    DEFAULT_CONVERT_TIMEOUT_SEC,
)
MAX_UPLOAD_SIZE_MB = read_int_env(
    "MARKITDOWN_MAX_UPLOAD_SIZE_MB",
    DEFAULT_MAX_UPLOAD_SIZE_MB,
)
MAX_UPLOAD_SIZE_BYTES = MAX_UPLOAD_SIZE_MB * 1024 * 1024
THREADPOOL_WORKERS = read_int_env(
    "MARKITDOWN_THREADPOOL_WORKERS",
    DEFAULT_THREADPOOL_WORKERS,
)
SUBPROCESS_TIMEOUT_SEC = read_int_env(
    "MARKITDOWN_SUBPROCESS_TIMEOUT_SEC",
    DEFAULT_SUBPROCESS_TIMEOUT_SEC,
)
SOFFICE_PATH = os.getenv("MARKITDOWN_SOFFICE_PATH", DEFAULT_SOFFICE_PATH).strip() or DEFAULT_SOFFICE_PATH


class RequestLoggerAdapter(logging.LoggerAdapter):
    """为单次请求注入 request_id，方便串联日志。"""

    def process(
        self, msg: str, kwargs: MutableMapping[str, Any]
    ) -> tuple[str, MutableMapping[str, Any]]:
        extra_context: Mapping[str, Any] = (
            self.extra if isinstance(self.extra, Mapping) else {}
        )
        request_id = str(extra_context.get("request_id", "unknown"))
        extra = kwargs.setdefault("extra", {})
        extra.setdefault("request_id", request_id)
        return f"[request_id={request_id}] {msg}", kwargs


class BusyError(Exception):
    """标记服务内部并发槽位耗尽。"""


@dataclass(slots=True)
class ConvertOptions:
    """封装 `/convert` 表单透传的转换参数。"""

    enable_plugins: bool
    keep_data_uris: bool
    llm_model: str | None
    llm_prompt: str | None
    openai_api_key: str | None
    openai_base_url: str | None
    exiftool_path: str | None
    style_map: str | None
    docintel_endpoint: str | None
    docintel_api_version: str | None


@dataclass(slots=True)
class StoredUpload:
    """描述已落盘的上传文件。"""

    filename: str
    content_type: str | None
    temp_dir: Path
    temp_file_path: Path
    size_bytes: int


def normalize_content_type(content_type: str | None) -> str:
    """规范化 Content-Type，仅保留主值。"""

    raw = (content_type or "").strip().lower()
    if not raw:
        return ""
    return raw.split(";", 1)[0].strip()


def compact_message(value: str, fallback: str, limit: int = 400) -> str:
    """压缩外部工具输出，避免把长日志直接回传给调用方。"""

    compacted = " ".join(value.split())
    if not compacted:
        return fallback
    if len(compacted) <= limit:
        return compacted
    return f"{compacted[:limit]}..."


def resolve_binary_status(configured_path: str) -> dict[str, Any]:
    """解析依赖命令路径，并返回当前可用性。"""

    normalized = configured_path.strip()
    if not normalized:
        return {
            "configured_path": configured_path,
            "resolved_path": None,
            "available": False,
        }

    if "/" in normalized:
        resolved_path = str(Path(normalized))
        available = Path(resolved_path).is_file() and os.access(resolved_path, os.X_OK)
        return {
            "configured_path": configured_path,
            "resolved_path": resolved_path,
            "available": available,
        }

    resolved_path = shutil.which(normalized)
    return {
        "configured_path": configured_path,
        "resolved_path": resolved_path,
        "available": resolved_path is not None,
    }


def get_dependency_status(exiftool_path: str | None = None) -> dict[str, dict[str, Any]]:
    """返回服务关键依赖的实时状态。"""

    normalized_exiftool_path = exiftool_path or DEFAULT_EXIFTOOL_PATH
    return {
        "soffice": resolve_binary_status(SOFFICE_PATH),
        "exiftool": resolve_binary_status(normalized_exiftool_path),
    }


def build_error_detail(
    *,
    code: str,
    message: str,
    detected_type: str | None,
    stage: str,
    retryable: bool,
    attempted_stages: list[str],
    **extras: Any,
) -> dict[str, Any]:
    """构建统一的结构化错误详情。"""

    detail: dict[str, Any] = {
        "ok": False,
        "code": code,
        "message": message,
        "detected_type": detected_type,
        "stage": stage,
        "retryable": retryable,
        "attempted_stages": attempted_stages,
    }
    for key, value in extras.items():
        if value is not None:
            detail[key] = value
    return detail


def raise_structured_http_error(
    *,
    status_code: int,
    code: str,
    message: str,
    detected_type: str | None,
    stage: str,
    retryable: bool,
    attempted_stages: list[str],
    **extras: Any,
) -> None:
    """抛出统一结构的 HTTPException。"""

    raise HTTPException(
        status_code=status_code,
        detail=build_error_detail(
            code=code,
            message=message,
            detected_type=detected_type,
            stage=stage,
            retryable=retryable,
            attempted_stages=attempted_stages,
            **extras,
        ),
    )


def detect_type_from_extension(filename: str) -> DetectedInputType | None:
    """基于扩展名识别输入类型。"""

    extension = Path(filename.strip().lower()).suffix
    if extension == ".doc":
        return "doc"
    if extension == ".docx":
        return "docx"
    if extension == ".ppt":
        return "ppt"
    if extension == ".pptx":
        return "pptx"
    if extension == ".xls":
        return "xls"
    if extension == ".xlsx":
        return "xlsx"
    if extension == ".pdf":
        return "pdf"
    if extension in IMAGE_EXTENSIONS:
        return "image"
    if extension in TEXT_EXTENSIONS:
        return "text"
    return None


def detect_type_from_content_type(content_type: str | None) -> DetectedInputType | None:
    """基于 Content-Type 识别输入类型。"""

    normalized = normalize_content_type(content_type)
    if not normalized:
        return None
    if normalized == "application/msword":
        return "doc"
    if normalized == "application/vnd.openxmlformats-officedocument.wordprocessingml.document":
        return "docx"
    if normalized == "application/vnd.ms-powerpoint":
        return "ppt"
    if normalized == "application/vnd.openxmlformats-officedocument.presentationml.presentation":
        return "pptx"
    if normalized == "application/vnd.ms-excel":
        return "xls"
    if normalized == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet":
        return "xlsx"
    if normalized == "application/pdf":
        return "pdf"
    if normalized.startswith("image/"):
        return "image"
    if normalized.startswith("text/") or normalized in TEXT_MIME_TYPES:
        return "text"
    return None


def detect_input_type(filename: str, content_type: str | None) -> DetectedInputType:
    """统一识别输入类型；扩展名优先，明显冲突则直接失败。"""

    extension_type = detect_type_from_extension(filename)
    content_type_value = normalize_content_type(content_type)
    mime_type = detect_type_from_content_type(content_type_value)
    attempted_stages = ["detect_input_type"]

    if extension_type and mime_type and extension_type != mime_type:
        raise_structured_http_error(
            status_code=415,
            code="FILE_TYPE_CONFLICT",
            message=(
                f"Filename extension and content type conflict: {filename} "
                f"vs {content_type_value or 'unknown content type'}"
            ),
            detected_type=extension_type,
            stage="detect_input_type",
            retryable=False,
            attempted_stages=attempted_stages,
            filename=filename,
            content_type=content_type_value or None,
        )

    if extension_type:
        return extension_type
    if mime_type:
        return mime_type

    raise_structured_http_error(
        status_code=415,
        code="UNSUPPORTED_FILE_TYPE",
        message="Unsupported file type for markitdown conversion.",
        detected_type=None,
        stage="detect_input_type",
        retryable=False,
        attempted_stages=attempted_stages,
        filename=filename,
        content_type=content_type_value or None,
    )


def append_pipeline_stage(
    pipeline: list[dict[str, Any]],
    stage: str,
    started_at: float,
    **extras: Any,
) -> None:
    """记录阶段耗时，供成功响应返回诊断信息。"""

    pipeline.append(
        {
            "stage": stage,
            "duration_ms": int((time.perf_counter() - started_at) * 1000),
            **{key: value for key, value in extras.items() if value is not None},
        }
    )


def build_converter_kwargs(options: ConvertOptions) -> dict[str, Any]:
    """将 HTTP 层参数转换为 MarkItDown 初始化参数。"""

    converter_kwargs: dict[str, Any] = {
        "enable_plugins": options.enable_plugins,
        "exiftool_path": options.exiftool_path,
        "style_map": options.style_map,
        "docintel_endpoint": options.docintel_endpoint,
        "docintel_api_version": options.docintel_api_version,
    }
    converter_kwargs = {
        key: value for key, value in converter_kwargs.items() if value is not None
    }

    if options.llm_model:
        client_kwargs: dict[str, Any] = {}
        if options.openai_api_key:
            client_kwargs["api_key"] = options.openai_api_key
        resolved_base_url = (
            options.openai_base_url or os.getenv("OPENAI_BASE_URL") or DEFAULT_OPENAI_BASE_URL
        )
        client_kwargs["base_url"] = resolved_base_url
        converter_kwargs["llm_client"] = OpenAI(**client_kwargs)
        converter_kwargs["llm_model"] = options.llm_model
        if options.llm_prompt:
            converter_kwargs["llm_prompt"] = options.llm_prompt

    return converter_kwargs


def disable_multimodal_options(options: ConvertOptions) -> ConvertOptions:
    """复制一份关闭多模态描述的参数，用于增强链路失败后的降级重试。"""

    return replace(
        options,
        llm_model=None,
        llm_prompt=None,
    )


def should_retry_without_multimodal(options: ConvertOptions) -> bool:
    """判断当前请求是否启用了多模态增强，从而允许降级重试。"""

    return bool(options.llm_model)


def pick_preprocessed_output(
    output_dir: Path,
    source_file_path: Path,
    target_type: EffectiveInputType,
) -> Path | None:
    """从 LibreOffice 输出目录中选择最匹配的转换结果。"""

    candidates = sorted(output_dir.glob(f"*.{target_type}"))
    if not candidates:
        return None

    source_stem = source_file_path.stem.lower()
    for candidate in candidates:
        if candidate.stem.lower() == source_stem:
            return candidate
    return candidates[0]


def preprocess_legacy_office_file(
    *,
    file_path: Path,
    source_type: Literal["doc", "ppt"],
    working_dir: Path,
    attempted_stages: list[str],
) -> tuple[Path, EffectiveInputType]:
    """使用 LibreOffice 将旧版 Office 文件转换为 OOXML。"""

    dependency_status = get_dependency_status()
    soffice_status = dependency_status["soffice"]
    if not soffice_status["available"]:
        raise_structured_http_error(
            status_code=500,
            code="DEPENDENCY_MISSING",
            message=f"LibreOffice executable is not available: {SOFFICE_PATH}",
            detected_type=source_type,
            stage="preprocess_office",
            retryable=False,
            attempted_stages=attempted_stages,
            dependency="soffice",
        )

    target_type = PREPROCESS_TARGETS[source_type]
    output_dir = working_dir / "preprocessed"
    profile_dir = working_dir / "soffice-profile"
    output_dir.mkdir(parents=True, exist_ok=True)
    profile_dir.mkdir(parents=True, exist_ok=True)

    command = [
        str(soffice_status["resolved_path"] or SOFFICE_PATH),
        "--headless",
        "--nologo",
        "--nofirststartwizard",
        "--nodefault",
        "--norestore",
        f"-env:UserInstallation={profile_dir.resolve().as_uri()}",
        "--convert-to",
        target_type,
        "--outdir",
        str(output_dir),
        str(file_path),
    ]

    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=False,
            timeout=SUBPROCESS_TIMEOUT_SEC,
        )
    except subprocess.TimeoutExpired:
        raise_structured_http_error(
            status_code=408,
            code="PREPROCESS_TIMEOUT",
            message=f"LibreOffice preprocess timed out after {SUBPROCESS_TIMEOUT_SEC} seconds.",
            detected_type=source_type,
            stage="preprocess_office",
            retryable=True,
            attempted_stages=attempted_stages,
            target_type=target_type,
        )

    if completed.returncode != 0:
        process_message = compact_message(
            completed.stderr or completed.stdout,
            f"LibreOffice failed to convert {source_type} to {target_type}.",
        )
        raise_structured_http_error(
            status_code=422,
            code="PREPROCESS_FAILED",
            message=process_message,
            detected_type=source_type,
            stage="preprocess_office",
            retryable=False,
            attempted_stages=attempted_stages,
            target_type=target_type,
        )

    converted_path = pick_preprocessed_output(output_dir, file_path, target_type)
    if converted_path is None:
        raise_structured_http_error(
            status_code=422,
            code="PREPROCESS_FAILED",
            message=f"LibreOffice did not produce a .{target_type} output file.",
            detected_type=source_type,
            stage="preprocess_office",
            retryable=False,
            attempted_stages=attempted_stages,
            target_type=target_type,
        )

    return converted_path, target_type


def run_markitdown_convert(
    *,
    file_path: Path,
    effective_type: EffectiveInputType,
    options: ConvertOptions,
    attempted_stages: list[str],
    request_logger: RequestLoggerAdapter | None = None,
) -> dict[str, Any]:
    """执行最终的 MarkItDown 转换。"""

    try:
        converter = MarkItDown(**build_converter_kwargs(options))
        result = converter.convert(file_path, keep_data_uris=options.keep_data_uris)
    except Exception as exc:
        if should_retry_without_multimodal(options):
            original_error = compact_message(
                str(exc),
                f"MarkItDown failed to convert {effective_type} with multimodal enabled.",
            )
            if request_logger is not None:
                request_logger.warning(
                    "llm-enhanced conversion failed, retrying without multimodal",
                    extra={
                        "effective_type": effective_type,
                        "source_file": str(file_path),
                        "original_error": original_error,
                    },
                )

            retry_options = disable_multimodal_options(options)
            try:
                converter = MarkItDown(**build_converter_kwargs(retry_options))
                result = converter.convert(
                    file_path,
                    keep_data_uris=retry_options.keep_data_uris,
                )
            except Exception as retry_exc:
                raise_structured_http_error(
                    status_code=422,
                    code="MARKITDOWN_CONVERT_FAILED",
                    message=compact_message(
                        str(retry_exc),
                        (
                            f"MarkItDown failed to convert {effective_type} "
                            "after retrying without multimodal enhancement."
                        ),
                    ),
                    detected_type=effective_type,
                    stage="convert_markitdown",
                    retryable=False,
                    attempted_stages=attempted_stages,
                    multimodal_fallback_attempted=True,
                    multimodal_fallback_reason=MULTIMODAL_FALLBACK_REASON,
                    original_error=original_error,
                )

            return {
                "title": result.title,
                "markdown": result.markdown,
                "text_content": result.text_content,
                "multimodal_fallback_applied": True,
                "multimodal_fallback_reason": MULTIMODAL_FALLBACK_REASON,
            }

        raise_structured_http_error(
            status_code=422,
            code="MARKITDOWN_CONVERT_FAILED",
            message=compact_message(
                str(exc),
                f"MarkItDown failed to convert {effective_type}.",
            ),
            detected_type=effective_type,
            stage="convert_markitdown",
            retryable=False,
            attempted_stages=attempted_stages,
        )

    return {
        "title": result.title,
        "markdown": result.markdown,
        "text_content": result.text_content,
        "multimodal_fallback_applied": False,
        "multimodal_fallback_reason": None,
    }


def run_conversion_pipeline(
    stored_upload: StoredUpload,
    options: ConvertOptions,
    request_logger: RequestLoggerAdapter | None = None,
) -> dict[str, Any]:
    """执行规范化转换管线，并返回统一成功响应所需字段。"""

    overall_started_at = time.perf_counter()
    pipeline: list[dict[str, Any]] = []
    attempted_stages: list[str] = []

    detect_started_at = time.perf_counter()
    attempted_stages.append("detect_input_type")
    detected_input_type = detect_input_type(
        stored_upload.filename,
        stored_upload.content_type,
    )
    append_pipeline_stage(
        pipeline,
        "detect_input_type",
        detect_started_at,
        detected_type=detected_input_type,
    )

    current_file_path = stored_upload.temp_file_path
    effective_type: EffectiveInputType = cast(EffectiveInputType, detected_input_type)
    preprocessed_from: DetectedInputType | None = None

    if detected_input_type in PREPROCESS_TARGETS:
        preprocess_started_at = time.perf_counter()
        attempted_stages.append("preprocess_office")
        current_file_path, effective_type = preprocess_legacy_office_file(
            file_path=current_file_path,
            source_type=cast(Literal["doc", "ppt"], detected_input_type),
            working_dir=stored_upload.temp_dir,
            attempted_stages=attempted_stages,
        )
        preprocessed_from = detected_input_type
        append_pipeline_stage(
            pipeline,
            "preprocess_office",
            preprocess_started_at,
            source_type=detected_input_type,
            target_type=effective_type,
        )

    convert_started_at = time.perf_counter()
    attempted_stages.append("convert_markitdown")
    conversion_result = run_markitdown_convert(
        file_path=current_file_path,
        effective_type=effective_type,
        options=options,
        attempted_stages=attempted_stages,
        request_logger=request_logger,
    )
    append_pipeline_stage(
        pipeline,
        "convert_markitdown",
        convert_started_at,
        detected_type=effective_type,
        multimodal_fallback_applied=conversion_result["multimodal_fallback_applied"],
        multimodal_fallback_reason=conversion_result["multimodal_fallback_reason"],
    )

    return {
        "title": conversion_result["title"],
        "markdown": conversion_result["markdown"],
        "text_content": conversion_result["text_content"],
        "detected_type": effective_type,
        "preprocessed_from": preprocessed_from,
        "pipeline": pipeline,
        "duration_ms": int((time.perf_counter() - overall_started_at) * 1000),
    }


async def store_upload_file(
    file: UploadFile,
    request_logger: RequestLoggerAdapter,
) -> StoredUpload:
    """将上传文件写入请求级临时目录。"""

    if not file.filename:
        raise_structured_http_error(
            status_code=400,
            code="MISSING_FILENAME",
            message="Missing uploaded filename.",
            detected_type=None,
            stage="store_upload",
            retryable=False,
            attempted_stages=["store_upload"],
        )

    temp_dir = Path(tempfile.gettempdir()) / f"markitdown-{uuid.uuid4().hex}"
    temp_dir.mkdir(parents=True, exist_ok=False)
    temp_file_path = temp_dir / Path(file.filename).name
    size_bytes = 0

    try:
        with temp_file_path.open("wb") as output:
            while True:
                chunk = await file.read(UPLOAD_CHUNK_SIZE)
                if not chunk:
                    break
                size_bytes += len(chunk)
                if size_bytes > MAX_UPLOAD_SIZE_BYTES:
                    raise_structured_http_error(
                        status_code=413,
                        code="FILE_TOO_LARGE",
                        message=f"Uploaded file exceeds the {MAX_UPLOAD_SIZE_MB} MB limit.",
                        detected_type=None,
                        stage="store_upload",
                        retryable=False,
                        attempted_stages=["store_upload"],
                    )
                output.write(chunk)
    except Exception:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise
    finally:
        await file.close()

    request_logger.info(
        "upload stored",
        extra={
            "upload_filename": file.filename,
            "content_type": normalize_content_type(file.content_type),
            "size_bytes": size_bytes,
        },
    )
    return StoredUpload(
        filename=file.filename,
        content_type=file.content_type,
        temp_dir=temp_dir,
        temp_file_path=temp_file_path,
        size_bytes=size_bytes,
    )


async def run_conversion_with_limit(
    stored_upload: StoredUpload,
    options: ConvertOptions,
    request_logger: RequestLoggerAdapter,
) -> dict[str, Any]:
    """在并发限制内执行完整转换流程。"""

    semaphore: asyncio.Semaphore = app.state.convert_semaphore
    thread_pool: ThreadPoolExecutor = app.state.thread_pool

    if semaphore.locked():
        request_logger.warning("conversion rejected because server is busy")
        raise BusyError

    async with semaphore:
        loop = asyncio.get_running_loop()
        started_at = time.perf_counter()
        try:
            result = await asyncio.wait_for(
                loop.run_in_executor(
                    thread_pool,
                    run_conversion_pipeline,
                    stored_upload,
                    options,
                    request_logger,
                ),
                timeout=CONVERT_TIMEOUT_SEC,
            )
        except TimeoutError as exc:
            request_logger.warning(
                "conversion timed out",
                extra={
                    "upload_filename": stored_upload.filename,
                    "size_bytes": stored_upload.size_bytes,
                    "timeout_sec": CONVERT_TIMEOUT_SEC,
                },
            )
            raise HTTPException(
                status_code=408,
                detail=build_error_detail(
                    code="CONVERSION_TIMEOUT",
                    message=f"Conversion timed out after {CONVERT_TIMEOUT_SEC} seconds.",
                    detected_type=None,
                    stage="conversion_pipeline",
                    retryable=True,
                    attempted_stages=["conversion_pipeline"],
                ),
            ) from exc
        except HTTPException:
            raise
        except Exception as exc:
            request_logger.exception(
                "conversion failed unexpectedly",
                extra={
                    "upload_filename": stored_upload.filename,
                    "size_bytes": stored_upload.size_bytes,
                },
            )
            raise HTTPException(
                status_code=500,
                detail=build_error_detail(
                    code="INTERNAL_CONVERSION_ERROR",
                    message=compact_message(
                        str(exc),
                        "Unexpected internal error during conversion.",
                    ),
                    detected_type=None,
                    stage="conversion_pipeline",
                    retryable=False,
                    attempted_stages=["conversion_pipeline"],
                ),
            ) from exc

        duration_ms = int((time.perf_counter() - started_at) * 1000)
        request_logger.info(
            "conversion completed",
            extra={
                "upload_filename": stored_upload.filename,
                "size_bytes": stored_upload.size_bytes,
                "duration_ms": duration_ms,
                "detected_type": result["detected_type"],
                "preprocessed_from": result["preprocessed_from"],
            },
        )
        return result


@asynccontextmanager
async def lifespan(app_instance: FastAPI):
    """初始化线程池和转换并发控制。"""

    app_instance.state.convert_semaphore = asyncio.Semaphore(MAX_CONCURRENT_JOBS)
    app_instance.state.thread_pool = ThreadPoolExecutor(
        max_workers=THREADPOOL_WORKERS,
        thread_name_prefix="markitdown-convert",
    )
    dependency_status = get_dependency_status()
    logger.info(
        "markitdown server started",
        extra={
            "max_concurrent_jobs": MAX_CONCURRENT_JOBS,
            "convert_timeout_sec": CONVERT_TIMEOUT_SEC,
            "subprocess_timeout_sec": SUBPROCESS_TIMEOUT_SEC,
            "max_upload_size_mb": MAX_UPLOAD_SIZE_MB,
            "threadpool_workers": THREADPOOL_WORKERS,
            "uvicorn_workers": read_int_env(
                "MARKITDOWN_UVICORN_WORKERS",
                DEFAULT_UVICORN_WORKERS,
            ),
            "dependencies": dependency_status,
        },
    )
    try:
        yield
    finally:
        app_instance.state.thread_pool.shutdown(wait=False, cancel_futures=True)


app = FastAPI(title="MarkItDown API", version="0.2.0", lifespan=lifespan)


@app.get("/health")
def health() -> dict[str, Any]:
    """返回服务可用性及关键依赖状态。"""

    dependency_status = get_dependency_status()
    overall_status = (
        "ok"
        if all(item["available"] for item in dependency_status.values())
        else "degraded"
    )
    return {
        "status": overall_status,
        "dependencies": dependency_status,
    }


@app.post("/convert")
async def convert(
    file: UploadFile = File(..., description="待转换文件"),
    enable_plugins: bool = Form(
        False, description="是否启用 MarkItDown 三方插件（默认 false）"
    ),
    keep_data_uris: bool = Form(
        False, description="是否保留 data URI（默认 false）"
    ),
    llm_model: str | None = Form(
        None, description="多模态模型名，例如 gpt-4o（用于图片描述）"
    ),
    llm_prompt: str | None = Form(
        None, description="自定义图片描述提示词；仅在传入 llm_model 时生效"
    ),
    openai_api_key: str | None = Form(
        None, description="OpenAI API Key；不传则使用环境变量"
    ),
    openai_base_url: str | None = Form(
        None,
        description="OpenAI 网关地址；优先级: 表单值 > OPENAI_BASE_URL 环境变量 > https://api.openai.com/v1",
    ),
    exiftool_path: str | None = Form(
        DEFAULT_EXIFTOOL_PATH,
        description="exiftool 可执行文件路径，默认 /usr/bin/exiftool",
    ),
    style_map: str | None = Form(
        None, description="DOCX 转换 style_map（mammoth 语法）"
    ),
    docintel_endpoint: str | None = Form(
        None, description="Azure Document Intelligence Endpoint（可选）"
    ),
    docintel_api_version: str | None = Form(
        None, description="Azure Document Intelligence API 版本（可选）"
    ),
) -> dict[str, Any]:
    """上传文件并按标准化管线转换为 Markdown。"""

    request_id = uuid.uuid4().hex
    request_logger = RequestLoggerAdapter(logger, {"request_id": request_id})
    options = ConvertOptions(
        enable_plugins=enable_plugins,
        keep_data_uris=keep_data_uris,
        llm_model=llm_model,
        llm_prompt=llm_prompt,
        openai_api_key=openai_api_key,
        openai_base_url=openai_base_url,
        exiftool_path=exiftool_path,
        style_map=style_map,
        docintel_endpoint=docintel_endpoint,
        docintel_api_version=docintel_api_version,
    )

    stored_upload: StoredUpload | None = None
    try:
        stored_upload = await store_upload_file(file, request_logger)
        conversion_result = await run_conversion_with_limit(
            stored_upload,
            options,
            request_logger,
        )
    except BusyError as exc:
        raise HTTPException(
            status_code=429,
            detail=build_error_detail(
                code="SERVER_BUSY",
                message="Server is busy, please retry later.",
                detected_type=None,
                stage="queue",
                retryable=True,
                attempted_stages=[],
            ),
        ) from exc
    finally:
        if stored_upload is not None:
            shutil.rmtree(stored_upload.temp_dir, ignore_errors=True)

    if stored_upload is None:
        raise HTTPException(
            status_code=500,
            detail=build_error_detail(
                code="UPLOAD_STATE_MISSING",
                message="Upload state missing.",
                detected_type=None,
                stage="store_upload",
                retryable=False,
                attempted_stages=["store_upload"],
            ),
        )

    return {
        "ok": True,
        "filename": stored_upload.filename,
        "title": conversion_result["title"],
        "markdown": conversion_result["markdown"],
        "text_content": conversion_result["text_content"],
        "detected_type": conversion_result["detected_type"],
        "pipeline": conversion_result["pipeline"],
        "preprocessed_from": conversion_result["preprocessed_from"],
        "duration_ms": conversion_result["duration_ms"],
    }
