import re
from pathlib import Path

root = Path(__file__).resolve().parents[1]
allowed = {
    "GEMINI_API_KEY",
    "GROQ_API_KEY",
    "JARVIS_ALLOWED_PATHS",
    "JARVIS_JWT_ISSUER",
    "JARVIS_JWT_SECRET",
    "JARVIS_REDIS_URL",
    "MONGODB_DB_NAME",
    "MONGODB_URI",
    "OPENAI_API_KEY",
    "OPENWEATHER_KEY",
    "TELEGRAM_TOKEN",
    "VOICE_MAX_SAMPLES",
    "VOICE_TEXT_SIMILARITY_THRESHOLD",
}

pat = re.compile(
    r"os\.getenv\(\s*['\"]([A-Z][A-Z0-9_]*)['\"]"
    r"|os\.environ\.get\(\s*['\"]([A-Z][A-Z0-9_]*)['\"]"
    r"|os\.environ\[\s*['\"]([A-Z][A-Z0-9_]*)['\"]\]"
    r"|env\.get(?:_str|_int|_float|_bool)?\(\s*['\"]([A-Z][A-Z0-9_]*)['\"]"
    r"|process\.env\.([A-Z][A-Z0-9_]*)"
)

files = []
for d in ["src", "apps", "scripts", "frontend/src"]:
    p = root / d
    if p.exists():
        for x in p.rglob("*"):
            if x.is_file() and x.suffix.lower() in {".py", ".js", ".jsx", ".ts", ".tsx"}:
                files.append(x)

usage = {}
for f in files:
    txt = f.read_text(encoding="utf-8", errors="ignore")
    for i, line in enumerate(txt.splitlines(), start=1):
        for m in pat.finditer(line):
            key = next(g for g in m.groups() if g)
            usage.setdefault(key, []).append((f.relative_to(root).as_posix(), i))

all_keys = sorted(usage.keys())
non = [k for k in all_keys if k not in allowed]

print(f"TOTAL_KEYS={len(all_keys)}")
print(f"WHITELIST_KEYS={len(all_keys) - len(non)}")
print(f"NON_WHITELIST_KEYS={len(non)}")
print("NON_WHITELIST_LIST=" + ",".join(non))
for k in non:
    loc = usage[k][0]
    print(f"NON::{k}::{loc[0]}:{loc[1]}")
