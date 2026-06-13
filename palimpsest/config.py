# palimpsest/config.py
import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

class ConfigError(Exception):
    pass

@dataclass(frozen=True)
class Config:
    raw: dict
    storage_root: Path
    db_path: Path
    broker: dict
    mcp: dict
    harvest: dict
    ocr: dict
    features: dict
    embed: dict
    gapjoin: dict
    models: dict
    nodes: dict
    orchestrator: dict

def load(path: str | Path | None = None) -> Config:
    if not path:
        path = os.environ.get("PALIMPSEST_CONFIG", "config.toml")
    p = Path(path)
    if not p.exists():
        raise ConfigError(f"Config file not found at {p}")
    with open(p, "rb") as f:
        data = tomllib.load(f)
    
    # Validation
    required = ["storage", "db", "broker", "mcp", "harvest", "ocr", "features", "embed", "gapjoin", "models", "nodes"]
    missing = [sec for sec in required if sec not in data]
    if missing:
        raise ConfigError(f"Missing sections: {', '.join(missing)}")
    
    root_str = data["storage"]["root"]
    db_path_str = data["db"]["path"].replace("{storage.root}", root_str)
    
    return Config(
        raw=data,
        storage_root=Path(root_str),
        db_path=Path(db_path_str),
        broker=data["broker"],
        mcp=data["mcp"],
        harvest=data["harvest"],
        ocr=data["ocr"],
        features=data["features"],
        embed=data["embed"],
        gapjoin=data["gapjoin"],
        models=data["models"],
        nodes=data["nodes"],
        orchestrator=data.get("orchestrator", {})
    )
