# L3B Architecture Record

## 1. Tổng quan hệ thống

Hệ thống xử lý một khiếu nại thương mại điện tử theo mô hình A2A có điều phối
trung tâm. `Coordinator` chỉ giao việc và kiểm soát vòng đời; các specialist
agent chỉ thu thập hoặc diễn giải evidence thuộc miền được phân quyền. Mọi MCP
call luôn mang `case_id` của case hiện tại.

```text
Input case
    |
    v
Coordinator / Router
    |
    +--> Order/Item Agent -----------+
    |                                 |
    +--> Shipment Agent -------------+--> Policy Agent --> Verifier --> Output
    |                                 |                         |
    +--> Payment Agent --------------+                         +--> Trace
              (MCP evidence collector)
```

Điểm vào là `solve_case(case, gateway, trace)`. Hàm trả về đúng một JSON theo
`l3b-output-v2.schema.json`; không thêm field ngoài public contract.

## 2. Shared state và nguyên tắc bất biến

`ComplaintState` là bộ nhớ dùng chung trong `src/student_agent/state.py`.
Worker không được sửa evidence đã có. Hai collection sau dùng reducer
`operator.add` để chỉ được bổ sung:

| Field | Ý nghĩa |
| --- | --- |
| `evidence` | MCP evidence envelope nguyên vẹn, gồm `evidence_ref` do server cấp |
| `errors` | Lỗi observable của worker, không chứa prompt hoặc chain-of-thought |

State còn giữ `case_id`, danh sách tool đã discovery, `current_worker`, số vòng
`iteration_count`, order đã resolve/reject và dữ liệu phân tích theo miền. Mỗi
case có state riêng, do đó evidence không bị dùng chéo case.

## 3. Ownership, handoff và quyền tool

| Agent | Trách nhiệm | Input | Tool được phép | Handoff |
| --- | --- | --- | --- | --- |
| Coordinator / Router | Khởi tạo state, giới hạn vòng lặp, phân việc | `case` | Không gọi evidence tool | Order/Item hoặc Verifier |
| Order/Item Agent | Resolve order và item scope | `order_id`, candidates | Chỉ tool order đã được MCP công bố | Shipment, Payment, Policy |
| Shipment Agent | Thu thập vận đơn/timeline | Resolved order | Chỉ tool shipment đã được MCP công bố | Policy / Verifier |
| Payment Agent | Thu thập capture, refund, payment reference | Resolved order | Chỉ tool payment/refund đã được MCP công bố | Policy / Verifier |
| Policy Agent | Thu thập quy chế liên quan | Resolved order + evidence hiện có | Chỉ tool policy đã được MCP công bố | Verifier |
| Verifier Agent | Kiểm tra invariant và tạo output | Shared state | Không gọi MCP | END OUTPUT |

Tool discovery không đồng nghĩa với toàn quyền. Workflow chọn tool từ danh sách
MCP đã công bố; không probe hay gọi tên tool không tồn tại. Một tool được chọn
chỉ gọi tối đa một lần cho một case, trừ retry có kiểm soát.

## 4. A2A protocol

Mỗi message/handoff được tương quan bằng `case_id`. Trace chỉ ghi sự kiện quan
sát được, theo thứ tự sau:

```text
case_received
  -> task_assigned (Coordinator -> Order/Item)
  -> tool_result_consumed (nếu MCP trả evidence)
  -> handoff (Order/Item -> specialist team)
  -> task_assigned / tool_result_consumed cho từng specialist
  -> verification_completed
  -> case_finalized
```

Order/Item Agent ưu tiên `order_id` có sẵn trong input. Nếu chỉ có candidates,
agent lấy candidate phù hợp từ MCP, lưu candidate khác vào
`rejected_candidates`. Evidence MCP xác nhận order cho confidence 0.90; ID chỉ
có từ input được coi là chưa xác thực với confidence 0.50. Không resolve được
order thì không gọi specialist và handoff thẳng sang Verifier.

## 5. Evidence lifecycle và conflict policy

1. `EvidenceGateway` gọi MCP với đúng `case_id`.
2. Response được validate bằng `mcp-evidence-response-v1.schema.json`.
3. Worker lưu nguyên evidence envelope và lấy đúng `evidence_ref` server cấp.
4. Trace phát `tool_result_consumed` với tool name và `evidence_refs` tương ứng.
5. Verifier chỉ đưa các evidence ref đã thu thập của case vào output.

Không tự tạo, chỉnh sửa hoặc tái sử dụng `evidence_ref`. Nếu evidence giữa các
nguồn mâu thuẫn hoặc thiếu miền bắt buộc, agent không suy diễn dữ liệu còn thiếu:
Verifier giữ verdict `insufficient_evidence`, status `needs_investigation`, hoàn
tiền đề xuất bằng 0 và đưa case vào manual review.

## 6. Failure, retry và efficiency

| Sự cố | Retry budget | Xử lý fallback | Kết quả |
| --- | ---: | --- | --- |
| Timeout/lỗi mạng MCP | 1 retry, tối đa 2 call | Ghi lỗi; dừng mở rộng specialist | Manual investigation |
| Tool không được discovery | 0 | Không gọi tool | Insufficient evidence |
| Không resolve được order | 0 | Bỏ qua specialist | Manual investigation |
| MCP response invalid | 1 retry, tối đa 2 call | Ghi lỗi; dừng worker | Manual investigation |
| Vượt `MAX_ITERATIONS = 5` | 0 | Handoff sang Verifier | Safe fallback output |

Chỉ retry evidence read có tính idempotent. Không retry vô hạn, không quét rộng
tool, và không gọi lại evidence đã có. Các giới hạn này vừa ngăn loop vừa bảo vệ
điểm efficiency của cuộc thi.

## 7. Verification invariants

Trước khi finalize, Verifier phải bảo đảm:

- `case_id` output đúng input case;
- không có field ngoài `l3b-output-v2.schema.json`;
- mọi `evidence_refs` có mặt trong evidence state của chính case;
- financial totals không âm và refund không được bịa khi thiếu payment evidence;
- `resolved_order_ids`, affected entities và entity-resolution nhất quán;
- confidence nằm trong đoạn `[0, 1]`;
- case thiếu evidence đi theo `needs_investigation`, không khẳng định trách nhiệm
  hoặc hoàn tiền.

## 8. Reproducibility và vận hành

Yêu cầu Python 3.11+ cùng dependency pin trong `pyproject.toml`. Chạy kiểm tra
local bằng:

```powershell
.\.venv\Scripts\python.exe -m ruff check src tests
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m student_agent.cli mcp-tools
.\.venv\Scripts\python.exe -m student_agent.cli run
.\.venv\Scripts\python.exe -m student_agent.cli validate
```

`.env`, Team API key, prompt nội bộ và chain-of-thought không được ghi vào trace,
output hoặc submission package.
