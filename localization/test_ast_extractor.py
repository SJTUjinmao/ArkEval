from __future__ import annotations

import subprocess
import tempfile
import unittest
import json
import os
from pathlib import Path
from unittest.mock import patch

from localization_engine.ast.extractor import (
    _iter_ts_files_for_export_map,
    _resolve_import_to_paths,
    _run_extract_deps,
    _run_extract_deps_batch,
    get_repo_export_map,
)


class AstExtractorTest(unittest.TestCase):
    def test_relative_directory_import_resolves_index_file(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            repo = Path(root)
            source = repo / "src" / "main.ets"
            dependency = repo / "src" / "util" / "index.ts"
            source.parent.mkdir()
            dependency.parent.mkdir()
            source.write_text("import { value } from './util'", encoding="utf-8")
            dependency.write_text("export const value = 1", encoding="utf-8")

            self.assertEqual(
                _resolve_import_to_paths(source, "./util", repo),
                [dependency],
            )

    @patch("localization_engine.ast.extractor.subprocess.run")
    def test_git_file_list_is_decoded_as_utf8(self, run) -> None:
        with tempfile.TemporaryDirectory() as root:
            repo = Path(root)
            source = repo / "unicode" / "文件.ets"
            source.parent.mkdir()
            source.write_text("export const value = 1", encoding="utf-8")
            run.return_value = subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout="unicode/文件.ets\0",
                stderr="",
            )

            self.assertEqual(_iter_ts_files_for_export_map(repo), [source.resolve()])
            self.assertEqual(run.call_args.kwargs["encoding"], "utf-8")
            self.assertEqual(run.call_args.kwargs["errors"], "replace")
            self.assertEqual(run.call_args.kwargs["timeout"], 120)

    @unittest.skipUnless(os.name == "nt", "requires Windows case-insensitive paths")
    @patch("localization_engine.ast.extractor.subprocess.run")
    def test_git_file_list_deduplicates_case_aliases(self, run) -> None:
        with tempfile.TemporaryDirectory() as root:
            repo = Path(root)
            source = repo / "ImageKnifeOption.test.ets"
            source.write_text("export default function test() {}", encoding="utf-8")
            run.return_value = subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout="ImageKnifeOption.test.ets\0imageknifeOption.test.ets\0",
                stderr="",
            )

            self.assertEqual(_iter_ts_files_for_export_map(repo), [source.resolve()])

    @patch("localization_engine.ast.extractor.subprocess.run")
    def test_single_ast_failure_is_not_treated_as_empty_dependencies(self, run) -> None:
        source = Path(r"E:\repo\Index.ets")
        run.return_value = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout='{"ok":false,"error":"parse failed"}',
            stderr="",
        )
        with self.assertRaisesRegex(RuntimeError, "parse failed"):
            _run_extract_deps(source)

    @patch("localization_engine.ast.extractor.subprocess.run")
    def test_batch_ast_requires_every_input(self, run) -> None:
        first = Path(r"E:\repo\Index.ets")
        second = Path(r"E:\repo\Model.ets")
        run.return_value = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=(
                '{"results":[{"path":"E:\\\\repo\\\\Index.ets","ok":true,'
                '"imports":[],"exports":[],"typeRefs":[]}]}'
            ),
            stderr="",
        )
        with self.assertRaisesRegex(RuntimeError, "omitted 1 input files"):
            _run_extract_deps_batch([first, second])

    @patch("localization_engine.ast.extractor.subprocess.run")
    def test_batch_ast_rejects_non_json(self, run) -> None:
        run.return_value = subprocess.CompletedProcess(args=[], returncode=0, stdout="not-json", stderr="")
        with self.assertRaisesRegex(RuntimeError, "non-JSON"):
            _run_extract_deps_batch([Path(r"E:\repo\Index.ets")])

    def test_old_export_map_cache_is_rebuilt(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            repo = Path(root)
            source = repo / "Index.ets"
            source.write_text("export const value = 1", encoding="utf-8")
            cache = repo / ".codephoenix" / "cache"
            cache.mkdir(parents=True)
            (cache / "export_map.json").write_text(
                json.dumps({"Stale": [str(source)]}),
                encoding="utf-8",
            )
            (cache / "export_map_meta.json").write_text(
                json.dumps(
                    {
                        "hash_scheme": "git_blob_v1",
                        "file_hashes": {str(source): "git:abc"},
                        "file_exports": {str(source): ["Stale"]},
                    }
                ),
                encoding="utf-8",
            )
            extracted = {str(source.resolve()): {"imports": [], "exports": ["Fresh"], "typeRefs": []}}
            with (
                patch(
                    "localization_engine.ast.extractor._iter_ts_files_for_export_map",
                    return_value=[source.resolve()],
                ),
                patch(
                    "localization_engine.ast.extractor._current_file_hashes",
                    return_value={str(source.resolve()): "git:abc"},
                ),
                patch("localization_engine.ast.extractor._extract_deps_many", return_value=extracted) as run,
            ):
                export_map = get_repo_export_map(repo)
            self.assertEqual(export_map, {"Fresh": [source.resolve()]})
            run.assert_called_once()
            meta = json.loads((cache / "export_map_meta.json").read_text(encoding="utf-8"))
            self.assertEqual(meta["schema_version"], 2)


if __name__ == "__main__":
    unittest.main()
