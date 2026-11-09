from . import auth
from . import client
from . import debug
from . import orders
from . import streaming
from . import utils

from .version import version as __version__

# The token errors and what reads them, at the top as well as in utils.
from .utils import (
        LoginErrorKind, LoginExchangeError, NoBrowserAvailable, PaceRequest,
        TokenErrorKind, TokenFreshness, TokenRefreshError, find_token_error,
        server_error_code)

LOG_REDACTOR = debug.LogRedactor()

# The dependencies' loggers exist once the imports above have run.
from . import _redact  # noqa: E402
_redact.protect_dependencies()

__all__ = [
    'LOG_REDACTOR', 'LoginErrorKind', 'LoginExchangeError',
    'NoBrowserAvailable', 'PaceRequest', 'TokenErrorKind', 'TokenFreshness',
    'TokenRefreshError', 'auth', 'client', 'debug', 'find_token_error',
    'orders', 'server_error_code', 'streaming', 'utils', 'version',
]
