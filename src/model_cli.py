"""Provider-agnostic non-interactive model runtime.

This module dispatches prompts to either the Codex CLI or Claude Code CLI and
normalizes model/provider selection for the rest of the system.
"""

import asyncio
import json
import os
import tempfile
from dataclasses import dataclass


PROVIDER_ALIASES = {
    "anthropic": "claude",
    "claude": "claude",
    "codex": "codex",
    "openai": "codex",
}

DEFAULT_MODELS = {
    "claude": {
        "analysis": "opus",
        "evolution": "sonnet",
        "prediction": "sonnet",
    },
    "codex": {
        "analysis": "gpt-5.4",
        "evolution": "gpt-5.4",
        "prediction": "gpt-5.4",
    },
}

CLAUDE_MODEL_NAMES = {"haiku", "opus", "sonnet"}

DEFAULT_REASONING_EFFORT = os.environ.get("AUTORESEARCH_REASONING_EFFORT", "high")
DEFAULT_CLAUDE_EFFORT = os.environ.get("AUTORESEARCH_CLAUDE_EFFORT", DEFAULT_REASONING_EFFORT)


def normalize_provider(provider: str | None) -> str:
    raw = (provider or "codex").strip().lower()
    normalized = PROVIDER_ALIASES.get(raw)
    if normalized is None:
        raise ValueError(f"Unsupported model provider: {provider}")
    return normalized


DEFAULT_PROVIDER = normalize_provider(os.environ.get("AUTORESEARCH_MODEL_PROVIDER", "codex"))
DEFAULT_PREDICTION_MODEL = DEFAULT_MODELS[DEFAULT_PROVIDER]["prediction"]
DEFAULT_EVOLUTION_MODEL = DEFAULT_MODELS[DEFAULT_PROVIDER]["evolution"]
DEFAULT_ANALYSIS_MODEL = DEFAULT_MODELS[DEFAULT_PROVIDER]["analysis"]


@dataclass(frozen=True)
class ModelRuntime:
    provider: str
    model: str


def get_default_model(provider: str | None = None, kind: str = "prediction") -> str:
    resolved_provider = normalize_provider(provider or DEFAULT_PROVIDER)
    defaults = DEFAULT_MODELS[resolved_provider]
    if kind not in defaults:
        raise ValueError(f"Unsupported model kind: {kind}")
    return defaults[kind]


def _split_model_identifier(model: str | None) -> tuple[str | None, str | None]:
    if not model:
        return None, None
    raw = model.strip()
    if not raw:
        return None, None
    if ":" not in raw:
        return None, raw
    prefix, remainder = raw.split(":", 1)
    provider = PROVIDER_ALIASES.get(prefix.strip().lower())
    if provider is None:
        return None, raw
    remainder = remainder.strip()
    return provider, (remainder or None)


def infer_provider_from_model(model: str | None) -> str | None:
    explicit_provider, raw_model = _split_model_identifier(model)
    if explicit_provider:
        return explicit_provider
    if not raw_model:
        return None
    lowered = raw_model.strip().lower()
    if lowered in CLAUDE_MODEL_NAMES or lowered.startswith("claude-"):
        return "claude"
    return "codex"


def normalize_model_name(
    model: str | None,
    default: str | None = None,
    provider: str | None = None,
    kind: str = "prediction",
) -> str:
    _, raw_model = _split_model_identifier(model)
    if raw_model:
        return raw_model
    if default:
        return default
    return get_default_model(provider=provider, kind=kind)


def resolve_model_runtime(
    model: str | None,
    provider: str | None = None,
    kind: str = "prediction",
) -> ModelRuntime:
    explicit_provider, raw_model = _split_model_identifier(model)
    inferred_provider = explicit_provider or infer_provider_from_model(raw_model)
    resolved_provider = normalize_provider(inferred_provider or provider or DEFAULT_PROVIDER)
    resolved_model = normalize_model_name(raw_model, provider=resolved_provider, kind=kind)
    return ModelRuntime(provider=resolved_provider, model=resolved_model)


async def _run_codex_prompt(prompt: str, model: str, cwd: str, timeout: int) -> str:
    fd, output_path = tempfile.mkstemp(prefix="codex-last-message-", suffix=".txt")
    os.close(fd)

    proc = None
    try:
        codex_bin = os.environ.get("AUTORESEARCH_CODEX_BIN", "codex")
        proc = await asyncio.create_subprocess_exec(
            codex_bin,
            "-a",
            "never",
            "exec",
            "--ephemeral",
            "--color",
            "never",
            "-c",
            f'model_reasoning_effort="{DEFAULT_REASONING_EFFORT}"',
            "-m",
            model,
            "-s",
            "danger-full-access",
            "-C",
            cwd,
            "-o",
            output_path,
            "-",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(prompt.encode("utf-8")),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.communicate()
            raise

        if proc.returncode != 0:
            detail = stderr.decode("utf-8", errors="replace").strip()
            if not detail:
                detail = stdout.decode("utf-8", errors="replace").strip()
            raise RuntimeError(detail or f"codex exec failed with exit code {proc.returncode}")

        with open(output_path) as f:
            return f.read().strip()
    except FileNotFoundError as exc:
        raise RuntimeError("Codex CLI not found. Ensure 'codex' is on PATH.") from exc
    finally:
        if os.path.exists(output_path):
            os.unlink(output_path)


async def _run_claude_prompt(prompt: str, model: str, cwd: str, timeout: int) -> str:
    proc = None
    try:
        claude_bin = os.environ.get("AUTORESEARCH_CLAUDE_BIN", "claude")
        proc = await asyncio.create_subprocess_exec(
            claude_bin,
            "-p",
            "--output-format",
            "json",
            "--no-session-persistence",
            "--tools",
            "",
            "--effort",
            DEFAULT_CLAUDE_EFFORT,
            "--model",
            model,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(prompt.encode("utf-8")),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.communicate()
            raise

        if proc.returncode != 0:
            detail = stderr.decode("utf-8", errors="replace").strip()
            if not detail:
                detail = stdout.decode("utf-8", errors="replace").strip()
            raise RuntimeError(detail or f"claude print failed with exit code {proc.returncode}")

        raw_output = stdout.decode("utf-8", errors="replace").strip()
        if not raw_output:
            return ""
        try:
            payload = json.loads(raw_output)
        except json.JSONDecodeError:
            return raw_output
        result = payload.get("result")
        return result.strip() if isinstance(result, str) else raw_output
    except FileNotFoundError as exc:
        raise RuntimeError("Claude CLI not found. Ensure 'claude' is on PATH.") from exc


async def run_model_prompt(
    prompt: str,
    model: str | None,
    cwd: str,
    timeout: int,
    provider: str | None = None,
    kind: str = "prediction",
) -> str:
    runtime = resolve_model_runtime(model, provider=provider, kind=kind)
    if runtime.provider == "claude":
        return await _run_claude_prompt(prompt, runtime.model, cwd, timeout)
    return await _run_codex_prompt(prompt, runtime.model, cwd, timeout)


async def run_codex_prompt(prompt: str, model: str | None, cwd: str, timeout: int) -> str:
    return await run_model_prompt(prompt, model, cwd, timeout)
