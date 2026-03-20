import json
import asyncio
import logging
import os
import re
import tempfile

from src.codex_cli import (
    DEFAULT_ANALYSIS_MODEL,
    DEFAULT_EVOLUTION_MODEL,
    DEFAULT_PREDICTION_MODEL,
    run_codex_prompt,
)

logger = logging.getLogger(__name__)

def parse_prediction_response(response: str) -> dict | None:
    try:
        data = json.loads(response)
        if "prediction" in data and data["prediction"] in ("Up", "Down"):
            return {
                "prediction": data["prediction"],
                "confidence": data.get("confidence", 0.5),
                "reasoning": data.get("reasoning", ""),
            }
    except json.JSONDecodeError:
        pass
    json_match = re.search(r'\{[^}]*"prediction"\s*:\s*"(Up|Down)"[^}]*\}', response)
    if json_match:
        try:
            data = json.loads(json_match.group())
            return {
                "prediction": data["prediction"],
                "confidence": data.get("confidence", 0.5),
                "reasoning": data.get("reasoning", ""),
            }
        except json.JSONDecodeError:
            pass
    lower = response.lower()
    if "predict up" in lower or "prediction: up" in lower:
        return {"prediction": "Up", "confidence": 0.5, "reasoning": response[:200]}
    if "predict down" in lower or "prediction: down" in lower:
        return {"prediction": "Down", "confidence": 0.5, "reasoning": response[:200]}
    return None


