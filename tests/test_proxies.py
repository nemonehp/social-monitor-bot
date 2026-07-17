import pytest

from app.utils.proxies import parse_proxy_line


def test_proxy_formats():
    parsed = parse_proxy_line("127.0.0.1:8080:user:pass")
    assert parsed.url == "http://user:pass@127.0.0.1:8080"
    assert parsed.display == "http://user:***@127.0.0.1:8080"

    parsed = parse_proxy_line("socks5://127.0.0.1:1080")
    assert parsed.scheme == "socks5"
    assert parse_proxy_line("socks5h://127.0.0.1:1080").scheme == "socks5"


def test_bad_proxy():
    with pytest.raises(ValueError):
        parse_proxy_line("not-a-proxy")
