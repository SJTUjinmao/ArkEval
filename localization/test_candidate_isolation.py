from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from localization_engine.locate_flow import (
    _aggregate_file_scores,
    ask_llm_to_add_deps,
    _current_repo_path,
    _parse_llm_selected_paths,
    _tiered_top_k_hits,
)
from localization_engine.types import SearchHit
from localization_engine.utils.hashing import sha256_text


def hit(path: Path, score: float) -> SearchHit:
    absolute = path.resolve()
    lines = absolute.read_text(encoding="utf-8").splitlines() if absolute.is_file() else ["missing"]
    end = len(lines)
    chunk_text = "\n".join(lines)
    return SearchHit(
        file_path=str(absolute),
        line_start=1,
        line_end=end,
        chunk_hash=sha256_text(f"{absolute}:1:{end}:{chunk_text}"),
        score=score,
        extra={},
    )


class CandidateIsolationTest(unittest.TestCase):
    def test_foreign_worker_hit_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            base = Path(root)
            run01 = base / "run01" / "repo"
            run03 = base / "run03" / "repo"
            run01.mkdir(parents=True)
            run03.mkdir(parents=True)
            foreign = run03 / "Index.ets"
            foreign.write_text("foreign", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "foreign localization hit"):
                _current_repo_path(run01.resolve(), foreign)

    def test_same_file_chunks_keep_max_score(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            repo = Path(root).resolve()
            source = repo / "Index.ets"
            source.write_text("source", encoding="utf-8")
            candidates = _aggregate_file_scores(repo, [hit(source, 0.4), hit(source, 0.8)])
            self.assertEqual(len(candidates), 1)
            self.assertEqual(next(iter(candidates.values()))[1], 0.8)

    def test_stale_same_worker_hit_is_filtered(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            repo = Path(root).resolve()
            stale = repo / "removed.ets"
            candidates = _aggregate_file_scores(repo, [hit(stale, 0.9)])
            self.assertEqual(candidates, {})

    def test_same_path_old_content_hit_is_filtered(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            repo = Path(root).resolve()
            source = repo / "Index.ets"
            source.write_text("old", encoding="utf-8")
            stale_hit = hit(source, 0.9)
            source.write_text("new", encoding="utf-8")
            self.assertEqual(_aggregate_file_scores(repo, [stale_hit]), {})

    def test_tiered_top_k_is_monotonic_at_boundaries(self) -> None:
        for left, right in ((100, 101), (500, 501), (2000, 2001)):
            self.assertLessEqual(_tiered_top_k_hits(left), _tiered_top_k_hits(right))

    def test_case_variants_share_one_relative_key(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            repo = Path(root).resolve()
            source = repo / "Index.ets"
            source.write_text("source", encoding="utf-8")
            absolute, relative, key = _current_repo_path(repo, source)
            self.assertEqual(Path(absolute), source)
            self.assertEqual(key, relative.casefold())

    def test_llm_path_list_requires_complete_json(self) -> None:
        candidates = [r"E:\repo\Index.ets", r"E:\repo\Model.ets"]
        parsed, paths = _parse_llm_selected_paths(
            '```json\n["E:\\\\repo\\\\Index.ets"]\n```',
            candidates,
        )
        self.assertTrue(parsed)
        self.assertEqual(paths, [candidates[0]])

        parsed, paths = _parse_llm_selected_paths(
            '["E:\\\\repo\\\\Index.ets", "E:\\\\repo\\\\Model',
            candidates,
        )
        self.assertFalse(parsed)
        self.assertEqual(paths, [])

    def test_llm_dependency_list_accepts_only_valid_empty_array(self) -> None:
        candidates = [r"E:\repo\Dependency.ets"]
        self.assertEqual(_parse_llm_selected_paths("[]", candidates), (True, []))
        self.assertEqual(_parse_llm_selected_paths("", candidates), (False, []))
        self.assertEqual(
            _parse_llm_selected_paths('["E:\\\\repo\\\\Unknown.ets"]', candidates),
            (False, []),
        )

    @patch("localization_engine.locate_flow._trace_row")
    @patch("localization_engine.locate_flow._trace_llm")
    @patch("localization_engine.locate_flow._should_use_localization_llm", return_value=True)
    @patch("localization_engine.llm.client.ModelScopeLLMClient")
    @patch("localization_engine.config.load_config")
    def test_llm_dependency_expansion_retries_invalid_json(
        self,
        load_config: MagicMock,
        client_class: MagicMock,
        _should_use_llm: MagicMock,
        _trace_llm: MagicMock,
        _trace_row: MagicMock,
    ) -> None:
        config = MagicMock()
        config.llm.base_url = "https://example.invalid/v1"
        config.llm.api_key = "token"
        config.llm.model_name = "kimi-k2.7-code"
        config.llm.endpoint_path = "chat/completions"
        config.llm.timeout_seconds = 120.0
        config.llm.max_retries = 3
        config.llm.max_tokens = 8192
        load_config.return_value = config
        client_class.return_value.chat.side_effect = ['["E:\\\\repo\\\\Dependency.ets"', "[]"]

        core = r"E:\repo\Index.ets"
        dependency = r"E:\repo\Dependency.ets"
        self.assertEqual(
            ask_llm_to_add_deps(Path(r"E:\repo"), "query", [core], [(dependency, core)]),
            [],
        )
        self.assertEqual(client_class.return_value.chat.call_count, 2)
        self.assertTrue(
            any(call.args[0].get("stage") == "llm_dep_expansion_invalid" for call in _trace_llm.call_args_list)
        )


if __name__ == "__main__":
    unittest.main()