class Predictor:
    # Model tiers: keep cheap fallback for predictions and a stronger tier for research work.
    MODEL_FAST = DEFAULT_PREDICTION_MODEL
    MODEL_BALANCED = DEFAULT_EVOLUTION_MODEL
    MODEL_STRONG = DEFAULT_ANALYSIS_MODEL

    def __init__(self, timeout_seconds: int = 90, model: str = DEFAULT_PREDICTION_MODEL):
        self.timeout = timeout_seconds
        self.model = model
        self.script_timeout = min(30, timeout_seconds)

    def _discover_executable_scripts(self, agent_dir: str, scripts: dict) -> list[str]:
        scripts_dir = os.path.join(agent_dir, "scripts")
        if not os.path.isdir(scripts_dir):
            return []
        script_paths = []
        for name in sorted(scripts):
            if not name.endswith(".py"):
                continue
            path = os.path.join(scripts_dir, name)
            if os.path.isfile(path):
                script_paths.append(path)
        return script_paths

    def _write_snapshot_file(self, agent_dir: str, snapshot: dict) -> str:
        fd, path = tempfile.mkstemp(prefix="snapshot-", suffix=".json", dir=agent_dir)
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(snapshot, f)
            return path
        except Exception:
            os.unlink(path)
            raise

    async def _run_script(self, script_path: str, snapshot_path: str) -> tuple[str, dict | None]:
        script_name = os.path.basename(script_path)
        try:
            proc = await asyncio.wait_for(
                asyncio.create_subprocess_exec(
                    "python3",
                    script_path,
                    snapshot_path,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=os.path.dirname(os.path.dirname(script_path)),
                ),
                timeout=self.script_timeout,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=self.script_timeout)
            if proc.returncode != 0:
                logger.warning(f"Script {script_name} failed: {stderr.decode()[:200]}")
                return script_name, None
            try:
                return script_name, json.loads(stdout.decode("utf-8").strip())
            except json.JSONDecodeError:
                logger.warning(f"Script {script_name} returned invalid JSON")
                return script_name, None
        except asyncio.TimeoutError:
            logger.warning(f"Script {script_name} timed out after {self.script_timeout}s")
            return script_name, None
        except Exception as e:
            logger.warning(f"Script {script_name} execution failed: {e}")
            return script_name, None

    def _normalize_direct_prediction(self, payload: dict) -> dict | None:
        prediction = payload.get("prediction", payload.get("direction"))
        if not isinstance(prediction, str):
            return None
        normalized = prediction.strip().capitalize()
        if normalized not in ("Up", "Down"):
            return None
        confidence = float(payload.get("confidence", 0.5))
        return {
            "prediction": normalized,
            "confidence": max(0.0, min(confidence, 1.0)),
            "reasoning": payload.get("reasoning", payload.get("rationale", "")),
        }

    def _extract_signal_score(self, payload: dict) -> tuple[float, float] | None:
        if not isinstance(payload, dict):
            return None
        confidence = float(payload.get("confidence", 1.0))
        for key, value in payload.items():
            if key in {"confidence", "metadata", "reasoning", "rationale", "prediction", "direction"}:
                continue
            if isinstance(value, (int, float)) and 0.0 <= float(value) <= 1.0:
                return float(value), max(0.0, min(confidence, 1.0))
        return None

    def _aggregate_script_outputs(self, outputs: list[tuple[str, dict | None]]) -> dict | None:
        direct_votes = []
        direct_confidences = []
        signal_votes = []
        reasons = []

        for script_name, payload in outputs:
            if payload is None:
                continue
            direct = self._normalize_direct_prediction(payload)
            if direct is not None:
                weight = direct["confidence"] or 0.5
                direct_votes.append(weight if direct["prediction"] == "Up" else -weight)
                direct_confidences.append(direct["confidence"])
                reasons.append(f"{script_name} => {direct['prediction']} ({weight:.2f})")
                continue

            signal = self._extract_signal_score(payload)
            if signal is not None:
                score, weight = signal
                signal_votes.append((score, weight))
                reasons.append(f"{script_name} => signal {score:.2f} (w={weight:.2f})")

        if direct_votes:
            if len(direct_votes) == 1:
                return {
                    "prediction": "Up" if direct_votes[0] >= 0 else "Down",
                    "confidence": direct_confidences[0],
                    "reasoning": "Script-first decision: " + "; ".join(reasons[:6]),
                }
            total_weight = sum(abs(v) for v in direct_votes) or 1.0
            net = sum(direct_votes)
            return {
                "prediction": "Up" if net >= 0 else "Down",
                "confidence": max(0.5, min(abs(net) / total_weight, 1.0)),
                "reasoning": "Script-first decision: " + "; ".join(reasons[:6]),
            }

        if signal_votes:
            total_weight = sum(weight for _, weight in signal_votes) or 1.0
            combined = sum(score * weight for score, weight in signal_votes) / total_weight
            return {
                "prediction": "Up" if combined >= 0.5 else "Down",
                "confidence": max(0.5, min(0.5 + abs(combined - 0.5), 1.0)),
                "reasoning": "Script-first signal aggregate: " + "; ".join(reasons[:6]),
            }

        return None

    async def _get_script_prediction(self, agent_dir: str, snapshot: dict, scripts: dict) -> dict | None:
        script_paths = self._discover_executable_scripts(agent_dir, scripts)
        if not script_paths:
            return None

        snapshot_path = self._write_snapshot_file(agent_dir, snapshot)
        try:
            results = await asyncio.gather(
                *(self._run_script(script_path, snapshot_path) for script_path in script_paths),
                return_exceptions=False,
            )
            return self._aggregate_script_outputs(results)
        finally:
            if os.path.exists(snapshot_path):
                os.unlink(snapshot_path)

    def build_batch_prompt(self, strategy: str, snapshots: list[dict], scripts: dict, notes: str) -> str:
        """Build a prompt for batch prediction (multiple rounds in one call)."""
        parts = [
            "You are a trading strategy agent. For EACH market snapshot below, predict whether BTC will go UP or DOWN in the next 5 minutes.",
            "",
            "## Your Strategy",
            strategy,
            "",
        ]
        if notes:
            parts.extend(["## Notes", notes, ""])
        if scripts:
            parts.append("## Your Analysis Scripts")
            for name, code in scripts.items():
                parts.extend([f"### {name}", f"```python\n{code}\n```", ""])

        parts.append(f"## Market Snapshots ({len(snapshots)} rounds)")
        parts.append("")
        for i, snap in enumerate(snapshots):
            # Compact snapshot — only include key fields to save tokens
            compact = {}
            if "binance_candles_5m" in snap:
                candles = snap["binance_candles_5m"].get("candles", [])[-10:]
                compact["candles_5m"] = [
                    {"o": c.get("open"), "h": c.get("high"), "l": c.get("low"),
                     "c": c.get("close"), "v": c.get("volume")}
                    for c in candles
                ]
            if "polymarket_orderbook" in snap:
                compact["poly_book"] = snap["polymarket_orderbook"]
            if "polling" in snap:
                polling = {}
                for k, v in snap["polling"].items():
                    if isinstance(v, dict) and "data" in v:
                        polling[k] = v["data"][-2:] if v["data"] else []
                    else:
                        polling[k] = v
                compact["polling"] = polling
            if "binance_orderbook" in snap:
                ob = snap["binance_orderbook"]
                compact["binance_ob"] = {
                    "bids": ob.get("bids", [])[:5],
                    "asks": ob.get("asks", [])[:5],
                }

            parts.append(f"### Round {i+1}")
            parts.append(f"```json\n{json.dumps(compact, separators=(',', ':'))}\n```")
            parts.append("")

        parts.extend([
            "## Instructions",
            f"Analyze each of the {len(snapshots)} rounds using your strategy.",
            "Respond with ONLY a JSON array of predictions, one per round, in order:",
            "```json",
            '[{"prediction": "Up" or "Down", "confidence": 0.0-1.0, "reasoning": "brief"}]',
            "```",
            f"The array MUST have exactly {len(snapshots)} entries, one per round.",
        ])
        return "\n".join(parts)

    def build_prompt(self, strategy: str, snapshot: dict, scripts: dict, recent_results: str, notes: str) -> str:
        parts = [
            "You are a trading strategy agent. Analyze the market data below and predict whether BTC will go UP or DOWN in the next 5 minutes.",
            "",
            "## Your Strategy",
            strategy,
            "",
        ]
        if notes:
            parts.extend(["## Notes & Coordinator Suggestions", notes, ""])
        if scripts:
            parts.append("## Your Analysis Scripts")
            for name, code in scripts.items():
                parts.extend([f"### {name}", f"```python\n{code}\n```", ""])
        if recent_results:
            parts.extend(["## Your Recent Results", recent_results, ""])
        parts.extend([
            "## Current Market Snapshot",
            f"```json\n{json.dumps(snapshot, indent=2)}\n```",
            "",
            "## Instructions",
            "1. Analyze the data using your strategy framework",
            "2. Prioritize live intraround data first: live_features, current order books, recent trades, and Polymarket YES/NO books",
            "3. Use 5-minute candles and the round-open snapshot only as background context, not as the primary signal",
            "4. Treat the provided scripts as reference only; do not run shell commands or modify files",
            "5. Make your prediction",
            "",
            "Respond with ONLY a JSON object in this exact format:",
            '```json',
            '{"prediction": "Up" or "Down", "confidence": 0.0-1.0, "reasoning": "brief explanation"}',
            '```',
        ])
        return "\n".join(parts)

    async def _get_model_prediction(self, agent_dir: str, strategy: str, snapshot: dict, scripts: dict, recent_results: str, notes: str, model: str | None = None) -> dict | None:
        prompt = self.build_prompt(strategy, snapshot, scripts, recent_results, notes)
        use_model = model or self.model
        try:
            response = await run_codex_prompt(prompt, use_model, agent_dir, self.timeout)
            return parse_prediction_response(response)
        except asyncio.TimeoutError:
            logger.warning(f"Prediction timed out after {self.timeout}s")
            return None
        except Exception as e:
            logger.error(f"Prediction failed: {e}")
            return None

    async def get_batch_predictions(self, agent_dir: str, strategy: str, snapshots: list[dict], scripts: dict, notes: str, model: str | None = None) -> list[dict | None]:
        """Get predictions for multiple rounds in a single Codex call.

        Returns a list of prediction dicts (or None for failed parses), one per snapshot.
        Much faster than calling get_prediction() N times due to reduced CLI overhead.
        """
        script_paths = self._discover_executable_scripts(agent_dir, scripts)
        if script_paths:
            script_results = await asyncio.gather(
                *(self._get_script_prediction(agent_dir, snapshot, scripts) for snapshot in snapshots),
                return_exceptions=False,
            )
            if all(result is not None for result in script_results):
                return script_results

            merged = list(script_results)
            missing_indexes = [i for i, result in enumerate(merged) if result is None]
            if missing_indexes:
                fallbacks = await asyncio.gather(
                    *(
                        self._get_model_prediction(
                            agent_dir=agent_dir,
                            strategy=strategy,
                            snapshot=snapshots[i],
                            scripts=scripts,
                            recent_results="",
                            notes=notes,
                            model=model,
                        )
                        for i in missing_indexes
                    ),
                    return_exceptions=False,
                )
                for index, result in zip(missing_indexes, fallbacks):
                    merged[index] = result
            return merged

        prompt = self.build_batch_prompt(strategy, snapshots, scripts, notes)
        use_model = model or self.model
        batch_timeout = min(self.timeout * 2, 300)
        try:
            response = await run_codex_prompt(prompt, use_model, agent_dir, batch_timeout)
            return self._parse_batch_response(response, len(snapshots))
        except asyncio.TimeoutError:
            logger.warning(f"Batch prediction timed out after {batch_timeout}s")
            return [None] * len(snapshots)
        except Exception as e:
            logger.error(f"Batch prediction failed: {e}")
            return [None] * len(snapshots)

    def _parse_batch_response(self, response: str, expected_count: int) -> list[dict | None]:
        """Parse a batch prediction response (JSON array of predictions)."""
        # Try direct parse
        try:
            data = json.loads(response)
            if isinstance(data, list):
                return [parse_prediction_response(json.dumps(item)) if isinstance(item, dict) else None for item in data]
        except json.JSONDecodeError:
            pass

        # Try to find JSON array in response
        array_match = re.search(r'\[[\s\S]*\]', response)
        if array_match:
            try:
                data = json.loads(array_match.group())
                if isinstance(data, list):
                    return [parse_prediction_response(json.dumps(item)) if isinstance(item, dict) else None for item in data]
            except json.JSONDecodeError:
                pass

        # Fallback: try to find individual JSON objects
        results = []
        for match in re.finditer(r'\{[^{}]*"prediction"\s*:\s*"(Up|Down)"[^{}]*\}', response):
            try:
                results.append(parse_prediction_response(match.group()))
            except Exception:
                results.append(None)

        # Pad to expected length
        while len(results) < expected_count:
            results.append(None)

        return results[:expected_count]

    async def get_prediction(self, agent_dir: str, strategy: str, snapshot: dict, scripts: dict, recent_results: str, notes: str, model: str | None = None) -> dict | None:
        script_prediction = await self._get_script_prediction(agent_dir, snapshot, scripts)
        if script_prediction is not None:
            return script_prediction
        return await self._get_model_prediction(agent_dir, strategy, snapshot, scripts, recent_results, notes, model)
