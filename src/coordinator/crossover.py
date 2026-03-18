import os
import shutil
import time
import logging

logger = logging.getLogger(__name__)

class CrossPollinator:
    def __init__(self, agents_dir: str):
        self.agents_dir = agents_dir

    def copy_script(self, source_agent: str, target_agent: str, script_name: str, context: str):
        source_path = os.path.join(self.agents_dir, source_agent, "scripts", script_name)
        target_scripts = os.path.join(self.agents_dir, target_agent, "scripts")
        os.makedirs(target_scripts, exist_ok=True)
        target_path = os.path.join(target_scripts, script_name)
        shutil.copy2(source_path, target_path)
        self.add_suggestion(
            target_agent,
            f"Script `{script_name}` copied from {source_agent}. Context: {context}. "
            f"Consider integrating this into your strategy."
        )
        logger.info(f"Copied {script_name} from {source_agent} to {target_agent}")

    def add_suggestion(self, agent_name: str, suggestion: str):
        notes_path = os.path.join(self.agents_dir, agent_name, "notes.md")
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
        entry = f"\n## Coordinator Suggestion ({timestamp})\n{suggestion}\n"
        tmp_path = notes_path + ".tmp"
        existing = ""
        if os.path.exists(notes_path):
            with open(notes_path) as f:
                existing = f.read()
        with open(tmp_path, "w") as f:
            f.write(existing + entry)
        os.rename(tmp_path, notes_path)
        logger.info(f"Added suggestion to {agent_name}")
