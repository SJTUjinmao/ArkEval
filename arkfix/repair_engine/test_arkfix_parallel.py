from __future__ import annotations

import hashlib
import importlib.util
import inspect
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock


ENGINE_ROOT = Path(__file__).resolve().parent
WORKSPACE_ROOT = ENGINE_ROOT.parents[1]
for import_root in (ENGINE_ROOT, WORKSPACE_ROOT):
    if str(import_root) not in sys.path:
        sys.path.insert(0, str(import_root))

from arkfix import run_repair
import run_arkts_pipeline
from run import Main, SaveApplyPatchHook, _append_retry_build_feedback
from scripts.run_arkts_model_patch_batch import (
    DEFAULT_REPO_POOL,
    DatasetRow,
    PatchCandidate,
    TimingInfo,
    WorkerSpec,
    assign_rows_to_slots,
    build_worker_specs,
    check_repo_slots,
    collect_candidates,
    compatible_slots_by_row,
    discover_repo_slots,
    find_trajectory_dir,
    is_patch_only_config,
    load_resume_specs,
    localization_preflight_failures,
    parse_args as parse_batch_args,
    run_workers,
    serial_eval_apply_check,
    select_base_aware_active_slots,
    write_outputs,
)
from multi_swe_bench.harness.image import Config
from multi_swe_bench.harness.instance import Instance
from multi_swe_bench.harness.pull_request import Base, PullRequest
from sweagent.agent.agents import Agent, AgentConfig
from sweagent.environment.swe_env import SWEEnv, _agent_command_format_feedback
from sweagent.environment.utils import (
    NoOutputTimeoutError,
    PROCESS_DONE_MARKER_END,
    PROCESS_DONE_MARKER_START,
    get_git_bash_path,
    get_native_shell,
    native_build_permit,
    read_with_timeout_experimental,
    terminate_process_tree,
)
from sweagent.utils.patch_utils import defect_tree_sha256
from sweagent.utils.patch_utils import find_arkts_forbidden_added_syntax
from sweagent.utils.patch_utils import filter_submission_remove_self_tests
from sweagent.utils.patch_utils import is_agent_self_test_patch_path
from sweagent.utils.patch_utils import scope_ranked_defect_files_to_harmony_project
from sweagent.utils.repair_status import compute_repair_status
from sweagent.utils import native_repo
from sweagent.agent.models import OpenAIModel, _stop_openai_retry, _wait_openai_retry
from tools.common import apply_vpn_extension_api20_profile_adapter
from evaluation.run_llm_patch_eval import _determine_evaluation_scope
import evaluation.run_llm_patch_eval as repair_evaluator


