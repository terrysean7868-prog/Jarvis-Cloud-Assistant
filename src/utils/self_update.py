# src/utils/self_update.py
"""
Enhanced self-update system that allows Jarvis to update, add, and edit itself
via voice commands with automatic GitHub version control.
"""
import os
import re
import json
import time
import logging
import importlib
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Optional
from src.utils.git_sync import git_sync
from src.config.config import Config

logger = logging.getLogger("jarvis.self_update")

# Lazy-load OpenAI client to avoid errors during import if API key is not set
_client = None

def get_openai_client():
    """Get or create OpenAI client (lazy-loaded)."""
    global _client
    if _client is None:
        try:
            from openai import OpenAI
        except Exception as e:
            raise RuntimeError(f"OpenAI SDK not available: {e}")
        api_key = os.getenv("OPENAI_API_KEY") or os.getenv("PRIMARY_API_KEY")
        if not api_key:
            raise RuntimeError("OpenAI API key not found in OPENAI_API_KEY or PRIMARY_API_KEY")
        _client = OpenAI(api_key=api_key)
    return _client


ROOT_DIR = Path(__file__).parent.parent.parent
MODULES_DIR = ROOT_DIR / "modules"
SRC_DIR = ROOT_DIR / "src"
FRONTEND_DIR = ROOT_DIR / "jarvis-frontend" / "src"


def extract_code_blocks(text: str) -> List[str]:
    """Extract all code blocks from AI response."""
    patterns = [
        r"```(?:python|javascript|jsx|css|json|html)?\s*([\s\S]*?)```",
        r"```([\s\S]*?)```"
    ]
    blocks = []
    for pattern in patterns:
        matches = re.findall(pattern, text)
        blocks.extend([m.strip() for m in matches if m.strip()])
    return blocks if blocks else [text.strip()]


def analyze_file_structure(file_path: Path) -> Dict:
    """Analyze file to understand its structure and dependencies."""
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            content = f.read()
        
        return {
            "path": str(file_path),
            "size": len(content),
            "lines": content.count('\n'),
            "language": file_path.suffix,
            "has_imports": bool(re.search(r'^(import|from)\s+', content, re.MULTILINE)),
            "has_functions": bool(re.search(r'^\s*def\s+\w+', content, re.MULTILINE)),
            "has_classes": bool(re.search(r'^\s*class\s+\w+', content, re.MULTILINE))
        }
    except Exception as e:
        logger.error(f"Error analyzing file {file_path}: {e}")
        return {}


def generate_update_code(description: str, file_path: Optional[Path] = None, context: str = "") -> str:
    """Generate code update using AI."""
    system_prompt = """You are an expert Python/JavaScript developer helping improve a Jarvis AI assistant.
You must provide ONLY valid code without explanations, markdown formatting, or comments outside code blocks.
Follow existing code style and patterns. Ensure code is production-ready and error-handled."""

    if file_path and file_path.exists():
        with open(file_path, 'r', encoding='utf-8') as f:
            existing_code = f.read()
        user_prompt = f"""Update this file based on: {description}

Existing code:
{existing_code[:3000]}

Context: {context}

Provide the complete updated code file."""
    else:
        user_prompt = f"""Create new code based on: {description}

Context: {context}

Provide complete, production-ready code."""

    try:
        client = get_openai_client()
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ],
            temperature=0.3,
            max_tokens=4000
        )
        code = response.choices[0].message.content
        blocks = extract_code_blocks(code)
        return blocks[0] if blocks else code
    except Exception as e:
        logger.error(f"AI code generation failed: {e}")
        raise


def validate_code(code: str, file_path: Path) -> bool:
    """Basic code validation."""
    if not code or len(code.strip()) < 10:
        return False
    
    # Python syntax check
    if file_path.suffix == '.py':
        try:
            compile(code, str(file_path), 'exec')
            return True
        except SyntaxError as e:
            logger.warning(f"Syntax error in generated code: {e}")
            return False
    
    return True


