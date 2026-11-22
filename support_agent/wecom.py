import base64
import binascii
import hashlib
import hmac
import re
import time
from xml.etree import ElementTree

import httpx
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.padding import PKCS7

from .config import Settings


class InvalidCallback(ValueError):
    """Invalid signature, payload or recipient. / 签名、负载或接收方无效。"""


class WeComError(Exception):
    """Safe API error without credentials or customer data. / 不含密钥或客户数据的接口错误。"""


def parse_xml(body: bytes) -> ElementTree.Element:
    """Read UTF-8 callback XML with no DTD or entity declarations.
    仅解析 UTF-8 回调 XML，拒绝 DTD 和实体声明。
    """
    content = body.decode("utf-8")
    upper_content = content.upper()
    if "<!DOCTYPE" in upper_content or "<!ENTITY" in upper_content:
        raise ValueError("XML declarations are not supported")
    root = ElementTree.fromstring(content)
    if root.tag != "xml":
        raise ValueError("Unexpected XML root")
    return root


class CallbackCipher:
    """Verify and decode WeCom envelopes. / 校验并解密企业微信回调包。"""

    def __init__(self, settings: Settings):
        encoded_key = settings.encoding_aes_key
        if not re.fullmatch(r"[A-Za-z0-9+/]{43}", encoded_key):
            raise ValueError("WECOM_ENCODING_AES_KEY must be a 43-character Base64 key")
        self.key = base64.b64decode(encoded_key + "=", validate=True)
        self.token = settings.callback_token
        self.receiver = settings.corp_id.encode()

    def decrypt(self, signature: str, timestamp: str, nonce: str, encrypted: str) -> bytes:
        """Verify the signature before AES and reject invalid padding or another CorpID.
        解密前先验签，填充、长度或企业 ID 不符时拒绝回调。
        """
        if not re.fullmatch(r"[a-fA-F0-9]{40}", signature):
            raise InvalidCallback("Invalid callback signature")
        fields = sorted([self.token, timestamp, nonce, encrypted])
        expected = hashlib.sha1("".join(fields).encode(), usedforsecurity=False).hexdigest()
        if not hmac.compare_digest(expected, signature.lower()):
            raise InvalidCallback("Invalid callback signature")
        try:
            ciphertext = base64.b64decode(encrypted, validate=True)
            decryptor = Cipher(algorithms.AES(self.key), modes.CBC(self.key[:16])).decryptor()
            padded = decryptor.update(ciphertext) + decryptor.finalize()
            unpadder = PKCS7(256).unpadder()
            frame = unpadder.update(padded) + unpadder.finalize()
        except (ValueError, binascii.Error):
            raise InvalidCallback("Invalid callback ciphertext") from None
        if len(frame) < 20:
            raise InvalidCallback("Invalid callback frame")
        end = 20 + int.from_bytes(frame[16:20], "big")
        if not hmac.compare_digest(frame[end:], self.receiver):
            raise InvalidCallback("Unexpected callback recipient")
        return frame[20:end]


class WeComClient:
    """Call the official API for one test account. / 调用单个测试账号的官方接口。"""

    def __init__(self, http: httpx.AsyncClient, settings: Settings):
        self.http = http
        self.settings = settings
        self.access_token = ""
        self.token_expires_at = 0.0

    async def _request(self, method: str, path: str, params: dict, body: dict | None = None) -> dict:
        # 保留接口名与错误码，避免 URL 或正文进入日志。
        # Keep endpoint names and error codes, never credential-bearing URLs or payloads.
        try:
            response = await self.http.request(method, path, params=params, json=body)
            response.raise_for_status()
            data = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise WeComError(f"{path}: {type(exc).__name__}") from None
        if not isinstance(data, dict) or type(data.get("errcode")) is not int:
            raise WeComError(f"{path}: invalid response")
        return data

    async def _token(self) -> str:
        # 使用微信客服 Secret 短暂缓存 API token。 / Cache the API token briefly using the KF secret.
        if self.access_token and time.monotonic() < self.token_expires_at:
            return self.access_token
        data = await self._request("GET", "/cgi-bin/gettoken", {
            "corpid": self.settings.corp_id, "corpsecret": self.settings.secret,
        })
        token, expires = data.get("access_token"), data.get("expires_in")
        if data["errcode"] or not isinstance(token, str) or not token or type(expires) is not int:
            raise WeComError(f"gettoken: errcode={data['errcode']}")
        self.access_token = token
        self.token_expires_at = time.monotonic() + max(0, expires - 120)
        return token

    async def _post(self, endpoint: str, body: dict) -> dict:
        # 仅在 token 被明确拒绝时刷新并重试。 / Refresh only on an explicit token rejection.
        path = f"/cgi-bin/kf/{endpoint}"
        payload = {"open_kfid": self.settings.open_kfid, **body}
        data = await self._request("POST", path, {"access_token": await self._token()}, payload)
        if data["errcode"] in (40014, 42001):
            self.access_token = ""
            data = await self._request("POST", path, {"access_token": await self._token()}, payload)
        if data["errcode"]:
            raise WeComError(f"{endpoint}: errcode={data['errcode']}")
        return data

    async def sync(self, token: str, cursor: str) -> tuple[str, bool, list[dict]]:
        """Pull one page even when a preceding page was empty. / 拉取单页并校验下一页游标。"""
        body = {"token": token, "limit": 100}
        if cursor:
            body["cursor"] = cursor
        data = await self._post("sync_msg", body)
        next_cursor, has_more, messages = (data.get(key) for key in ("next_cursor", "has_more", "msg_list"))
        if (not isinstance(next_cursor, str) or has_more not in (0, 1)
                or not isinstance(messages, list) or any(not isinstance(item, dict) for item in messages)
                or (has_more and (not next_cursor or cursor == next_cursor))):
            raise WeComError("sync_msg: invalid page or cursor")
        return next_cursor or cursor, bool(has_more), messages

    async def queue_for_human(self, sender_id: str) -> tuple[str, str | None]:
        """Check live state before a possible transfer, including after a timeout.
        转接前查询真实状态，超时重试时避免重复转接或重开已结束会话。
        """
        body = {"external_userid": sender_id}
        current = await self._post("service_state/get", body)
        state = current.get("service_state")
        if state in (0, 1):
            await self._post("service_state/trans", {**body, "service_state": 2})
            return "queued", None
        if state == 2:
            return "queued", None
        if state == 3:
            return "accepted", current.get("servicer_userid") or current.get("service_userid")
        if state == 4:
            return "closed", None
        raise WeComError("service_state/get: unknown state")
