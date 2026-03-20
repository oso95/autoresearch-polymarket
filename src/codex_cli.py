import asyncio
import os
import tempfile


DEFAULT_PREDICTION_MODEL = "gpt-5.4"
DEFAULT_EVOLUTION_MODEL = "gpt-5.4"
DEFAULT_ANALYSIS_MODEL = "gpt-5.4"
DEFAULT_REASONING_EFFORT = "high"

LEGACY_MODEL_ALIASES = {
    "haiku": DEFAULT_PREDICTION_MODEL,
    "sonnet": DEFAULT_EVOLUTION_MODEL,
    "opus": DEFAULT_ANALYSIS_MODEL,
}


def normalize_model_name(model: str | None, default: str = DEFAULT_PREDICTION_MODEL) -> str:
    if not model:
        return default
    return LEGACY_MODEL_ALIASES.get(model, model)


async def run_codex_prompt(prompt: str, model: str | None, cwd: str, timeout: int) -> str:
    resolved_model = normalize_model_name(model)
    fd, output_path = tempfile.mkstemp(prefix="codex-last-message-", suffix=".txt")
    os.close(fd)

    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(
            "codex",
            "-a",
            "never",
            "exec",
            "--ephemeral",
            "--color",
            "never",
            "-c",
            f'model_reasoning_effort="{DEFAULT_REASONING_EFFORT}"',
            "-m",
            resolved_model,
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
            raise RuntimeError(detail or f"codex exec failed with exit code {proc.returncode}")

        with open(output_path) as f:
            return f.read().strip()
    except FileNotFoundError as exc:
        raise RuntimeError("Codex CLI not found. Ensure 'codex' is on PATH.") from exc
    finally:
        if os.path.exists(output_path):
            os.unlink(output_path)
