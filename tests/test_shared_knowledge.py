import os

from src.shared_knowledge import SharedKnowledgeForum, build_shared_knowledge_context


def test_forum_create_vote_comment_and_context(tmp_path):
    shared_dir = tmp_path / "shared_knowledge"
    forum = SharedKnowledgeForum(str(shared_dir))
    post = forum.create_post("agent-001", "Threshold discovery", "Raise the downtrend threshold to 0.8%.")

    assert post["post_id"].startswith("post-")

    assert forum.vote_post("agent-002", post["post_id"], "up", "Worked in severe downtrends")
    assert forum.comment_post("agent-003", post["post_id"], "Saw similar results on my side")

    context = build_shared_knowledge_context(str(shared_dir), "agent-004")
    assert "Shared Knowledge Forum Index" in context
    assert post["post_id"] in context
    assert "Threshold discovery" in context
    assert "Worked in severe downtrends" in context
    assert "Saw similar results on my side" in context


def test_context_prioritizes_unseen_posts(tmp_path):
    shared_dir = tmp_path / "shared_knowledge"
    forum = SharedKnowledgeForum(str(shared_dir))
    post1 = forum.create_post("agent-001", "Seen post", "already voted on")
    post2 = forum.create_post("agent-002", "New post", "not yet seen")
    forum.vote_post("agent-010", post1["post_id"], "up")

    context = forum.build_context("agent-010", include_full_posts=2, include_index_posts=2)
    first_seen = context.find(post1["post_id"])
    first_new = context.find(post2["post_id"])
    assert first_new != -1 and first_seen != -1
    assert first_new < first_seen
