from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tg_cli_relay.relay import apply_cursor_harness_prefix
from tg_cli_relay.task_router import (
    Route,
    TaskRouterConfig,
    format_router_status,
    load_task_router_config,
    route_text,
)


class TaskRouterConfigTest(unittest.TestCase):
    def test_missing_config_falls_back_to_default_rules(self) -> None:
        with patch.dict(os.environ, {"TGR_ROUTER_CONFIG": "/tmp/does-not-exist-router.toml"}):
            loaded = load_task_router_config()
        self.assertTrue(loaded.fallback_used)
        self.assertEqual(loaded.config.threshold, 3)
        decision = route_text("請重構三個檔案、補 tests、跑完整測試並建立 PR", loaded.config)
        self.assertEqual(decision.route, Route.DELEGATE)
        self.assertGreaterEqual(decision.delegate_score, loaded.config.threshold)

    def test_invalid_config_falls_back_without_raising(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "router.toml"
            path.write_text("[router\nthis is broken", encoding="utf-8")
            with patch.dict(os.environ, {"TGR_ROUTER_CONFIG": str(path)}):
                loaded = load_task_router_config()
        self.assertTrue(loaded.fallback_used)
        self.assertIn("error", loaded.message.lower())
        self.assertEqual(route_text("幫我看現在幾個 relay 在跑", loaded.config).route, Route.DIRECT)

    def test_external_toml_rules_override_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "router.toml"
            path.write_text(
                """
[router]
enabled = true
threshold = 5
prompt_header = "Custom Router"

[[rules]]
id = "force_delegate"
route = "delegate"
weight = 6
description = "custom deploy trigger"
keywords = ["blue-green"]

[[rules]]
id = "force_direct"
route = "direct"
weight = 10
description = "custom quick trigger"
keywords = ["ping"]
""".strip(),
                encoding="utf-8",
            )
            with patch.dict(os.environ, {"TGR_ROUTER_CONFIG": str(path)}):
                loaded = load_task_router_config()
        self.assertFalse(loaded.fallback_used)
        self.assertEqual(loaded.config.prompt_header, "Custom Router")
        self.assertEqual(route_text("需要 blue-green 部署方案", loaded.config).route, Route.DELEGATE)
        self.assertEqual(route_text("ping", loaded.config).route, Route.DIRECT)

    def test_batch_backfill_routes_by_semantics_not_a_fixed_count(self) -> None:
        loaded = load_task_router_config(env={"TGR_ROUTER_CONFIG": "/missing/router.toml"})
        samples = (
            "優先批次補有標籤的資料，剩下的再慢慢回補，完成後驗證報告",
            "先回補 120 檔，背景執行並持續監控進度",
            "backfill the tagged universe in batches and verify the DuckDB output",
        )
        for text in samples:
            with self.subTest(text=text):
                decision = route_text(text, loaded.config)
                self.assertEqual(decision.route, Route.DELEGATE)
                self.assertTrue(any(rule.id == "batch_backfill_job" for rule in decision.matched_rules))

    def test_single_scope_coding_is_delegated_for_cursor_implementation(self) -> None:
        loaded = load_task_router_config(env={"TGR_ROUTER_CONFIG": "/missing/router.toml"})
        for text in (
            "請實作登入頁的欄位驗證",
            "幫我修 bug 並補測試",
            "implement a cache function",
        ):
            with self.subTest(text=text):
                decision = route_text(text, loaded.config)
                self.assertEqual(decision.route, Route.DELEGATE)
                self.assertTrue(
                    any(rule.id == "coding_implementation" for rule in decision.matched_rules)
                )

    def test_implementation_beats_incidental_status_on_score_tie(self) -> None:
        cfg = load_task_router_config(env={"TGR_ROUTER_CONFIG": "/missing/router.toml"}).config
        result = route_text("現在修 bug", cfg)
        self.assertEqual(result.route, Route.DELEGATE)

    def test_explicit_worker_delegation_overrides_status_words(self) -> None:
        loaded = load_task_router_config(env={"TGR_ROUTER_CONFIG": "/missing/router.toml"})
        text = "資料是不是還沒補完？還沒補完派子WORKER去補"
        decision = route_text(text, loaded.config)
        self.assertEqual(decision.route, Route.DELEGATE)
        self.assertGreater(decision.delegate_score, decision.direct_score)
        self.assertTrue(any(rule.id == "explicit_worker_delegation" for rule in decision.matched_rules))

    def test_worker_mentions_without_dispatch_intent_stay_direct(self) -> None:
        loaded = load_task_router_config(env={"TGR_ROUTER_CONFIG": "/missing/router.toml"})
        for text in (
            "不要派 worker，直接查目前 status",
            "子 worker 現在有幾個？",
        ):
            with self.subTest(text=text):
                decision = route_text(text, loaded.config)
                self.assertEqual(decision.route, Route.DIRECT)
                self.assertFalse(
                    any(rule.id == "explicit_worker_delegation" for rule in decision.matched_rules)
                )

    def test_delegate_prompt_for_long_jobs_forbids_foreground_polling(self) -> None:
        wrapped = apply_cursor_harness_prefix("批次回補資料並等待完成")
        self.assertIn("background", wrapped.lower())
        self.assertIn("foreground polling", wrapped.lower())
        self.assertIn("reply immediately", wrapped.lower())

    def test_status_includes_rules_and_load_state(self) -> None:
        loaded = load_task_router_config(env={"TGR_ROUTER_CONFIG": "/missing/router.toml"})
        status = format_router_status(loaded)
        self.assertIn("Task Router", status)
        self.assertIn("fallback", status.lower())
        self.assertIn("/router test <文字>", status)
        self.assertIn("delegate", status.lower())


class CursorHarnessPrefixTest(unittest.TestCase):
    def test_prefix_includes_router_decision_and_dispatcher_for_delegate(self) -> None:
        prompt = "請跨多檔重構、補 tests、跑 lint/build，完成後建立 PR"
        with patch.dict(os.environ, {"TGR_ROUTER_CONFIG": "/tmp/missing-router.toml"}, clear=False):
            wrapped = apply_cursor_harness_prefix(prompt)
        self.assertIn("Task Router decision: DELEGATE", wrapped)
        self.assertIn("Job Dispatcher", wrapped)
        self.assertIn("worker session isolation", wrapped)
        self.assertTrue(wrapped.endswith(prompt))

    def test_prefix_marks_simple_questions_direct(self) -> None:
        prompt = "現在主機上有幾個 tg relay 在跑？"
        with patch.dict(os.environ, {"TGR_ROUTER_CONFIG": "/tmp/missing-router.toml"}, clear=False):
            wrapped = apply_cursor_harness_prefix(prompt)
        self.assertIn("Task Router decision: DIRECT", wrapped)
        self.assertTrue(wrapped.endswith(prompt))

    def test_router_can_be_disabled_by_config(self) -> None:
        cfg = TaskRouterConfig(enabled=False)
        decision = route_text("請重構三個檔案並跑測試", cfg)
        self.assertEqual(decision.route, Route.DIRECT)
        self.assertEqual(decision.matched_rules, ())


if __name__ == "__main__":
    unittest.main()
