from __future__ import annotations

import os
import shutil
import tempfile
import uuid
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from markitdown import MarkItDown
from openai import OpenAI

app = FastAPI(title="MarkItDown API", version="0.1.0")
DEFAULT_OPENAI_BASE_URL = "https://api.openai.com/v1"
DEFAULT_EXIFTOOL_PATH = "/usr/bin/exiftool"


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
    if not file.filename:
        raise HTTPException(status_code=400, detail="Missing uploaded filename")

    converter_kwargs: dict[str, Any] = {
        "enable_plugins": enable_plugins,
        "exiftool_path": exiftool_path,
        "style_map": style_map,
        "docintel_endpoint": docintel_endpoint,
        "docintel_api_version": docintel_api_version,
    }
    converter_kwargs = {k: v for k, v in converter_kwargs.items() if v is not None}

    if llm_model:
        client_kwargs: dict[str, Any] = {}
        if openai_api_key:
            client_kwargs["api_key"] = openai_api_key
        resolved_base_url = (
            openai_base_url
            or os.getenv("OPENAI_BASE_URL")
            or DEFAULT_OPENAI_BASE_URL
        )
        client_kwargs["base_url"] = resolved_base_url

        converter_kwargs["llm_client"] = OpenAI(**client_kwargs)
        converter_kwargs["llm_model"] = llm_model
        if llm_prompt:
            converter_kwargs["llm_prompt"] = llm_prompt

    converter = MarkItDown(**converter_kwargs)

    temp_dir = Path(tempfile.gettempdir()) / f"markitdown-{uuid.uuid4().hex}"
    temp_dir.mkdir(parents=True, exist_ok=False)
    temp_file_path = temp_dir / Path(file.filename).name

    try:
        with temp_file_path.open("wb") as f:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                f.write(chunk)

        result = converter.convert(temp_file_path, keep_data_uris=keep_data_uris)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Conversion failed: {exc}") from exc
    finally:
        await file.close()
        shutil.rmtree(temp_dir, ignore_errors=True)

    return {
        "filename": file.filename,
        "title": result.title,
        "markdown": result.markdown,
        "text_content": result.text_content,
    }
