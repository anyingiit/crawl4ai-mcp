import crawl4ai_mcp.__main__ as main_module
from crawl4ai_mcp.config import AppConfig


class FakeMCP:
    def __init__(self):
        self.run_kwargs = {}

    def run(self, **kwargs):
        self.run_kwargs = kwargs


class FakeService:
    pass


def test_run_server_uses_validated_bind_config(monkeypatch, tmp_path):
    config = AppConfig(
        bind_host="127.0.0.1", bind_port=12345, database_path=tmp_path / "p.db"
    )
    fake = FakeMCP()
    monkeypatch.setattr(main_module, "create_server", lambda *_a, **_k: fake)
    main_module.run_server(config, service=FakeService())
    assert fake.run_kwargs["host"] == "127.0.0.1"
    assert fake.run_kwargs["port"] == 12345
    assert fake.run_kwargs["path"] == "/mcp"
    assert fake.run_kwargs["host_origin_protection"] is True
    assert fake.run_kwargs["allowed_hosts"] == ["127.0.0.1:12345", "localhost:12345"]


def test_run_server_builds_and_owns_service_when_not_injected(monkeypatch, tmp_path):
    config = AppConfig(database_path=tmp_path / "p.db")
    fake = FakeMCP()
    created = []
    monkeypatch.setattr(main_module, "CrawlService", lambda config: FakeService())

    def fake_create(service, lifespan=None):
        created.append((service, lifespan))
        return fake

    monkeypatch.setattr(main_module, "create_server", fake_create)
    main_module.run_server(config)
    assert isinstance(created[0][0], FakeService)
    assert created[0][1] is not None
    assert fake.run_kwargs["host"] == "127.0.0.1"
    assert fake.run_kwargs["port"] == 11236


def test_main_loads_config_once_and_calls_run_server(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(
        main_module,
        "load_config",
        lambda *_a, **_k: AppConfig(database_path=tmp_path / "p.db"),
    )
    monkeypatch.setattr(main_module, "run_server", lambda config: calls.append(config))
    main_module.main()
    assert len(calls) == 1
    assert isinstance(calls[0], AppConfig)


def test_run_server_appends_extra_allowed_hosts(monkeypatch, tmp_path):
    config = AppConfig(
        bind_host="127.0.0.1",
        bind_port=12345,
        database_path=tmp_path / "p.db",
        extra_allowed_hosts=["*.ts.net"],
    )
    fake = FakeMCP()
    monkeypatch.setattr(main_module, "create_server", lambda *_a, **_k: fake)
    main_module.run_server(config, service=FakeService())
    assert fake.run_kwargs["allowed_hosts"] == [
        "127.0.0.1:12345",
        "localhost:12345",
        "*.ts.net",
    ]
