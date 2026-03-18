import json
import os
import fcntl
from pathlib import Path


def atomic_write_json(path: str, data: dict) -> None:
    tmp_path = path + ".tmp"
    try:
        content = json.dumps(data, indent=2)
        with open(tmp_path, "w") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.rename(tmp_path, path)
    except:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise


def atomic_append_jsonl(path: str, record: dict) -> None:
    line = json.dumps(record, separators=(",", ":")) + "\n"
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            f.write(line)
            f.flush()
            os.fsync(f.fileno())
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)


def read_jsonl(path: str) -> list[dict]:
    if not os.path.exists(path):
        return []
    results = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                results.append(json.loads(line))
    return results
