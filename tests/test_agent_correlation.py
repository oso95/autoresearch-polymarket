import json
import os

from src.runner.agent_correlation import (
    build_correlation_context,
    build_prediction_matrix,
    compute_agreement_matrix,
    compute_correlation_matrix,
    find_diverse_ensemble,
    find_diversity_clusters,
    run_full_analysis,
)


def _write_predictions(agent_dir, predictions):
    """Write predictions to a JSONL file."""
    pred_path = os.path.join(agent_dir, "predictions.jsonl")
    with open(pred_path, "w") as f:
        for p in predictions:
            f.write(json.dumps(p) + "\n")


def _make_pred(round_ts, prediction, correct, revision=1, confidence=0.6, active=True):
    return {
        "round": round_ts,
        "prediction": prediction,
        "confidence": confidence,
        "correct": correct,
        "active": active,
        "revision": revision,
    }


def _setup_agents(tmp_path, agent_data):
    """Create agent directories with predictions.

    agent_data: dict of agent_name -> list of prediction dicts
    """
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir(parents=True, exist_ok=True)
    (tmp_path / "data").mkdir(parents=True, exist_ok=True)

    for name, preds in agent_data.items():
        agent_dir = agents_dir / name
        agent_dir.mkdir(parents=True, exist_ok=True)
        _write_predictions(str(agent_dir), preds)

    return str(agents_dir)


# ---- Test build_prediction_matrix ----

def test_prediction_matrix_basic(tmp_path):
    """Test that prediction matrix correctly extracts final predictions."""
    agents_dir = _setup_agents(tmp_path, {
        "agent-001-alpha": [
            _make_pred(100, "Up", True, revision=1),
            _make_pred(100, "Down", True, revision=2),  # later revision wins
            _make_pred(200, "Up", False, revision=1),
        ],
        "agent-002-beta": [
            _make_pred(100, "Down", False, revision=1),
            _make_pred(200, "Up", True, revision=1),
        ],
    })

    names, rounds, preds = build_prediction_matrix(agents_dir)

    assert names == ["agent-001-alpha", "agent-002-beta"]
    assert rounds == [100, 200]
    # agent-001-alpha: round 100 -> Down (revision 2), round 200 -> Up
    assert preds["agent-001-alpha"][100] == 0  # Down
    assert preds["agent-001-alpha"][200] == 1  # Up
    # agent-002-beta: round 100 -> Down, round 200 -> Up
    assert preds["agent-002-beta"][100] == 0
    assert preds["agent-002-beta"][200] == 1


def test_prediction_matrix_skips_unscored(tmp_path):
    """Predictions with correct=None are skipped."""
    agents_dir = _setup_agents(tmp_path, {
        "agent-001-alpha": [
            _make_pred(100, "Up", None, revision=1),  # unscored -> skip
            _make_pred(200, "Down", True, revision=1),
        ],
    })

    names, rounds, preds = build_prediction_matrix(agents_dir)

    assert names == ["agent-001-alpha"]
    assert rounds == [200]
    assert 100 not in preds["agent-001-alpha"]
    assert preds["agent-001-alpha"][200] == 0


def test_prediction_matrix_empty(tmp_path):
    """Empty agents directory returns empty results."""
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir(parents=True)

    names, rounds, preds = build_prediction_matrix(str(agents_dir))

    assert names == []
    assert rounds == []
    assert preds == {}


def test_prediction_matrix_nonexistent_dir(tmp_path):
    """Non-existent directory returns empty results."""
    names, rounds, preds = build_prediction_matrix(str(tmp_path / "nonexistent"))

    assert names == []
    assert rounds == []
    assert preds == {}


# ---- Test compute_agreement_matrix ----

def test_agreement_identical_agents(tmp_path):
    """Two agents that always agree should have agreement=1.0."""
    agents_dir = _setup_agents(tmp_path, {
        "agent-001-a": [
            _make_pred(100, "Up", True),
            _make_pred(200, "Down", False),
            _make_pred(300, "Up", True),
        ],
        "agent-002-b": [
            _make_pred(100, "Up", True),
            _make_pred(200, "Down", True),
            _make_pred(300, "Up", False),
        ],
    })

    names, rounds, preds = build_prediction_matrix(agents_dir)
    agreement = compute_agreement_matrix(names, rounds, preds)

    assert agreement[("agent-001-a", "agent-002-b")] == 1.0


