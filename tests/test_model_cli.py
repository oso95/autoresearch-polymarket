import asyncio

import src.model_cli as model_cli


def test_resolve_model_runtime_defaults_to_codex():
    runtime = model_cli.resolve_model_runtime(None, provider="codex")
    assert runtime.provider == "codex"
    assert runtime.model == "gpt-5.4"


def test_resolve_model_runtime_infers_claude_from_alias():
    runtime = model_cli.resolve_model_runtime("sonnet")
    assert runtime.provider == "claude"
    assert runtime.model == "sonnet"


def test_resolve_model_runtime_prefixed_identifier_wins():
    runtime = model_cli.resolve_model_runtime("claude:opus", provider="codex")
    assert runtime.provider == "claude"
    assert runtime.model == "opus"


def test_normalize_model_name_uses_provider_defaults():
    assert model_cli.normalize_model_name(None, provider="claude", kind="analysis") == "opus"
    assert model_cli.normalize_model_name(None, provider="codex", kind="prediction") == "gpt-5.4"


def test_run_model_prompt_dispatches_to_claude(monkeypatch):
    calls = {}

    async def fake_claude(prompt: str, model: str, cwd: str, timeout: int) -> str:
        calls["claude"] = {
            "prompt": prompt,
            "model": model,
            "cwd": cwd,
            "timeout": timeout,
        }
        return "ok"

    async def fake_codex(prompt: str, model: str, cwd: str, timeout: int) -> str:
        raise AssertionError("codex runner should not be used")

    monkeypatch.setattr(model_cli, "_run_claude_prompt", fake_claude)
    monkeypatch.setattr(model_cli, "_run_codex_prompt", fake_codex)

    result = asyncio.run(model_cli.run_model_prompt("hello", "sonnet", "/tmp/agent", 12))

    assert result == "ok"
    assert calls["claude"]["model"] == "sonnet"
    assert calls["claude"]["cwd"] == "/tmp/agent"
