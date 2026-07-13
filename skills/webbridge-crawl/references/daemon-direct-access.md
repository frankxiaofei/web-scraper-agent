# WebBridge Daemon 直连调试记录

**问题描述**：Hermes 的 `skill_webbridge_*` 工具通过 `POST /api/crawl-agent/skills/execute` 调用 web_scraper 后端，
但有时后端返回 `"tool" field required`（422）错误，因为 Hermes 发送 `{skill, args}` 而非 `{tool, arguments}`。

## 直连尝试

WebBridge daemon 运行在 `127.0.0.1:10086`，使用 WebSocket 协议：

```python
import asyncio, json, websockets

async def test():
    headers = {"Origin": "http://localhost:10086"}
    async with websockets.connect("ws://127.0.0.1:10086/ws", additional_headers=headers) as ws:
        await ws.send(json.dumps({"type": "listSessions", "id": "test-1"}))
        resp = await ws.recv()
        print(resp)

asyncio.run(test())
```

**结果**：HTTP 403。daemon 只接受浏览器扩展的连接，拒绝外部客户端。

## 正确的修复

不要尝试直连 daemon。修复 web_scraper 后端 `crawl_agent_routes.py` 的 Pydantic model：

```python
# 修改前
class CrawlAgentSkillExecuteRequest(BaseModel):
    tool: str
    arguments: dict[str, Any] = {}

# 修改后（兼容 Hermes 的 skill/args 格式）
class CrawlAgentSkillExecuteRequest(BaseModel):
    tool: str = ""
    skill: str = ""
    arguments: dict[str, Any] = {}
    args: dict[str, Any] = {}
```

同时 handler 做 fallback：

```python
@app.post("/api/crawl-agent/skills/execute")
async def api_execute_crawl_agent_skill(body: CrawlAgentSkillExecuteRequest):
    tool = (body.tool or body.skill or "").strip()
    if not tool:
        raise HTTPException(status_code=400, detail="tool 不能为空")
    arguments = body.arguments or body.args or {}
    return _execute_crawl_agent_tool(tool, arguments)
```

之后重启 web_scraper UI 即可。
