"""수집기. 원시 데이터를 적재만 하고 해석하지 않는다."""

from .player_poller import PlayerPoller, TickOutcome

__all__ = ["PlayerPoller", "TickOutcome"]
