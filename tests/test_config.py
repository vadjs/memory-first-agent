from agent.config import Settings, get_settings


def test_defaults():
    s = Settings(_env_file=None)
    s2 = Settings(_env_file=None)
    assert s.chat_deployment == "gpt-5.6-luna"
    assert s.utility_deployment == "gpt-5-nano"
    assert s.embed_deployment == "text-embedding-3-small"
    assert s.cache_threshold == 0.85
    assert s.chunk_threshold == 0.70
    assert s.borderline_floor == 0.55
    assert s.slow_ttl_days == 7
    assert s.top_k == 5
    assert s.rate_limit_per_min == 30
    assert s.max_query_chars == 2000
    assert s.degraded_answers is True
    assert s == s2


def test_env_override(monkeypatch):
    monkeypatch.setenv("CHUNK_THRESHOLD", "0.62")
    monkeypatch.setenv("REDIS_URL", "redis://example:6380")
    s = Settings(_env_file=None)
    assert s.chunk_threshold == 0.62
    assert s.redis_url == "redis://example:6380"


def test_get_settings_cached():
    get_settings.cache_clear()
    assert get_settings() is get_settings()
