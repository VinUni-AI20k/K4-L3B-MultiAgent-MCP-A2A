# Chạy bài L3B trên Windows

## 1. Môi trường Python

Mở PowerShell tại thư mục repo:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
```

Không cần activate, nên không cần đổi ExecutionPolicy của Windows.

## 2. Model Qwen3-4B

Cài Ollama từ https://ollama.com/download/windows rồi chạy:

```powershell
ollama pull qwen3:4b
.\.venv\Scripts\day09.exe check-model
```

Ứng dụng Ollama phải đang chạy nền. Nếu chưa có server, chạy `ollama serve` ở một terminal
riêng. Lệnh `check-model` gọi model thật và kiểm tra JSON; không cần Team API Key.

File `.env` dùng các biến sau (đã có mẫu trong `.env.example`):

```dotenv
LLM_BACKEND=ollama
LLM_BASE_URL=http://127.0.0.1:11434/v1
LLM_MODEL=qwen3:4b
LLM_MODEL_PARAMETERS_B=4.02
LLM_API_KEY=
LLM_TIMEOUT_SECONDS=180
LLM_CONTEXT_TOKENS=16384
```

Client dùng API native `/api/chat` của Ollama để tắt thinking và yêu cầu JSON schema.
Giá trị `/v1` trong base URL được tự bỏ khi dùng backend Ollama. Nếu máy phản hồi quá chậm,
tăng timeout (tối đa 900 giây). Model 8B trên GPU 4 GB có thể phải dùng thêm RAM/CPU.
Không chuyển sang model trên 10B. Biến số tham số là khai báo của người dùng, không thay
thế việc xác minh thông tin model từ nhà phát hành.

## 3. Thông tin cuộc thi

Đăng ký team tại Competition Workspace của lớp và điền `COMPETITION_TEAM_API_KEY` vào
`.env`. Giữ URL API và MCP theo hướng dẫn của lớp. Ollama không cung cấp Team API Key.

```powershell
.\.venv\Scripts\day09.exe mcp-tools
.\.venv\Scripts\day09.exe start-run
```

Nếu chưa có `.env`, copy từ `.env.example` trước. Không commit hoặc gửi key trong chat.
`mcp-tools` chỉ xác nhận discovery, không chứng minh đọc được dữ liệu. Workspace yêu cầu
một run L3B đang hoạt động; `start-run` gọi API giống trang workspace. Lệnh `run` tự làm
bước này. Khi chưa mở run, các tool có thể chỉ trả lỗi chung dù API key đúng.

## 4. Input và phiên bản

100 case đã được chuẩn bị trong `inputs/` cùng `case-set.json`, phiên bản
`l3b-competition-v1` lấy từ ZIP chính thức của release v1:
https://github.com/VinUni-AI20k/K4-L3B-MultiAgent-MCP-A2A/releases/tag/v1.
Các file runtime này được gitignore. Khi cài lại từ source, ưu tiên lấy `case-set.json`
từ bản phát hành của lớp và đặt đúng cấu trúc trong README.
Nếu chỉ có các file case, xác nhận `case_set_version` với lớp rồi dùng:

```powershell
.\.venv\Scripts\day09.exe prepare-inputs --source l3b-inputs-v1/inputs --version PHIEN_BAN_CHINH_THUC
.\.venv\Scripts\day09.exe validate-inputs
```

Thay `PHIEN_BAN_CHINH_THUC` bằng giá trị thật. Không đoán phiên bản từ tên thư mục.
Lệnh không ghi đè manifest/input đã tồn tại.

## 5. Kiểm thử, chạy và đóng gói

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m ruff check src tests
.\.venv\Scripts\day09.exe run
.\.venv\Scripts\day09.exe validate
.\.venv\Scripts\day09.exe package --output dist/submission.zip
```

Test dùng MCP/model giả lập để kiểm tra workflow, schema và các chốt kiểm tra.
Pass test không có nghĩa đã có bằng chứng MCP thật hoặc bảo đảm điểm thi.
`run` xử lý tuần tự 100 case, không tự ghi đè kết quả của lần chạy cũ. Nếu lỗi giữa chừng,
sao lưu output/trace trước khi chạy lại; không trộn evidence giữa các lượt chạy.

ZIP L3B chỉ có `manifest.json`, `trace.jsonl`, `outputs/*.json` theo contract của repo.
Không nén cả thư mục source. Chỉ nộp sau khi chạy bằng MCP thật và validate thành công.
Code không tự commit, push hoặc upload bài.

## Giới hạn cần biết

- Đã discovery 10 MCP tool và đọc được order/customer/items/payment/policy/product thật sau
  khi mở run. Một số tool vẫn có thể lỗi theo case (đã gặp refund timeline ở case 001).
  Chất lượng output vẫn cần kiểm tra khi chạy model cùng toàn bộ evidence.
- Tool không thuộc whitelist của role hoặc không theo tiền tố đọc sẽ bị chặn mặc định.
  Cần đối chiếu danh sách thật nếu workflow báo thiếu tool.
- Một verifier gọi model riêng đối chiếu dữ liệu gốc, nhưng dùng cùng Qwen3 nên vẫn có thể
  mắc lỗi tương quan. Các phép kiểm tra xác định bằng Python là lớp kiểm tra bổ sung.
- Không tạo đáp án hoặc evidence giả khi mất kết nối. Không có output thi trong bộ test.
