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


def read_jsonl_tail(path: str, n: int = 20) -> list[dict]:
    """Read only the last N lines of a JSONL file efficiently."""
    if not os.path.exists(path):
        return []
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)  # Seek to end
            size = f.tell()
            if size == 0:
                return []
            # Read last chunk (generous estimate: 500 bytes per line)
            chunk_size = min(size, n * 500)
            f.seek(max(0, size - chunk_size))
            data = f.read().decode("utf-8")
        lines = [l.strip() for l in data.split("\n") if l.strip()]
        results = []
        for line in lines[-n:]:
            try:
                results.append(json.loads(line))
            except json.JSONDecodeError:
                continue  # Skip partial first line from mid-seek
        return results
    except Exception:
        return read_jsonl(path)[-n:]  # Fallback
