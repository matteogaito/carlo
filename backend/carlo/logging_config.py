import logging


def configure_api_logging(log_level: str) -> None:
    logging.getLogger().setLevel(log_level)


def configure_logging(log_level: str = "INFO") -> None:
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
        force=True,
    )
