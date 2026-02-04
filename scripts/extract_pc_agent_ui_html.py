from __future__ import annotations

from pathlib import Path


def main() -> int:
    repo_root = Path(__file__).resolve().parents[1]
    src_path = repo_root / "pc_agent_app.py"
    src = src_path.read_text(encoding="utf-8")

    marker = '"""<!doctype html>'
    start = src.find(marker)
    if start == -1:
        raise SystemExit("Could not find embedded HTML marker")

    content_start = start + 3  # skip opening triple quotes
    end = src.find('"""', content_start)
    if end == -1:
        raise SystemExit("Could not find end of embedded HTML")

    html = src[content_start:end]

    out = repo_root / "assets" / "pc_agent_ui.html"
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.exists():
        print("assets/pc_agent_ui.html already exists; not overwriting")
        return 0

    out.write_text(html, encoding="utf-8")
    print(f"Wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