def apply_file_update(file_path: Path, new_code: str, backup: bool = True) -> bool:
    """Apply code update to file with backup."""
    try:
        # Create backup
        if backup and file_path.exists():
            backup_dir = ROOT_DIR / "backups"
            backup_dir.mkdir(exist_ok=True)
            timestamp = int(time.time())
            backup_path = backup_dir / f"{file_path.stem}_{timestamp}{file_path.suffix}"
            with open(file_path, 'r', encoding='utf-8') as f:
                with open(backup_path, 'w', encoding='utf-8') as bf:
                    bf.write(f.read())
            logger.info(f"Backup created: {backup_path}")

        # Write new code
        file_path.parent.mkdir(parents=True, exist_ok=True)
        with open(file_path, 'w', encoding='utf-8') as f:
            f.write(new_code)
        
        logger.info(f"File updated: {file_path}")
        return True
    except Exception as e:
        logger.error(f"Failed to update file {file_path}: {e}")
        return False


def reload_module(module_name: str):
    """Hot-reload a Python module."""
    try:
        if module_name in ['app', 'jarvis_brain', 'executor']:
            mod_path = f"src.core.{module_name}" if module_name != 'app' else module_name
        else:
            mod_path = f"modules.{module_name}"
        
        if mod_path in importlib.sys.modules:
            importlib.reload(importlib.sys.modules[mod_path])
            logger.info(f"Module reloaded: {mod_path}")
    except Exception as e:
        logger.warning(f"Module reload failed: {e}")


def self_update_file(description: str, file_path_str: str) -> Dict:
    """
    Update a specific file based on voice command description.
    Returns status dict with success/error info.
    """
    try:
        file_path = Path(file_path_str)
        if not file_path.is_absolute():
            # Try to find file in common directories
            search_dirs = [ROOT_DIR, MODULES_DIR, SRC_DIR, FRONTEND_DIR]
            found = False
            for base_dir in search_dirs:
                candidate = base_dir / file_path
                if candidate.exists():
                    file_path = candidate
                    found = True
                    break
                # Also try with filename only
                candidate = base_dir / file_path.name
                if candidate.exists():
                    file_path = candidate
                    found = True
                    break
            
            if not found:
                # Create new file in appropriate location
                if file_path_str.endswith('.py'):
                    file_path = MODULES_DIR / file_path.name
                elif file_path_str.endswith(('.jsx', '.js', '.css')):
                    file_path = FRONTEND_DIR / file_path.name
                else:
                    file_path = ROOT_DIR / file_path.name

        # Analyze existing file if it exists
        context = ""
        if file_path.exists():
            analysis = analyze_file_structure(file_path)
            context = f"File type: {analysis.get('language')}, Lines: {analysis.get('lines')}"

        # Generate updated code
        logger.info(f"Generating update for {file_path} based on: {description}")
        new_code = generate_update_code(description, file_path if file_path.exists() else None, context)

        # Validate code
        if not validate_code(new_code, file_path):
            return {
                "status": "error",
                "message": "Generated code failed validation"
            }

        # Apply update
        if not apply_file_update(file_path, new_code):
            return {
                "status": "error",
                "message": "Failed to write file"
            }

        # Reload if Python module
        if file_path.suffix == '.py' and file_path.exists():
            module_name = file_path.stem
            reload_module(module_name)

        # Auto-sync to GitHub
        try:
            commit_msg = f"Self-update: {description[:50]}"
            git_sync(repo_path=str(ROOT_DIR), commit_msg=commit_msg)
            logger.info("Changes synced to GitHub")
        except Exception as git_err:
            logger.warning(f"Git sync failed: {git_err}")

        return {
            "status": "success",
            "message": f"File {file_path.name} updated and synced",
            "path": str(file_path)
        }

    except Exception as e:
        logger.error(f"Self-update failed: {e}")
        return {
            "status": "error",
            "message": str(e)
        }


