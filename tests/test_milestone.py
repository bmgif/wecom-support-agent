import base64
import hashlib
import sqlite3
import struct
import time

import httpx
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from fastapi.testclient import TestClient

from support_agent.app import create_app
from support_agent.config import Settings


KEY = bytes(range(32))
CORP = "ww-test"
ACCOUNT = "wk-test"
TOKEN = "callback-test-token"


def encrypted_request(message, receiver=CORP):
    payload = message.encode("utf-8")
    frame = b"0123456789abcdef" + struct.pack(">I", len(payload)) + payload + receiver.encode()
    padding = 32 - len(frame) % 32
    encryptor = Cipher(algorithms.AES(KEY), modes.CBC(KEY[:16])).encryptor()
    body = frame + bytes([padding]) * padding
    encrypted = base64.b64encode(encryptor.update(body) + encryptor.finalize()).decode()
    timestamp = str(int(time.time()))
    nonce = "1"
    signature = hashlib.sha1("".join(sorted([TOKEN, timestamp, nonce, encrypted])).encode()).hexdigest()
    return {"msg_signature": signature, "timestamp": timestamp, "nonce": nonce}, encrypted


def callback(client, pull_token, account=ACCOUNT):
    message = (
        "<xml><MsgType>event</MsgType><Event>kf_msg_or_event</Event>"
        f"<Token>{pull_token}</Token><OpenKfId>{account}</OpenKfId></xml>"
    )
    params, encrypted = encrypted_request(message)
    return client.post(
        "/webhooks/wecom/kf", params=params, content=f"<xml><Encrypt>{encrypted}</Encrypt></xml>"
    )


def settings(tmp_path):
    return Settings(CORP, "kf-test-secret", TOKEN, base64.b64encode(KEY).decode().rstrip("="),
                    ACCOUNT, tmp_path / "milestone.sqlite3")


def wait_for(database, query, predicate):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        with sqlite3.connect(database) as connection:
            rows = connection.execute(query).fetchall()
        if predicate(rows):
            return rows
        time.sleep(0.025)
    raise AssertionError(f"Unexpected rows from {query}: {rows}")


def text_message(message_id, text="测试转人工", origin=3):
    return {"msgid": message_id, "open_kfid": ACCOUNT, "external_userid": "customer-test",
            "origin": origin, "msgtype": "text", "text": {"content": text}}


def test_health_and_callback_url_validation(tmp_path):
    with TestClient(create_app(settings(tmp_path))) as client:
        assert client.get("/healthz").json() == {"status": "ok"}
        query, encrypted = encrypted_request("echo-without-newline")
        response = client.get("/webhooks/wecom/kf", params={**query, "echostr": encrypted})
        assert response.content == b"echo-without-newline"
        query["msg_signature"] = "0" * 40
        assert client.get("/webhooks/wecom/kf", params={**query, "echostr": encrypted}).status_code == 403
        foreign, encrypted = encrypted_request("echo", receiver="ww-other")
        assert client.get("/webhooks/wecom/kf", params={**foreign, "echostr": encrypted}).status_code == 403


def test_callback_scope_and_signed_xml_are_required(tmp_path):
    config = settings(tmp_path)
    with TestClient(create_app(config)) as client:
        assert callback(client, "other-pull", account="wk-other").status_code == 403
        query, encrypted = encrypted_request("<!DOCTYPE xml><xml/>")
        response = client.post("/webhooks/wecom/kf", params=query,
                               content=f"<xml><Encrypt>{encrypted}</Encrypt></xml>")
        assert response.status_code == 400
        with sqlite3.connect(config.db_path) as connection:
            assert connection.execute("SELECT count(*) FROM callbacks").fetchone() == (0,)


def test_callback_paginates_deduplicates_and_confirms_human_pickup_after_restart(tmp_path):
    config = settings(tmp_path)
    calls = []

    def platform(request):
        import json

        path = request.url.path
        body = json.loads(request.content) if request.content else {}
        calls.append((path, body))
        if path.endswith("gettoken"):
            assert request.url.params["corpid"] == CORP
            assert request.url.params["corpsecret"] == "kf-test-secret"
            return httpx.Response(200, json={"errcode": 0, "access_token": "test", "expires_in": 7200})
        assert request.url.params["access_token"] == "test"
        assert body["open_kfid"] == ACCOUNT
        if path.endswith("sync_msg"):
            if not body.get("cursor"):
                return httpx.Response(200, json={"errcode": 0, "next_cursor": "c1", "has_more": 1,
                                                  "msg_list": []})
            if body["cursor"] == "c1":
                assert body["token"] == "initial"
                messages = [text_message("m1"), text_message("m1"),
                            text_message("m2", "你好"), text_message("agent", origin=5)]
                return httpx.Response(200, json={"errcode": 0, "next_cursor": "c2", "has_more": 0,
                                                  "msg_list": messages})
            assert body["cursor"] == "c2"
            assert body["token"] == "pickup"
            event = {"msgtype": "event", "event": {"event_type": "session_status_change", "change_type": 1,
                     "open_kfid": ACCOUNT, "external_userid": "customer-test", "new_servicer_userid": "agent1"}}
            return httpx.Response(200, json={"errcode": 0, "next_cursor": "c3", "has_more": 0,
                                              "msg_list": [event]})
        if path.endswith("service_state/get"):
            return httpx.Response(200, json={"errcode": 0, "service_state": 0})
        assert path.endswith("service_state/trans")
        assert body == {"open_kfid": ACCOUNT, "external_userid": "customer-test", "service_state": 2}
        return httpx.Response(200, json={"errcode": 0})

    transport = httpx.MockTransport(platform)
    with TestClient(create_app(config, transport=transport)) as client:
        assert callback(client, "initial").text == "success"
        assert callback(client, "initial").text == "success"
        assert wait_for(config.db_path, "SELECT message_id, state FROM messages ORDER BY message_id",
                        lambda rows: ("m1", "queued") in rows) == [("m1", "queued"), ("m2", "received")]

    with TestClient(create_app(config, transport=transport)) as client:
        assert callback(client, "pickup").status_code == 200
        assert wait_for(config.db_path, "SELECT state, servicer_id FROM messages WHERE message_id = 'm1'",
                        lambda rows: rows == [("accepted", "agent1")]) == [("accepted", "agent1")]

    paths = [path for path, _ in calls]
    assert paths.count("/cgi-bin/kf/sync_msg") == 3
    assert paths.count("/cgi-bin/kf/service_state/trans") == 1


def test_transfer_timeout_recovers_from_provider_state_without_resending(tmp_path):
    config = settings(tmp_path)
    transfers = 0

    def platform(request):
        nonlocal transfers
        path = request.url.path
        if path.endswith("gettoken"):
            return httpx.Response(200, json={"errcode": 0, "access_token": "test", "expires_in": 7200})
        if path.endswith("sync_msg"):
            return httpx.Response(200, json={"errcode": 0, "next_cursor": "done", "has_more": 0,
                                              "msg_list": [text_message("m1")]})
        if path.endswith("service_state/get"):
            return httpx.Response(200, json={"errcode": 0, "service_state": 2 if transfers else 0})
        assert path.endswith("service_state/trans")
        transfers += 1
        raise httpx.ReadTimeout("simulated response lost", request=request)

    with TestClient(create_app(config, transport=httpx.MockTransport(platform))) as client:
        assert callback(client, "initial").status_code == 200
        wait_for(config.db_path, "SELECT state, attempts FROM messages", lambda rows: rows == [("queued", 1)])
    assert transfers == 1
