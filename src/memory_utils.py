import os
import re
import time


CURRENT_VERSION_RE = re.compile(r"(## Current Version\s*\n)([^\n]+)")
VERSION_HEADER_RE = re.compile(r"^###\s+(v\d+\.\d+)\s*$", re.MULTILINE)


def memory_path(agent_dir: str) -> str:
    return os.path.join(agent_dir, "memory.md")


def notes_path(agent_dir: str) -> str:
    return os.path.join(agent_dir, "notes.md")


def init_memory(agent_dir: str, agent_name: str, origin: str, change: str, why: str, version: str = "v1.0") -> None:
    content = "\n".join([
        f"# Strategy Memory for {agent_name}",
        "",
        "## Current Version",
        version,
        "",
        "## Change Log",
        "",
        f"### {version}",
        f"Change: {change}",
        f"Why: {why}",
        "Status: active",
        f"Origin: {origin}",
        f"Recorded At: {time.strftime('%Y-%m-%d %H:%M:%S')}",
        "",
    ])
    with open(memory_path(agent_dir), "w") as f:
        f.write(content)


def read_memory(agent_dir: str) -> str:
    path = memory_path(agent_dir)
    if os.path.exists(path):
        with open(path) as f:
            return f.read()
    return ""


def read_legacy_notes(agent_dir: str) -> str:
    path = notes_path(agent_dir)
    if os.path.exists(path):
        with open(path) as f:
            return f.read()
    return ""


def read_memory_bundle(agent_dir: str) -> str:
    parts = []
    memory = read_memory(agent_dir).strip()
    if memory:
        parts.append(memory)
    legacy_notes = read_legacy_notes(agent_dir).strip()
    if legacy_notes:
        parts.append(f"## Supplemental Notes\n{legacy_notes}")
    return "\n\n".join(parts)


def current_memory_version(agent_dir: str, default: str = "v1.0") -> str:
    content = read_memory(agent_dir)
    if not content:
        return default
    match = CURRENT_VERSION_RE.search(content)
    if match:
        return match.group(2).strip()
    versions = VERSION_HEADER_RE.findall(content)
    return versions[-1] if versions else default


def next_memory_version(version: str) -> str:
    match = re.fullmatch(r"v(\d+)\.(\d+)", version.strip())
    if not match:
        return "v1.0"
    major = int(match.group(1))
    minor = int(match.group(2))
    return f"v{major}.{minor + 1}"


def _set_current_version(content: str, version: str) -> str:
    if CURRENT_VERSION_RE.search(content):
        return CURRENT_VERSION_RE.sub(rf"\1{version}", content, count=1)
    if content.endswith("\n"):
        sep = ""
    else:
        sep = "\n"
    return content + sep + "\n".join(["## Current Version", version, ""])


def set_current_memory_version(agent_dir: str, version: str) -> None:
    content = read_memory(agent_dir)
    if not content:
        return
    with open(memory_path(agent_dir), "w") as f:
        f.write(_set_current_version(content, version))


def append_memory_entry(agent_dir: str, version: str, change: str, why: str, status: str, origin: str | None = None) -> None:
    content = read_memory(agent_dir)
    if not content:
        init_memory(agent_dir, os.path.basename(agent_dir), origin or "unknown", change, why, version=version)
        return
    content = _set_current_version(content, version)
    if not content.endswith("\n"):
        content += "\n"
    entry_lines = [
        "",
        f"### {version}",
        f"Change: {change}",
        f"Why: {why}",
        f"Status: {status}",
    ]
    if origin:
        entry_lines.append(f"Origin: {origin}")
    entry_lines.append(f"Recorded At: {time.strftime('%Y-%m-%d %H:%M:%S')}")
    entry_lines.append("")
    with open(memory_path(agent_dir), "w") as f:
        f.write(content + "\n".join(entry_lines))


def append_memory_outcome(agent_dir: str, version: str, status: str, summary: str, current_version: str | None = None) -> None:
    content = read_memory(agent_dir)
    if not content:
        return
    if current_version:
        content = _set_current_version(content, current_version)
    if not content.endswith("\n"):
        content += "\n"
    entry = "\n".join([
        "",
        f"### {version} Outcome",
        f"Status: {status}",
        f"Summary: {summary}",
        f"Recorded At: {time.strftime('%Y-%m-%d %H:%M:%S')}",
        "",
    ])
    with open(memory_path(agent_dir), "w") as f:
        f.write(content + entry)
