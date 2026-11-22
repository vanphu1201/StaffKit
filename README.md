# Auto-Prompt Engine

Bộ máy điều phối lập trình tự động tối ưu token và chống ảo giác cho **Antigravity (`agy` CLI / Python SDK / IDE)** theo kiến trúc 5 tầng (**Planner–Compiler–Executor–Verifier**).

Xem đặc tả kiến trúc chi tiết tại `auto_prompt_engine_architecture_implementation_spec.md`.

## Cài đặt nhanh

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
```

Hoặc nếu đã cài `uv`:

```bash
uv sync --extra dev
```

## Chạy bằng App Desktop (Khuyên dùng)

* **Cách 1 (1-click trên Windows):** Nhấp đúp vào file `AutoPromptEngine.bat`.
* **Cách 2 (qua terminal):**

```powershell
.\.venv\Scripts\engine.exe ui --config config.yaml
```

Ứng dụng sẽ khởi động máy chủ nội bộ tại `127.0.0.1` và tự mở cửa sổ Desktop độc lập để bạn chọn thư mục dự án, nhập/mở PRD, lập kế hoạch, duyệt và theo dõi log chạy theo thời gian thực.

## Quy trình sử dụng qua CLI

```powershell
# 1. Kiểm tra công cụ sẵn sàng và tạo config.yaml
engine init --check
engine init --smoke-agy

# 2. Lập kế hoạch DAG từ PRD (hoặc import tasks.json)
engine plan path/to/prd.md

# 3. Duyệt kế hoạch trước khi thực thi
engine approve

# 4. Chạy vòng lặp tự động qua Antigravity (`agy -p`)
engine run

# 5. Nếu gián đoạn giữa chừng, khôi phục sạch và chạy tiếp
engine resume

# 6. Xem thống kê token / pass-fail
engine report
```
