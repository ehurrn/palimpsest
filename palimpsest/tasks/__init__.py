# palimpsest/tasks/__init__.py
from typing import Callable, Dict, Any
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
