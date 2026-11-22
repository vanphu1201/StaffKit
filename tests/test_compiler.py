"""Unit tests for RepoIndexer XML skeleton lookup and Surgical PromptCompiler."""

from pathlib import Path

import pytest

from engine.compiler import PromptCompiler, PromptTooLargeError
from engine.indexer import extract_file_skeletons
from engine.models import EngineConfig, LimitsConfig, ProjectConfig, TaskSpec


def _write_repo_map(repo: Path) -> Path:
    auto_dir = repo / ".auto-engine"
    auto_dir.mkdir(parents=True, exist_ok=True)
    xml_path = auto_dir / "repo_map.xml"
    xml_path.write_text(
        """<repomix>
<files>
<file path="src/types/user.ts">
export interface User { id: string; role: 'admin' | 'user'; }
</file>
<file path="src/big_module.ts">
export function hugeHandler(req: Request): void
export function helperB(): string
</file>
<file path="src/unrelated_secret_module.ts">
UNRELATED_MARKER_SHOULD_NOT_APPEAR_IN_PROMPT
</file>
</files>
</repomix>
""",
        encoding="utf-8",
    )
    return xml_path


def test_compiler_static_prefix_invariance_and_xml_lookup(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / "src" / "types").mkdir(parents=True)
    (repo / "src" / "small.ts").write_text("export const x = 1;\n", encoding="utf-8")
    (repo / "src" / "big_module.ts").write_text(
        "\n".join(f"const line{i} = {i};" for i in range(350)) + "\n",
        encoding="utf-8",
    )
    map_path = _write_repo_map(repo)

    cfg = EngineConfig(
        project=ProjectConfig(target_repo=str(repo)),
        limits=LimitsConfig(inline_file_max_lines=300, max_prompt_tokens_per_call=12000),
    )
    compiler = PromptCompiler(cfg)

    t1 = TaskSpec(
        task_id="T-01",
        title="Small file edit",
        objective="Update small.ts",
        allowed_write_files=["src/small.ts"],
        readonly_context_files=["src/types/user.ts"],
        verify_commands=["npx tsc --noEmit"],
    )
    t2 = TaskSpec(
        task_id="T-02",
        title="Big file edit",
        objective="Update big_module.ts",
        allowed_write_files=["src/big_module.ts"],
        readonly_context_files=["src/types/user.ts"],
        verify_commands=["npx tsc --noEmit"],
    )

    p1, _ = compiler.compile(t1, attempt=1, last_error="TS2322 error", last_verdict="VERIFY_FAIL", compact_diff="DIFF_SHOULD_OMIT", repo_map_path=map_path)
    p2, _ = compiler.compile(t2, attempt=1, last_error="TS2322 error", last_verdict="VERIFY_FAIL", compact_diff="DIFF_SHOULD_INCLUDE", repo_map_path=map_path)

    # Static prefix is byte-for-byte identical before delimiter
    prefix1 = p1.split("---\n[DYNAMIC TASK PAYLOAD", 1)[0]
    prefix2 = p2.split("---\n[DYNAMIC TASK PAYLOAD", 1)[0]
    assert prefix1 == prefix2
    assert "PROTECTED_TEST_FILES" in prefix1

    cfg_no_shell = cfg.model_copy(deep=True)
    cfg_no_shell.agy.forbid_shell_verify_commands = True
    assert "TUYỆT ĐỐI KHÔNG tự chạy lệnh test" in PromptCompiler(cfg_no_shell).build_static_prefix()

    # Full repo_map is NOT dumped; unrelated module must not appear
    assert "UNRELATED_MARKER_SHOULD_NOT_APPEAR_IN_PROMPT" not in p1
    assert "export interface User" in p1

    # Small file is inlined with line numbers and omits duplicate git diff on retry
    assert "L1: export const x = 1;" in p1
    assert "DIFF_SHOULD_OMIT" not in p1

    # Big file (>300 lines) uses XML skeleton and keeps compact git diff on retry
    assert "skeleton only" in p2
    assert "export function hugeHandler" in p2
    assert "DIFF_SHOULD_INCLUDE" in p2


def test_compiler_raises_when_prompt_exceeds_hard_cap(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    # Create an oversized file under 300 lines (very long lines) to trigger PromptTooLargeError
    (repo / "src" / "wide.ts").write_text(("x" * 1000 + "\n") * 50, encoding="utf-8")
    cfg = EngineConfig(
        project=ProjectConfig(target_repo=str(repo)),
        limits=LimitsConfig(inline_file_max_lines=300, max_prompt_tokens_per_call=800),
    )
    compiler = PromptCompiler(cfg)
    task = TaskSpec(
        task_id="T-99",
        title="Oversized",
        objective="Test cap",
        allowed_write_files=["src/wide.ts"],
    )
    with pytest.raises(PromptTooLargeError):
        compiler.compile(task)


def test_extract_file_skeletons_handles_windows_paths(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    map_path = _write_repo_map(repo)
    found = extract_file_skeletons(map_path, [r"src\types\user.ts"])
    assert "src/types/user.ts" in found


def test_extract_file_skeletons_handles_real_repomix_preamble_and_html(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    auto_dir = repo / ".auto-engine"
    auto_dir.mkdir(parents=True, exist_ok=True)
    xml_path = auto_dir / "repo_map.xml"
    xml_path.write_text(
        "This file is a merged representation of a subset of the codebase.\n\n"
        "<file_summary>notes</file_summary>\n"
        "<files>\n"
        '<file path="src/ui/index.html">\n'
        '<div class="box">a & b < 3</div>\n'
        "</file>\n"
        "</files>\n",
        encoding="utf-8",
    )
    found = extract_file_skeletons(xml_path, ["src/ui/index.html"])
    assert "src/ui/index.html" in found
    assert '<div class="box">a & b < 3</div>' in found["src/ui/index.html"]
