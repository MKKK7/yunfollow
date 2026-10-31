"""Register the FNOS edition extensions with the upstream plugin registry."""
from ..fnos.sources import FnosSources  # noqa: F401
from ..fnos.policy import FnosDecision  # noqa: F401
from ..fnos.media import FnosMedia  # noqa: F401
from ..fnos.bot import FnosNotify  # noqa: F401
from ..fnos.preflight import VerifiedParser  # noqa: F401
