# palimpsest/tasks/__init__.py
from typing import Callable, Dict

from palimpsest.config import Config as Config  # re-exported for task modules


class PermanentJobError(Exception):
    """An error indicating that the job has failed permanently and should not be retried."""
    pass

# Handler signature: fn(cfg, job, *, lost_evt=None, shutdown_event=None) -> result_dict
# lost_evt and shutdown_event are threading.Event instances passed by the worker so that
# long-running handlers can check for lease loss or graceful shutdown between iterations
# and abort early rather than grinding through work the broker will discard.
HANDLERS: Dict[str, Callable[..., dict]] = {}

def handler(job_type: str):
    """Decorator: @handler('ocr') registers fn(cfg, job, *, lost_evt, shutdown_event) -> result_dict."""
    def decorator(func: Callable[..., dict]):
        HANDLERS[job_type] = func
        return func
    return decorator

# Import submodules to trigger registration
try:
    import palimpsest.tasks.ocr
except ImportError:
    pass

try:
    import palimpsest.tasks.features
except ImportError:
    pass

try:
    import palimpsest.tasks.embed  # noqa: F401
except ImportError:
    pass

try:
    import palimpsest.tasks.gapjoin  # noqa: F401
except ImportError:
    pass

try:
    import palimpsest.tasks.brief  # noqa: F401
except ImportError:
    pass
