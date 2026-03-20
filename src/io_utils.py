import json
import os
import fcntl
import tempfile
import logging
from pathlib import Path

logger = logging.getLogger(__name__)


def atomic_write_json(path: str, data: dict) -> None:
    tmp_path = None
    try:
        content = json.dumps(data, indent=2)
        parent = Path(path).parent
        parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(prefix=Path(path).name + ".", suffix=".tmp", dir=parent)
        with os.fdopen(fd, "w") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except Exception:
        if tmp_path and os.path.exists(tmp_path):
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


def atomic_write_jsonl(path: str, records: list[dict]) -> None:
    parent = Path(path).parent
    parent.mkdir(parents=True, exist_ok=True)
    lock_path = parent / (Path(path).name + ".lock")
    tmp_path = None
    with open(lock_path, "w") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            fd, tmp_path = tempfile.mkstemp(prefix=Path(path).name + ".", suffix=".tmp", dir=parent)
            with os.fdopen(fd, "w") as f:
                for record in records:
                    f.write(json.dumps(record, separators=(",", ":")) + "\n")
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, path)
        except Exception:
            if tmp_path and os.path.exists(tmp_path):
                os.unlink(tmp_path)
            raise
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def read_jsonl(path: str) -> list[dict]:
    if not os.path.exists(path):
        return []
    results = []
    with open(path) as f:
        for line_no, line in enumerate(f, 1):
            line = line.strip()
            if line:
                try:
                    results.append(json.loads(line))
                except json.JSONDecodeError:
                    logger.warning("Skipping malformed JSONL line in %s at line %s", path, line_no)
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