def self_add_feature(description: str, feature_type: str = "module") -> Dict:
    """
    Add a new feature (module, component, etc.) based on voice command.
    """
    try:
        if feature_type == "module":
            # Generate module name from description
            module_name = re.sub(r'[^a-zA-Z0-9_]', '_', description.lower()[:30])
            file_path = MODULES_DIR / f"{module_name}.py"
            
            prompt = f"Create a new Python module for: {description}"
            code = generate_update_code(prompt, None, "Telegram bot module with register function")
            
        elif feature_type == "component":
            component_name = re.sub(r'[^a-zA-Z0-9]', '', description.title()[:30])
            file_path = FRONTEND_DIR / "components" / f"{component_name}.jsx"
            prompt = f"Create a React component for: {description}"
            code = generate_update_code(prompt, None, "React functional component")
            
        else:
            return {"status": "error", "message": f"Unknown feature type: {feature_type}"}

        if not validate_code(code, file_path):
            return {"status": "error", "message": "Generated code invalid"}

        if not apply_file_update(file_path, code):
            return {"status": "error", "message": "Failed to create file"}

        # Sync to GitHub
        try:
            commit_msg = f"Added {feature_type}: {description[:50]}"
            git_sync(repo_path=str(ROOT_DIR), commit_msg=commit_msg)
        except Exception as git_err:
            logger.warning(f"Git sync failed: {git_err}")

        return {
            "status": "success",
            "message": f"{feature_type.title()} created: {file_path.name}",
            "path": str(file_path)
        }

    except Exception as e:
        logger.error(f"Add feature failed: {e}")
        return {"status": "error", "message": str(e)}


def parse_voice_command(text: str) -> Optional[Dict]:
    """
    Parse voice command to extract update intent.
    Returns dict with action, target, and description.
    """
    text_lower = text.lower().strip()
    
    # Patterns for self-update commands
    patterns = {
        "update": [
            r"(?:update|modify|improve|change|edit)\s+(?:the\s+)?(?:file\s+)?([^\s]+(?:\s+[^\s]+)*?)(?:\s+(?:with|to|by|using)\s+)?(.+?)(?:\.|$)",
            r"(?:update|modify|improve)\s+(.+?)(?:\s+(?:in|to|with)\s+)?(.+?)(?:\.|$)"
        ],
        "add": [
            r"(?:add|create|make|build)\s+(?:a\s+)?(?:new\s+)?(module|component|feature|file)\s+(?:called\s+)?([^\s]+(?:\s+[^\s]+)*?)(?:\s+(?:that|which|to)\s+)?(.+?)(?:\.|$)",
            r"(?:add|create)\s+(.+?)(?:\s+(?:module|component|feature))"
        ],
        "edit": [
            r"(?:edit|change|modify)\s+(?:the\s+)?([^\s]+(?:\s+[^\s]+)*?)(?:\s+(?:to|with|by)\s+)?(.+?)(?:\.|$)"
        ]
    }

    for action, action_patterns in patterns.items():
        for pattern in action_patterns:
            match = re.search(pattern, text_lower, re.IGNORECASE)
            if match:
                groups = match.groups()
                if action == "update":
                    if len(groups) >= 2:
                        return {
                            "action": "update",
                            "target": groups[0].strip(),
                            "description": groups[1].strip()
                        }
                elif action == "add":
                    if len(groups) >= 2:
                        feature_type = groups[0] if groups[0] in ["module", "component", "feature", "file"] else "module"
                        name = groups[1] if len(groups) > 1 else "new_feature"
                        desc = groups[2] if len(groups) > 2 else text
                        return {
                            "action": "add",
                            "feature_type": feature_type,
                            "name": name,
                            "description": desc
                        }
                elif action == "edit":
                    if len(groups) >= 2:
                        return {
                            "action": "edit",
                            "target": groups[0].strip(),
                            "description": groups[1].strip()
                        }

    return None

