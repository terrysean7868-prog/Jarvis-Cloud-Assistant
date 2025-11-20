# src/utils/test_generator.py
import os
import json
import subprocess
from typing import List
from src.core.llm_adapter import LLMAdapter


llm = LLMAdapter()


TEST_DIR = os.getenv("AUTO_TEST_DIR", "tests/auto_generated")
os.makedirs(TEST_DIR, exist_ok=True)


async def generate_tests_for_change(summary: str, changed_files: List[str]) -> List[str]:
    """Use LLM to generate pytest test cases for the changed files.
    Returns list of written test file paths.
    """
    prompt = f"""
You are an assistant that writes pytest unit tests for Python code changes.
Summary of change:\n{summary}\nFiles changed:\n{changed_files}\n
Generate 1-3 focused pytest tests that validate the behavior of the changed code. Return a JSON array of objects: {{"filename": "test_x.py", "content": "<full file content>"}}
"""
    resp = await llm.generate_response(prompt, context="", mode="chat", capabilities=["test_gen"])
    # parse
    tests = []
    content = resp.get("text", "")
    # attempt JSON parse
    try:
        parsed = json.loads(content)
    except Exception:
        # fallback: extract JSON
        import re
        m = re.search(r"\[\{[\s\S]*\}\]", content)
        if m:
            parsed = json.loads(m.group())
        else:
            parsed = []


    written = []
    for obj in parsed:
        fname = obj.get("filename")
        body = obj.get("content")
        if not fname or not body:
            continue
        path = os.path.join(TEST_DIR, fname)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(body)
        written.append(path)
    return written


def run_pytest(test_paths: List[str], timeout: int = 60) -> dict:
    """Run pytest on given paths and return summary (passed/failed)."""
    try:
        cmd = ["pytest", "-q"] + test_paths
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return {"returncode": proc.returncode, "stdout": proc.stdout, "stderr": proc.stderr}
    except Exception as e:
        return {"error": str(e)}