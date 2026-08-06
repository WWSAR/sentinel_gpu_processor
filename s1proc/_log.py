import atexit
import logging
import logging.config

# Shared FileHandler attached to every s1proc logger once file logging is
# enabled (see enable_file_logging).  Kept as a module global so it is not
# garbage-collected and can be swapped/replaced on a second call.
_file_handler: logging.FileHandler | None = None


def _attach_file_handler(logger: logging.Logger) -> None:
    """Attach the shared file handler to *logger* if file logging is on."""
    if _file_handler is not None and _file_handler not in logger.handlers:
        logger.addHandler(_file_handler)


def setup_logger(name, level="INFO"):
    logging_config = {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {
            "standard": {
                "format": "[%(asctime)s] [%(levelname)s|%(module)s|L%(lineno)d]"
                + "%(message)s",
                "datefmt": "%m/%d %H:%M:%S",
            },
        },
        "handlers": {
            "console": {
                "class": "logging.StreamHandler",
                "formatter": "standard",
                "stream": "ext://sys.stdout",
            },
        },
        "loggers": {
            name: {  # Root logger configuration
                "level": level.upper(),
                "handlers": ["console"],
                # Each module logger emits on its own StreamHandler; records
                # are NOT propagated so a single stdout line is printed once.
                "propagate": False,
            },
        },
    }

    logging.config.dictConfig(logging_config)
    logger = logging.getLogger(name)
    _attach_file_handler(logger)
    return logger


def enable_file_logging(log_path):
    """Mirror all s1proc log output to *log_path* (in addition to stdout).

    Creates a single ``FileHandler`` and attaches it to every current and
    future s1proc logger.  Because each module logger keeps its own
    ``StreamHandler`` and ``propagate=False``, stdout output is unchanged and
    every record is written to the file exactly once.  Calling this function a
    second time replaces the previous file handler.
    """
    global _file_handler

    if _file_handler is not None:
        _detach_file_handler()

    _file_handler = logging.FileHandler(log_path, encoding="utf-8")
    _file_handler.setLevel(logging.DEBUG)
    _file_handler.setFormatter(
        logging.Formatter(
            "[%(asctime)s] [%(levelname)s|%(module)s|L%(lineno)d]%(message)s",
            datefmt="%m/%d %H:%M:%S",
        )
    )

    for name, lg in list(logging.Logger.manager.loggerDict.items()):
        if isinstance(lg, logging.Logger) and name.startswith("s1proc"):
            if _file_handler not in lg.handlers:
                lg.addHandler(_file_handler)


def _detach_file_handler():
    """Remove and close the current shared file handler from all loggers."""
    global _file_handler
    if _file_handler is None:
        return
    for name, lg in list(logging.Logger.manager.loggerDict.items()):
        if isinstance(lg, logging.Logger) and _file_handler in lg.handlers:
            lg.removeHandler(_file_handler)
    try:
        _file_handler.close()
    except Exception:
        pass
    _file_handler = None


@atexit.register
def _close_file_handler():
    _detach_file_handler()


# Create a method to change the logging level dynamically
def set_logging_level(logger, level):
    logger.setLevel(level.upper())  # Set the new logging level


logger = setup_logger("s1proc")
