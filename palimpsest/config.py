import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict

if sys.version_info < (3, 12):
    import tomli as tomllib
else:
    import tomllib

class ConfigError(Exception):
    pass

@dataclass(frozen=True)
class Config:
    raw: Dict[str, Any]
    storage_root: Path
    db_path: Path
    broker: Dict[str, Any]
    mcp: Dict[str, Any]
    harvest: Dict[str, Any]
    ocr: Dict[str, Any]
    features: Dict[str, Any]
    embed: Dict[str, Any]
    gapjoin: Dict[str, Any]
    models: Dict[str, Any]
    nodes: Dict[str, Any]
    orchestrator: Dict[str, Any]

def load(path: str | Path | None = None) -> Config:
    if not path:
        path = os.environ.get("PALIMPSEST_CONFIG", Path(__file__).parent.parent / "config.toml")
    
    path = Path(path)
    if not path.exists():
        raise ConfigError(f"Config file not found: {path}")

    with open(path, "rb") as f:
        data = tomllib.load(f)

    # Validate top-level keys
    required_sections = ["storage", "db", "broker", "mcp", "harvest", "ocr", "features", "embed", "gapjoin", "models", "nodes"]
    missing = [s for s in required_sections if s not in data]
    if missing:
        raise ConfigError(f"Missing config sections: {', '.join(missing)}")

    # Expand variables
    def expand_vars(value: Any, context: Dict[str, str]) -> Any:
        if isinstance(value, str) and "{storage.root}" in value:
            return value.replace("{storage.root}", context["storage.root"])
        return value

    context = {"storage.root": data["storage"]["root"]}
    
    db_path = Path(expand_vars(data["db"]["path"], context))

    return Config(
        raw=data,
        storage_root=Path(data["storage"]["root"]),
        db_path=db_path,
        broker=data["broker"],
        mcp=data["mcp"],
        harvest=data["harvest"],
        ocr=data["ocr"],
        features=data["features"],
        embed=data["embed"],
        gapjoin=data["gapjoin"],
        models=data["models"],
        nodes=data["nodes"],
        orchestrator=data.get("orchestrator", {}),
    )
