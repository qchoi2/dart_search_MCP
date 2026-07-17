"""Build a deterministic, secret-free Claude Desktop MCPB release."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config.defaults import PRODUCT_VERSION  # noqa: E402
from app.mcp_server.tool_contracts import TOOLS  # noqa: E402


PACKAGE_NAME = "dart-disclosure-search"
TEXT_SUFFIXES = {".py", ".json", ".toml", ".html", ".md", ".cmd"}
FORBIDDEN_PARTS = {".env", "_local_data", "tests", ".git", "__pycache__", "fixtures", "query_log.jsonl", "agent_log.jsonl"}


def _manifest() -> dict:
    return {
        "$schema": "https://raw.githubusercontent.com/anthropics/mcpb/main/schemas/mcpb-manifest-v0.4.schema.json",
        "manifest_version": "0.4",
        "name": PACKAGE_NAME,
        "display_name": "공시검색 MCP",
        "version": PRODUCT_VERSION,
        "description": "한국 DART 공시를 빠르게 찾고 원문 근거 링크와 함께 보여주는 Claude Desktop 확장",
        "long_description": "속도우선 검색과 사용자가 확인한 심화 검색을 제공하며, 각 검색결과에 DART 공시 원문 링크를 포함합니다.",
        "author": {"name": "DART Search MCP Project"},
        "icon": "icon.png",
        "server": {
            "type": "uv",
            "entry_point": "server.py",
            "mcp_config": {
                "command": "uv",
                "args": ["run", "--directory", "${__dirname}", "server.py"],
                "env": {
                    "DART_API_KEY": "${user_config.dart_api_key}",
                    "DART_MCP_DATA_DIR": "${HOME}/AppData/Local/DisclosureSearchMCP/_local_data",
                    "PYTHONUTF8": "1",
                },
            },
        },
        "tools": [{"name": item["name"], "description": item["description"]} for item in TOOLS],
        "tools_generated": False,
        "keywords": ["DART", "공시", "OpenDART", "한국", "리서치"],
        "compatibility": {"claude_desktop": ">=0.10.0", "platforms": ["win32"], "runtimes": {"python": ">=3.10"}},
        "user_config": {
            "dart_api_key": {
                "type": "string",
                "title": "OpenDART API 인증키",
                "description": "OpenDART에서 발급받은 40자리 API 인증키입니다. Claude Desktop의 보안 저장소에 보관됩니다.",
                "required": True,
                "sensitive": True,
            }
        },
    }


def _write_package_tree(stage: Path) -> None:
    if stage.exists():
        shutil.rmtree(stage)
    stage.mkdir(parents=True)
    for source in sorted((ROOT / "app").rglob("*.py")):
        if "__pycache__" in source.parts:
            continue
        target = stage / source.relative_to(ROOT)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    for source in sorted((ROOT / "app" / "rules").glob("*.yaml")):
        target = stage / source.relative_to(ROOT)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    shutil.copy2(ROOT / "settings.json", stage / "settings.json")
    shutil.copy2(ROOT / "app" / "assets" / "disclosure-detective.png", stage / "icon.png")
    (stage / "server.py").write_text(
        "from app.mcp_server.server import main\n\nif __name__ == '__main__':\n    main()\n",
        encoding="utf-8",
        newline="\n",
    )
    (stage / "pyproject.toml").write_text(
        "[project]\nname = \"dart-disclosure-search\"\nversion = \"" + PRODUCT_VERSION + "\"\nrequires-python = \">=3.10\"\ndependencies = []\n",
        encoding="utf-8",
        newline="\n",
    )
    (stage / "manifest.json").write_text(
        json.dumps(_manifest(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def _validate_tree(stage: Path) -> None:
    manifest = json.loads((stage / "manifest.json").read_text(encoding="utf-8"))
    required = {"name", "version", "description", "author", "server"}
    if not required.issubset(manifest):
        raise ValueError("MCPB manifest required fields are missing")
    if manifest["manifest_version"] != "0.4" or manifest["server"]["type"] != "uv":
        raise ValueError("unsupported MCPB contract")
    if not manifest["user_config"]["dart_api_key"].get("sensitive"):
        raise ValueError("API key must be sensitive")
    for path in stage.rglob("*"):
        relative_parts = set(path.relative_to(stage).parts)
        if relative_parts & FORBIDDEN_PARTS:
            raise ValueError(f"forbidden release path: {path}")
        if path.is_file() and path.suffix.lower() in TEXT_SUFFIXES:
            text = path.read_text(encoding="utf-8")
            for line in text.splitlines():
                if line.strip().startswith("DART_API_KEY=") and "${user_config.dart_api_key}" not in line:
                    raise ValueError(f"possible API key in release: {path}")


def _zip_tree(stage: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for source in sorted(path for path in stage.rglob("*") if path.is_file()):
            info = zipfile.ZipInfo(source.relative_to(stage).as_posix(), date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            archive.writestr(info, source.read_bytes(), compresslevel=9)


def build(output_dir: Path) -> dict:
    stage = ROOT / "build" / "mcpb-root"
    _write_package_tree(stage)
    _validate_tree(stage)
    package = output_dir / f"공시검색-MCP-{PRODUCT_VERSION}.mcpb"
    _zip_tree(stage, package)
    guide_text = (ROOT / "사용설명서.html").read_text(encoding="utf-8").replace(
        "app/assets/disclosure-detective.png", "disclosure-detective.png"
    )
    (output_dir / "사용설명서.html").write_text(guide_text, encoding="utf-8", newline="\n")
    shutil.copy2(ROOT / "app" / "assets" / "disclosure-detective.png", output_dir / "disclosure-detective.png")
    shutil.copy2(ROOT / "app" / "assets" / "disclosure-detective.ico", output_dir / "disclosure-detective.ico")
    digest = hashlib.sha256(package.read_bytes()).hexdigest()
    checksum = output_dir / "SHA256SUMS.txt"
    checksum.write_text(f"{digest}  {package.name}\n", encoding="utf-8", newline="\n")
    return {"package": str(package), "sha256": digest, "guide": str(output_dir / "사용설명서.html")}


def main() -> None:
    parser = argparse.ArgumentParser(description="공시검색 MCP 배포 패키지를 만듭니다.")
    parser.add_argument("--output", type=Path, default=ROOT / "dist")
    args = parser.parse_args()
    print(json.dumps(build(args.output.resolve()), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
