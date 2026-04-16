"""
Dreaming — Background Memory Consolidation

Inspired by human sleep memory consolidation. Runs as a background process
(via cron or scheduled task) to:

1. EXTRACT: Pull important facts, preferences, and decisions from conversation logs
2. CONSOLIDATE: Merge with existing MEMORY.md, dedup, update stale entries  
3. INDEX: Build a semantic search index for fast recall

The bridge Claude only needs to READ memory — dreaming handles the WRITING.

Architecture:
- Reads Claude session JSONL files (the actual conversation transcripts)
- Uses Claude API (via Claude Code) to extract memories
- Writes to workspace MEMORY.md and memory/ files
- Maintains memory/dream_state.json to track what's been processed
"""

import json
import os
import sys
import logging
import hashlib
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Dict, Optional
import subprocess

logger = logging.getLogger(__name__)


class DreamState:
    """Tracks which conversations have been processed."""
    
    def __init__(self, state_file: Path):
        self.state_file = state_file
        self.state = self._load()
    
    def _load(self) -> dict:
        try:
            if self.state_file.exists():
                return json.loads(self.state_file.read_text())
        except Exception as e:
            logger.warning(f"Failed to load dream state: {e}")
        return {
            "last_dream": None,
            "processed_sessions": {},  # session_id -> last_processed_offset
            "dream_count": 0,
        }
    
    def save(self):
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        self.state_file.write_text(json.dumps(self.state, indent=2))
    
    def get_last_offset(self, session_id: str) -> int:
        return self.state.get("processed_sessions", {}).get(session_id, 0)
    
    def set_last_offset(self, session_id: str, offset: int):
        self.state.setdefault("processed_sessions", {})[session_id] = offset
    
    def mark_dreamed(self):
        self.state["last_dream"] = datetime.now().isoformat()
        self.state["dream_count"] = self.state.get("dream_count", 0) + 1