def test_agreement_opposite_agents(tmp_path):
    """Two agents that always disagree should have agreement=0.0."""
    agents_dir = _setup_agents(tmp_path, {
        "agent-001-a": [
            _make_pred(100, "Up", True),
            _make_pred(200, "Down", False),
            _make_pred(300, "Up", True),
        ],
        "agent-002-b": [
            _make_pred(100, "Down", False),
            _make_pred(200, "Up", True),
            _make_pred(300, "Down", False),
        ],
    })

    names, rounds, preds = build_prediction_matrix(agents_dir)
    agreement = compute_agreement_matrix(names, rounds, preds)

    assert agreement[("agent-001-a", "agent-002-b")] == 0.0


def test_agreement_partial(tmp_path):
    """Agents that agree 2/3 of the time should have agreement ~0.667."""
    agents_dir = _setup_agents(tmp_path, {
        "agent-001-a": [
            _make_pred(100, "Up", True),
            _make_pred(200, "Down", False),
            _make_pred(300, "Up", True),
        ],
        "agent-002-b": [
            _make_pred(100, "Up", True),
            _make_pred(200, "Down", True),
            _make_pred(300, "Down", False),  # disagree here
        ],
    })

    names, rounds, preds = build_prediction_matrix(agents_dir)
    agreement = compute_agreement_matrix(names, rounds, preds)

    assert abs(agreement[("agent-001-a", "agent-002-b")] - 2 / 3) < 0.01


# ---- Test compute_correlation_matrix ----

def test_correlation_identical(tmp_path):
    """Identical predictions should have correlation=+1."""
    agents_dir = _setup_agents(tmp_path, {
        "agent-001-a": [
            _make_pred(100, "Up", True),
            _make_pred(200, "Down", False),
            _make_pred(300, "Up", True),
            _make_pred(400, "Down", False),
        ],
        "agent-002-b": [
            _make_pred(100, "Up", True),
            _make_pred(200, "Down", True),
            _make_pred(300, "Up", False),
            _make_pred(400, "Down", True),
        ],
    })

    names, rounds, preds = build_prediction_matrix(agents_dir)
    corr = compute_correlation_matrix(names, rounds, preds)

    assert abs(corr[("agent-001-a", "agent-002-b")] - 1.0) < 0.01


def test_correlation_opposite(tmp_path):
    """Opposite predictions should have correlation=-1."""
    agents_dir = _setup_agents(tmp_path, {
        "agent-001-a": [
            _make_pred(100, "Up", True),
            _make_pred(200, "Down", False),
            _make_pred(300, "Up", True),
            _make_pred(400, "Down", False),
        ],
        "agent-002-b": [
            _make_pred(100, "Down", False),
            _make_pred(200, "Up", True),
            _make_pred(300, "Down", False),
            _make_pred(400, "Up", True),
        ],
    })

    names, rounds, preds = build_prediction_matrix(agents_dir)
    corr = compute_correlation_matrix(names, rounds, preds)

    assert abs(corr[("agent-001-a", "agent-002-b")] - (-1.0)) < 0.01


def test_correlation_constant_prediction(tmp_path):
    """Agent that always predicts the same -> correlation=0 (undefined)."""
    agents_dir = _setup_agents(tmp_path, {
        "agent-001-a": [
            _make_pred(100, "Up", True),
            _make_pred(200, "Up", False),
            _make_pred(300, "Up", True),
        ],
        "agent-002-b": [
            _make_pred(100, "Up", True),
            _make_pred(200, "Down", True),
            _make_pred(300, "Up", False),
        ],
    })

    names, rounds, preds = build_prediction_matrix(agents_dir)
    corr = compute_correlation_matrix(names, rounds, preds)

    # agent-001-a always predicts Up (constant) -> denominator is 0 -> correlation = 0
    assert corr[("agent-001-a", "agent-002-b")] == 0.0


