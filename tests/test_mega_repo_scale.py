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
    assert "pkg/cells.py" in (located.target_files + located.readonly_files)
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


def test_universal_stack_detection_dynamic_blueprint_and_traceback_escalation(tmp_path: Path) -> None:
    from engine.bootstrapper import detect_existing_stack
    from engine.interviewer import save_custom_blueprint_if_needed
    from engine.models import ProductSpec

    # 1. Non-listed tech product (e.g. Rust / Go / Flutter) detected at 0 tokens
    rust_dir = tmp_path / "rust_cli"
    rust_dir.mkdir(parents=True)
    (rust_dir / "Cargo.toml").write_text('[package]\nname = "demo"\nversion = "0.1.0"\n', encoding="utf-8")
    stack, v_cmds, f_cmds, prev = detect_existing_stack(rust_dir)
    assert stack == "rust_app"
    assert "cargo check" in v_cmds
    assert "cargo test" in f_cmds

    # 2. Dynamic blueprint synthesis for any brand-new tech product (e.g. Godot/Unity/Chrome Extension)
    custom_dir = tmp_path / "chrome_ext"
    custom_dir.mkdir(parents=True)
    spec = ProductSpec(
        product_title="Trình quản lý Tab thông minh",
        features_vi=["Gom nhóm tab tự động"],
        prd_markdown="# PRD\nBuild Chrome Extension Manifest V3",
        verify_commands=["node scripts/validate_manifest.js"],
        preview_command="npx web-ext run",
        quality_rules=["Manifest V3 strict CSP"],
    )
    bp_path = save_custom_blueprint_if_needed(custom_dir, spec, stack="generic")
    assert bp_path is not None and bp_path.is_file()
    st2, vc2, _fc2, pv2 = detect_existing_stack(custom_dir)
    assert st2 == "custom_product"
    assert vc2 == ["node scripts/validate_manifest.js"]
    assert pv2 == "npx web-ext run"

    # 3. Pillar 4: Traceback file+line auto-extraction on retry attempt > 0
    (custom_dir / "src").mkdir(parents=True, exist_ok=True)
    (custom_dir / "src" / "worker.py").write_text(
        "\n".join(f"x_{i} = {i}" for i in range(1, 30)) + "\n",
        encoding="utf-8",
    )
    cfg = EngineConfig(project=ProjectConfig(target_repo=str(custom_dir)))
    compiler = PromptCompiler(cfg)
    task = TaskSpec(
        task_id="T-ERR",
        title="Fix worker",
        objective="Fix crash",
        allowed_write_files=["src/other.py"],
        create_new_files=["src/other.py"],
    )
    tb = f'Traceback (most recent call last):\n  File "{(custom_dir / "src" / "worker.py").as_posix()}", line 15, in run\n    raise ValueError("bad")'
    retry_prompt, _ = compiler.compile(task, attempt=1, last_error=tb, last_verdict="VERIFY_FAIL")
    assert "Crashing Code Context (Auto-Extracted from Traceback)" in retry_prompt
    assert "> L15: x_15 = 15" in retry_prompt


