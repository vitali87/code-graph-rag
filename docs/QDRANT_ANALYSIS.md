# Phân Tích Chi Tiết Qdrant Trong Dự Án Code-Graph-RAG

Tài liệu này cung cấp cái nhìn chi tiết về cách Qdrant được tích hợp và sử dụng để hỗ trợ tìm kiếm ngữ nghĩa (semantic search) trong hệ thống code-graph-rag.

## 1. Kiến Trúc Hoạt Động

Qdrant không được triển khai như một service độc lập (container riêng) trong dự án này. Thay vào đó, nó hoạt động ở chế độ **Embedded Mode** (hoặc Local Storage mode).

- **Thư viện sử dụng**: `qdrant-client`.
- **Cơ chế**: Khi khởi tạo `QdrantClient` với một đường dẫn (`path`), thư viện sẽ sử dụng engine lưu trữ cục bộ dựa trên disk, chạy trong cùng tiến trình với ứng dụng Python.
- **Vị trí dữ liệu**: Mặc định lưu tại thư mục `.qdrant_code_embeddings` nằm ở thư mục gốc của repository đang được index.

## 2. Cấu Hình & Tham Số

Các thông số cấu hình chính được định nghĩa trong `codebase_rag/config.py`:

| Tham số | Giá trị mặc định | Ý nghĩa |
| :--- | :--- | :--- |
| `QDRANT_DB_PATH` | `./.qdrant_code_embeddings` | Thư mục lưu trữ database cục bộ. |
| `QDRANT_COLLECTION_NAME` | `code_embeddings` | Tên collection duy nhất để lưu trữ vector. |
| `QDRANT_VECTOR_DIM` | `768` | Kích thước vector (tương ứng với mô hình embedding). |
| `QDRANT_TOP_K` | `5` | Số lượng kết quả tìm kiếm mặc định. |
| `Distance` | `Cosine` | Lựa chọn thuật toán đo khoảng cách (Cosine Similarity). |

## 3. Quy Trình Indexing (Dữ liệu vào)

Quy trình tạo và lưu trữ vector diễn ra ở giai đoạn cuối của quá trình index repository, được điều khiển bởi lớp `GraphUpdater` trong `codebase_rag/graph_updater.py`:

1. **Truy vấn Memgraph**: Lấy danh sách các hàm/lớp đã được index kèm theo vị trí mã nguồn (file path, line numbers).
2. **Trích xuất Code**: Đọc nội dung mã nguồn thực tế từ disk.
3. **Tạo Embedding**: Sử dụng module `embedder.py` (thường gọi đến một mô hình như gte-base hoặc OpenAI) để chuyển code thành vector số.
4. **Lưu Qdrant**: Gọi hàm `store_embedding` để đẩy vector vào Qdrant cùng với `node_id` (Id từ Memgraph) và `qualified_name` trong phần payload.

## 4. Quy Trình Truy Vấn (Tìm kiếm ngữ nghĩa)

Khi người dùng thực hiện tìm kiếm ngữ nghĩa (Semantic Search), quy trình diễn ra tại `codebase_rag/tools/semantic_search.py`:

1. **Query Embedding**: Chuyển câu hỏi/truy vấn của người dùng thành vector.
2. **Vector Search (Qdrant)**: Tìm các vector tương đồng nhất trong Qdrant. Kết quả trả về là một danh sách các `node_id` kèm theo điểm số tương đồng (score).
3. **Graph Enrichment (Memgraph)**: Sử dụng các `node_id` tìm được để truy vấn ngược lại Memgraph, lấy thông tin chi tiết về node đó (loại node, tên đầy đủ, quan hệ...).
4. **Kết quả**: Hệ thống trả về danh sách các thành phần code liên quan nhất đến ý nghĩa của câu truy vấn.

## 5. Ưu Điểm & Lưu Ý

- **Tính linh hoạt**: Qdrant là một dependency tùy chọn. Nếu không cài đặt `qdrant-client`, hệ thống vẫn hoạt động nhưng các tính năng liên quan đến Semantic Search sẽ bị vô hiệu hóa.
- **Tốc độ**: Chế độ embedded giúp loại bỏ overhead của các truy vấn HTTP qua mạng giữa application và database server.
- **Quản lý dữ liệu**: Vì dữ liệu nằm trong thư mục ẩn `.qdrant_code_embeddings` ngay tại repo repo đích, việc xóa database vector chỉ đơn giản là xóa thư mục này.

---
*Tài liệu này được tạo tự động bởi hệ thống phân tích mã nguồn.*
