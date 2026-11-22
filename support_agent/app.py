import asyncio
import logging
import sqlite3
from contextlib import asynccontextmanager, suppress
from xml.etree.ElementTree import ParseError

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import PlainTextResponse, Response

from .config import Settings
from .store import Store
from .wecom import CallbackCipher, InvalidCallback, WeComClient, WeComError, parse_xml


logger = logging.getLogger("uvicorn.error")
CALLBACK_PATH = "/webhooks/wecom/kf"
CALLBACK_LIMIT = 128 * 1024


async def _worker(store: Store, wecom: WeComClient) -> None:
    # 每次只处理一页和一条转接，让两类任务都有机会执行。
    # Process at most one page and one transfer per iteration to avoid starvation.
    while True:
        try:
            callback, handoff, cursor = store.work()
            if callback:
                try:
                    cursor, more, items = await wecom.sync(callback["token"], cursor)
                    store.save_page(callback["id"], cursor, more, items)
                    logger.info("WeCom page persisted: items=%d more=%s", len(items), more)
                except WeComError as exc:
                    count = store.retry(callback, str(exc), callback=True)
                    logger.warning("WeCom sync attempt %d: %s", count, exc)
            if handoff:
                # 同页事件可能已经确认接起，取最新状态后再转接。
                # A page may confirm human pickup, so reload pending work before routing.
                _, handoff, _ = store.work()
                if handoff:
                    try:
                        state, servicer = await wecom.queue_for_human(handoff["sender_id"])
                        store.finish_handoff(handoff["message_id"], state, servicer)
                        logger.info("WeCom handoff result: %s", state)
                    except WeComError as exc:
                        count = store.retry(handoff, str(exc), callback=False)
                        logger.warning("WeCom handoff attempt %d: %s", count, exc)
            if not callback and not handoff:
                await asyncio.sleep(0.2)
        except sqlite3.Error as exc:
            logger.error("WeCom ledger unavailable: %s", type(exc).__name__)
            await asyncio.sleep(1)


def create_app(settings: Settings | None = None, *, transport: httpx.AsyncBaseTransport | None = None) -> FastAPI:
    """Build the FastAPI app with one ledger worker, or health only when unconfigured.
    构建带单个持久化 Worker 的 FastAPI 应用，未配置时只提供健康检查。
    """
    @asynccontextmanager
    async def lifespan(_: FastAPI):
        if settings is None:
            yield
            return
        async with httpx.AsyncClient(base_url="https://qyapi.weixin.qq.com", timeout=8, transport=transport) as http:
            task = asyncio.create_task(_worker(store, WeComClient(http, settings)))
            try:
                yield
            finally:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task

    app = FastAPI(title="WeCom Support Agent", lifespan=lifespan)

    @app.get("/healthz")
    async def health():
        """Report process health without exposing credentials. / 返回进程健康状态，不暴露配置。"""
        return {"status": "ok"}

    if settings is None:
        return app

    cipher = CallbackCipher(settings)
    store = Store(settings.db_path, settings.corp_id, settings.open_kfid)

    @app.get(CALLBACK_PATH, response_class=Response)
    async def verify(msg_signature: str, timestamp: str, nonce: str, echostr: str):
        """Return exactly the verified echo bytes for WeCom. / 原样返回验签解密后的企微验证串。"""
        try:
            echo = cipher.decrypt(msg_signature, timestamp, nonce, echostr)
        except InvalidCallback:
            raise HTTPException(403, "Invalid WeCom callback") from None
        return Response(content=echo, media_type="text/plain")

    @app.post(CALLBACK_PATH, response_class=PlainTextResponse)
    async def receive(request: Request, msg_signature: str, timestamp: str, nonce: str):
        """Authenticate and enqueue a callback without waiting for WeCom API calls.
        验签后只将回调写入队列，异步调用企微拉取和转接接口。
        """
        body = bytearray()
        async for chunk in request.stream():
            if len(body) + len(chunk) > CALLBACK_LIMIT:
                raise HTTPException(413, "WeCom callback too large")
            body.extend(chunk)
        try:
            envelope = parse_xml(body)
            encrypted = envelope.findtext("Encrypt") or ""
            payload = parse_xml(cipher.decrypt(msg_signature, timestamp, nonce, encrypted))
        except InvalidCallback:
            raise HTTPException(403, "Invalid WeCom callback") from None
        except (ParseError, ValueError):
            raise HTTPException(400, "Invalid WeCom XML") from None
        if payload.findtext("Event") != "kf_msg_or_event":
            return "success"
        if payload.findtext("OpenKfId") != settings.open_kfid:
            raise HTTPException(403, "Unexpected WeCom customer service account")
        token = payload.findtext("Token")
        if not token:
            raise HTTPException(400, "Missing WeCom pull token")
        try:
            store.enqueue_callback(token)
        except sqlite3.Error:
            raise HTTPException(503, "WeCom ledger unavailable") from None
        return "success"

    return app


app = create_app(Settings.from_env())