class Dreamer:
    """Background memory consolidation engine."""
    
    def __init__(self, agent_dir: str, workspace_dir: str):
        self.agent_dir = Path(agent_dir)
        self.workspace_dir = Path(workspace_dir)
        self.memory_file = self.workspace_dir / "MEMORY.md"
        self.memory_dir = self.workspace_dir / "memory"
        self.dream_dir = self.memory_dir / ".dreams"
        self.state = DreamState(self.dream_dir / "dream_state.json")
        
        # Find the session JSONL from the session_state
        self.session_id = self._get_session_id()
        
    def _get_session_id(self) -> Optional[str]:
        """Get the current session ID from session_state.json."""
        state_file = self.agent_dir / "session_state.json"
        try:
            if state_file.exists():
                data = json.loads(state_file.read_text())
                return data.get("session_id")
        except Exception:
            pass
        return None
    
    def _find_session_jsonls(self) -> List[Path]:
        """Find ALL Claude session JSONL files for this workspace.
        
        Scans ~/.claude/projects/ for project dirs matching the workspace path,
        then returns all .jsonl session files within (sorted oldest first).
        """
        claude_dir = Path.home() / ".claude" / "projects"
        if not claude_dir.exists():
            return []
        
        # Claude encodes workspace path as dir name: /Users/fuzz/workspace/athena -> -Users-fuzz-workspace-athena
        workspace_slug = str(self.workspace_dir).replace("/", "-")
        if workspace_slug.startswith("-"):
            pass  # already starts with -
        
        results = []
        for project_dir in claude_dir.iterdir():
            if not project_dir.is_dir():
                continue
            # Match by workspace slug
            if workspace_slug in project_dir.name or project_dir.name == workspace_slug:
                for jsonl in sorted(project_dir.glob("*.jsonl")):
                    # Skip subagent files
                    if "subagent" in str(jsonl) or "/subagents/" in str(jsonl):
                        continue
                    results.append(jsonl)
        
        # Also check specific session ID if we have one
        if self.session_id:
            for project_dir in claude_dir.iterdir():
                jsonl = project_dir / f"{self.session_id}.jsonl"
                if jsonl.exists() and jsonl not in results:
                    results.append(jsonl)
        
        # Sort by modification time (oldest first)
        results.sort(key=lambda p: p.stat().st_mtime)
        return results
    
    def _extract_conversations(self, since_offset: int = 0) -> List[Dict]:
        """Extract human/assistant message pairs from all session JSONLs."""
        jsonl_paths = self._find_session_jsonls()
        if not jsonl_paths:
            logger.warning(f"No session JSONLs found for workspace {self.workspace_dir}")
            return []
        
        conversations = []
        total_lines = 0
        
        for jsonl_path in jsonl_paths:
            session_id = jsonl_path.stem
            file_offset = self.state.get_last_offset(session_id)
            current_exchange = []
            
            logger.info(f"Reading {jsonl_path.name} (offset {file_offset})")
            
            with open(jsonl_path) as f:
                for i, line in enumerate(f):
                    if i < file_offset:
                        continue
                    try:
                        entry = json.loads(line.strip())
                        msg_type = entry.get("type")
                        
                        if msg_type in ("human", "user"):
                            if current_exchange:
                                conversations.append(current_exchange)
                            current_exchange = []
                            
                            content = entry.get("message", {}).get("content", [])
                            text = ""
                            if isinstance(content, str):
                                text = content
                            elif isinstance(content, list):
                                for block in content:
                                    if isinstance(block, str):
                                        text += block
                                    elif isinstance(block, dict) and block.get("type") == "text":
                                        text += block.get("text", "")
                            if text.strip():
                                current_exchange.append({"role": "human", "text": text.strip()})
                        
                        elif msg_type == "assistant":
                            content = entry.get("message", {}).get("content", [])
                            text = ""
                            if isinstance(content, list):
                                for block in content:
                                    if isinstance(block, dict) and block.get("type") == "text":
                                        text += block.get("text", "")
                            if text.strip():
                                current_exchange.append({"role": "assistant", "text": text.strip()})
                    
                    except (json.JSONDecodeError, KeyError):
                        continue
                    
                    total_lines = i + 1
            
            if current_exchange:
                conversations.append(current_exchange)
            
            # Track offset per session file
            self.state.set_last_offset(session_id, total_lines)
        
        return conversations
    
    def _chunk_conversations(self, conversations: List[Dict], max_chars: int = 8000) -> List[str]:
        """Chunk conversations into digestible pieces for the LLM."""
        chunks = []
        current_chunk = []
        current_len = 0
        
        for exchange in conversations:
            exchange_text = "\n".join(f"[{m['role']}]: {m['text']}" for m in exchange)
            if current_len + len(exchange_text) > max_chars and current_chunk:
                chunks.append("\n\n---\n\n".join(current_chunk))
                current_chunk = []
                current_len = 0
            current_chunk.append(exchange_text)
            current_len += len(exchange_text)
        
        if current_chunk:
            chunks.append("\n\n---\n\n".join(current_chunk))
        
        return chunks
    
    def _extract_memories_with_llm(self, conversation_text: str, existing_memory: str) -> str:
        """Use Claude to extract important memories from conversations."""
        prompt = f"""You are a memory consolidation system. Your job is to extract important, durable memories from conversation transcripts.

EXISTING LONG-TERM MEMORY:
{existing_memory[:3000] if existing_memory else "(empty)"}

RECENT CONVERSATIONS:
{conversation_text}

INSTRUCTIONS:
Extract memories that are worth keeping long-term. Focus on:
1. **Facts about the human** — name, preferences, timezone, communication style, interests
2. **Decisions made** — what was decided and why
3. **Preferences expressed** — likes, dislikes, how they want things done
4. **Important context** — projects, goals, relationships, recurring topics
5. **Lessons learned** — what worked, what didn't, mistakes to avoid
6. **Behavioral patterns** — how the human communicates, what frustrates them

DO NOT extract:
- Trivial small talk
- Temporary/one-time information
- Things already in existing memory (unless updating)
- Raw technical details unless they represent a preference

For each memory, write it as a clear, concise statement. Group them under categories.
If a memory UPDATES something already in existing memory, note it as an update.

Output format — just the memories, one per line, grouped:
## Category Name
- Memory statement here
- Another memory here

If nothing worth remembering, output: NO_NEW_MEMORIES"""

        try:
            result = subprocess.run(
                ["claude", "--print", "--model", "claude-sonnet-4-6", 
                 "--max-turns", "1"],
                input=prompt,
                capture_output=True,
                text=True,
                timeout=60,
            )
            
            if result.returncode == 0 and result.stdout.strip():
                return result.stdout.strip()
            else:
                logger.error(f"Claude extraction failed: {result.stderr[:200]}")
                return "NO_NEW_MEMORIES"
        except subprocess.TimeoutExpired:
            logger.error("Claude extraction timed out")
            return "NO_NEW_MEMORIES"
        except FileNotFoundError:
            logger.error("claude CLI not found")
            return "NO_NEW_MEMORIES"
    
    def _merge_memories(self, existing: str, new_memories: str) -> str:
        """Use Claude to merge new memories into existing MEMORY.md."""
        prompt = f"""You are a memory manager. Merge new memories into the existing memory file.

EXISTING MEMORY.md:
{existing}

NEW MEMORIES TO INTEGRATE:
{new_memories}

INSTRUCTIONS:
1. Add new information to appropriate sections
2. Update any entries that have new/corrected information
3. Remove duplicates
4. Keep the same markdown structure and headers
5. Don't remove existing memories unless they're clearly outdated/wrong
6. Keep it concise — facts, not prose
7. Preserve any section headers and organization from the existing file

Output the complete updated MEMORY.md content."""

        try:
            result = subprocess.run(
                ["claude", "--print", "--model", "claude-sonnet-4-6",
                 "--max-turns", "1"],
                input=prompt,
                capture_output=True,
                text=True,
                timeout=60,
            )
            
            if result.returncode == 0 and result.stdout.strip():
                return result.stdout.strip()
            else:
                logger.error(f"Claude merge failed: {result.stderr[:200]}")
                return existing  # Return unchanged on failure
        except (subprocess.TimeoutExpired, FileNotFoundError):
            logger.error("Claude merge failed")
            return existing
    
    def dream(self) -> Dict:
        """Run a full dreaming cycle. Returns a report."""
        report = {
            "timestamp": datetime.now().isoformat(),
            "session_id": self.session_id,
            "conversations_processed": 0,
            "memories_extracted": 0,
            "status": "started",
        }
        
        logger.info(f"Starting dream cycle for session {self.session_id}")
        
        # 1. Get conversations since last dream (offsets tracked per-session in state)
        conversations = self._extract_conversations()
        
        if not conversations:
            report["status"] = "no_new_conversations"
            logger.info("No new conversations to process")
            return report
        
        report["conversations_processed"] = len(conversations)
        logger.info(f"Found {len(conversations)} new conversation exchanges")
        
        # 2. Chunk and extract memories
        chunks = self._chunk_conversations(conversations)
        existing_memory = self.memory_file.read_text() if self.memory_file.exists() else ""
        
        all_new_memories = []
        for i, chunk in enumerate(chunks):
            logger.info(f"Extracting memories from chunk {i+1}/{len(chunks)}")
            memories = self._extract_memories_with_llm(chunk, existing_memory)
            if memories != "NO_NEW_MEMORIES":
                all_new_memories.append(memories)
        
        if not all_new_memories:
            report["status"] = "no_new_memories"
            self.state.mark_dreamed()
            self.state.save()
            logger.info("No new memories extracted")
            return report
        
        combined_new = "\n\n".join(all_new_memories)
        report["memories_extracted"] = combined_new.count("\n- ")
        
        # 3. Merge into MEMORY.md
        logger.info("Merging memories into MEMORY.md")
        updated_memory = self._merge_memories(existing_memory, combined_new)
        
        # 4. Write
        self.memory_file.write_text(updated_memory)
        
        # 5. Also save the raw dream to daily dream log
        today = datetime.now().strftime("%Y-%m-%d")
        dream_log = self.dream_dir / f"{today}.md"
        dream_log.parent.mkdir(parents=True, exist_ok=True)
        with open(dream_log, "a") as f:
            f.write(f"\n## Dream at {datetime.now().strftime('%H:%M')}\n")
            f.write(f"Processed {len(conversations)} exchanges\n")
            f.write(f"### Extracted Memories\n{combined_new}\n")
        
        # 6. Update state (offsets already tracked per-session during extraction)
        self.state.mark_dreamed()
        self.state.save()
        
        report["status"] = "completed"
        logger.info(f"Dream cycle complete: {report['memories_extracted']} memories extracted")
        
        return report


