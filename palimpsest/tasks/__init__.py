# palimpsest/tasks/__init__.py
from typing import Callable, Dict

from palimpsest.config import Config


class PermanentJobError(Exception):
    """An error indicating that the job has failed permanently and should not be retried."""
    pass

HANDLERS: Dict[str, Callable[[Config, dict], dict]] = {}

def handler(job_type: str):
    """Decorator: @handler('ocr') registers fn(cfg, job) -> result_dict."""
    def decorator(func: Callable[[Config, dict], dict]):
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