class ArkFixParallelTest(unittest.TestCase):
    def test_retry_build_feedback_is_scoped_to_instance(self) -> None:
        raw = json.dumps({"row38": "HardKeyUtils.ets:62:5 ',' expected."})
        self.assertEqual(_append_retry_build_feedback("issue", "other", raw), "issue")
        self.assertIn(
            "PRIOR REAL BUILD FEEDBACK:\nHardKeyUtils.ets:62:5 ',' expected.",
            _append_retry_build_feedback("issue", "row38", raw) or "",
        )

    def test_retry_build_feedback_rejects_non_object_json(self) -> None:
        with self.assertRaises(ValueError):
            _append_retry_build_feedback("issue", "row38", "[]")

    def test_repair_evaluator_uses_canonical_arkeval_build_tools(self) -> None:
        self.assertEqual(
            repair_evaluator.TOOLS_DIR.resolve(),
            (WORKSPACE_ROOT / "evaluation" / "command_line_tools_test" / "tools").resolve(),
        )

    def test_canonical_kika_preprocess_repairs_base_syntax_error(self) -> None:
        tools_dir = WORKSPACE_ROOT / "evaluation" / "command_line_tools_test" / "tools"
        sys.path.insert(0, str(tools_dir))
        try:
            spec = importlib.util.spec_from_file_location("arkeval_canonical_common", tools_dir / "common.py")
            assert spec is not None and spec.loader is not None
            module = importlib.util.module_from_spec(spec)
            sys.modules[spec.name] = module
            spec.loader.exec_module(module)
        finally:
            sys.path.remove(str(tools_dir))
        with tempfile.TemporaryDirectory() as raw_temp:
            repo = Path(raw_temp) / "CompleteApps" / "KikaInput"
            target = repo / "entry" / "src" / "main" / "ets" / "model" / "HardKeyUtils.ets"
            target.parent.mkdir(parents=True)
            target.write_text('const keys = {\n    2043: ","\n    2044: ".",\n}\n', encoding="utf-8")
            notes = module._prepare_kika_input_legacy_build_shims(repo)
            self.assertIn('    2043: ",",\n    2044: ".",', target.read_text(encoding="utf-8"))
            self.assertIn("ENV_PREPARE_KIKA_INPUT_SYNTAX=HardKeyUtils.ets added_missing_comma", notes)

    def test_evaluator_rebases_runtime_project_paths_before_module_selection(self) -> None:
        patch = (
            "diff --git a/CompleteApps/App/entry/src/main/ets/Foo.ets "
            "b/CompleteApps/App/entry/src/main/ets/Foo.ets\n"
            "--- a/CompleteApps/App/entry/src/main/ets/Foo.ets\n"
            "+++ b/CompleteApps/App/entry/src/main/ets/Foo.ets\n"
            "@@ -1 +1 @@\n-before\n+after\n"
        )
        entry = {
            "project_path": "CompleteApps/App",
            "defect_files": ["CompleteApps/App/entry/src/main/ets/Foo.ets"],
        }
        modules = [{"name": "entry", "type": "entry", "srcPath": "./entry", "dir": "entry"}]
        with mock.patch("evaluation.run_llm_patch_eval._project_modules", return_value=modules):
            scope = _determine_evaluation_scope(Path("."), entry, patch, "")
        self.assertEqual(scope["paths"], ["entry/src/main/ets/Foo.ets"])
        self.assertEqual([module["name"] for module in scope["build_modules"]], ["entry"])

    def test_forbidden_arkts_syntax_is_rejected_before_submit_finishes(self) -> None:
        bad_patch = (
            "diff --git a/src/main/ets/Foo.ets b/src/main/ets/Foo.ets\n"
            "--- a/src/main/ets/Foo.ets\n+++ b/src/main/ets/Foo.ets\n"
            "@@ -1 +1 @@\n-let value = 1\n+var value = 1\n"
        )
        harmless_patch = bad_patch.replace("+var value = 1", "+let value = 'var value = 1'")
        ts_patch = bad_patch.replace("Foo.ets", "Foo.ts")
        self.assertIn("var declaration", find_arkts_forbidden_added_syntax(bad_patch) or "")
        self.assertIsNone(find_arkts_forbidden_added_syntax(harmless_patch))
        self.assertIsNone(find_arkts_forbidden_added_syntax(ts_patch))
        prompt = (ENGINE_ROOT / "config" / "arkts_system_prompt_no_demo_patch_only.yaml").read_text(
            encoding="utf-8"
        )
        self.assertIn("禁止 `var`、`any`", prompt)

    def test_non_utf8_defect_file_uses_applyable_binary_patch(self) -> None:
        with tempfile.TemporaryDirectory() as raw_temp:
            repo = Path(raw_temp) / "repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            subprocess.run(["git", "-C", str(repo), "config", "core.autocrlf", "false"], check=True)
            subprocess.run(["git", "-C", str(repo), "config", "user.email", "arkfix@example.invalid"], check=True)
            subprocess.run(["git", "-C", str(repo), "config", "user.name", "ArkFix Test"], check=True)
            target = repo / "src" / "main" / "ets" / "Foo.ets"
            target.parent.mkdir(parents=True)
            target.write_bytes("let message = '中文';\n".encode("gb18030"))
            subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
            subprocess.run(["git", "-C", str(repo), "commit", "-qm", "base"], check=True)
            base = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
            expected = "let message = '修复';\n".encode("gb18030")
            target.write_bytes(expected)
            subprocess.run(["git", "-C", str(repo), "add", "--", "src/main/ets/Foo.ets"], check=True)

            env = object.__new__(SWEEnv)
            env.native_mode = True
            env.native_workdir = repo
            env._native_env_base_tree = base
            env.record = mock.Mock()
            env.record.data = {"defect_files": ["src/main/ets/Foo.ets"]}
            patch = env._native_cached_git_diff_submission()
            self.assertIsNotNone(patch)
            self.assertIn("GIT binary patch", patch or "")

            subprocess.run(["git", "-C", str(repo), "reset", "--hard", "-q", base], check=True)
            applied = subprocess.run(
                ["git", "-C", str(repo), "apply", "--binary", "-"],
                input=(patch or "").encode("utf-8"),
                capture_output=True,
                check=False,
            )
            self.assertEqual(applied.returncode, 0, applied.stderr.decode("utf-8", errors="replace"))
            self.assertEqual(target.read_bytes(), expected)

    def test_defaults_are_twenty_workers_with_serial_check_and_eight_builds(self) -> None:
        batch_args = parse_batch_args([])
        wrapper_args = run_repair.parse_args([])
        self.assertEqual(batch_args.workers, 20)
        self.assertEqual(batch_args.build_concurrency, 8)
        self.assertTrue(batch_args.serial_apply_check)
        self.assertEqual(batch_args.worker_task_batch_size, 3)
        self.assertEqual(batch_args.worker_start_interval_seconds, 0.25)
        self.assertEqual(wrapper_args.workers, 20)
        self.assertEqual(wrapper_args.build_concurrency, 8)
        self.assertTrue(wrapper_args.serial_apply_check)
        self.assertEqual(wrapper_args.worker_task_batch_size, 3)
        self.assertEqual(wrapper_args.worker_start_interval_seconds, 0.25)
        pipeline_args = run_arkts_pipeline.parse_args(["--rows", "1"])
        self.assertEqual(pipeline_args.workers, 20)
        self.assertEqual(pipeline_args.build_concurrency, 8)
        self.assertEqual(pipeline_args.worker_task_batch_size, 3)
        self.assertEqual(pipeline_args.worker_start_interval_seconds, 0.25)

    def test_repair_status_runs_in_subshell_and_preserves_project_cwd(self) -> None:
        for command_file in (
            ENGINE_ROOT / "config" / "commands" / "defaults.sh",
            ENGINE_ROOT / "config" / "commands" / "cursors_defaults.sh",
        ):
            text = command_file.read_text(encoding="utf-8")
            body = text.split("repair_status() {", 1)[1].split("\n}", 1)[0]
            self.assertIn("\n    (\n", body)
            self.assertIn('cd "$ROOT" || exit 1', body)

    def test_failed_batch_can_resume_with_same_identity_and_recheck_cleanup_slots(self) -> None:
        with tempfile.TemporaryDirectory() as raw_temp:
            output_dir = Path(raw_temp)
            batch_run_id = "a" * 32
            (output_dir / "batch_metadata.json").write_text(
                json.dumps({"batch_run_id": batch_run_id}),
                encoding="utf-8",
            )
            workers = [
                {
                    "attempt": 1,
                    "worker": 1,
                    "rows": [1],
                    "repo_dir": str(output_dir / "run01"),
                    "batch_run_id": batch_run_id,
                    "started_at_epoch": 1.0,
                    "suffix": "attempt01_w1_row_0001",
                    "log_path": str(output_dir / "row1.log"),
                    "trajectory_dir": str(output_dir / "trajectory1"),
                    "exit_code": 0,
                    "cleanup_error": "",
                },
                {
                    "attempt": 1,
                    "worker": 2,
                    "rows": [2],
                    "repo_dir": str(output_dir / "run02"),
                    "batch_run_id": batch_run_id,
                    "started_at_epoch": 2.0,
                    "suffix": "attempt01_w2_row_0002",
                    "log_path": str(output_dir / "row2.log"),
                    "trajectory_dir": "",
                    "exit_code": 1,
                    "cleanup_error": "repo:clean:timed out",
                },
            ]
            (output_dir / "batch_failure_report.json").write_text(
                json.dumps({"workers": workers}),
                encoding="utf-8",
            )

            resumed_id, specs = load_resume_specs(output_dir)

        self.assertEqual(resumed_id, batch_run_id)
        self.assertEqual([spec.attempt for spec in specs], [1, 1])
        self.assertEqual(specs[0].trajectory_dir.name, "trajectory1")
        self.assertEqual(specs[1].cleanup_error, "repo:clean:timed out")
        self.assertTrue(parse_batch_args(["--resume-existing"]).resume_existing)
        main_source = inspect.getsource(sys.modules[load_resume_specs.__module__].main)
        self.assertIn("quarantined_slots.update", main_source)
        self.assertIn("quarantined_slots = set()", main_source)
        self.assertNotIn("no retry was attempted", main_source)

    def test_native_repo_cleanup_handles_windows_junctions_submodules_and_case_collisions(self) -> None:
        source = inspect.getsource(native_repo.reset_repo_to_commit)
        self.assertLess(
            source.index("remove_untracked_reparse_points"),
            source.index('_run_git(repo, clean_args or ["clean"'),
        )
        self.assertIn("_reset_submodules(repo)", source)
        self.assertIn("mask_windows_case_collisions(repo)", source)

        repo = Path("E:/repo")
        with mock.patch.object(native_repo, "_windows_case_collision_paths", return_value=["A.ets", "a.ets"]), mock.patch.object(native_repo, "_run_git") as run_git:
            self.assertEqual(native_repo.mask_windows_case_collisions(repo), ["A.ets", "a.ets"])
        run_git.assert_called_once_with(
            repo.resolve(),
            ["update-index", "--assume-unchanged", "--", "A.ets", "a.ets"],
            check=False,
        )

        if os.name == "nt":
            with tempfile.TemporaryDirectory() as raw_temp:
                temp_repo = Path(raw_temp)
                subprocess.run(["git", "init", "-q", str(temp_repo)], check=True)
                reserved = "\\\\?\\" + str(temp_repo / "nul")
                with open(reserved, "wb") as handle:
                    handle.write(b"generated")
                removed = native_repo.remove_untracked_reparse_points(temp_repo)
                self.assertIn("nul", removed)
                self.assertFalse(os.path.exists(reserved))

    def test_model_query_has_one_http_call_per_attempt_and_five_attempts(self) -> None:
        self.assertIs(OpenAIModel.query.retry.stop, _stop_openai_retry)
        retry_state = mock.Mock(attempt_number=4)
        retry_state.outcome.exception.return_value = RuntimeError("standard")
        self.assertFalse(_stop_openai_retry(retry_state))
        retry_state.attempt_number = 5
        self.assertTrue(_stop_openai_retry(retry_state))
        self.assertIs(OpenAIModel.query.retry.wait, _wait_openai_retry)
        source = inspect.getsource(OpenAIModel.query.__wrapped__)
        self.assertEqual(source.count("self._create_chat_completion(history)"), 1)
        self.assertIn("AuthenticationError", inspect.getsource(OpenAIModel.query))
        init_source = inspect.getsource(OpenAIModel.__init__)
        self.assertIn("connect=15.0", init_source)
        self.assertIn("read=http_timeout", init_source)
        self.assertIn("write=30.0", init_source)
        self.assertIn("pool=15.0", init_source)

    def test_native_shell_timeout_restart_reloads_agent_commands(self) -> None:
        env = SWEEnv.__new__(SWEEnv)
        env.native_mode = True
        env.native_workdir = Path("E:/arkfix/repo")
        env.container = mock.Mock()
        env.container.poll.return_value = 1
        env.logger = mock.Mock()
        env._cleanup_native_commands_dir = mock.Mock()
        env._init_scripts = mock.Mock()
        env._restore_native_shell_context = mock.Mock()
        replacement_shell = mock.Mock()

        with mock.patch(
            "sweagent.environment.utils.get_native_shell",
            return_value=(replacement_shell, {"1"}),
        ):
            self.assertTrue(env._restart_native_shell_after_failure("timeout"))
        self.assertTrue(env._native_shell_requires_agent_init)

        state_ready = False

        def communicate(_command: str) -> str:
            return '{"working_dir":"."}' if state_ready else "bash: state: command not found"

        env.communicate = communicate
        with self.assertRaises(json.JSONDecodeError):
            json.loads(env.communicate("state"))

        agent = mock.Mock()

        def initialize(_env: SWEEnv) -> None:
            nonlocal state_ready
            state_ready = True

        agent.init_environment_vars.side_effect = initialize
        Agent._reinitialize_after_native_shell_restart(agent, env)
        self.assertEqual(json.loads(env.communicate("state")), {"working_dir": "."})
        self.assertFalse(env._native_shell_requires_agent_init)
        agent.init_environment_vars.assert_called_once_with(env)
        self.assertEqual(
            inspect.getsource(Agent.run).count("self._reinitialize_after_native_shell_restart(env)"),
            4,
        )

    @unittest.skipUnless(os.name == "nt", "Windows named-pipe regression")
    def test_windows_pipe_timeout_does_not_steal_later_output(self) -> None:
        shell = subprocess.Popen(
            [str(get_git_bash_path()), "--noprofile", "--norc"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        marker = f"{PROCESS_DONE_MARKER_START}0{PROCESS_DONE_MARKER_END}"
        try:
            assert shell.stdin is not None
            shell.stdin.write(f"sleep 0.3; echo {marker}\n".encode())
            shell.stdin.flush()
            with self.assertRaises(NoOutputTimeoutError):
                read_with_timeout_experimental(shell, 1, 0.05)
            time.sleep(0.4)
            output, exit_code = read_with_timeout_experimental(shell, 1, 1)
            self.assertEqual(output, "")
            self.assertEqual(exit_code, "0")
        finally:
            terminate_process_tree(shell)
            if shell.stdin is not None:
                shell.stdin.close()
            if shell.stdout is not None:
                shell.stdout.close()

    def test_agent_reloads_commands_before_next_sub_action_and_keeps_failed_exit_code(self) -> None:
        agent = Agent.__new__(Agent)
        agent.name = "primary"
        agent.logger = mock.Mock()
        agent.hooks = []
        agent.last_container_id = "native-test"
        agent.history = []
        agent._last_prompt_hash = ""
        agent.config = mock.Mock()
        agent.config.state_command.name = "state"
        agent.config.submit_command = "submit"
        agent.model = mock.Mock()
        agent.model.args.max_steps_per_instance = 0
        agent.model.stats.to_dict.return_value = {}
        agent.setup = mock.Mock()
        agent.save_trajectory = mock.Mock()
        agent._guard_multiline_input = mock.Mock(side_effect=lambda action: action)
        agent.forward = mock.Mock(
            side_effect=[
                ("thought", "two-actions", "output"),
                ("thought", "submit", "output"),
            ]
        )

        def split_actions(action: str) -> list[dict[str, str]]:
            if action == "two-actions":
                return [
                    {"agent": "primary", "cmd_name": "first", "action": "first"},
                    {"agent": "primary", "cmd_name": "second", "action": "second"},
                ]
            return [{"agent": "primary", "cmd_name": "submit", "action": "submit"}]

        agent.split_actions = mock.Mock(side_effect=split_actions)
        env = mock.Mock()
        env.native_mode = True
        env.container_name = "native-test"
        env.record = mock.Mock(
            data={"instance_id": "timeout-lifecycle"},
            language="zh",
        )
        env.name = "native-test"
        env.returncode = 0
        env._native_shell_requires_agent_init = False
        env.communicate.return_value = "{}"
        env.get_available_actions.return_value = []

        reloads: list[str] = []

        def reload_commands(_env: SWEEnv) -> None:
            reloads.append("commands")
            _env.returncode = 0

        agent.init_environment_vars = mock.Mock(side_effect=reload_commands)
        action_calls: list[str] = []

        def env_step(_env: SWEEnv, action: str, _clock) -> tuple[str, int, bool, dict]:
            action_calls.append(action)
            if action == "first":
                _env._native_shell_requires_agent_init = True
                _env.returncode = 1
                return "EXECUTION TIMED OUT\nNative shell was restarted", 0, False, {
                    "action_exit_code": 1,
                    "native_shell_restarted": True,
                }
            if action == "second":
                self.assertEqual(reloads, ["commands"])
                self.assertFalse(_env._native_shell_requires_agent_init)
                _env.returncode = 0
                return "second-ok", 0, False, {"action_exit_code": 0}
            _env.returncode = 0
            return "patch", 0, True, {"action_exit_code": 0, "exit_status": "submitted"}

        agent._env_step_repair_aware = mock.Mock(side_effect=env_step)
        with tempfile.TemporaryDirectory() as raw_temp:
            info, trajectory = agent.run(
                {},
                env,
                traj_dir=Path(raw_temp),
                return_type="info_trajectory",
            )

        self.assertEqual(action_calls, ["first", "second", "submit"])
        self.assertEqual(trajectory[0]["command_results"][0]["exit_code"], 1)
        self.assertEqual(info["exit_status"], "submitted")

    def test_timeout_recovery_holds_build_permit_and_cannot_fake_repair_status(self) -> None:
        permit_state = {"held": False}

        class Permit:
            def __enter__(self):
                permit_state["held"] = True

            def __exit__(self, _type, _value, _traceback):
                permit_state["held"] = False

        env = SWEEnv.__new__(SWEEnv)
        env.native_mode = True
        env.logger = mock.Mock()
        env.returncode = 0
        env.communicate = mock.Mock(side_effect=TimeoutError("timeout", "partial"))

        def interrupt() -> str:
            self.assertTrue(permit_state["held"])
            return "Interrupted."

        def restart(_reason: str) -> bool:
            self.assertTrue(permit_state["held"])
            return True

        env.interrupt = mock.Mock(side_effect=interrupt)
        env._restart_native_shell_after_failure = mock.Mock(side_effect=restart)
        env._format_current_repair_status = mock.Mock(
            return_value="REPAIR_STATUS\nsubmit_readiness: SCOPE_OK_ALL_DEFECT_CODE"
        )
        with mock.patch("sweagent.environment.swe_env.native_build_permit", return_value=Permit()):
            observation, _, done, info = env.step("hvigorw assembleHap --no-daemon")

        self.assertFalse(permit_state["held"])
        self.assertFalse(done)
        self.assertEqual(info["action_exit_code"], 1)
        self.assertTrue(info["native_shell_restarted"])
        self.assertEqual(env.returncode, 1)
        self.assertIn("EXECUTION TIMED OUT", observation)
        env._format_current_repair_status.assert_not_called()

        env.communicate = mock.Mock(side_effect=TimeoutError("timeout", "partial"))
        env.interrupt = mock.Mock(return_value="Interrupted.")
        env._restart_native_shell_after_failure = mock.Mock(return_value=True)
        observation, _, done, info = env.step("repair_status")
        self.assertFalse(done)
        self.assertEqual(info["action_exit_code"], 1)
        self.assertNotIn("submit_readiness: SCOPE_OK", observation)
        env._format_current_repair_status.assert_not_called()

    def test_restart_requires_old_shell_termination_and_dead_shell_closes_job(self) -> None:
        env = SWEEnv.__new__(SWEEnv)
        env.native_mode = True
        env.native_workdir = Path("E:/arkfix/repo")
        env.container = mock.Mock()
        env.logger = mock.Mock()
        env._cleanup_native_commands_dir = mock.Mock()
        replacement = mock.Mock()
        with (
            mock.patch(
                "sweagent.environment.swe_env.terminate_process_tree",
                side_effect=RuntimeError("kill failed"),
            ),
            mock.patch(
                "sweagent.environment.utils.get_native_shell",
                return_value=(replacement, {"1"}),
            ) as start_shell,
        ):
            self.assertFalse(env._restart_native_shell_after_failure("timeout"))
        self.assertIsNot(env.container, replacement)
        start_shell.assert_not_called()
        env._cleanup_native_commands_dir.assert_not_called()

        job = mock.Mock()
        dead_process = mock.Mock()
        dead_process.arkfix_job = job
        dead_process.poll.return_value = 7
        terminate_process_tree(dead_process)
        job.close.assert_called_once_with()
        self.assertIsNone(dead_process.arkfix_job)
        self.assertIn("job.assign(process)", inspect.getsource(get_native_shell))

    def test_native_shell_restart_reuses_sdk_exports_without_reprocessing(self) -> None:
        env = SWEEnv.__new__(SWEEnv)
        env.native_mode = True
        env.record = mock.Mock(data={"defect_files": ["entry/src/main/ets/Index.ets"]})
        env._native_project_path = "."
        env._native_harmony_export_command = "export DEVECO_SDK_HOME=/sdk"
        env._native_env_base_tree = "original-baseline-tree"
        env.communicate_with_handling = mock.Mock()
        env._apply_native_harmony_sdk_adapter = mock.Mock()

        env._restore_native_shell_context()

        env._apply_native_harmony_sdk_adapter.assert_not_called()
        self.assertEqual(env._native_env_base_tree, "original-baseline-tree")
        self.assertEqual(
            env.communicate_with_handling.call_args_list[-1].kwargs["input"],
            "export DEVECO_SDK_HOME=/sdk",
        )

    def test_evaluators_skip_fetch_when_base_commit_is_local(self) -> None:
        evaluator_paths = (
            WORKSPACE_ROOT / "evaluation" / "run_llm_patch_eval.py",
            ENGINE_ROOT / "evaluation" / "run_llm_patch_eval.py",
        )
        for index, evaluator_path in enumerate(evaluator_paths, 1):
            spec = importlib.util.spec_from_file_location(f"arkfix_eval_test_{index}", evaluator_path)
            self.assertIsNotNone(spec)
            self.assertIsNotNone(spec.loader if spec else None)
            module = importlib.util.module_from_spec(spec)
            assert spec is not None and spec.loader is not None
            spec.loader.exec_module(module)
            git_calls: list[list[str]] = []

            def fake_git(args: list[str], _repo_dir: Path) -> tuple[int, str, str]:
                git_calls.append(args)
                return 0, "", ""

            with (
                mock.patch.object(module, "_wait_for_git_index_lock", return_value=(True, "")),
                mock.patch.object(module, "_git", side_effect=fake_git),
                mock.patch.object(module, "_git_with_index_lock_retry", return_value=(0, "", "")),
            ):
                ok, error = module.reset_repo(Path("C:/arkfix/repo"), "a" * 40)
            self.assertTrue(ok, error)
            self.assertEqual(git_calls, [["cat-file", "-e", f"{'a' * 40}^{{commit}}"]])

    def test_vpn_extension_api20_profile_adapter_is_narrow_and_shared(self) -> None:
        serial_common_path = ENGINE_ROOT / "command_line_tools_test" / "tools" / "common.py"
        serial_tools_dir = str(serial_common_path.parent)
        sys.path.insert(0, serial_tools_dir)
        try:
            spec = importlib.util.spec_from_file_location("arkfix_serial_common_test", serial_common_path)
            self.assertIsNotNone(spec)
            self.assertIsNotNone(spec.loader if spec else None)
            serial_common = importlib.util.module_from_spec(spec)
            assert spec is not None and spec.loader is not None
            sys.modules[spec.name] = serial_common
            spec.loader.exec_module(serial_common)
        finally:
            sys.path.remove(serial_tools_dir)

        with tempfile.TemporaryDirectory() as raw_temp:
            temp_root = Path(raw_temp)
            sdk_root = temp_root / "sdk"
            for api, extension_types in ((14, ["service"]), (20, ["service", "vpn"])):
                schema = sdk_root / str(api) / "toolchains" / "modulecheck" / "module.json"
                schema.parent.mkdir(parents=True)
                schema.write_text(json.dumps({"enum": extension_types}), encoding="utf-8")

            def make_project(name: str, extension_type: str = "vpn") -> Path:
                project = temp_root / name
                manifest = project / "entry" / "src" / "main" / "module.json5"
                manifest.parent.mkdir(parents=True)
                manifest.write_text(
                    json.dumps({"module": {"extensionAbilities": [{"type": extension_type}]}}),
                    encoding="utf-8",
                )
                (project / "build-profile.json5").write_text(
                    json.dumps(
                        {
                            "app": {
                                "products": [
                                    {
                                        "name": "default",
                                        "compileSdkVersion": 14,
                                        "targetSdkVersion": 14,
                                        "compatibleSdkVersion": 14,
                                        "runtimeOS": "OpenHarmony",
                                    }
                                ]
                            },
                            "modules": [{"name": "entry", "srcPath": "./entry"}],
                        }
                    ),
                    encoding="utf-8",
                )
                return project

            for index, adapter in enumerate(
                (apply_vpn_extension_api20_profile_adapter, serial_common.apply_vpn_extension_api20_profile_adapter),
                1,
            ):
                project = make_project(f"vpn_{index}")
                notes = adapter(project, [sdk_root])
                profile = json.loads((project / "build-profile.json5").read_text(encoding="utf-8"))
                product = profile["app"]["products"][0]
                self.assertEqual(product["compileSdkVersion"], 20)
                self.assertEqual(product["targetSdkVersion"], 14)
                self.assertEqual(product["compatibleSdkVersion"], 14)
                self.assertEqual(len(notes), 1)
                self.assertIn("ENV_PREPARE_SDK_ADAPTER=vpn_extension_compile_api14_to_api20", notes[0])

            non_vpn_project = make_project("non_vpn", "service")
            self.assertEqual(apply_vpn_extension_api20_profile_adapter(non_vpn_project, [sdk_root]), [])
            self.assertIn('"compileSdkVersion": 14', (non_vpn_project / "build-profile.json5").read_text())

        generation_source = inspect.getsource(SWEEnv._apply_native_harmony_sdk_adapter)
        self.assertLess(
            generation_source.index("apply_vpn_extension_api20_profile_adapter"),
            generation_source.index("require_sdk_roots_for_repo"),
        )
        serial_source = inspect.getsource(serial_common.prepare_native_repair_environment)
        self.assertLess(
            serial_source.index("apply_vpn_extension_api20_profile_adapter"),
            serial_source.index("require_sdk_roots_for_repo"),
        )

    def test_evaluator_legacy_build_environment_shims(self) -> None:
        common_path = WORKSPACE_ROOT / "evaluation" / "command_line_tools_test" / "tools" / "common.py"
        tools_dir = str(common_path.parent)
        sys.path.insert(0, tools_dir)
        try:
            spec = importlib.util.spec_from_file_location("arkeval_evaluator_common_test", common_path)
            self.assertIsNotNone(spec)
            self.assertIsNotNone(spec.loader)
            evaluator_common = importlib.util.module_from_spec(spec)
            sys.modules[spec.name] = evaluator_common
            spec.loader.exec_module(evaluator_common)
        finally:
            sys.path.remove(tools_dir)

        with tempfile.TemporaryDirectory() as raw_temp:
            root = Path(raw_temp)

            model_project = root / "model"
            model_project.mkdir()
            model_config = model_project / "oh-package.json5"
            model_config.write_text('{"modelVersion":"5.0.4"}\n', encoding="utf-8")
            self.assertTrue(evaluator_common._prepare_legacy_hvigor_model_version(model_project))
            self.assertIn('"modelVersion":"5.0.0"', model_config.read_text(encoding="utf-8"))

            photo_project = root / "photo-deal-demo"
            photo_project.mkdir()
            photo_profile = photo_project / "build-profile.json5"
            photo_profile.write_text(
                '{"app":{"compileSdkVersion":16,"compatibleSdkVersion":16}}\n',
                encoding="utf-8",
            )
            sdk_root = root / "sdk"
            sdk_root.mkdir()
            sdk_meta = {
                "compileSdkVersion": 16,
                "compatibleSdkVersion": 16,
                "sdk_selection_api_level": 16,
            }
            with mock.patch.object(
                evaluator_common, "resolve_sdk_api_slice_for_api", return_value=sdk_root / "18"
            ), mock.patch.object(evaluator_common, "_sdk_component_api_version", return_value=18):
                self.assertTrue(
                    evaluator_common._prepare_build_profile_sdk_api_compat(
                        photo_project, sdk_root, sdk_meta
                    )
                )
            self.assertNotIn(":16", photo_profile.read_text(encoding="utf-8"))
            self.assertEqual(sdk_meta["sdk_selection_api_level"], 18)

            app_market = root / "MultiDeviceAppDev" / "AppMarket"
            ability_stage = app_market / "entry/src/main/ets/Application/AbilityStage.ts"
            main_ability = app_market / "entry/src/main/ets/MainAbility/MainAbility.ets"
            ability_stage.parent.mkdir(parents=True)
            main_ability.parent.mkdir(parents=True)
            (app_market / "build-profile.json5").write_text(
                '{"app":{"compileSdkVersion":9,"compatibleSdkVersion":9}}\n',
                encoding="utf-8",
            )
            ability_stage.write_text(
                "import AbilityStage from '@ohos.application.AbilityStage'\n",
                encoding="utf-8",
            )
            main_ability.write_text(
                "import Ability from '@ohos.application.Ability'\n"
                "export default class MainAbility extends Ability {}\n",
                encoding="utf-8",
            )
            self.assertEqual(len(evaluator_common._prepare_app_market_legacy_shims(app_market)), 3)
            self.assertIn("@ohos.app.ability.AbilityStage", ability_stage.read_text(encoding="utf-8"))
            self.assertIn("extends UIAbility", main_ability.read_text(encoding="utf-8"))

            upgrade_popup = root / "ETSUI" / "UpgradePopup"
            upgrade_stage = upgrade_popup / "entry/src/main/ets/Application/MyAbilityStage.ts"
            upgrade_main = upgrade_popup / "entry/src/main/ets/MainAbility/MainAbility.ts"
            upgrade_stage.parent.mkdir(parents=True)
            upgrade_main.parent.mkdir(parents=True)
            (upgrade_popup / "build-profile.json5").write_text(
                '{"app":{"compileSdkVersion":9,"compatibleSdkVersion":9}}\n',
                encoding="utf-8",
            )
            upgrade_stage.write_text("@ohos.application.AbilityStage\n", encoding="utf-8")
            upgrade_main.write_text(
                "import Ability from '@ohos.application.Ability'\n"
                "export default class MainAbility extends Ability {}\n",
                encoding="utf-8",
            )
            self.assertEqual(len(evaluator_common._prepare_upgrade_popup_legacy_shims(upgrade_popup)), 3)
            self.assertIn("extends UIAbility", upgrade_main.read_text(encoding="utf-8"))

            ijkplayer = root / "ohos_ijkplayer"
            entry_package = ijkplayer / "entry/oh-package.json5"
            entry_package.parent.mkdir(parents=True)
            entry_package.write_text(
                '{"dependencies":{"@ohos/ijkplayer":"file:../ijkplayer"}}\n',
                encoding="utf-8",
            )
            ijk_profile = ijkplayer / "build-profile.json5"
            ijk_profile.write_text(
                '{"modules":[{"name":"entry","srcPath":"./entry"},'
                '{"name":"ijkplayer","srcPath":"./ijkplayer"}]}\n',
                encoding="utf-8",
            )
            self.assertEqual(
                len(evaluator_common._prepare_ijkplayer_prebuilt_har_dependency(ijkplayer)),
                2,
            )
            self.assertIn('"2.0.7-rc.3"', entry_package.read_text(encoding="utf-8"))
            self.assertNotIn(
                '"name": "ijkplayer"',
                ijk_profile.read_text(encoding="utf-8"),
            )

            distributed_note = root / "data" / "DistributedNote"
            distributed_stage = distributed_note / "entry/src/main/ets/Application/AbilityStage.ts"
            distributed_main = distributed_note / "entry/src/main/ets/MainAbility/MainAbility.ts"
            distributed_stage.parent.mkdir(parents=True)
            distributed_main.parent.mkdir(parents=True)
            distributed_stage.write_text(
                "import AbilityStage from '@ohos.application.AbilityStage'\n",
                encoding="utf-8",
            )
            distributed_main.write_text(
                "import Ability from '@ohos.application.Ability'\n"
                "export default class MainAbility extends Ability {}\n",
                encoding="utf-8",
            )
            self.assertEqual(
                len(evaluator_common._prepare_legacy_api9_ability_imports(distributed_note)),
                2,
            )
            self.assertIn("extends UIAbility", distributed_main.read_text(encoding="utf-8"))

            flipclock = root / "Solutions" / "Tools" / "FlipClock"
            flipclock_ets = flipclock / "entry/src/main/ets"
            flipclock_ets.mkdir(parents=True)
            flipclock_hvigor = flipclock / "hvigor/hvigor-config.json5"
            flipclock_hvigor.parent.mkdir()
            flipclock_hvigor.write_text(
                '{"hvigorVersion":"2.0.0","dependencies":{"@ohos/hvigor-ohos-plugin":"2.0.0"}}\n',
                encoding="utf-8",
            )
            flipclock_module = flipclock / "entry/src/main/module.json5"
            flipclock_module.write_text(
                '{"module":{"requestPermissions":[{"name":"ohos.permission.READ_CALENDAR"}]}}\n',
                encoding="utf-8",
            )

            def copy_notification(_name: str, target: Path, **_kwargs: object) -> bool:
                target.write_text(
                    "declare namespace notificationManager {\n}\nexport default notificationManager;\n",
                    encoding="utf-8",
                )
                return True

            with mock.patch.object(
                evaluator_common, "_copy_installed_sdk_api_file", side_effect=copy_notification
            ):
                flipclock_notes = evaluator_common._prepare_flipclock_legacy_build_shims(flipclock)
            self.assertEqual(len(flipclock_notes), 5)
            self.assertIn('"hvigorVersion":"4.1.2"', flipclock_hvigor.read_text(encoding="utf-8"))
            permission = json.loads(flipclock_module.read_text(encoding="utf-8"))["module"][
                "requestPermissions"
            ][0]
            self.assertEqual(permission["reason"], "$string:module_desc")
            self.assertEqual(permission["usedScene"]["when"], "inuse")
            self.assertTrue((flipclock_ets / "@ohos.brightness.d.ts").is_file())
            notification_text = (flipclock_ets / "@ohos.notificationManager.d.ts").read_text(
                encoding="utf-8"
            )
            self.assertIn("isNotificationEnabled(bundle", notification_text)
            self.assertIn("setNotificationEnable(bundle", notification_text)

            cache_project = root / "cache_project"
            cache_config = cache_project / "hvigor/hvigor-config.json5"
            cache_config.parent.mkdir(parents=True)
            cache_config.write_text(
                '{"dependencies":{"@ohos/hvigor-ohos-plugin":"4.1.2"}}\n',
                encoding="utf-8",
            )
            fake_home = root / "home"
            project_name = hashlib.md5(str(cache_project.resolve()).encode("utf-8")).hexdigest()
            cache_workspace = fake_home / ".hvigor/project_caches" / project_name / "workspace"
            cache_workspace.mkdir(parents=True)
            (cache_workspace / "package.json").write_text(
                '{"dependencies":{"@ohos/hvigor-ohos-plugin":"4.1.2"}}\n',
                encoding="utf-8",
            )
            install_failed = subprocess.CompletedProcess(
                args=["pnpm", "install"],
                returncode=1,
                stdout="ERR_PNPM_NO_OFFLINE_TARBALL missing tarball",
                stderr="",
            )
            with (
                mock.patch.object(evaluator_common.Path, "home", return_value=fake_home),
                mock.patch.object(evaluator_common, "FileLock"),
                mock.patch.object(
                    evaluator_common.subprocess,
                    "run",
                    return_value=install_failed,
                ) as install_run,
                mock.patch.object(evaluator_common.time, "sleep"),
            ):
                with self.assertRaises(evaluator_common.StructuredToolError):
                    evaluator_common._prepare_hvigor_project_cache_plugin_dependencies(
                        cache_project, root / "deveco"
                    )
            self.assertEqual(install_run.call_count, 1)
            self.assertIn("--offline", install_run.call_args_list[0].args[0])

            legacy_project = root / "legacy_project"
            legacy_project.mkdir()
            (legacy_project / "package.json").write_text(
                '{"dependencies":{"@ohos/hvigor":"1.0.0"}}\n', encoding="utf-8"
            )
            fake_npm = root / "deveco/tools/node/npm.cmd"
            fake_npm.parent.mkdir(parents=True)
            fake_npm.write_text("@echo off\n", encoding="utf-8")
            npm_offline_failed = subprocess.CompletedProcess(
                args=["npm", "install"],
                returncode=1,
                stdout="npm ERR! code ENOTCACHED cache mode is only-if-cached",
                stderr="",
            )
            with (
                mock.patch.object(evaluator_common, "workspace_root", return_value=root),
                mock.patch.object(evaluator_common, "FileLock"),
                mock.patch.object(
                    evaluator_common.subprocess,
                    "run",
                    return_value=npm_offline_failed,
                ) as npm_run,
                mock.patch.object(evaluator_common.time, "sleep"),
            ):
                with self.assertRaises(evaluator_common.StructuredToolError):
                    evaluator_common._prepare_legacy_hvigor_npm_dependencies(
                        legacy_project, root / "deveco", 60
                    )
            self.assertEqual(npm_run.call_count, 1)
            self.assertIn("--offline", npm_run.call_args_list[0].args[0])

        npm_source = inspect.getsource(evaluator_common._prepare_legacy_hvigor_npm_dependencies)
        self.assertLess(npm_source.index("FileLock"), npm_source.index("subprocess.run"))
        project_cache_source = inspect.getsource(
            evaluator_common._prepare_hvigor_project_cache_plugin_dependencies
        )
        self.assertLess(project_cache_source.index("FileLock"), project_cache_source.index("subprocess.run"))
        video_source = inspect.getsource(evaluator_common._prepare_video_show_local_har)
        self.assertIn("_link_legacy_sdk_component", video_source)

    def test_twenty_dynamic_slots_duplicate_pool_and_rare_repo_assignment(self) -> None:
        with tempfile.TemporaryDirectory() as raw_temp:
            pool = Path(raw_temp) / "repair_repo"
            slots = []
            for index in range(1, 21):
                slot = pool / f"run{index:02d}"
                (slot / "applications_app_samples" / ".git").mkdir(parents=True)
                slots.append(slot)

            rare_repos = [
                "Homogram",
                "HPRichText",
                "RdbPlus",
                "rdbStore",
                "photo-deal-demo",
                "ohos_ijkplayer",
                "md360player",
                "ImageKnife",
            ]
            for slot, repo in zip(slots, rare_repos):
                (slot / repo / ".git").mkdir(parents=True)

            discovered = discover_repo_slots([pool])
            self.assertEqual([slot.name for slot in discovered], [f"run{i:02d}" for i in range(1, 21)])
            with self.assertRaises(SystemExit):
                discover_repo_slots([pool, pool])

            rows: dict[int, DatasetRow] = {
                index: DatasetRow(index, f"org__applications_app_samples+sha-{index}", "applications_app_samples", "a" * 40)
                for index in range(1, 21)
            }
            for offset, repo in enumerate(rare_repos, 11):
                rows[offset] = DatasetRow(offset, f"org__{repo}+sha-{offset}", repo, "b" * 40)
            assignments = assign_rows_to_slots(list(rows), 20, rows, discovered)
            self.assertEqual(len(assignments), 20)
            self.assertEqual(len({slot.resolve() for slot, _ in assignments}), 20)
            for slot, assigned_rows in assignments:
                for row in assigned_rows:
                    self.assertTrue((slot / rows[row].repo / ".git").is_dir())

            base_aware = select_base_aware_active_slots(
                [1],
                1,
                discovered,
                {1: (discovered[7],)},
            )
            self.assertEqual(base_aware, [discovered[7]])

    def test_full_502_readonly_preflight_uses_twenty_slots_and_nine_repos(self) -> None:
        records = run_repair.load_jsonl(run_repair.latest_arkfix_input())
        policies = run_repair.load_test_patch_policies()
        self.assertEqual(len(policies), 502)
        localized_records: dict[str, list[dict]] = {}
        for name in (
            "arkeval_gpt-5.6-sol.jsonl",
            "arkeval_kimi-2.7-code.jsonl",
            "arkeval_openpangu2.0-flash.jsonl",
        ):
            localized = run_repair.load_jsonl(WORKSPACE_ROOT / "dataset" / name)
            localized_records[name] = localized
            self.assertEqual(len(localized), 502)
            self.assertEqual(set(policies), {record["instance_id"] for record in localized})
            for record in localized:
                self.assertEqual(
                    (record["allow_test_patch"], record["allow_test_patch_reason"]),
                    policies[record["instance_id"]],
                )
                self.assertEqual(record["fix_patch"], "")
                self.assertEqual(record["test_patch"], "")
        available_rows = [
            run_repair.row_number_for_record(index, record)
            for index, record in enumerate(records, 1)
        ]
        scoped, _ = run_repair.build_scoped_dataset(
            records,
            selected_rows=available_rows,
            allow_original_defect_files=False,
        )
        self.assertEqual(len(scoped), 502)
        for record in scoped:
            self.assertEqual(
                (record["allow_test_patch"], record["allow_test_patch_reason"]),
                policies[record["instance_id"]],
            )
            self.assertEqual(record["fix_patch"], "")
            self.assertEqual(record["test_patch"], "")

        expected_policy_rows = {
            "issue+gold_fix": {5, 10, 18, 50, 53, 59, 61, 82, 193, 202, 205},
            "issue": {51, 196, 263, 267, 336},
            "gold_fix": {447},
        }
        for reason, rows in expected_policy_rows.items():
            self.assertTrue(all(scoped[row - 1]["allow_test_patch_reason"] == reason for row in rows))
        unauthorized_test_rows = {
            row
            for row, record in enumerate(localized_records["arkeval_gpt-5.6-sol.jsonl"], 1)
            if any(is_agent_self_test_patch_path(path) for path in record["defect_files"])
            and not record["allow_test_patch"]
        }
        self.assertEqual(
            unauthorized_test_rows,
            {3, 4, 17, 63, 112, 116, 117, 155, 158, 174, 345, 368, 377, 379, 486},
        )
        config = Config(need_clone=False, global_env=None, clear_env=False)
        for record in scoped:
            pr = PullRequest(
                org=str(record["org"]),
                repo=str(record["repo"]),
                number=int(record["number"]),
                state=str(record["state"]),
                title=str(record["title"]),
                body=record.get("body"),
                base=Base(**record["base"]),
                resolved_issues=[],
                fix_patch="",
                test_patch="",
            )
            self.assertEqual(Instance.create(pr, config).repo_name, f"{pr.org}/{pr.repo}")
        dataset_rows: dict[int, DatasetRow] = {}
        for row, record in enumerate(scoped, 1):
            dataset_rows[row] = DatasetRow(
                row=row,
                instance_id=str(record["instance_id"]),
                repo=str(record["repo"]),
                base_sha=str(record["base"]["sha"]),
            )
        self.assertEqual(len({item.repo for item in dataset_rows.values()}), 9)

        slots = discover_repo_slots([DEFAULT_REPO_POOL])
        assignments = assign_rows_to_slots(list(dataset_rows), 20, dataset_rows, slots)
        self.assertEqual(len(assignments), 20)
        self.assertEqual(len({slot for slot, _ in assignments}), 20)
        acceptance_rows = list(range(1, 13)) + list(range(183, 187)) + list(range(380, 383)) + [385]
        acceptance_assignments = assign_rows_to_slots(acceptance_rows, 20, dataset_rows, slots)
        self.assertEqual(len(acceptance_assignments), 20)
        self.assertEqual(len({slot for slot, _ in acceptance_assignments}), 20)
        self.assertEqual(
            {slot.name for slot, _ in acceptance_assignments},
            {f"run{index:02d}" for index in range(1, 21)},
        )
        self.assertEqual(
            {dataset_rows[row].repo for row in acceptance_rows},
            {item.repo for item in dataset_rows.values()},
        )
        acceptance_compatible = compatible_slots_by_row(acceptance_rows, dataset_rows, slots)
        dynamic_specs = build_worker_specs(
            rows=acceptance_rows,
            attempt=1,
            workers=20,
            dataset_rows=dataset_rows,
            repo_pools=[DEFAULT_REPO_POOL],
            output_dir=Path(tempfile.gettempdir()) / "arkfix-readonly-plan",
            batch_slug="readonly",
            batch_run_id="readonly-batch",
            python_exe=sys.executable,
            config_file=ENGINE_ROOT / "config" / "arkts_system_prompt.yaml",
            model_name="MiniMax-M2.5",
            temperature=0.0,
            top_p=1.0,
            dataset=Path("readonly.jsonl"),
            max_steps=50,
            rag_mode="off",
            rag_docs_roots="",
            rag_samples_roots="",
            rag_index_name="arkfix_default",
            rag_top_k_docs=4,
            rag_top_k_code=4,
            rag_max_context_chars=12000,
            rag_storage_dir="",
            task_batch_size=3,
            precomputed_slots=slots,
            precomputed_compatible=acceptance_compatible,
        )
        self.assertEqual(len(dynamic_specs), 20)
        self.assertEqual(sorted(row for spec in dynamic_specs for row in spec.rows), acceptance_rows)
        self.assertTrue(all(1 <= len(spec.rows) <= 3 for spec in dynamic_specs))
        for spec in dynamic_specs:
            self.assertEqual(len({dataset_rows[row].repo for row in spec.rows}), 1)
            self.assertTrue(all(acceptance_compatible[row] == spec.compatible_slots for row in spec.rows))
            flag_index = spec.command.index("--skip_workdir_reset")
            self.assertEqual(spec.command[flag_index + 1], "true")
        self.assertEqual(
            {slot.name for spec in dynamic_specs for slot in spec.compatible_slots},
            {f"run{index:02d}" for index in range(1, 21)},
        )
        check_repo_slots(dynamic_specs, dataset_rows, precomputed_compatible=acceptance_compatible)
        retry_specs = build_worker_specs(
            rows=acceptance_rows,
            attempt=2,
            workers=20,
            dataset_rows=dataset_rows,
            repo_pools=[DEFAULT_REPO_POOL],
            output_dir=Path(tempfile.gettempdir()) / "arkfix-readonly-plan",
            batch_slug="readonly",
            batch_run_id="readonly-batch",
            python_exe=sys.executable,
            config_file=ENGINE_ROOT / "config" / "arkts_system_prompt.yaml",
            model_name="MiniMax-M2.5",
            temperature=0.0,
            top_p=1.0,
            dataset=Path("readonly.jsonl"),
            max_steps=50,
            rag_mode="off",
            rag_docs_roots="",
            rag_samples_roots="",
            rag_index_name="arkfix_default",
            rag_top_k_docs=4,
            rag_top_k_code=4,
            rag_max_context_chars=12000,
            rag_storage_dir="",
            task_batch_size=3,
            precomputed_slots=slots,
            precomputed_compatible=acceptance_compatible,
        )
        self.assertEqual(len(retry_specs), len(acceptance_rows))
        self.assertTrue(all(len(spec.rows) == 1 for spec in retry_specs))
        acceptance_slots = {
            row: slot
            for slot, rows in acceptance_assignments
            for row in rows
        }
        for row in acceptance_rows:
            record = scoped[row - 1]
            repo_dir = acceptance_slots[row] / dataset_rows[row].repo
            base_sha = dataset_rows[row].base_sha
            first_path = str(record["defect_files"][0]).replace("\\", "/")
            parts = first_path.split("/")[:-1]
            project_roots: list[str] = []
            while True:
                prefix = "/".join(parts)

                def git_object_exists(relative: str) -> bool:
                    object_path = f"{base_sha}:{relative}" if relative else base_sha
                    return subprocess.run(
                        ["git", "-C", str(repo_dir), "cat-file", "-e", object_path],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        check=False,
                    ).returncode == 0

                build_profile = f"{prefix}/build-profile.json5" if prefix else "build-profile.json5"
                if git_object_exists(build_profile):
                    marker_paths = [
                        f"{prefix}/{name}" if prefix else name
                        for name in ("AppScope", "entry", "hvigor")
                    ]
                    if any(git_object_exists(marker) for marker in marker_paths):
                        project_roots.append(prefix or ".")
                if not parts:
                    break
                parts.pop()
            self.assertEqual(
                len(project_roots),
                1,
                f"row {row} rank-1 path must resolve to one project at base: {project_roots}",
            )
        specs = [
            WorkerSpec(
                attempt=1,
                worker=index,
                rows=rows,
                repo_dir=slot,
                suffix=f"preflight-{index}",
                instance_filter="^preflight$",
                command=["preflight"],
                log_path=Path(tempfile.gettempdir()) / f"arkfix-preflight-{index}.log",
                batch_run_id="readonly-preflight",
            )
            for index, (slot, rows) in enumerate(assignments, 1)
        ]
        check_repo_slots(specs, dataset_rows)

    def test_unauthorized_test_localization_fails_before_workers(self) -> None:
        test_path = "entry/src/ohosTest/ets/test/Ability.test.ets"
        production_path = "entry/src/main/ets/pages/Index.ets"
        rows = {
            1: DatasetRow(1, "org__repo+1", "repo", "a" * 40, (test_path,)),
            2: DatasetRow(
                2,
                "org__repo+2",
                "repo",
                "b" * 40,
                (production_path, test_path),
            ),
            3: DatasetRow(
                3,
                "org__repo+3",
                "repo",
                "c" * 40,
                (test_path,),
                True,
                "issue",
            ),
            4: DatasetRow(4, "org__repo+4", "repo", "d" * 40, (production_path,)),
        }
        failures = localization_preflight_failures(list(rows), rows)
        self.assertEqual(set(failures), {1, 2})
        self.assertTrue(all("localization_failure" in item.reason for item in failures.values()))

    def test_new_and_historic_app_sample_layouts_use_rank_one_project(self) -> None:
        with tempfile.TemporaryDirectory() as raw_temp:
            root = Path(raw_temp)
            modern = root / "code" / "Docs" / "Category" / "ModernApp"
            (modern / "entry" / "src" / "main" / "ets").mkdir(parents=True)
            (modern / "build-profile.json5").write_text("{}", encoding="utf-8")
            nested_module = modern / "feature"
            (nested_module / "src" / "main" / "ets").mkdir(parents=True)
            (nested_module / "hvigor").mkdir()
            (nested_module / "build-profile.json5").write_text("{}", encoding="utf-8")
            other = root / "code" / "Other" / "Category" / "OtherApp"
            (other / "entry" / "src" / "main" / "ets").mkdir(parents=True)
            (other / "build-profile.json5").write_text("{}", encoding="utf-8")
            modern_paths = [
                "code/Docs/Category/ModernApp/feature/src/main/ets/Index.ets",
                "code/Docs/Category/ModernApp/entry/src/main/ets/Model.ets",
                "code/Other/Category/OtherApp/entry/src/main/ets/Other.ets",
            ]
            project, scoped = scope_ranked_defect_files_to_harmony_project(root, modern_paths)
            self.assertEqual(project, "code/Docs/Category/ModernApp")
            self.assertEqual(scoped, modern_paths[:2])

            historic = root / "ability" / "ServiceAbility"
            (historic / "entry" / "src" / "main" / "ets").mkdir(parents=True)
            historic_paths = [
                "ability/ServiceAbility/entry/src/main/ets/Index.ets",
                "ability/ServiceAbility/entry/src/main/ets/service.ts",
                "ability/DMS/entry/src/main/ets/service.ts",
            ]
            project, scoped = scope_ranked_defect_files_to_harmony_project(root, historic_paths)
            self.assertEqual(project, "ability/ServiceAbility")
            self.assertEqual(scoped, historic_paths[:2])

    def test_micro_batches_do_not_mix_repo_or_compatible_slots(self) -> None:
        slots = [Path(f"C:/arkfix/run{index:02d}") for index in range(1, 5)]
        rows = {
            1: DatasetRow(1, "org__repo_a+1", "repo_a", "a" * 40),
            2: DatasetRow(2, "org__repo_a+2", "repo_a", "a" * 40),
            3: DatasetRow(3, "org__repo_b+3", "repo_b", "b" * 40),
            4: DatasetRow(4, "org__repo_b+4", "repo_b", "b" * 40),
        }
        compatible = {
            1: (slots[0], slots[1]),
            2: (slots[0], slots[1]),
            3: (slots[2], slots[3]),
            4: (slots[2], slots[3]),
        }
        specs = build_worker_specs(
            rows=list(rows),
            attempt=1,
            workers=2,
            dataset_rows=rows,
            repo_pools=[Path("C:/arkfix")],
            output_dir=Path("C:/arkfix/output"),
            batch_slug="unit",
            batch_run_id="unit-batch",
            python_exe=sys.executable,
            config_file=Path("config.yaml"),
            model_name="MiniMax-M2.5",
            temperature=0.0,
            top_p=1.0,
            dataset=Path("dataset.jsonl"),
            max_steps=50,
            rag_mode="off",
            rag_docs_roots="",
            rag_samples_roots="",
            rag_index_name="arkfix_default",
            rag_top_k_docs=4,
            rag_top_k_code=4,
            rag_max_context_chars=12000,
            rag_storage_dir="",
            task_batch_size=3,
            precomputed_slots=slots,
            precomputed_compatible=compatible,
        )
        self.assertEqual(sorted(row for spec in specs for row in spec.rows), [1, 2, 3, 4])
        for spec in specs:
            self.assertEqual(len({rows[row].repo for row in spec.rows}), 1)
            self.assertEqual(len({compatible[row] for row in spec.rows}), 1)

    @staticmethod
    def _valid_trajectory(build_action: str | None = None) -> list[dict[str, str]]:
        return [
            {"action": "edit_file entry/src/main/ets/Index.ets 1:1", "observation": "EDIT_STATUS=APPLIED"},
            {
                "action": build_action
                or "python E:/WorkApp/arkeval/arkfix/repair_engine/command_line_tools_test/tools/build_app.py --repo-path . --module entry --task assembleHap",
                "observation": (
                    "BUILD_STATUS=SUCCESS\nBUILD_ACTION_EXIT_CODE=0\n"
                    f"BUILD_TREE_SHA256={'a' * 64}"
                ),
            },
            {
                "action": "repair_status",
                "observation": "REPAIR_STATUS\nsubmit_readiness: SCOPE_OK_ALL_DEFECT_CODE",
            },
            {"action": "submit", "observation": "diff --git a/a b/a"},
        ]

    def test_final_validation_rejects_fakes_and_accepts_valid_forced_submit(self) -> None:
        info = {"exit_status": "submitted"}
        ok, reason, _ = SaveApplyPatchHook._final_validation(info, self._valid_trajectory())
        self.assertTrue(ok, reason)

        fake = self._valid_trajectory()
        fake[1] = {
            "action": "echo BUILD_STATUS=SUCCESS BUILD_ACTION_EXIT_CODE=0",
            "observation": "BUILD_STATUS=SUCCESS\nBUILD_ACTION_EXIT_CODE=0",
        }
        ok, reason, _ = SaveApplyPatchHook._final_validation(info, fake)
        self.assertFalse(ok)
        self.assertEqual(reason, "no_build_after_last_edit")

        masked = self._valid_trajectory(
            "python E:/x/build_app.py || echo BUILD_STATUS=SUCCESS"
        )
        ok, reason, _ = SaveApplyPatchHook._final_validation(info, masked)
        self.assertFalse(ok)
        self.assertEqual(reason, "no_build_after_last_edit")

        background = self._valid_trajectory(
            "python E:/x/build_app.py >/dev/null 2>&1 & echo BUILD_STATUS=SUCCESS"
        )
        ok, reason, _ = SaveApplyPatchHook._final_validation(info, background)
        self.assertFalse(ok)
        self.assertEqual(reason, "no_build_after_last_edit")

        edit_after_build_in_one_action = self._valid_trajectory(
            "python E:/x/build_app.py\nedit_file a.ets 1:1\nx\nend_of_edit_file"
        )
        ok, reason, _ = SaveApplyPatchHook._final_validation(info, edit_after_build_in_one_action)
        self.assertFalse(ok)
        self.assertEqual(reason, "no_build_after_last_edit")

        contradictory = self._valid_trajectory()
        contradictory[1]["observation"] = (
            "BUILD_STATUS=FAILED\nBUILD_STATUS=SUCCESS\nBUILD_ACTION_EXIT_CODE=0"
        )
        ok, reason, _ = SaveApplyPatchHook._final_validation(info, contradictory)
        self.assertFalse(ok)
        self.assertEqual(reason, "last_build_contains_failure_marker")

        later_failure = self._valid_trajectory()
        later_failure.insert(
            2,
            {
                "action": "python E:/x/build_app.py --repo-path . --module entry --task assembleHap",
                "observation": "BUILD_STATUS=FAILED\nBUILD_ACTION_EXIT_CODE=1",
            },
        )
        ok, reason, _ = SaveApplyPatchHook._final_validation(info, later_failure)
        self.assertFalse(ok)
        self.assertEqual(reason, "last_build_exit_nonzero")

        rejected_edit = self._valid_trajectory()
        rejected_edit.insert(2, {"action": "str_replace a.ets", "observation": "EDIT_STATUS=REJECTED"})
        ok, reason, _ = SaveApplyPatchHook._final_validation(info, rejected_edit)
        self.assertTrue(ok, reason)

        forced = dict(info, max_steps_forced_submit=True)
        ok, reason, _ = SaveApplyPatchHook._final_validation(forced, self._valid_trajectory())
        self.assertTrue(ok, reason)

        hvigor = self._valid_trajectory("E:/project/hvigorw.bat --mode module -p module=entry assembleHap")
        hvigor[1]["observation"] = (
            f"BUILD SUCCESSFUL\nBUILD_ACTION_EXIT_CODE=0\nBUILD_TREE_SHA256={'b' * 64}"
        )
        ok, reason, _ = SaveApplyPatchHook._final_validation(info, hvigor)
        self.assertTrue(ok, reason)

        bad_status = self._valid_trajectory()
        bad_status[2]["observation"] = "REPAIR_STATUS\nsubmit_readiness: OUTSIDE_SCOPE"
        ok, reason, _ = SaveApplyPatchHook._final_validation(info, bad_status)
        self.assertFalse(ok)
        self.assertEqual(reason, "missing_valid_repair_status_after_build")

        timed_out_status = self._valid_trajectory()
        timed_out_status[2]["observation"] = (
            "EXECUTION TIMED OUT\nNative shell was restarted\n"
            "REPAIR_STATUS\nsubmit_readiness: SCOPE_OK_ALL_DEFECT_CODE"
        )
        ok, reason, _ = SaveApplyPatchHook._final_validation(info, timed_out_status)
        self.assertFalse(ok)
        self.assertEqual(reason, "missing_valid_repair_status_after_build")

    def test_patch_only_validation_defers_real_build_to_serial_gate(self) -> None:
        trajectory = [
            {
                "action": "str_replace entry/src/main/ets/Index.ets",
                "observation": (
                    "EDIT_STATUS=APPLIED\nREPAIR_STATUS\n"
                    "submit_readiness: SCOPE_OK_ALL_DEFECT_CODE"
                ),
            },
            {"action": "submit", "observation": "diff --git a/a b/a"},
        ]
        info = {"exit_status": "submitted"}
        ok, reason, steps = SaveApplyPatchHook._final_validation(
            info,
            trajectory,
            require_build=False,
        )
        self.assertTrue(ok, reason)
        self.assertEqual(reason, "passed_patch_only")
        self.assertTrue(steps["serial_build_required"])
        self.assertEqual(steps["repair_status_source"], "edit_observation")

        ok, reason, _ = SaveApplyPatchHook._final_validation(info, trajectory)
        self.assertFalse(ok)
        self.assertEqual(reason, "no_build_after_last_edit")

        trajectory[0]["observation"] = "EDIT_STATUS=APPLIED\nREPAIR_STATUS\nsubmit_readiness: OUTSIDE_SCOPE"
        ok, reason, _ = SaveApplyPatchHook._final_validation(
            info,
            trajectory,
            require_build=False,
        )
        self.assertFalse(ok)
        self.assertEqual(reason, "missing_valid_repair_status_after_edit")

    def test_patch_only_config_and_saved_metadata_require_serial_build(self) -> None:
        config_path = ENGINE_ROOT / "config" / "arkts_system_prompt_no_demo_patch_only.yaml"
        self.assertTrue(is_patch_only_config(config_path))
        self.assertTrue(AgentConfig.load_yaml_utf8(config_path).patch_only_generation)

        path = "entry/src/main/ets/Index.ets"
        patch = (
            f"diff --git a/{path} b/{path}\n"
            f"--- a/{path}\n+++ b/{path}\n@@ -1 +1 @@\n-before\n+after\n"
        )
        trajectory = [
            {
                "action": f"str_replace {path}",
                "observation": (
                    "EDIT_STATUS=APPLIED\nREPAIR_STATUS\n"
                    "submit_readiness: SCOPE_OK_ALL_DEFECT_CODE"
                ),
            },
            {"action": "submit", "observation": patch},
        ]
        with tempfile.TemporaryDirectory() as raw_temp:
            hook = SaveApplyPatchHook()
            hook._traj_dir = Path(raw_temp)
            hook._instance = mock.Mock(
                data={
                    "defect_files": [path],
                    "allow_test_patch": False,
                    "allow_test_patch_reason": "none",
                }
            )
            hook._args = mock.Mock(
                environment=mock.Mock(),
                suffix="unit-patch-only",
                agent=mock.Mock(config=mock.Mock(patch_only_generation=True)),
            )
            info = {"exit_status": "submitted", "submission": patch}
            with (
                mock.patch("run._native_mode_for_dataset", return_value=False),
                mock.patch.object(hook, "_print_patch_message"),
            ):
                patch_path = hook._save_patch("org__repo+base-patch-only", info, trajectory)
            self.assertIsNotNone(patch_path)
            assert patch_path is not None
            metadata = json.loads(patch_path.with_suffix(".meta.json").read_text(encoding="utf-8"))
            self.assertTrue(metadata["patch_only_generation"])
            self.assertEqual(metadata["final_validation"], "patch_only_pending_serial_build")
            self.assertEqual(
                metadata["validation_status"],
                "patch_only_scope_apply_pending_serial_build",
            )

    def test_patch_only_generation_defers_native_preprocess(self) -> None:
        args = mock.Mock(
            print_config=False,
            agent=mock.Mock(patch_only_generation=True),
            environment=mock.Mock(),
            run_name="unit-patch-only",
        )
        with (
            mock.patch.dict(os.environ, {}, clear=False),
            mock.patch("run.SWEEnv"),
            mock.patch("run.Agent"),
            mock.patch.object(Main, "_save_arguments"),
            mock.patch.object(Main, "add_hook"),
            mock.patch("pathlib.Path.mkdir"),
        ):
            Main(args)
            self.assertEqual(os.environ.get("ARKFIX_PATCH_ONLY_GENERATION"), "1")

            env = object.__new__(SWEEnv)
            env.native_mode = True
            env.logger = mock.Mock()
            env._apply_native_harmony_sdk_adapter(".")
            env.logger.info.assert_called_once_with(
                "ArkTS native preprocess deferred to serial apply-check for patch-only generation"
            )

    def test_max_step_forced_submit_saves_scoped_patch_without_build_gate(self) -> None:
        path = "entry/src/main/ets/Index.ets"
        patch = (
            f"diff --git a/{path} b/{path}\n"
            f"--- a/{path}\n+++ b/{path}\n@@ -1 +1 @@\n-before\n+after\n"
        )
        trajectory = [
            {
                "action": "edit_file entry/src/main/ets/Index.ets 1:1",
                "observation": "EDIT_STATUS=APPLIED",
            },
            {"action": "submit", "observation": patch},
        ]
        with tempfile.TemporaryDirectory() as raw_temp:
            hook = SaveApplyPatchHook()
            hook._traj_dir = Path(raw_temp)
            hook._instance = mock.Mock(
                data={
                    "defect_files": [path],
                    "allow_test_patch": False,
                    "allow_test_patch_reason": "none",
                }
            )
            hook._args = mock.Mock(environment=mock.Mock(), suffix="unit-forced")
            info = {
                "exit_status": "submitted",
                "submission": patch,
                "max_steps_forced_submit": True,
            }
            with (
                mock.patch("run._native_mode_for_dataset", return_value=False),
                mock.patch.object(hook, "_print_patch_message"),
            ):
                patch_path = hook._save_patch("org__repo+base-forced", info, trajectory)
            self.assertIsNotNone(patch_path)
            assert patch_path is not None
            metadata = json.loads(patch_path.with_suffix(".meta.json").read_text(encoding="utf-8"))
            self.assertEqual(metadata["final_validation"], "forced_unvalidated")
            self.assertEqual(metadata["validation_status"], "forced_submit_scope_apply_only")

    def test_test_patch_requires_policy_and_matching_defect_path(self) -> None:
        known_test = "entry/src/ohosTest/ets/test/Ability.test.ets"
        external_test = "entry/src/ohosTest/ets/testability/pages/Index.ets"

        def patch_for(path: str, before: str, after: str) -> str:
            return (
                f"diff --git a/{path} b/{path}\n"
                f"--- a/{path}\n"
                f"+++ b/{path}\n"
                "@@ -1 +1 @@\n"
                f"-{before}\n"
                f"+{after}\n"
            )

        submission = patch_for(known_test, "before", "fixed") + patch_for(
            external_test, "before", "self-check"
        )
        filtered = filter_submission_remove_self_tests(
            submission,
            [known_test],
            allow_test_patch=True,
        )
        self.assertIn(known_test, filtered)
        self.assertNotIn(external_test, filtered)
        self.assertEqual(
            filter_submission_remove_self_tests(submission, [known_test]),
            "",
        )
        self.assertEqual(
            filter_submission_remove_self_tests(
                submission,
                [external_test],
                allow_test_patch=True,
            ),
            patch_for(external_test, "before", "self-check"),
        )

        denied_status = compute_repair_status([known_test], [known_test])
        self.assertFalse(denied_status.has_defect_code_files)
        self.assertEqual(denied_status.self_test_files, [known_test])
        allowed_status = compute_repair_status(
            [known_test],
            [known_test, external_test],
            allow_test_patch=True,
        )
        self.assertEqual(allowed_status.modified_defect_code_files, [known_test])
        self.assertEqual(allowed_status.self_test_files, [external_test])

        with tempfile.TemporaryDirectory() as raw_temp:
            hook = SaveApplyPatchHook()
            hook._traj_dir = Path(raw_temp)
            hook._instance = mock.Mock(
                data={
                    "defect_files": [known_test],
                    "allow_test_patch": True,
                    "allow_test_patch_reason": "issue",
                }
            )
            hook._args = mock.Mock(environment=mock.Mock(), suffix="unit")
            info = {"exit_status": "submitted", "submission": submission}
            with (
                mock.patch("run._native_mode_for_dataset", return_value=False),
                mock.patch.object(hook, "_print_patch_message"),
            ):
                patch_path = hook._save_patch("org__repo+base-1", info, self._valid_trajectory())
            self.assertIsNotNone(patch_path)
            assert patch_path is not None
            saved = patch_path.read_text(encoding="utf-8")
            self.assertIn(known_test, saved)
            self.assertNotIn(external_test, saved)
            metadata = json.loads(patch_path.with_suffix(".meta.json").read_text(encoding="utf-8"))
            self.assertTrue(metadata["allow_test_patch"])
            self.assertEqual(metadata["allow_test_patch_reason"], "issue")

            denied_hook = SaveApplyPatchHook()
            denied_hook._traj_dir = Path(raw_temp)
            denied_hook._instance = mock.Mock(
                data={
                    "defect_files": [known_test],
                    "allow_test_patch": False,
                    "allow_test_patch_reason": "none",
                }
            )
            denied_hook._args = mock.Mock(environment=mock.Mock(), suffix="unit-denied")
            denied_info = {"exit_status": "submitted", "submission": patch_for(known_test, "a", "b")}
            with (
                mock.patch("run._native_mode_for_dataset", return_value=False),
                mock.patch.object(denied_hook, "_print_patch_message"),
            ):
                self.assertIsNone(
                    denied_hook._save_patch(
                        "org__repo+base-2",
                        denied_info,
                        self._valid_trajectory(),
                    )
                )

    def test_defect_tree_hash_changes_after_post_build_write(self) -> None:
        with tempfile.TemporaryDirectory() as raw_temp:
            root = Path(raw_temp)
            target = root / "entry" / "src" / "main" / "ets" / "Index.ets"
            target.parent.mkdir(parents=True)
            target.write_text("before\n", encoding="utf-8")
            defect_files = ["entry/src/main/ets/Index.ets"]
            build_hash = defect_tree_sha256(root, defect_files)
            target.write_text("after\n", encoding="utf-8")
            self.assertNotEqual(build_hash, defect_tree_sha256(root, defect_files))

    def test_untracked_copy_and_git_patch_writes_are_rejected(self) -> None:
        for action in (
            "cp /tmp/unbuilt.ets entry/src/main/ets/Index.ets",
            "git apply /tmp/unbuilt.patch",
            "git -C . apply /tmp/unbuilt.patch",
            "open a.ets && mv /tmp/unbuilt.ets a.ets",
        ):
            feedback = _agent_command_format_feedback(action)
            self.assertIsNotNone(feedback, action)
            self.assertIn("COMMAND_FORMAT_ERROR", feedback or "")

    def test_dynamic_queue_reuses_slot_before_slow_row_finishes(self) -> None:
        with tempfile.TemporaryDirectory() as raw_temp:
            root = Path(raw_temp)
            slot1 = root / "run01"
            slot2 = root / "run02"
            repo1 = slot1 / "repo"
            repo1.mkdir(parents=True)
            subprocess.run(["git", "init", str(repo1)], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(repo1), "config", "user.email", "arkfix@test"], check=True)
            subprocess.run(["git", "-C", str(repo1), "config", "user.name", "ArkFix Test"], check=True)
            (repo1 / "tracked.txt").write_text("base\n", encoding="utf-8")
            (repo1 / ".gitignore").write_text("build/\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(repo1), "add", "tracked.txt", ".gitignore"], check=True)
            subprocess.run(["git", "-C", str(repo1), "commit", "-m", "base"], check=True, capture_output=True)
            base_sha = subprocess.run(
                ["git", "-C", str(repo1), "rev-parse", "HEAD"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            slot2.mkdir()
            subprocess.run(["git", "clone", str(repo1), str(slot2 / "repo")], check=True, capture_output=True)
            slots = (slot1.resolve(), slot2.resolve())
            rows = {
                row: DatasetRow(row, f"org__repo+base-{row}", "repo", base_sha)
                for row in (1, 2, 3)
            }
            specs: list[WorkerSpec] = []
            for row, delay in ((1, 10.0), (2, 0.1), (3, 0.1)):
                suffix = f"dynamic_attempt01_w1_row_{row:04d}"
                command = [
                    sys.executable,
                    "-c",
                    (
                        "import pathlib,sys,time; "
                        "slot=pathlib.Path(sys.argv[sys.argv.index('--repo_dir')+1]); "
                        "build=slot/'repo'/'build'; build.mkdir(parents=True,exist_ok=True); "
                        f"(build/'row{row}.tmp').write_text('x'); time.sleep({delay})"
                    ),
                    "--repo_dir",
                    str(slot1),
                    "--suffix",
                    suffix,
                ]
                specs.append(
                    WorkerSpec(
                        attempt=1,
                        worker=1,
                        rows=[row],
                        repo_dir=slot1,
                        suffix=suffix,
                        instance_filter=f"^{row}$",
                        command=command,
                        log_path=root / "logs" / f"row{row}.log",
                        batch_run_id="dynamic-batch",
                        compatible_slots=slots,
                    )
                )
            completed = run_workers(
                specs,
                dataset_rows=rows,
                worker_timeout_seconds=10,
                worker_start_interval_seconds=0,
                build_concurrency=2,
            )
            by_row = {spec.rows[0]: spec for spec in completed}
            self.assertEqual(set(by_row), {1, 2, 3})
            self.assertEqual(by_row[2].repo_dir, slot2.resolve())
            self.assertEqual(by_row[3].repo_dir, slot2.resolve())
            self.assertLess(by_row[3].started_at_epoch or 0, time.time())
            self.assertFalse((slot1 / "repo" / "build").exists())
            self.assertFalse((slot2 / "repo" / "build").exists())

    def test_run_workers_starts_twenty_slots_concurrently(self) -> None:
        with tempfile.TemporaryDirectory() as raw_temp:
            root = Path(raw_temp)
            marker_dir = root / "markers"
            marker_dir.mkdir()
            slots = [root / f"run{index:02d}" for index in range(1, 21)]
            repo = slots[0] / "repo"
            repo.mkdir(parents=True)
            subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(repo), "config", "user.email", "arkfix@test"], check=True)
            subprocess.run(["git", "-C", str(repo), "config", "user.name", "ArkFix Test"], check=True)
            (repo / "tracked.txt").write_text("base\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(repo), "add", "tracked.txt"], check=True)
            subprocess.run(["git", "-C", str(repo), "commit", "-m", "base"], check=True, capture_output=True)
            base_sha = subprocess.run(
                ["git", "-C", str(repo), "rev-parse", "HEAD"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            for slot in slots[1:]:
                slot.mkdir()
                subprocess.run(["git", "clone", str(repo), str(slot / "repo")], check=True, capture_output=True)
            resolved_slots = tuple(slot.resolve() for slot in slots)
            rows = {
                row: DatasetRow(row, f"org__repo+parallel-{row}", "repo", base_sha)
                for row in range(1, 21)
            }
            code = (
                "import os,pathlib,time; "
                f"root=pathlib.Path({str(marker_dir)!r}); "
                "(root/(os.environ['ARKFIX_WORKER_SLOT']+'.start')).write_text('1'); "
                "deadline=time.time()+5; "
                "exec(\"while len(list(root.glob('*.start'))) < 20 and time.time() < deadline:\\n time.sleep(0.02)\"); "
                "(root/'overlap.ok').write_text('1') if len(list(root.glob('*.start'))) >= 20 else None; "
                "time.sleep(0.2)"
            )
            specs = [
                WorkerSpec(
                    attempt=1,
                    worker=1,
                    rows=[row],
                    repo_dir=resolved_slots[0],
                    suffix=f"parallel_attempt01_w1_row_{row:04d}",
                    instance_filter=f"^{row}$",
                    command=[
                        sys.executable,
                        "-c",
                        code,
                        "--repo_dir",
                        str(resolved_slots[0]),
                        "--suffix",
                        f"parallel_attempt01_w1_row_{row:04d}",
                    ],
                    log_path=root / "logs" / f"row{row}_worker01.log",
                    batch_run_id="parallel-batch",
                    compatible_slots=resolved_slots,
                )
                for row in rows
            ]
            completed = run_workers(
                specs,
                dataset_rows=rows,
                worker_timeout_seconds=10,
                worker_start_interval_seconds=0,
                build_concurrency=20,
            )
            self.assertEqual(len(completed), 20)
            self.assertEqual(len({spec.repo_dir for spec in completed}), 20)
            self.assertTrue((marker_dir / "overlap.ok").is_file())

    def test_native_build_permit_allows_eight_concurrent_build_requests(self) -> None:
        active = 0
        peak = 0
        start = threading.Event()
        counter_lock = threading.Lock()

        def build() -> None:
            nonlocal active, peak
            start.wait()
            with native_build_permit():
                with counter_lock:
                    active += 1
                    peak = max(peak, active)
                time.sleep(0.1)
                with counter_lock:
                    active -= 1

        with tempfile.TemporaryDirectory() as raw_temp:
            with mock.patch.dict(os.environ, {"ARKFIX_BUILD_CONCURRENCY": "8"}):
                with mock.patch(
                    "sweagent.environment.utils.tempfile.gettempdir",
                    return_value=raw_temp,
                ):
                    with ThreadPoolExecutor(max_workers=20) as pool:
                        futures = [pool.submit(build) for _ in range(20)]
                        start.set()
                        for future in futures:
                            future.result()
        self.assertEqual(peak, 8)

    def test_same_timestamp_claim_is_atomic(self) -> None:
        with tempfile.TemporaryDirectory() as raw_temp:
            run_dir = Path(raw_temp) / "repair_same_timestamp"

            def claim() -> str:
                try:
                    run_repair.claim_run_directory(run_dir)
                    return "claimed"
                except FileExistsError:
                    return "rejected"

            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(lambda _: claim(), range(2)))
            self.assertEqual(results.count("claimed"), 1)
            self.assertEqual(results.count("rejected"), 1)

    def test_latest_input_requires_matching_manifest_and_prefers_complete_rows(self) -> None:
        with tempfile.TemporaryDirectory() as raw_temp:
            root = Path(raw_temp)

            def make_candidate(name: str, rows: list[int], manifest_rows: list[int], mtime: float) -> Path:
                folder = root / "localization" / "outputs" / "phase" / name
                folder.mkdir(parents=True)
                path = folder / "arkfix_input.jsonl"
                path.write_text(
                    "".join(json.dumps({"row": row}) + "\n" for row in rows),
                    encoding="utf-8",
                    newline="\n",
                )
                (folder / "manifest.json").write_text(
                    json.dumps({"rows": manifest_rows}), encoding="utf-8"
                )
                os.utime(path, (mtime, mtime))
                return path

            complete = make_candidate("complete", [1, 2, 3, 4], [1, 2, 3, 4], 100.0)
            make_candidate("newer_partial", [1, 2, 3], [1, 2, 3], 200.0)
            make_candidate("mismatch", [1, 2, 3, 4, 5], [1, 2, 3, 4], 300.0)
            with mock.patch.object(run_repair, "ROOT", root):
                self.assertEqual(run_repair.latest_arkfix_input(), complete)

            invalid = root / "invalid.jsonl"
            invalid.write_bytes(b'{"row":1,"text":"\xff"}\n')
            with self.assertRaises(UnicodeDecodeError):
                run_repair.load_jsonl(invalid)

    def test_trajectory_identity_raw_source_sha_and_lf_output_sha(self) -> None:
        with tempfile.TemporaryDirectory() as raw_temp:
            root = Path(raw_temp)
            trajectory_root = root / "trajectories"
            suffix = "unique_batch_suffix"
            old_dir = trajectory_root / f"old__{suffix}"
            old_dir.mkdir(parents=True)
            os.utime(old_dir, (10.0, 10.0))
            self.assertIsNone(find_trajectory_dir(suffix, trajectory_root, not_before_epoch=20.0))

            trajectory_dir = trajectory_root / f"new__{suffix}"
            patch_dir = trajectory_dir / "patches"
            patch_dir.mkdir(parents=True)
            now = time.time()
            instance_id = "org__repo+base-1"
            base_sha = "a" * 40
            batch_run_id = "batch-identity"
            raw_patch = (
                b"diff --git a/a.ets b/a.ets\r\n"
                b"--- a/a.ets\r\n+++ b/a.ets\r\n@@ -1 +1 @@\r\n-a\r\n+b\r\n"
            )
            patch_path = patch_dir / f"{instance_id}.patch"
            patch_path.write_bytes(raw_patch)
            meta_path = patch_dir / f"{instance_id}.meta.json"
            meta = {
                "instance_id": instance_id,
                "batch_run_id": batch_run_id,
                "worker_slot": "run03",
                "worker_suffix": suffix,
                "base_sha": base_sha,
                "project_path": ".",
                "defect_files": ["a.ets"],
                "allow_test_patch": False,
                "allow_test_patch_reason": "none",
                "max_steps_forced_submit": False,
                "final_validation": "passed",
                "base_apply_check": "leaderboard_raw_base_utf8_lf",
                "patch_sha256": hashlib.sha256(raw_patch).hexdigest(),
            }
            meta_path.write_text(json.dumps(meta), encoding="utf-8")
            traj_path = trajectory_dir / f"{instance_id}.traj"
            traj_path.write_text(
                json.dumps(
                    {
                        "trajectory": [],
                        "history": [],
                        "info": {"exit_status": "submitted", "max_steps_forced_submit": False},
                    }
                ),
                encoding="utf-8",
            )
            os.utime(trajectory_dir, (now, now))
            self.assertEqual(
                find_trajectory_dir(suffix, trajectory_root, not_before_epoch=now - 1),
                trajectory_dir,
            )

            spec = WorkerSpec(
                attempt=1,
                worker=1,
                rows=[1],
                repo_dir=root / "run03",
                suffix=suffix,
                instance_filter="^x$",
                command=["python"],
                log_path=root / "worker.log",
                batch_run_id=batch_run_id,
                started_at_epoch=now - 1,
                trajectory_dir=trajectory_dir,
                exit_code=0,
            )
            dataset_rows = {1: DatasetRow(1, instance_id, "repo", base_sha)}
            candidates, statuses = collect_candidates([spec], dataset_rows)
            self.assertTrue(statuses[1].ok, statuses[1].reason)
            candidate = candidates[1]
            self.assertEqual(candidate.source_patch_sha256, hashlib.sha256(raw_patch).hexdigest())
            self.assertNotIn("\r", candidate.text)
            self.assertEqual(candidate.sha256, hashlib.sha256(candidate.text.encode("utf-8")).hexdigest())

            meta["patch_only_generation"] = True
            meta["final_validation"] = "patch_only_pending_serial_build"
            meta["validation_status"] = "patch_only_scope_apply_pending_serial_build"
            meta_path.write_text(json.dumps(meta), encoding="utf-8")
            patch_only_candidates, patch_only_status = collect_candidates([spec], dataset_rows)
            self.assertTrue(patch_only_status[1].ok, patch_only_status[1].reason)
            self.assertIn(1, patch_only_candidates)
            meta["patch_only_generation"] = False
            meta["final_validation"] = "passed"
            meta.pop("validation_status", None)
            meta_path.write_text(json.dumps(meta), encoding="utf-8")

            deferred_output = root / "deferred-output"
            deferred_output.mkdir()
            deferred = write_outputs(
                deferred_output,
                dataset_rows,
                candidates,
                [1],
                attempt=1,
                dataset_path=root / "missing-dataset.jsonl",
                config_file=root / "missing-config.yaml",
                persist=False,
            )
            self.assertTrue(deferred[0].ok)
            self.assertEqual(list(deferred_output.iterdir()), [])

            output_dir = root / "output"
            output_dir.mkdir()
            written = write_outputs(
                output_dir,
                dataset_rows,
                candidates,
                [1],
                attempt=1,
                dataset_path=root / "missing-dataset.jsonl",
                config_file=root / "missing-config.yaml",
                publish_manifest=True,
            )
            self.assertTrue(written[0].ok)
            self.assertEqual((output_dir / "model_patch_1.patch").read_bytes(), candidate.text.encode("utf-8"))
            public_meta = json.loads((output_dir / "model_patch_1.meta.json").read_text(encoding="utf-8"))
            self.assertEqual(public_meta["source_patch_sha256"], hashlib.sha256(raw_patch).hexdigest())
            self.assertEqual(public_meta["output_patch_sha256"], candidate.sha256)
            self.assertTrue((output_dir / "manifest.jsonl").is_file())
            self.assertGreaterEqual(
                (output_dir / "manifest.jsonl").stat().st_mtime_ns,
                (output_dir / "self_check.json").stat().st_mtime_ns,
            )

            meta["batch_run_id"] = "wrong-batch"
            meta_path.write_text(json.dumps(meta), encoding="utf-8")
            rejected, rejected_status = collect_candidates([spec], dataset_rows)
            self.assertFalse(rejected)
            self.assertEqual(rejected_status[1].reason, "patch_meta_batch_mismatch")

            meta["batch_run_id"] = batch_run_id
            meta["base_sha"] = "b" * 40
            meta_path.write_text(json.dumps(meta), encoding="utf-8")
            rejected, rejected_status = collect_candidates([spec], dataset_rows)
            self.assertFalse(rejected)
            self.assertEqual(rejected_status[1].reason, "patch_meta_base_mismatch")

            meta["base_sha"] = base_sha
            meta["max_steps_forced_submit"] = True
            meta_path.write_text(json.dumps(meta), encoding="utf-8")
            rejected, rejected_status = collect_candidates([spec], dataset_rows)
            self.assertFalse(rejected)
            self.assertEqual(rejected_status[1].reason, "forced_submit_provenance_mismatch")

            traj_path.write_text(
                json.dumps(
                    {
                        "trajectory": [],
                        "history": [],
                        "info": {"exit_status": "submitted", "max_steps_forced_submit": True},
                    }
                ),
                encoding="utf-8",
            )
            meta["final_validation"] = "forced_unvalidated"
            meta_path.write_text(json.dumps(meta), encoding="utf-8")
            forced_candidates, forced_status = collect_candidates([spec], dataset_rows)
            self.assertTrue(forced_status[1].ok, forced_status[1].reason)
            self.assertIn(1, forced_candidates)

            meta["max_steps_forced_submit"] = False
            meta["final_validation"] = "passed"
            meta_path.write_text(json.dumps(meta), encoding="utf-8")
            rejected, rejected_status = collect_candidates([spec], dataset_rows)
            self.assertFalse(rejected)
            self.assertEqual(rejected_status[1].reason, "forced_submit_provenance_mismatch")

    def test_serial_apply_check_runs_real_build_and_records_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as raw_temp:
            root = Path(raw_temp)
            slot = root / "run03"
            repo = slot / "repo"
            (repo / ".git").mkdir(parents=True)
            (repo / "build-profile.json5").write_text("{}", encoding="utf-8")
            patch_text = (
                "diff --git a/entry/src/main/ets/Index.ets b/entry/src/main/ets/Index.ets\n"
                "--- a/entry/src/main/ets/Index.ets\n"
                "+++ b/entry/src/main/ets/Index.ets\n"
                "@@ -1 +1 @@\n-before\n+after\n"
            )
            source_meta = {
                "project_path": ".",
                "patch_only_generation": True,
                "validation_status": "patch_only_scope_apply_pending_serial_build",
            }
            candidate = PatchCandidate(
                row=1,
                instance_id="org__repo+base-1",
                patch_path=root / "source.patch",
                meta_path=root / "source.meta.json",
                traj_path=root / "source.traj",
                trajectory_dir=root,
                attempt=1,
                worker=3,
                repo_dir=slot,
                batch_run_id="batch",
                base_sha="a" * 40,
                text=patch_text,
                bytes_len=len(patch_text.encode("utf-8")),
                sha256=hashlib.sha256(patch_text.encode("utf-8")).hexdigest(),
                source_patch_sha256=hashlib.sha256(patch_text.encode("utf-8")).hexdigest(),
                trajectory_sha256="b" * 64,
                source_meta=source_meta,
                timing=TimingInfo(None, None, None, "missing_timing"),
            )
            dataset_rows = {
                1: DatasetRow(
                    1,
                    candidate.instance_id,
                    "repo",
                    candidate.base_sha,
                    ("entry/src/main/ets/Index.ets",),
                )
            }
            dataset_entries = {
                1: {
                    "base": {"sha": candidate.base_sha},
                    "defect_files": ["entry/src/main/ets/Index.ets"],
                }
            }
            permit = mock.MagicMock()
            with (
                mock.patch("evaluation.run_llm_patch_eval.reset_repo", return_value=(True, "ok")),
                mock.patch("evaluation.run_llm_patch_eval.apply_patch", return_value=(True, "ok")),
                mock.patch("evaluation.run_llm_patch_eval.run_environment_preprocess", return_value=(0, "ok")),
                mock.patch(
                    "evaluation.run_llm_patch_eval._determine_evaluation_scope",
                    return_value={"build_modules": [{"name": "entry"}]},
                ),
                mock.patch(
                    "evaluation.run_llm_patch_eval.run_build",
                    return_value=(0, "BUILD_STATUS=SUCCESS"),
                ) as run_build_mock,
                mock.patch("sweagent.environment.utils.native_build_permit", return_value=permit),
            ):
                statuses = serial_eval_apply_check(
                    output_dir=root,
                    dataset_entries=dataset_entries,
                    dataset_rows=dataset_rows,
                    candidates={1: candidate},
                    target_rows=[1],
                    repo_root=slot,
                    deveco_path="E:/DevEco",
                )
            self.assertTrue(statuses[1].ok, statuses[1].reason)
            run_build_mock.assert_called_once()
            self.assertEqual(source_meta["validation_status"], "validated_serial_build_scope_apply")
            self.assertEqual(source_meta["serial_validation"]["build_exit_code"], 0)

            with (
                mock.patch("evaluation.run_llm_patch_eval.reset_repo", return_value=(True, "ok")),
                mock.patch("evaluation.run_llm_patch_eval.apply_patch", return_value=(True, "ok")),
                mock.patch("evaluation.run_llm_patch_eval.run_environment_preprocess", return_value=(0, "ok")),
                mock.patch(
                    "evaluation.run_llm_patch_eval._determine_evaluation_scope",
                    return_value={"build_modules": [{"name": "entry"}]},
                ),
                mock.patch("evaluation.run_llm_patch_eval.run_build", return_value=(1, "BUILD FAILED")),
                mock.patch("sweagent.environment.utils.native_build_permit", return_value=permit),
            ):
                failed = serial_eval_apply_check(
                    output_dir=root,
                    dataset_entries=dataset_entries,
                    dataset_rows=dataset_rows,
                    candidates={1: candidate},
                    target_rows=[1],
                    repo_root=slot,
                    deveco_path="E:/DevEco",
                )
            self.assertFalse(failed[1].ok)
            self.assertTrue(failed[1].reason.startswith("serial_build_failed"))

    def test_evaluator_retries_transient_hvigor_project_cache_lock(self) -> None:
        build_app_path = WORKSPACE_ROOT / "evaluation" / "command_line_tools_test" / "tools" / "build_app.py"
        tools_dir = str(build_app_path.parent)
        sys.path.insert(0, tools_dir)
        try:
            spec = importlib.util.spec_from_file_location("arkeval_evaluator_build_app_test", build_app_path)
            self.assertIsNotNone(spec)
            self.assertIsNotNone(spec.loader)
            build_app = importlib.util.module_from_spec(spec)
            sys.modules[spec.name] = build_app
            spec.loader.exec_module(build_app)
        finally:
            sys.path.remove(tools_dir)

        transient = subprocess.CompletedProcess(
            args=["hvigorw"],
            returncode=1,
            stdout=(
                "EPERM: operation not permitted, open "
                "'C:\\Users\\xb\\.hvigor\\project_caches\\abc\\workspace\\node_modules\\.bin\\hvigor.ps1'"
            ),
            stderr="",
        )
        success = subprocess.CompletedProcess(
            args=["hvigorw"], returncode=0, stdout="BUILD SUCCESSFUL", stderr=""
        )
        with tempfile.TemporaryDirectory() as raw_temp:
            root = Path(raw_temp)
            repo = root / "repo"
            sdk = root / "sdk"
            deveco = root / "deveco"
            for path in (repo, sdk, deveco):
                path.mkdir()
            with (
                mock.patch.object(build_app, "find_hvigor_wrapper", return_value=Path("hvigorw.bat")),
                mock.patch.object(build_app, "find_node_home", return_value=Path("node")),
                mock.patch.object(build_app, "find_java_home", return_value=Path("java")),
                mock.patch.object(build_app, "build_harmony_command_env", return_value={}),
                mock.patch.object(build_app, "run_command", side_effect=[transient, success]) as run,
                mock.patch.object(build_app.time, "sleep"),
            ):
                output = build_app._run_hvigor_task_with_sdk_roots(
                    repo,
                    deveco,
                    task="assembleHap",
                    mode="module",
                    module="entry",
                    product="default",
                    target=None,
                    sdk_roots=[sdk],
                    sdk_meta={"sdk_selection_api_level": 18},
                )
            self.assertIn("BUILD SUCCESSFUL", output)
            self.assertEqual(run.call_count, 2)



if __name__ == "__main__":
    unittest.main()
