from __future__ import annotations

import asyncio
import logging
import os
import shutil
import tempfile
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, MutableMapping

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from markitdown import MarkItDown
from openai import OpenAI

DEFAULT_OPENAI_BASE_URL = "https://api.openai.com/v1"
DEFAULT_EXIFTOOL_PATH = "/usr/bin/exiftool"
DEFAULT_MAX_CONCURRENT_JOBS = 4
DEFAULT_CONVERT_TIMEOUT_SEC = 180
DEFAULT_MAX_UPLOAD_SIZE_MB = 100
DEFAULT_THREADPOOL_WORKERS = 4
DEFAULT_UVICORN_WORKERS = 2
UPLOAD_CHUNK_SIZE = 1024 * 1024

logger = logging.getLogger("markitdown_server")
logging.basicConfig(
    level=os.getenv("MARKITDOWN_LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)


def read_int_env(name: str, default: int, minimum: int = 1) -> int:
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


class RequestLoggerAdapter(logging.LoggerAdapter):
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
    pass


@dataclass(slots=True)
class ConvertOptions:
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
    filename: str
    temp_dir: Path
    temp_file_path: Path
    size_bytes: int


def build_converter_kwargs(options: ConvertOptions) -> dict[str, Any]:
    converter_kwargs: dict[str, Any] = {
        "enable_plugins": options.enable_plugins,
        "exiftool_path": options.exiftool_path,
        "style_map": options.style_map,
        "docintel_endpoint": options.docintel_endpoint,
        "docintel_api_version": options.docintel_api_version,
    }
    converter_kwargs = {key: value for key, value in converter_kwargs.items() if value is not None}

    if options.llm_model:
        client_kwargs: dict[str, Any] = {}
        if options.openai_api_key:
            client_kwargs["api_key"] = options.openai_api_key
        resolved_base_url = (
            options.openai_base_url
            or os.getenv("OPENAI_BASE_URL")
            or DEFAULT_OPENAI_BASE_URL
        )
        client_kwargs["base_url"] = resolved_base_url
        converter_kwargs["llm_client"] = OpenAI(**client_kwargs)
        converter_kwargs["llm_model"] = options.llm_model
        if options.llm_prompt:
            converter_kwargs["llm_prompt"] = options.llm_prompt

    return converter_kwargs


def run_conversion(file_path: Path, options: ConvertOptions) -> dict[str, Any]:
    converter = MarkItDown(**build_converter_kwargs(options))
    result = converter.convert(file_path, keep_data_uris=options.keep_data_uris)
    return {
        "title": result.title,
        "markdown": result.markdown,
        "text_content": result.text_content,
    }


async def store_upload_file(file: UploadFile, request_logger: RequestLoggerAdapter) -> StoredUpload:
    if not file.filename:
        raise HTTPException(status_code=400, detail="Missing uploaded filename")

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
                    raise HTTPException(
                        status_code=413,
                        detail=f"Uploaded file is too large, limit is {MAX_UPLOAD_SIZE_MB} MB",
                    )
                output.write(chunk)
    except Exception:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise
    finally:
        await file.close()

    request_logger.info(
        "upload stored",
        extra={"upload_filename": file.filename, "size_bytes": size_bytes},
    )
    return StoredUpload(
        filename=file.filename,
        temp_dir=temp_dir,
        temp_file_path=temp_file_path,
        size_bytes=size_bytes,
    )


async def run_conversion_with_limit(
    stored_upload: StoredUpload,
    options: ConvertOptions,
    request_logger: RequestLoggerAdapter,
) -> dict[str, Any]:
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
                    run_conversion,
                    stored_upload.temp_file_path,
                    options,
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
            raise HTTPException(status_code=408, detail="Conversion timed out") from exc
        except HTTPException:
            raise
        except Exception as exc:
            request_logger.exception(
                "conversion failed",
                extra={
                    "upload_filename": stored_upload.filename,
                    "size_bytes": stored_upload.size_bytes,
                },
            )
            raise HTTPException(
                status_code=400,
                detail=f"Conversion failed: {exc}",
            ) from exc
        duration_ms = int((time.perf_counter() - started_at) * 1000)
        request_logger.info(
            "conversion completed",
            extra={
                "upload_filename": stored_upload.filename,
                "size_bytes": stored_upload.size_bytes,
                "duration_ms": duration_ms,
            },
        )
        return result


@asynccontextmanager
async def lifespan(app_instance: FastAPI):
    app_instance.state.convert_semaphore = asyncio.Semaphore(MAX_CONCURRENT_JOBS)
    app_instance.state.thread_pool = ThreadPoolExecutor(
        max_workers=THREADPOOL_WORKERS,
        thread_name_prefix="markitdown-convert",
    )
    logger.info(
        "markitdown server started",
        extra={
            "max_concurrent_jobs": MAX_CONCURRENT_JOBS,
            "convert_timeout_sec": CONVERT_TIMEOUT_SEC,
            "max_upload_size_mb": MAX_UPLOAD_SIZE_MB,
            "threadpool_workers": THREADPOOL_WORKERS,
            "uvicorn_workers": read_int_env(
                "MARKITDOWN_UVICORN_WORKERS",
                DEFAULT_UVICORN_WORKERS,
            ),
        },
    )
    try:
        yield
    finally:
        app_instance.state.thread_pool.shutdown(wait=False, cancel_futures=True)


app = FastAPI(title="MarkItDown API", version="0.1.0", lifespan=lifespan)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


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
            detail="Server is busy, please retry later",
        ) from exc
    finally:
        if stored_upload is not None:
            shutil.rmtree(stored_upload.temp_dir, ignore_errors=True)

    if stored_upload is None:
        raise HTTPException(status_code=500, detail="Upload state missing")

    return {
        "filename": stored_upload.filename,
        "title": conversion_result["title"],
        "markdown": conversion_result["markdown"],
        "text_content": conversion_result["text_content"],
    }
