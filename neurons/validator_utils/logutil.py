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


class RewardLog:
    """Logger for the own-winner reward path (emission_manager and the validator's winner logic).
    While the challenge server runs the record ladder that path still tracks this validator's
    best result, but it no longer decides the weights, so its lines ("winner reward active",
    "burning emissions", ...) would contradict the ladder: they go to DEBUG. On servers without
    the ladder they stay at INFO."""

    ladder_active = False

    def __init__(self, logger: logging.Logger):
        self._logger = logger

    def info(self, msg, *args, **kwargs):
        (self._logger.debug if RewardLog.ladder_active else self._logger.info)(msg, *args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._logger, name)
