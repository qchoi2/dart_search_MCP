from __future__ import annotations

import json
import os
import struct
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

from app.indexing import IndexNeedEvidence, evaluate_index_need
from app.mcp_server.tool_contracts import TOOLS
from installer.build_release import build
from installer.claude_config import (
    SERVER_NAME,
    config_candidates,
    discover_config,
    find_registrations,
    register_server,
    unregister_server,
)


ROOT = Path(__file__).resolve().parents[1]


class ClaudeConfigTests(unittest.TestCase):
    def test_msix_config_is_discovered_and_existing_servers_are_preserved(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            appdata = root / "Roaming"
            local = root / "Local"
            standard = appdata / "Claude" / "claude_desktop_config.json"
            msix = local / "Packages" / "Claude_test" / "LocalCache" / "Roaming" / "Claude" / "claude_desktop_config.json"
            standard.parent.mkdir(parents=True)
            standard.write_text('{"mcpServers":{"existing":{"command":"old"}}}', encoding="utf-8")
            msix.parent.mkdir(parents=True)
            msix.write_text('{"mcpServers":{"other":{"command":"keep"}},"theme":"dark"}', encoding="utf-8")
            chosen = discover_config(appdata=appdata, localappdata=local)
            self.assertEqual(chosen.path, msix)
            result = register_server(msix, {"command": "python", "args": ["-m", "app.mcp_server.server"]})
            self.assertEqual(result["status"], "updated")
            config = json.loads(msix.read_text(encoding="utf-8"))
            self.assertIn("other", config["mcpServers"])
            self.assertIn(SERVER_NAME, config["mcpServers"])
            self.assertEqual(config["theme"], "dark")
            self.assertTrue(Path(result["backup"]).exists())

    def test_registration_is_idempotent_and_uninstall_preserves_other_settings(self):
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "claude_desktop_config.json"
            server = {"command": "python", "args": ["server.py"]}
            first = register_server(path, server)
            second = register_server(path, server)
            self.assertEqual(first["status"], "updated")
            self.assertEqual(second["status"], "unchanged")
            config = json.loads(path.read_text(encoding="utf-8"))
            config["unrelated"] = True
            path.write_text(json.dumps(config), encoding="utf-8")
            removed = unregister_server(path)
            self.assertEqual(removed["status"], "removed")
            self.assertTrue(json.loads(path.read_text(encoding="utf-8"))["unrelated"])

    def test_invalid_json_is_never_overwritten(self):
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "claude_desktop_config.json"
            path.write_text("{invalid", encoding="utf-8")
            with self.assertRaises(json.JSONDecodeError):
                register_server(path, {"command": "python"})
            self.assertEqual(path.read_text(encoding="utf-8"), "{invalid")

    def test_diagnostics_marks_invalid_configs_without_disclosing_secrets(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            items = config_candidates(appdata=root / "Roaming", localappdata=root / "Local")
            items[0].path.parent.mkdir(parents=True)
            items[0].path.write_text("[]", encoding="utf-8")
            result = find_registrations(items)
            self.assertEqual(result[0]["status"], "invalid")


class ReleaseBuildTests(unittest.TestCase):
    def test_mcpb_is_deterministic_allowlisted_and_sensitive(self):
        with tempfile.TemporaryDirectory() as first_raw, tempfile.TemporaryDirectory() as second_raw:
            first = build(Path(first_raw))
            second = build(Path(second_raw))
            self.assertEqual(first["sha256"], second["sha256"])
            package = Path(first["package"])
            with zipfile.ZipFile(package) as archive:
                names = set(archive.namelist())
                self.assertTrue({"manifest.json", "server.py", "pyproject.toml", "settings.json", "icon.png"}.issubset(names))
                self.assertTrue(
                    {
                        "app/rules/search_terms.yaml",
                        "app/rules/ranking_rules.yaml",
                        "app/rules/amendment_rules.yaml",
                    }.issubset(names)
                )
                self.assertFalse(any("_local_data" in name or ".env" in name or "tests/" in name for name in names))
                manifest = json.loads(archive.read("manifest.json"))
                self.assertEqual(manifest["manifest_version"], "0.4")
                self.assertEqual(manifest["server"]["type"], "uv")
                self.assertEqual(manifest["server"]["mcp_config"]["command"], "uv")
                self.assertTrue(manifest["user_config"]["dart_api_key"]["sensitive"])
                self.assertEqual({tool["name"] for tool in manifest["tools"]}, {tool["name"] for tool in TOOLS})
                combined_text = "\n".join(
                    archive.read(name).decode("utf-8", errors="ignore")
                    for name in names
                    if name.endswith((".py", ".json", ".toml"))
                )
                self.assertNotIn("DART_API_KEY=", combined_text)

    def test_bundled_server_starts_from_unpacked_package(self):
        with tempfile.TemporaryDirectory() as output_raw, tempfile.TemporaryDirectory() as unpack_raw:
            result = build(Path(output_raw))
            with zipfile.ZipFile(result["package"]) as archive:
                archive.extractall(unpack_raw)
            request = json.dumps({"jsonrpc": "2.0", "id": 7, "method": "initialize", "params": {}}) + "\n"
            environment = dict(os.environ)
            environment.pop("DART_API_KEY", None)
            completed = subprocess.run(
                [sys.executable, "server.py"],
                cwd=unpack_raw,
                input=request,
                text=True,
                encoding="utf-8",
                capture_output=True,
                timeout=15,
                env=environment,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            response = json.loads(completed.stdout.strip())
            self.assertEqual(response["id"], 7)
            self.assertEqual(response["result"]["serverInfo"]["version"], "0.3.4")

    def test_icons_include_alpha_png_and_seven_windows_sizes(self):
        png = ROOT / "app" / "assets" / "disclosure-detective.png"
        ico = ROOT / "app" / "assets" / "disclosure-detective.ico"
        self.assertEqual(png.read_bytes()[:8], b"\x89PNG\r\n\x1a\n")
        width, height, bit_depth, color_type = struct.unpack(">IIBB", png.read_bytes()[16:26])
        self.assertEqual((width, height), (1254, 1254))
        self.assertEqual(color_type, 6)
        reserved, image_type, count = struct.unpack("<HHH", ico.read_bytes()[:6])
        self.assertEqual((reserved, image_type, count), (0, 1, 7))

    def test_guide_uses_plain_language_and_links_to_opendart(self):
        guide = (ROOT / "사용설명서.html").read_text(encoding="utf-8")
        self.assertIn("속도우선 기능", guide)
        self.assertIn("심화 검색기능", guide)
        self.assertIn("https://opendart.fss.or.kr/", guide)
        self.assertNotIn("대화형 검색예산", guide)


class PermanentIndexGateTests(unittest.TestCase):
    def test_stage8_remains_off_without_measured_need(self):
        recommendation = evaluate_index_need(IndexNeedEvidence())
        self.assertFalse(recommendation.recommended)
        self.assertEqual(recommendation.status, "not_activated")

    def test_stage8_requires_demand_and_measured_benefit(self):
        demand_only = evaluate_index_need(IndexNeedEvidence(repeated_period_searches=True))
        self.assertFalse(demand_only.recommended)
        eligible = evaluate_index_need(
            IndexNeedEvidence(repeated_market_wide_searches=True, measured_recall_or_cost_improvement=True)
        )
        self.assertTrue(eligible.recommended)
        self.assertEqual(eligible.status, "eligible_for_separate_approval")


if __name__ == "__main__":
    unittest.main()
