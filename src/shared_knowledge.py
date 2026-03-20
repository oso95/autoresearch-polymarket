import json
import os
import re
import time
from pathlib import Path

from src.io_utils import atomic_append_jsonl, atomic_write_json, read_jsonl


def _slugify(text: str, fallback: str = "post") -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug or fallback


class SharedKnowledgeForum:
    def __init__(self, shared_dir: str):
        self.shared_dir = shared_dir
        self.forum_dir = os.path.join(shared_dir, "forum")
        self.posts_dir = os.path.join(self.forum_dir, "posts")
        self.index_path = os.path.join(self.forum_dir, "index.json")
        self.comments_dir = os.path.join(self.forum_dir, "comments")

    def ensure(self) -> None:
        Path(self.posts_dir).mkdir(parents=True, exist_ok=True)
        Path(self.comments_dir).mkdir(parents=True, exist_ok=True)
        if not os.path.exists(self.index_path):
            atomic_write_json(self.index_path, {"posts": [], "updated_at": int(time.time() * 1000)})

    def _read_index(self) -> dict:
        self.ensure()
        try:
            with open(self.index_path) as f:
                return json.load(f)
        except (OSError, json.JSONDecodeError):
            return {"posts": [], "updated_at": int(time.time() * 1000)}

    def _write_index(self, index: dict) -> None:
        index["updated_at"] = int(time.time() * 1000)
        atomic_write_json(self.index_path, index)

    def create_post(self, author: str, title: str, body: str) -> dict:
        self.ensure()
        now_ms = int(time.time() * 1000)
        post_id = f"post-{now_ms}-{_slugify(title)}"
        post_path = os.path.join(self.posts_dir, f"{post_id}.md")
        metadata = {
            "post_id": post_id,
            "author": author,
            "title": title.strip(),
            "score": 0,
            "upvotes": 0,
            "downvotes": 0,
            "comments_count": 0,
            "created_at": now_ms,
            "updated_at": now_ms,
            "post_path": post_path,
            "votes_by_agent": {},
            "excerpt": body.strip().splitlines()[0][:160] if body.strip() else "",
        }
        with open(post_path, "w") as f:
            f.write(f"# {title.strip()}\n\n{body.strip()}\n")

        index = self._read_index()
        index["posts"].append(metadata)
        self._write_index(index)
        return metadata

    def vote_post(self, agent_name: str, post_id: str, vote: str, reason: str = "") -> bool:
        self.ensure()
        normalized = vote.strip().lower()
        if normalized in {"up", "upvote", "+1", "1"}:
            value = 1
        elif normalized in {"down", "downvote", "-1"}:
            value = -1
        else:
            return False

        index = self._read_index()
        for post in index.get("posts", []):
            if post.get("post_id") != post_id:
                continue
            previous = int(post.get("votes_by_agent", {}).get(agent_name, 0))
            post.setdefault("votes_by_agent", {})[agent_name] = value
            post["score"] = int(post.get("score", 0)) - previous + value
            if previous == 1:
                post["upvotes"] = max(0, int(post.get("upvotes", 0)) - 1)
            elif previous == -1:
                post["downvotes"] = max(0, int(post.get("downvotes", 0)) - 1)
            if value == 1:
                post["upvotes"] = int(post.get("upvotes", 0)) + 1
            else:
                post["downvotes"] = int(post.get("downvotes", 0)) + 1
            post["updated_at"] = int(time.time() * 1000)
            self._write_index(index)
            if reason.strip():
                self.comment_post(agent_name, post_id, f"[vote:{'up' if value == 1 else 'down'}] {reason.strip()}")
            return True
        return False

    def comment_post(self, agent_name: str, post_id: str, comment: str) -> bool:
        self.ensure()
        if not comment.strip():
            return False
        index = self._read_index()
        for post in index.get("posts", []):
            if post.get("post_id") != post_id:
                continue
            comment_path = os.path.join(self.comments_dir, f"{post_id}.jsonl")
            atomic_append_jsonl(
                comment_path,
                {
                    "post_id": post_id,
                    "author": agent_name,
                    "comment": comment.strip(),
                    "created_at": int(time.time() * 1000),
                },
            )
            post["comments_count"] = int(post.get("comments_count", 0)) + 1
            post["updated_at"] = int(time.time() * 1000)
            self._write_index(index)
            return True
        return False

    def _comments_for_post(self, post_id: str, limit: int = 2) -> list[dict]:
        path = os.path.join(self.comments_dir, f"{post_id}.jsonl")
        comments = read_jsonl(path)
        return comments[-limit:]

    def _read_post_body(self, post: dict) -> str:
        path = post.get("post_path")
        if path and os.path.exists(path):
            with open(path) as f:
                return f.read().strip()
        return ""

    def build_context(self, agent_name: str, include_full_posts: int = 3, include_index_posts: int = 8) -> str:
        self.ensure()
        index = self._read_index()
        posts = index.get("posts", [])
        if not posts:
            return "No forum posts yet."

        def sort_key(post: dict):
            votes_by_agent = post.get("votes_by_agent", {})
            seen = 1 if agent_name in votes_by_agent else 0
            return (seen, -int(post.get("score", 0)), -int(post.get("comments_count", 0)), -int(post.get("updated_at", 0)))

        ranked = sorted(posts, key=sort_key)
        lines = ["## Shared Knowledge Forum Index"]
        for post in ranked[:include_index_posts]:
            seen = "seen" if agent_name in post.get("votes_by_agent", {}) else "new"
            lines.append(
                f"- {post['post_id']} | score {post.get('score',0)} | comments {post.get('comments_count',0)} | {seen} | {post.get('title','')}"
            )
            excerpt = post.get("excerpt", "")
            if excerpt:
                lines.append(f"  Excerpt: {excerpt}")

        lines.append("")
        lines.append("## Top Forum Posts")
        for post in ranked[:include_full_posts]:
            lines.append(f"### {post['post_id']} — {post.get('title','')}")
            lines.append(self._read_post_body(post) or post.get("excerpt", ""))
            comments = self._comments_for_post(post["post_id"])
            if comments:
                lines.append("Comments:")
                for comment in comments:
                    lines.append(f"- {comment.get('author')}: {comment.get('comment')}")
            lines.append("")
        return "\n".join(lines)