def main():
    """CLI entry point for dreaming."""
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    )
    
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <agent_config_dir>")
        print(f"Example: {sys.argv[0]} agents/ares")
        sys.exit(1)
    
    agent_dir = sys.argv[1]
    bridge_dir = Path(__file__).parent.parent
    
    if not Path(agent_dir).is_absolute():
        agent_dir = str(bridge_dir / agent_dir)
    
    # Load config
    from dotenv import load_dotenv
    env_file = Path(agent_dir) / "config.env"
    if not env_file.exists():
        print(f"Config not found: {env_file}")
        sys.exit(1)
    load_dotenv(env_file, override=True)
    
    workspace_dir = os.getenv("WORKSPACE_DIR", agent_dir)
    agent_name = os.getenv("AGENT_NAME", "unknown")
    
    print(f"🌙 Starting dream cycle for {agent_name}...")
    print(f"   Workspace: {workspace_dir}")
    
    dreamer = Dreamer(agent_dir, workspace_dir)
    report = dreamer.dream()
    
    print(f"\n📋 Dream Report:")
    print(f"   Status: {report['status']}")
    print(f"   Conversations: {report['conversations_processed']}")
    print(f"   Memories extracted: {report['memories_extracted']}")
    
    return 0 if report["status"] in ("completed", "no_new_conversations", "no_new_memories") else 1


if __name__ == "__main__":
    sys.exit(main())
