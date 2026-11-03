import pytest
from fastapi.testclient import TestClient

from marketflow.db import init_sqlite, make_engine
from marketflow.settings import Settings, load_settings
from marketflow.web.app import create_app
from marketflow.web.serve import uvicorn_kwargs

SECRET = "s" * 40


def client(tmp_path, **kw):
    s = Settings(database_url=f"sqlite:///{tmp_path / 'h.db'}", jwt_secret=SECRET, **kw)
    e = make_engine(s.database_url); init_sqlite(e)
    return TestClient(create_app(s, e))


def test_hsts_only_when_https_is_in_play(tmp_path):
    assert "Strict-Transport-Security" not in client(tmp_path).get("/health").headers
    assert "max-age" in client(tmp_path, hsts=True).get("/health").headers["Strict-Transport-Security"]
    assert "Strict-Transport-Security" in client(tmp_path, tls_cert="c.pem", tls_key="k.pem").get("/health").headers


def test_uvicorn_kwargs(tmp_path, capsys):
    assert uvicorn_kwargs(Settings(), "127.0.0.1", 8000) == {"host": "127.0.0.1", "port": 8000, "proxy_headers": False}
    px = uvicorn_kwargs(Settings(trust_proxy="10.0.0.5"), "127.0.0.1", 8000)
    assert px["proxy_headers"] is True and px["forwarded_allow_ips"] == "10.0.0.5"
    cert, key = tmp_path / "c.pem", tmp_path / "k.pem"
    cert.write_text("x"); key.write_text("y")
    kw = uvicorn_kwargs(Settings(tls_cert=str(cert), tls_key=str(key)), "0.0.0.0", 443)
    assert kw["ssl_certfile"] == str(cert) and kw["ssl_keyfile"] == str(key)
    with pytest.raises(SystemExit):
        uvicorn_kwargs(Settings(tls_cert=str(cert)), "127.0.0.1", 1)            # key missing
    with pytest.raises(SystemExit):
        uvicorn_kwargs(Settings(tls_cert=str(cert), tls_key=str(tmp_path / "nope")), "127.0.0.1", 1)
    uvicorn_kwargs(Settings(), "0.0.0.0", 8000)
    assert "plain HTTP" in capsys.readouterr().out
    uvicorn_kwargs(Settings(hsts=True), "0.0.0.0", 8000)                         # behind a TLS proxy: no warning
    assert capsys.readouterr().out == ""


def test_server_settings_from_file_and_env(tmp_path, monkeypatch):
    f = tmp_path / "s.toml"
    f.write_text('[server]\ntls_cert = "a.pem"\ntls_key = "b.pem"\nhsts = true\n')
    monkeypatch.setenv("MARKETFLOW_SETTINGS", str(f))
    s = load_settings()
    assert (s.tls_cert, s.tls_key, s.hsts) == ("a.pem", "b.pem", True)
    monkeypatch.setenv("HSTS", "false")
    assert load_settings().hsts is False
