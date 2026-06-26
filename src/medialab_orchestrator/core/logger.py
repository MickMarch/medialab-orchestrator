"""Logging configuration providing console output for the application."""

import logging
import sys

LOG_FORMAT: str = "%(asctime)s | %(levelname)-8s | %(module)s:%(funcName)s:%(lineno)d - %(message)s"
DATE_FORMAT: str = "%Y-%m-%d %H:%M:%S"


def setup_logger() -> logging.Logger:
    logger: logging.Logger = logging.getLogger("medialab_orchestrator")
    logger.setLevel(logging.INFO)

    if not logger.handlers:
        formatter: logging.Formatter = logging.Formatter(fmt=LOG_FORMAT, datefmt=DATE_FORMAT)
        console_handler: logging.StreamHandler = logging.StreamHandler(sys.stdout)
        console_handler.setFormatter(formatter)
        logger.addHandler(console_handler)

    return logger


app_logger: logging.Logger = setup_logger()
