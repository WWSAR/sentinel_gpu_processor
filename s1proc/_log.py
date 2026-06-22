import logging
import logging.config


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
                "propagate": False,
            },
        },
    }

    logging.config.dictConfig(logging_config)
    return logging.getLogger(name)


# Create a method to change the logging level dynamically
def set_logging_level(logger, level):
    logger.setLevel(level.upper())  # Set the new logging level
