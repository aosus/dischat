from dischat.config import FileConfig, Settings


def test_config_directory_is_treated_as_missing(tmp_path) -> None:
    config_directory = tmp_path / "config.yaml"
    config_directory.mkdir()

    settings = Settings(CONFIG_FILE=config_directory)

    assert settings.load_file_config() == FileConfig()
