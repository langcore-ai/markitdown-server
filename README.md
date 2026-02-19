# markitdown-server

基于 FastAPI 的 MarkItDown 文件转 Markdown API 服务。

## 安装依赖

```bash
uv sync
```

## 启动服务

```bash
uv run uvicorn main:app --host 0.0.0.0 --port 8000 --reload
```

## 接口

- `GET /health`：健康检查
- `POST /convert`：通过表单上传文件并转换为 Markdown

可选表单参数（部分）：

- `enable_plugins`：是否启用 MarkItDown 插件，默认 `false`
- `keep_data_uris`：是否保留 data URI，默认 `false`
- `llm_model`：启用多模态描述时使用的模型，如 `gpt-4o`
- `llm_prompt`：自定义图像描述 prompt
- `openai_api_key`：OpenAI API Key（不传则走环境变量）
- `openai_base_url`：OpenAI 网关地址，优先级：表单值 > `OPENAI_BASE_URL` 环境变量 > `https://api.openai.com/v1`
- `exiftool_path`：指定 exiftool 路径
- `style_map`：DOCX 转换使用的 mammoth style map
- `docintel_endpoint` / `docintel_api_version`：Azure Document Intelligence 配置

### curl 示例

```bash
curl -X POST "http://127.0.0.1:8000/convert" \
  -F "file=@./example.png" \
  -F "llm_model=gpt-4o" \
  -F "llm_prompt=Describe this image in detail"
```