def test_pillar_2_contract_first_and_pillar_3_final_gate_auto_repair(tmp_path: Path) -> None:
    import sys
    from engine.database import StateDB
    from engine.git_guard import GitGuard
    from engine.interviewer import persist_project_contracts
    from engine.models import ExecutionResult, ProductSpec, TokenUsage
    from engine.orchestrator import Orchestrator
    from engine.planner import synthesize_deterministic_contract
    from engine.verifier import Verifier

    repo = tmp_path / "contract_and_final_gate_repo"
    repo.mkdir(parents=True, exist_ok=True)
    for args in (
        ["init", "-b", "main"],
        ["config", "user.name", "Bot"],
        ["config", "user.email", "bot@example.com"],
    ):
        subprocess.run(["git", *args], cwd=str(repo), check=True, capture_output=True)

    (repo / "src").mkdir(parents=True, exist_ok=True)
    (repo / "src" / "__init__.py").write_text("", encoding="utf-8")
    (repo / "src" / "service.py").write_text(
        "def compute_total(val: int) -> int:\n    return val + 1\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "-A"], cwd=str(repo), check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=str(repo), check=True, capture_output=True)

    # 1. Pillar 2: Deterministic & Spec-driven Architecture Contract synthesis + Compiler injection
    t1 = TaskSpec(
        task_id="T-001",
        title="Implement service module",
        objective="Export compute_total(val: int) -> int returning val + 5",
        allowed_write_files=["src/service.py"],
        verify_commands=[f'"{sys.executable}" -c "import src.service"'],
    )
    t2 = TaskSpec(
        task_id="T-002",
        title="Verify contract consumer",
        objective="Ensure compute_total satisfies contract",
        depends_on=["T-001"],
        allowed_write_files=["src/service.py"],
        verify_commands=[f'"{sys.executable}" -c "import src.service"'],
    )
    auto_contract = synthesize_deterministic_contract("# PRD\nCompute total with +5 fee.", [t1, t2])
    assert "Module & File Ownership Map" in auto_contract
    assert "src/service.py" in auto_contract

    spec = ProductSpec(
        product_title="Billing Service",
        prd_markdown="# PRD\nCompute total with +5 fee.",
        architecture_contract="### Shared Interface\n- `src/service.py`: `compute_total(val: int) -> int` MUST return `val + 5`.",
        runtime_verify_commands=[
            f'"{sys.executable}" -c "from src.service import compute_total; assert compute_total(10) == 15"'
        ],
    )
    c_path = persist_project_contracts(repo, spec)
    assert c_path is not None and c_path.is_file()

    cfg = EngineConfig(
        project=ProjectConfig(target_repo=str(repo)),
        sprint={
            "final_verify_commands": spec.runtime_verify_commands,
        },
    )
    compiler = PromptCompiler(cfg)
    compiled_prompt, _ = compiler.compile(t1)
    assert "# ARCHITECTURE & INTERFACE CONTRACT (Shared Across All Tasks)" in compiled_prompt
    assert "compute_total(val: int) -> int" in compiled_prompt

    # 2. Pillar 3: Final Gate / Runtime Gate Auto-Repair Loop (T-FINAL-FIX)
    db = StateDB(cfg.state_db_path)
    db.replace_plan([t1], plan_hash="test_hash", approved=True)

    class RepairingExecutor:
        def __init__(self) -> None:
            self.seen_tasks: list[str] = []

        def execute(self, prompt: str, task: TaskSpec, model: str, effort: str) -> ExecutionResult:
            self.seen_tasks.append(task.task_id)
            if task.task_id == "T-001":
                # Task 1 leaves a subtle bug (returns val + 2 instead of val + 5), passing unit import check
                (repo / "src" / "service.py").write_text(
                    "def compute_total(val: int) -> int:\n    return val + 2\n",
                    encoding="utf-8",
                )
            elif task.task_id == "T-FINAL-FIX":
                # Auto-repair task receives the final gate failure + contract and fixes the bug
                assert "ARCHITECTURE & INTERFACE CONTRACT" in prompt
                (repo / "src" / "service.py").write_text(
                    "def compute_total(val: int) -> int:\n    return val + 5\n",
                    encoding="utf-8",
                )
            return ExecutionResult(
                success=True,
                model_used=model or "mock",
                exit_code=0,
                stdout="done",
                stderr="",
                duration_seconds=0.1,
                token_usage=TokenUsage(prompt_tokens=100, completion_tokens=50, total_tokens=150),
            )

    exec_mock = RepairingExecutor()
    guard = GitGuard(repo)
    orch = Orchestrator(
        cfg,
        db=db,
        guard=guard,
        verifier=Verifier(cfg, guard),
        compiler=compiler,
        executor=exec_mock,
    )
    summary = orch.run_sprint()

    assert "T-001" in exec_mock.seen_tasks
    assert "T-FINAL-FIX" in exec_mock.seen_tasks
    assert summary["final_verify_passed"] is True
    assert summary["summary"]["counts"]["DONE"] == 2


def test_multi_lang_brace_windowing_and_cross_task_handoff_and_acceptance_contract(tmp_path: Path) -> None:
    import sys
    from engine.database import StateDB
    from engine.git_guard import GitGuard
    from engine.interviewer import persist_project_contracts
    from engine.models import ExecutionResult, ProductSpec, TokenUsage
    from engine.orchestrator import Orchestrator
    from engine.verifier import Verifier

    # 1. Multi-Language Brace-Balanced Block Windowing (.tsx / .go / .rs)
    tsx_file = tmp_path / "OrderBoard.tsx"
    tsx_lines = [f"// header padding {i}" for i in range(1, 40)]
    tsx_lines.extend(
        [
            "export function OrderCard({ orderId, amount }: { orderId: string; amount: number }) {",
            "  const formatted = `$${amount.toFixed(2)}`;",
            "  const statusLabel = amount > 100 ? 'VIP' : 'Standard';",
            "  if (!orderId) {",
            "    return <div className='empty'>Missing order</div>;",
            "  }",
            "  return (",
            "    <section data-testid='order-card'>",
            "      <h3>{orderId}</h3>",
            "      <span>{formatted} - {statusLabel}</span>",
            "    </section>",
            "  );",
            "}",
        ]
    )
    tsx_lines.extend([f"// footer padding {i}" for i in range(1, 40)])
    tsx_file.write_text("\n".join(tsx_lines) + "\n", encoding="utf-8")

    win = extract_symbol_windows(tsx_file, ["statusLabel"], radius=5, max_total_lines=60)
    # Brace-balanced block extractor must capture the entire OrderCard component from declaration to closing brace
    assert "export function OrderCard" in win
    assert "<section data-testid='order-card'>" in win
    assert "L52: }" in win

    # 2. Acceptance Criteria Contract + 0-Token Cross-Task Episodic Handoff Memory
    repo = tmp_path / "handoff_repo"
    repo.mkdir(parents=True, exist_ok=True)
    for args in (
        ["init", "-b", "main"],
        ["config", "user.name", "Bot"],
        ["config", "user.email", "bot@example.com"],
    ):
        subprocess.run(["git", *args], cwd=str(repo), check=True, capture_output=True)

    (repo / "src").mkdir(parents=True, exist_ok=True)
    (repo / "src" / "__init__.py").write_text("", encoding="utf-8")
    (repo / "src" / "pricing.py").write_text("def calc_price(x: int) -> int:\n    return x\n", encoding="utf-8")
    (repo / "src" / "checkout.py").write_text("from src.pricing import calc_price\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=str(repo), check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=str(repo), check=True, capture_output=True)

    spec = ProductSpec(
        product_title="Smart Checkout",
        prd_markdown="# PRD\n- AC-1: Apply discount tier\n- AC-2: Validate negative input",
        architecture_contract="### Contract\n- `calc_price(x: int) -> int`",
        acceptance_criteria=[
            "AC-1: Apply discount tier accurately",
            "AC-2: Reject negative numbers with ValueError",
        ],
    )
    persist_project_contracts(repo, spec)

    t1 = TaskSpec(
        task_id="T-001",
        title="Build pricing engine",
        objective="Implement calc_price and apply_coupon in src/pricing.py",
        allowed_write_files=["src/pricing.py"],
        verify_commands=[f'"{sys.executable}" -c "import src.pricing"'],
    )
    t2 = TaskSpec(
        task_id="T-002",
        title="Wire checkout flow",
        depends_on=["T-001"],
        objective="Use apply_coupon in src/checkout.py",
        allowed_write_files=["src/checkout.py"],
        verify_commands=[f'"{sys.executable}" -c "import src.checkout"'],
    )

    cfg = EngineConfig(project=ProjectConfig(target_repo=str(repo)))
    db = StateDB(cfg.state_db_path)
    db.replace_plan([t1, t2], plan_hash="h_handoff", approved=True)

    t2_prompts: list[str] = []

    class HandoffCheckingExecutor:
        def execute(self, prompt: str, task: TaskSpec, model: str, effort: str) -> ExecutionResult:
            if task.task_id == "T-001":
                (repo / "src" / "pricing.py").write_text(
                    "def calc_price(x: int) -> int:\n    return x * 2\n\ndef apply_coupon(code: str) -> int:\n    return 10\n",
                    encoding="utf-8",
                )
            elif task.task_id == "T-002":
                t2_prompts.append(prompt)
                (repo / "src" / "checkout.py").write_text(
                    "from src.pricing import apply_coupon, calc_price\n\ndef checkout_total(x: int) -> int:\n    return calc_price(x) - apply_coupon('VIP')\n",
                    encoding="utf-8",
                )
            return ExecutionResult(
                success=True,
                model_used=model or "mock",
                exit_code=0,
                stdout="ok",
                stderr="",
                duration_seconds=0.1,
                token_usage=TokenUsage(prompt_tokens=80, completion_tokens=40, total_tokens=120),
            )

    guard = GitGuard(repo)
    orch = Orchestrator(
        cfg,
        db=db,
        guard=guard,
        verifier=Verifier(cfg, guard),
        compiler=PromptCompiler(cfg),
        executor=HandoffCheckingExecutor(),
    )
    res = orch.run_sprint()
    assert res["summary"]["counts"]["DONE"] == 2
    assert len(t2_prompts) == 1
    # Verify T-002 automatically received both Acceptance Criteria contract and 0-token T-001 Handoff signatures
    assert "## Acceptance Criteria (Must Pass Before Completion)" in t2_prompts[0]
    assert "AC-2: Reject negative numbers with ValueError" in t2_prompts[0]
    assert "# UPSTREAM COMPLETED TASKS HANDOFF" in t2_prompts[0]
    assert "### T-001: Build pricing engine" in t2_prompts[0]
    assert "def apply_coupon(code: str) -> int:" in t2_prompts[0]

    # 3. Greenfield create_new_files already created on disk during Attempt 0 must be inlined on Attempt 1 (never hidden as NEW FILE TO CREATE)
    greenfield_task = TaskSpec(
        task_id="T-GF",
        title="Create new service and test",
        objective="Create service_a.py and service_b.py",
        allowed_write_files=["src/service_a.py", "src/service_b.py"],
        create_new_files=["src/service_a.py", "src/service_b.py"],
    )
    (repo / "src" / "service_a.py").write_text("def already_written() -> int:\n    return 42\n", encoding="utf-8")
    gf_prompt, _ = PromptCompiler(cfg).compile(greenfield_task, attempt=1, last_error="missing service_b")
    assert "src/service_a.py (2 lines — ALREADY EXISTS ON DISK)" in gf_prompt
    assert "def already_written() -> int:" in gf_prompt
    assert "## src/service_b.py\n(NEW FILE TO CREATE" in gf_prompt



