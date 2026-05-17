import logging
from pythonjsonlogger import jsonlogger


def setup_logging(level: int = logging.INFO) -> None:
    logger = logging.getLogger()
    logger.setLevel(level)

    for handler in logger.handlers[:]:
        logger.removeHandler(handler)

    handler = logging.StreamHandler()
    formatter = jsonlogger.JsonFormatter(
        fmt="%(asctime)s %(levelname)s %(name)s %(message)s",
        rename_fields={"asctime": "timestamp", "levelname": "level"},
    )
    handler.setFormatter(formatter)
    logger.addHandler(handler)