def test_correlation_known_sequence(tmp_path):
    """Test correlation with a known manual calculation."""
    # A: Up, Down, Up, Up  -> 1, 0, 1, 1
    # B: Up, Up, Down, Up  -> 1, 1, 0, 1
    # n=4, sum_x=3, sum_y=3, sum_xy=2, sum_x2=3, sum_y2=3
    # num = 4*2 - 3*3 = 8-9 = -1
    # denom_x = 4*3 - 9 = 3, denom_y = 4*3 - 9 = 3
    # r = -1 / sqrt(3*3) = -1/3 = -0.333...
    agents_dir = _setup_agents(tmp_path, {
        "agent-001-a": [
            _make_pred(100, "Up", True),
            _make_pred(200, "Down", False),
            _make_pred(300, "Up", True),
            _make_pred(400, "Up", False),
        ],
        "agent-002-b": [
            _make_pred(100, "Up", True),
            _make_pred(200, "Up", True),
            _make_pred(300, "Down", False),
            _make_pred(400, "Up", True),
        ],
    })

    names, rounds, preds = build_prediction_matrix(agents_dir)
    corr = compute_correlation_matrix(names, rounds, preds)

    assert abs(corr[("agent-001-a", "agent-002-b")] - (-1 / 3)) < 0.01


# ---- Test no overlapping rounds ----

def test_no_overlapping_rounds(tmp_path):
    """Agents with no common rounds should have 0 agreement and 0 correlation."""
    agents_dir = _setup_agents(tmp_path, {
        "agent-001-a": [
            _make_pred(100, "Up", True),
            _make_pred(200, "Down", False),
        ],
        "agent-002-b": [
            _make_pred(300, "Up", True),
            _make_pred(400, "Down", False),
        ],
    })

    names, rounds, preds = build_prediction_matrix(agents_dir)
    agreement = compute_agreement_matrix(names, rounds, preds)
    corr = compute_correlation_matrix(names, rounds, preds)

    assert agreement[("agent-001-a", "agent-002-b")] == 0.0
    assert corr[("agent-001-a", "agent-002-b")] == 0.0


# ---- Test single agent ----

def test_single_agent(tmp_path):
    """Single agent has no pairs to compare."""
    agents_dir = _setup_agents(tmp_path, {
        "agent-001-a": [
            _make_pred(100, "Up", True),
            _make_pred(200, "Down", False),
        ],
    })

    names, rounds, preds = build_prediction_matrix(agents_dir)
    agreement = compute_agreement_matrix(names, rounds, preds)
    corr = compute_correlation_matrix(names, rounds, preds)

    assert len(names) == 1
    assert agreement == {}
    assert corr == {}


# ---- Test find_diverse_ensemble ----

def test_diverse_ensemble_basic():
    """Test greedy diverse ensemble selection."""
    agents = ["a", "b", "c", "d"]
    correlations = {
        ("a", "b"): 0.9,  # a and b are highly correlated
        ("a", "c"): 0.1,  # a and c are independent
        ("a", "d"): 0.2,
        ("b", "c"): 0.1,
        ("b", "d"): 0.2,
        ("c", "d"): 0.8,  # c and d are correlated
    }
    win_rates = {"a": 0.60, "b": 0.55, "c": 0.52, "d": 0.50}

    ensemble = find_diverse_ensemble(agents, correlations, win_rates, min_wr=0.48, size=3)

    assert len(ensemble) == 3
    assert ensemble[0] == "a"  # highest WR
    # Should prefer c or d over b (b has high corr with a)
    assert "b" not in ensemble or ("c" in ensemble and "d" not in ensemble)


def test_diverse_ensemble_min_wr_filter():
    """Agents below min_wr are excluded."""
    agents = ["a", "b", "c"]
    correlations = {("a", "b"): 0.5, ("a", "c"): 0.5, ("b", "c"): 0.5}
    win_rates = {"a": 0.60, "b": 0.40, "c": 0.52}

    ensemble = find_diverse_ensemble(agents, correlations, win_rates, min_wr=0.50, size=3)

    assert "b" not in ensemble
    assert "a" in ensemble
    assert "c" in ensemble


