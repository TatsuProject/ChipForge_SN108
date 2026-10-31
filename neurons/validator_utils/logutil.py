"""Log a state line at INFO only when it changes (DEBUG otherwise), so lines evaluated on every
10-second loop don't bury the ones that matter."""
import logging

_last: dict = {}


def info_on_change(logger: logging.Logger, key: str, message: str) -> None:
    if _last.get(key) != message:
        _last[key] = message
        logger.info(message)
    else:
        logger.debug(message)
