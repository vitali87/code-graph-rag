# Hướng dẫn Chạy Dự án Code-Graph-RAG

Dựa trên tài liệu gốc (`README.md` và `TechContext`), dưới đây là các bước chi tiết để cấu hình và chạy dự án **Code-Graph-RAG** thông qua giao diện dòng lệnh (CLI `cgr`).

## 1. Yêu cầu hệ thống (Prerequisites)

Đảm bảo máy tính của bạn đã cài đặt sẵn các thành phần sau:
- **Python 3.12+**
- **Docker & Docker Compose** (để chạy cơ sở dữ liệu Memgraph)
- **cmake** (để build thư viện `pymgclient`)
- **ripgrep (`rg`)** (hỗ trợ tìm kiếm văn bản tốc độ cao trong shell command)
- **uv** (Trình quản lý gói siêu tốc cho Python)

*(Trên Linux/Ubuntu, bạn có thể cài `cmake` và `ripgrep` thông qua lệnh: `sudo apt-get install cmake ripgrep`)*

## 2. Cài đặt Phụ thuộc (Dependencies)

Tại thư mục gốc của dự án (`/home/nguyen-thanh-hung/Documents/Code/code-graph-rag`), bạn có thể sử dụng `make` hoặc `uv` để cài đặt.

**Cách 1: Sử dụng Make (Khuyên dùng cho Development)**
Lệnh này sẽ tự động tải các gói phụ thuộc, biên dịch Tree-sitter grammars và thiết lập pre-commit hooks:
```bash
make dev
```

**Cách 2: Sử dụng UV**
Để cài đặt thông qua `uv` với đầy đủ hỗ trợ cho các ngôn ngữ:
```bash
uv sync --extra treesitter-full
```

## 3. Cấu hình Biến môi trường

Dự án sử dụng các API Models (OpenAI, Google Gemini, Ollama cục bộ) và cấu hình kết nối Memgraph.

Thiết lập tệp cấu hình bằng cách copy file mẫu:
```bash
cp .env.example .env
```
Mở tệp `.env` vừa tạo và điền các API key tương ứng.
Dự án cho phép trộn mô hình (Ví dụ: dùng Google cho `ORCHESTRATOR_PROVIDER` và Ollama cho `CYPHER_PROVIDER`).

## 4. Khởi động Cơ sở dữ liệu Memgraph

Hệ thống sử dụng Memgraph làm Data Store cho đồ thị. Để khởi chạy nó bằng Docker, hãy chạy:
```bash
docker-compose up -d
```

## 5. Khởi chạy Dự án (Sử dụng CLI `cgr`)

Chúng ta ưu tiên sử dụng lệnh CLI `cgr`, được thực thi bằng cách thêm tiền tố lệnh `uv run cgr`.

### Tùy chọn 1: Index một repository mới từ đầu (Khởi tạo đồ thị)
Sử dụng cờ `--clean` để xoá dữ liệu cũ và tiến hành parse lại toàn bộ dự án mới:
```bash
uv run cgr start --repo-path /đường/dẫn/tới/repo_của_bạn --update-graph --clean
```

### Tùy chọn 2: Khởi động phiên tương tác RAG (Truy vấn)
Nếu bạn đã index xong codebase (Ví dụ: `progcoder-shop-microservices`), chạy lệnh sau để mở phiên hỏi đáp bằng ngôn ngữ tự nhiên:
```bash
uv run cgr start --repo-path /home/nguyen-thanh-hung/Documents/Code/progcoder-shop-microservices
```

## 6. Các Tính năng Tùy chọn Khác

- **Cập nhật đồ thị theo thời gian thực (Real-time Updater):**
  Nếu bạn đang thay đổi code và muốn đồ thị tự động cập nhật, hãy mở một terminal thứ hai và chạy:
  ```bash
  make watch REPO_PATH=/đường/dẫn/tới/repo_của_bạn
  ```

- **Tối ưu hoá Code bằng AI:**
  Nhận gợi ý tối ưu và refactor đoạn mã cho từng ngôn ngữ (Ví dụ: `python`, `javascript`, `typescript`, `cpp`):
  ```bash
  uv run cgr optimize <ngôn_ngữ> --repo-path /đường/dẫn/tới/repo_của_bạn
  ```