def test_diverse_ensemble_empty():
    """No agents above threshold returns empty."""
    agents = ["a", "b"]
    correlations = {("a", "b"): 0.5}
    win_rates = {"a": 0.30, "b": 0.35}

    ensemble = find_diverse_ensemble(agents, correlations, win_rates, min_wr=0.48, size=3)

    assert ensemble == []


# ---- Test diversity clusters ----

def test_diversity_clusters():
    """High correlation agents grouped together."""
    agents = ["a", "b", "c", "d"]
    correlations = {
        ("a", "b"): 0.8,  # same cluster
        ("a", "c"): 0.1,
        ("a", "d"): 0.2,
        ("b", "c"): 0.1,
        ("b", "d"): 0.2,
        ("c", "d"): 0.7,  # same cluster
    }

    clusters = find_diversity_clusters(agents, correlations, threshold=0.5)

    # a+b in one cluster, c+d in another
    assert len(clusters) == 2
    cluster_sets = [set(c) for c in clusters]
    assert {"a", "b"} in cluster_sets
    assert {"c", "d"} in cluster_sets


# ---- Test run_full_analysis ----

def test_run_full_analysis(tmp_path):
    """Full analysis returns complete results dict."""
    agents_dir = _setup_agents(tmp_path, {
        "agent-001-a": [
            _make_pred(100, "Up", True),
            _make_pred(200, "Down", False),
            _make_pred(300, "Up", True),
        ],
        "agent-002-b": [
            _make_pred(100, "Down", False),
            _make_pred(200, "Up", True),
            _make_pred(300, "Down", False),
        ],
    })

    result = run_full_analysis(agents_dir)

    assert result["agent_count"] == 2
    assert result["round_count"] == 3
    assert "agent-001-a" in result["agents"]
    assert "agent-002-b" in result["agents"]
    assert "correlation_matrix" in result
    assert "agreement_matrix" in result
    assert "clusters" in result
    assert "recommended_ensemble" in result

    # Verify file was saved
    data_dir = os.path.join(str(tmp_path), "data")
    assert os.path.exists(os.path.join(data_dir, "agent_correlations.json"))


def test_run_full_analysis_single_agent(tmp_path):
    """Single agent returns minimal results."""
    agents_dir = _setup_agents(tmp_path, {
        "agent-001-a": [
            _make_pred(100, "Up", True),
        ],
    })

    result = run_full_analysis(agents_dir)

    assert result["agent_count"] == 1
    assert "correlation_matrix" not in result


# ---- Test build_correlation_context ----

def test_build_correlation_context(tmp_path):
    """Context string is non-empty for multiple agents."""
    agents_dir = _setup_agents(tmp_path, {
        "agent-001-a": [
            _make_pred(100, "Up", True),
            _make_pred(200, "Down", False),
            _make_pred(300, "Up", True),
        ],
        "agent-002-b": [
            _make_pred(100, "Down", False),
            _make_pred(200, "Up", True),
            _make_pred(300, "Down", False),
        ],
    })

    ctx = build_correlation_context(agents_dir)

    assert "Agent Prediction Correlation" in ctx
    assert "agent-001-a" in ctx
    assert "agent-002-b" in ctx


def test_build_correlation_context_single_agent(tmp_path):
    """Single agent returns empty context."""
    agents_dir = _setup_agents(tmp_path, {
        "agent-001-a": [
            _make_pred(100, "Up", True),
        ],
    })

    ctx = build_correlation_context(agents_dir)

    assert ctx == ""


def test_prediction_matrix_uses_last_revision(tmp_path):
    """Multiple revisions per round: the latest revision is used."""
    agents_dir = _setup_agents(tmp_path, {
        "agent-001-a": [
            _make_pred(100, "Up", True, revision=1),
            _make_pred(100, "Down", True, revision=2),  # revision 2 wins
            _make_pred(100, "Up", True, revision=3),     # revision 3 wins
        ],
    })

    names, rounds, preds = build_prediction_matrix(agents_dir)

    assert preds["agent-001-a"][100] == 1  # Up from revision 3
