import logging

import httpx

from agent import __main__ as entry


def test_request_urls_with_the_bot_token_are_not_logged(caplog, monkeypatch, tmp_path):
    from cryptography.fernet import Fernet
    monkeypatch.setenv("PROFILE_PATH", str(entry.__file__).replace("agent/__main__.py", "profile.example.yaml"))
    monkeypatch.setenv("DATA_KEY", Fernet.generate_key().decode())
    entry.main(["check"])  # configures logging
    handler = httpx.MockTransport(lambda request: httpx.Response(200, json={"ok": True, "result": []}))
    with caplog.at_level(logging.INFO), httpx.Client(transport=handler) as client:
        client.post("https://api.telegram.org/bot123456:SECRET-token/getUpdates")
    assert "SECRET-token" not in caplog.text
