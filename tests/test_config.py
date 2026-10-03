from anito.config import Config, parse_url


def test_parse_url_rejects_invalid_types() -> None:
    for bad in (None, [], {}, 0):
        try:
            parse_url(bad)
        except ValueError:
            pass
        else:
            raise AssertionError(f"parse_url({bad!r}) should reject invalid input")


def test_config_keeps_default_on_bad_url() -> None:
    cfg = Config.from_dict({"ollama_url": None})
    assert cfg.ollama_url == "http://localhost:11434"
