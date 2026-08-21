import logging
import os
from pathlib import Path
from typing import Union


def setup_logger(
    log_dir: Path,
    log_file_name: str,
    level: Union[int, str] = logging.INFO,
    print_to_console: bool = True,
) -> logging.Logger:
    if not log_dir.exists():
        log_dir.mkdir(parents=True, exist_ok=True)

    log_path = os.path.join(log_dir, log_file_name)

    handlers = [logging.FileHandler(log_path, encoding="utf-8")]
    if print_to_console:
        handlers.append(logging.StreamHandler())

    logging.basicConfig(
        level=level,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        handlers=handlers,
    )

    logger = logging.getLogger(str(log_path))

    return logger


def get_propagate_logger(
    log_dir: Path,
    log_file: str,
    level: Union[int, str] = logging.INFO,
) -> logging.Logger:
    if not log_dir.exists():
        log_dir.mkdir(parents=True, exist_ok=True)

    id = f"{log_dir.name}/{log_file}"
    propagate_logger = logging.getLogger(id)
    if propagate_logger.hasHandlers():
        return propagate_logger

    propagate_logger.setLevel(level)

    log_path = os.path.join(log_dir, log_file)
    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setLevel(logging.INFO)
    formatter = logging.Formatter(
        "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    )
    file_handler.setFormatter(formatter)

    propagate_logger.addHandler(file_handler)

    propagate_logger.propagate = True

    return propagate_logger


def get_non_propagate_logger(
    log_dir: Path,
    log_file: str,
    level: Union[int, str] = logging.INFO,
    print_to_console: bool = True,
) -> logging.Logger:
    if not log_dir.exists():
        log_dir.mkdir(parents=True, exist_ok=True)

    id = f"{log_dir.name}/{log_file}"
    non_propagate_logger = logging.getLogger(id)
    if non_propagate_logger.handlers:
        return non_propagate_logger

    non_propagate_logger.setLevel(level)

    log_path = os.path.join(log_dir, log_file)
    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setLevel(logging.INFO)
    formatter = logging.Formatter(
        "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    )
    file_handler.setFormatter(formatter)

    non_propagate_logger.addHandler(file_handler)
    if print_to_console:
        console_handler = logging.StreamHandler()
        console_handler.setFormatter(formatter)
        non_propagate_logger.addHandler(console_handler)

    non_propagate_logger.propagate = False

    return non_propagate_logger
