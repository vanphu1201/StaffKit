"""Enterprise / monorepo scale tests (thousands of files, hundreds of folders, huge source files)."""

import subprocess
import time
from pathlib import Path

from engine.compiler import PromptCompiler
from engine.indexer import (
    RepoIndexer,
    compact_repo_map_for_planner,
    extract_fast_skeleton,
    extract_symbol_windows,
    list_real_files,
    locate_relevant_files,
)
from engine.models import EngineConfig, LimitsConfig, ProjectConfig, TaskSpec


def test_mega_repo_2000_files_locator_compiler_and_planner_map(tmp_path: Path) -> None:
    repo = tmp_path / "enterprise_monorepo"
    repo.mkdir(parents=True, exist_ok=True)
    for args in (
        ["init", "-b", "main"],
        ["config", "user.name", "Bot"],
        ["config", "user.email", "bot@example.com"],
    ):
        subprocess.run(["git", *args], cwd=str(repo), check=True, capture_output=True)

    # Create 150 folders with 1,200+ source files + 450 test files (~1,650 tracked files total)
    for mod_idx in range(150):
        pkg_dir = repo / "packages" / f"domain_{mod_idx:03d}" / "src"
        test_dir = repo / "packages" / f"domain_{mod_idx:03d}" / "tests"
        pkg_dir.mkdir(parents=True, exist_ok=True)
        test_dir.mkdir(parents=True, exist_ok=True)
        for f_idx in range(8):
            (pkg_dir / f"module_{mod_idx:03d}_{f_idx}.py").write_text(
                f"def helper_{mod_idx}_{f_idx}(x: int) -> int:\n    return x + {f_idx}\n",
                encoding="utf-8",
            )
        for t_idx in range(3):
            (test_dir / f"test_mod_{mod_idx:03d}_{t_idx}.py").write_text(
                f"def test_ok_{t_idx}():\n    assert True\n",
                encoding="utf-8",
            )

    # Create target feature neighborhood + a 1,200-line service file + 1-hop imported model + guarded file
    inv_dir = repo / "services" / "inventory"
    inv_dir.mkdir(parents=True, exist_ok=True)
    (inv_dir / "models.py").write_text(
        "class StockItem:\n    def __init__(self, sku: str, available: int) -> None:\n        self.sku = sku\n        self.available = available\n",
        encoding="utf-8",
    )
    (inv_dir / "legacy_billing.py").write_text(
        "def charge_invoice(amount: int) -> int:\n    return amount\n",
        encoding="utf-8",
    )
    (inv_dir / "sibling_audit.py").write_text("AUDIT = True\n", encoding="utf-8")

    huge_lines = ["from services.inventory.models import StockItem", "", "class WarehouseAllocator:"]
    for idx in range(580):
        huge_lines.append(f"    def helper_step_{idx:03d}(self, qty: int) -> int:")
        huge_lines.append(f"        return qty + {idx}")
    huge_lines.extend(
        [
            "    def allocate_multi_warehouse(self, item: StockItem, qty: int) -> bool:",
            "        raise NotImplementedError",
        ]
    )
    (inv_dir / "warehouse_allocator.py").write_text("\n".join(huge_lines) + "\n", encoding="utf-8")

    (repo / "package.json").write_text('{"name": "enterprise-mono"}\n', encoding="utf-8")

    subprocess.run(["git", "add", "."], cwd=str(repo), check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init mega repo"], cwd=str(repo), check=True, capture_output=True)

    real_files = list_real_files(repo)
    assert len(real_files) >= 1650

    prd = (
        "Extend warehouse service (keep legacy_billing.py untouched without breaking it) "
        "to implement `WarehouseAllocator.allocate_multi_warehouse` in "
        "`services/inventory/warehouse_allocator.py`."
    )

    t0 = time.monotonic()
    located = locate_relevant_files(prd, real_files, repo)
    locate_sec = time.monotonic() - t0
    assert locate_sec < 3.0
    assert located.confident is True
    assert located.target_files[0] == "services/inventory/warehouse_allocator.py"
    assert "services/inventory/legacy_billing.py" not in located.target_files
    assert "services/inventory/models.py" in located.readonly_files
    assert "services/inventory/legacy_billing.py" in located.readonly_files
    assert len(located.protected_tests) >= 450

    cfg = EngineConfig(
        project=ProjectConfig(target_repo=str(repo)),
        limits=LimitsConfig(inline_file_max_lines=300, max_prompt_tokens_per_call=12000),
    )
    compiler = PromptCompiler(cfg)
    task = TaskSpec(
        task_id="T-MEGA-01",
        title="Implement allocate_multi_warehouse",
        objective=prd,
        allowed_write_files=located.target_files,
        readonly_context_files=located.readonly_files,
        protected_test_files=located.protected_tests,
        strict_scope=False,
    )
    prompt, est_tok = compiler.compile(task)
    assert est_tok < 6000
    assert "skeleton only" in prompt
    assert "allocate_multi_warehouse" in prompt
    assert "Relevant line windows:" in prompt
    assert "services/inventory/sibling_audit.py" in prompt
    assert "Folder Overview" in prompt
    assert "more protected test files" in prompt

    # Verify RepoIndexer fast mega-repo path skips full repomix and generates valid scoped XML
    indexer = RepoIndexer(cfg)
    t1 = time.monotonic()
    map_path = indexer.build_repo_map(
        focus_files=located.target_files + located.readonly_files + located.candidate_files,
        priority_identifiers=located.identifiers,
    )
    index_sec = time.monotonic() - t1
    assert index_sec < 2.0
    xml_text = map_path.read_text(encoding="utf-8")
    assert "<directory_structure>" in xml_text
    assert '<file path="services/inventory/warehouse_allocator.py">' in xml_text

    compacted = compact_repo_map_for_planner(
        xml_text,
        max_chars=12000,
        priority_files=set(located.target_files) | set(located.readonly_files),
    )
    assert "services/inventory/warehouse_allocator.py" in compacted
    assert "allocate_multi_warehouse" in compacted


def test_prose_locator_excludes_docs_and_skeleton_clips_giant_data_lines(tmp_path: Path) -> None:
    repo = tmp_path / "prose_repo"
    for d in ("pkg", "docs"):
        (repo / d).mkdir(parents=True, exist_ok=True)
    (repo / "CHANGES.md").write_text("wrapping double-width cells\n", encoding="utf-8")
    (repo / "docs" / "wrap.md").write_text("wrapping double-width cells\n", encoding="utf-8")
    (repo / "pkg" / "cells.py").write_text("def cell_len(s: str) -> int:\n    return len(s)\n", encoding="utf-8")
    (repo / "pkg" / "width_table.py").write_text("WIDTHS = [(0, 1)]\n", encoding="utf-8")
    (repo / "pkg" / "_wrap.py").write_text(
        "from .cells import cell_len\nfrom .width_table import WIDTHS\n\n"
        "def divide_line(text: str, width: int) -> list[int]:\n    return []\n",
        encoding="utf-8",
    )
    (repo / "pkg" / "_emoji_data.py").write_text(
        "EMOJI = {" + ", ".join(f'":{i}:": "x"' for i in range(4000)) + "}\n",
        encoding="utf-8",
    )

    real_files = {p.relative_to(repo).as_posix() for p in repo.rglob("*") if p.is_file()}
    located = locate_relevant_files(
        "Update wrapping logic for double-width cells when folding long words.",
        real_files,
        repo,
    )
    assert located.target_files[0] == "pkg/_wrap.py"
    assert "CHANGES.md" not in located.target_files
    assert "docs/wrap.md" not in located.target_files
    assert "pkg/cells.py" in located.target_files
    assert "pkg/width_table.py" in located.readonly_files

    skel = extract_fast_skeleton(repo / "pkg" / "_emoji_data.py")
    assert all(len(line) <= 225 for line in skel.splitlines())


def test_idf_and_dropoff_cutoff_demotes_low_score_files_to_readonly(tmp_path: Path) -> None:
    repo = tmp_path / "idf_repo"
    (repo / "src").mkdir(parents=True, exist_ok=True)
    # Write 10 files with common identifier, 2 runner-up files with 2 shared identifiers, and 1 core file with rare symbol + basename match.
    for idx in range(10):
        (repo / "src" / f"common_{idx}.py").write_text(
            f"def fn_{idx}(config_option: str) -> str:\n    return config_option\n",
            encoding="utf-8",
        )
    for idx in range(2):
        (repo / "src" / f"runner_{idx}.py").write_text(
            f"def aux_{idx}(config_option: str, fallback_mode: bool) -> str:\n    return config_option\n",
            encoding="utf-8",
        )
    (repo / "src" / "core_handler.py").write_text(
        "def unique_show_envvar(config_option: str, fallback_mode: bool) -> str:\n    return config_option\n",
        encoding="utf-8",
    )

    real_files = {p.relative_to(repo).as_posix() for p in repo.rglob("*.py")}
    located = locate_relevant_files(
        "Fix `unique_show_envvar` in `core_handler.py` when `config_option` uses `fallback_mode`.",
        real_files,
        repo,
    )
    assert located.target_files == ["src/core_handler.py"]
    assert any(p.startswith("src/runner_") for p in located.readonly_files)
    assert not any(p.startswith("src/common_") for p in located.target_files)


def test_ast_function_window_extracts_complete_enclosing_method(tmp_path: Path) -> None:
    src = tmp_path / "large_mod.py"
    lines = ["class Handler:"]
    for i in range(40):
        lines.append(f"    def filler_{i}(self) -> int:")
        lines.append(f"        return {i}")
    # Enclosing method >5 lines away from the hit line should still be captured from its header to return
    lines.extend(
        [
            "    @property",
            "    def compute_special_discount(self) -> int:",
            "        a = 10",
            "        b = 20",
            "        c = 30",
            "        target_formula_symbol = a + b + c",
            "        d = target_formula_symbol * 2",
            "        return d",
        ]
    )
    for i in range(40, 80):
        lines.append(f"    def trailing_{i}(self) -> int:")
        lines.append(f"        return {i}")
    src.write_text("\n".join(lines) + "\n", encoding="utf-8")

    window = extract_symbol_windows(src, ["target_formula_symbol"], radius=2, max_total_lines=80)
    assert "@property" in window
    assert "def compute_special_discount(self) -> int:" in window
    assert "target_formula_symbol = a + b + c" in window
    assert "return d" in window
    assert "filler_0" not in window
    assert "trailing_79" not in window
