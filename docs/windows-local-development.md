# Windows 本地开发

需要 Python 3.10 或以上。在项目根目录打开 PowerShell，创建虚拟环境并运行测试。

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m pytest -q
```

启动本地 FastAPI。

```powershell
.\.venv\Scripts\python.exe -m uvicorn support_agent.app:app --host 127.0.0.1 --port 8000 --no-access-log
```

在另一个 PowerShell 窗口检查。

```powershell
Invoke-RestMethod 'http://127.0.0.1:8000/healthz' | ConvertTo-Json -Compress
```

预期返回 `{"status":"ok"}`。未加载企微配置时，应用提供本机健康检查，自动化测试使用模拟的企微接口验证回调验签、消息拉取和坐席接起状态。真实企微回调和「测试转人工」验收见 [Linux + Caddy 固定 HTTPS 部署指南](linux-caddy-milestone-1.md)。
