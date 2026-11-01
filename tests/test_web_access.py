"""Who may reach the dashboard, and which requests it refuses."""

from fastapi.testclient import TestClient

from monitor.lake import BatchWriter, connect
from monitor.settings import Web
from monitor.web import app as web_app
from monitor.web.app import create_app, serve
from monitor.web.filters import read_watch

WATCH = "/watchlist/state?key=GA&watched=true"
LAPTOP = ("127.0.0.1", 50000)
OTHER_DEVICE = ("192.168.1.20", 50000)


def seeded(settings):
    conn = connect(settings)
    writer = BatchWriter(conn, settings)
    writer.add(
        "ref.states",
        [
            {
                "usps": "GA",
                "fips": "13",
                "name": "Georgia",
                "battleground": True,
            }
        ],
    )
    writer.flush()
    conn.close()
    return settings


def watched(settings):
    return read_watch(settings.paths.ui_db)[0]


def test_default_hosts_are_loopback_names_only():
    assert set(Web().allowed_hosts) == {"127.0.0.1", "localhost", "::1"}


def test_local_mode_accepts_loopback_host_names(settings):
    settings.web.allowed_hosts = Web().allowed_hosts
    with TestClient(create_app(settings)) as client:
        for host in ("127.0.0.1:8000", "localhost:8000", "[::1]:8000"):
            response = client.get("/frag/kpis", headers={"Host": host})
            assert response.status_code == 200, host


def test_local_mode_refuses_other_host_names(settings):
    settings.web.allowed_hosts = Web().allowed_hosts
    with TestClient(create_app(settings)) as client:
        for host in ("rebind.example", "192.168.1.5:8000", "testserver"):
            response = client.get("/frag/kpis", headers={"Host": host})
            assert response.status_code == 400, host


def test_a_post_from_another_origin_is_refused(settings):
    with TestClient(create_app(seeded(settings))) as client:
        response = client.post(
            WATCH, headers={"Origin": "http://evil.example"}
        )
    assert response.status_code == 403
    assert watched(settings) == []


def test_an_opaque_origin_is_refused(settings):
    with TestClient(create_app(seeded(settings))) as client:
        response = client.post(WATCH, headers={"Origin": "null"})
    assert response.status_code == 403
    assert watched(settings) == []


def test_a_post_from_the_same_origin_is_accepted(settings):
    with TestClient(create_app(seeded(settings))) as client:
        response = client.post(WATCH, headers={"Origin": "http://testserver"})
    assert response.status_code == 200
    assert watched(settings) == ["GA"]


def test_a_post_with_no_origin_is_accepted_locally(settings):
    with TestClient(create_app(seeded(settings))) as client:
        assert client.post(WATCH).status_code == 200
    assert watched(settings) == ["GA"]


def lan(settings):
    settings.web.host = "0.0.0.0"
    settings.web.allowed_hosts = Web().allowed_hosts
    return create_app(seeded(settings))


def test_lan_mode_accepts_any_host(settings):
    with TestClient(lan(settings), client=OTHER_DEVICE) as client:
        for host in ("192.168.1.5:8000", "laptop.local", "10.0.0.2"):
            response = client.get("/frag/kpis", headers={"Host": host})
            assert response.status_code == 200, host


def test_lan_mode_refuses_writes_from_other_devices(settings):
    with TestClient(lan(settings), client=OTHER_DEVICE) as client:
        response = client.post(WATCH)
    assert response.status_code == 403
    assert watched(settings) == []


def test_lan_mode_accepts_writes_from_the_laptop(settings):
    with TestClient(lan(settings), client=LAPTOP) as client:
        response = client.post(WATCH, headers={"Host": "127.0.0.1:8000"})
    assert response.status_code == 200
    assert watched(settings) == ["GA"]


def test_lan_mode_still_checks_the_origin_of_a_laptop_post(settings):
    with TestClient(lan(settings), client=LAPTOP) as client:
        response = client.post(
            WATCH,
            headers={
                "Host": "127.0.0.1:8000",
                "Origin": "http://evil.example",
            },
        )
    assert response.status_code == 403
    assert watched(settings) == []


def test_binding_any_non_loopback_address_is_lan_mode(settings):
    settings.web.host = "192.168.1.5"
    settings.web.allowed_hosts = Web().allowed_hosts
    with TestClient(
        create_app(seeded(settings)), client=OTHER_DEVICE
    ) as client:
        assert client.post(WATCH).status_code == 403


def test_documentation_pages_that_load_cdn_assets_are_off(settings):
    with TestClient(create_app(settings)) as client:
        for path in ("/docs", "/redoc", "/openapi.json"):
            assert client.get(path).status_code == 404, path


def captured_run(monkeypatch):
    calls = []

    def fake_run(target, **options):
        calls.append((target, options))

    monkeypatch.setattr(web_app.uvicorn, "run", fake_run)
    monkeypatch.setattr(web_app, "configure", lambda *args: None)
    return calls


def test_a_lan_server_limits_concurrency(settings, monkeypatch):
    calls = captured_run(monkeypatch)
    serve(settings, "0.0.0.0", 8031)
    target, options = calls[0]
    assert options["limit_concurrency"] == settings.web.lan_concurrency
    with TestClient(target, client=OTHER_DEVICE) as client:
        assert client.post(WATCH).status_code == 403


def test_a_local_server_has_no_concurrency_limit(settings, monkeypatch):
    calls = captured_run(monkeypatch)
    serve(settings, "127.0.0.1", 8031)
    assert calls[0][1]["limit_concurrency"] is None


def test_the_reloader_child_learns_the_bind_address(settings, monkeypatch):
    monkeypatch.setenv("WEB__HOST", "127.0.0.1")
    calls = captured_run(monkeypatch)
    serve(settings, "0.0.0.0", 8031, reload=True)
    assert calls[0][1]["limit_concurrency"] == settings.web.lan_concurrency
    assert web_app.os.environ["WEB__HOST"] == "0.0.0.0"