def ensure_shared_knowledge_forum(shared_dir: str) -> None:
    SharedKnowledgeForum(shared_dir).ensure()


def build_shared_knowledge_context(shared_dir: str, agent_name: str, legacy_discoveries_limit: int = 5) -> str:
    ensure_shared_knowledge_forum(shared_dir)
    sections: list[str] = []

    core_files = []
    discovery_files = []
    for fname in sorted(os.listdir(shared_dir)):
        fpath = os.path.join(shared_dir, fname)
        if not os.path.isfile(fpath) or not fname.endswith((".md", ".txt", ".json")):
            continue
        if fname.startswith("discovery-"):
            discovery_files.append((fname, fpath))
        elif (
            fname not in {"forum-summary.md", "archived-discoveries.md"}
            and not fname.startswith("leaderboard-snapshot-")
        ):
            core_files.append((fname, fpath))

    for fname, fpath in core_files:
        with open(fpath) as f:
            sections.append(f"## Shared Knowledge: {fname}\n{f.read()}")

    forum = SharedKnowledgeForum(shared_dir)
    sections.append(forum.build_context(agent_name))

    legacy_parts = []
    for fname, fpath in discovery_files[-legacy_discoveries_limit:]:
        with open(fpath) as f:
            legacy_parts.append(f"### Legacy Discovery: {fname}\n{f.read()}")
    if legacy_parts:
        sections.append("## Recent Legacy Discoveries\n" + "\n\n".join(legacy_parts))

    return "\n\n".join(section for section in sections if section.strip())
