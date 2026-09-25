# L3B Architecture Record

## 1. Tổng quan

Hệ thống điều tra từng khiếu nại bằng `solve_case(case, gateway, trace)`. Workflow dùng các quy tắc trong Python, không gọi mô hình ngôn ngữ. Dữ liệu nghiệp vụ được lấy qua MCP; kết quả cuối cùng là JSON theo schema L3B và trace ghi lại các bước xử lý.

```text
Case input
  → Coordinator
  → Policy agent
  → Entity agent
  → Customer agent + Order agent
  → Conflict resolver
  → Shipment agent + Payment agent
  → Classifier + Policy decision
  → Verifier
  → Output JSON + trace
```

Mỗi lần gọi `solve_case` tạo một `CaseContext` riêng để giữ dữ liệu, bộ nhớ đệm và các `evidence_ref` của đúng case đó.

## 2. Vai trò các agent

| Vai trò | Công việc |
| --- | --- |
| Coordinator | Điều phối các bước và ghi sự kiện giao việc, bàn giao trong trace. |
| Policy agent | Lấy chính sách theo `policy_version`; dùng quy tắc tương ứng để xác định hành động và khoản hoàn tiền. |
| Entity agent | Kiểm tra order ID được khai báo và các candidate; gọi `get_order` với ID hợp lệ. |
| Customer agent | Lấy lịch sử khách hàng khi case có `customer_unique_id_hint`. |
| Order agent | Lấy các item và seller của order đã xác định. |
| Conflict resolver | So sánh bản ghi order và customer history, chọn dòng thời gian phục vụ case và ghi nhận khác biệt giữa hai nguồn. |
| Shipment agent | Phân tích thời điểm giao hàng và sự kiện vận chuyển thuộc dòng thời gian đã chọn. |
| Payment agent | Phân tích sự kiện thanh toán; gọi thêm refund timeline khi cần. |
| Verifier | Kiểm tra sự nhất quán của kết quả trước khi trả output. |

Các vai trò trên là các hàm trong cùng workflow. Việc giao việc và bàn giao được thể hiện bằng các sự kiện `task_assigned` và `handoff` trong trace; hệ thống không triển khai một dịch vụ A2A riêng.

## 3. Luồng xử lý một case

Policy được tải trước bằng `get_policy`. Entity agent ưu tiên `claimed_order_id`, sau đó xét các candidate theo thứ tự đầu vào. ID không đúng định dạng bị loại mà không gọi MCP. Nếu không xác định được order, workflow tạo kết quả `insufficient_evidence` và `needs_investigation`.

Khi có order, workflow lấy customer history và order items. Conflict resolver so sánh bản ghi từ `get_order` với bản ghi cùng order ID trong customer history. Khi chọn một dòng thời gian, các trường khác nhau được ghi trong `data_conflicts` cùng nguồn được chọn. Sau đó shipment và payment agent chỉ xét các sự kiện phù hợp với dòng thời gian này.

Workflow phân loại vấn đề từ dữ liệu shipment và payment. Chính sách cung cấp trạng thái case, hành động đề xuất và mức hoàn tiền khi có quy tắc tương ứng. Verifier kiểm tra các quan hệ như tổng refund lines bằng mức hoàn tiền đề xuất và `no_action` không đi cùng khoản hoàn tiền dương.

## 4. Evidence và trace

Mọi lần gọi MCP đều truyền `case_id` hiện tại. `CaseContext` chỉ dùng `evidence_ref` do MCP trả về; không tự tạo hoặc sửa reference. Tool result hợp lệ được ghi bằng sự kiện `tool_result_consumed`. Output chọn các evidence liên quan đến vấn đề đã phân loại.

Các lời gọi trùng tool và tham số được lưu trong cache của **cùng case** để tránh gọi lại. Lỗi tool được ghi nhận nhưng không tạo evidence giả. Lỗi kết nối được báo lên để quá trình chạy có thể xử lý, thay vì tự kết luận khi chưa lấy được dữ liệu.

## 5. Trường hợp thiếu dữ liệu và kiểm tra

Khi không tìm được order hoặc solver không thể hoàn thành, workflow trả kết quả dự phòng với `insufficient_evidence`, `needs_investigation`, các tổng tiền chưa biết là `null` và không tạo refund line. Trace vẫn ghi sự kiện `verification_completed`.

Verifier kiểm tra tính nhất quán của issue, bên chịu trách nhiệm, hành động và số tiền. Nếu phát hiện vấn đề, workflow giảm confidence và ghi các cảnh báo vào trace. CLI tiếp tục kiểm tra output theo JSON schema.

## 6. Chạy và nộp

Yêu cầu Python 3.11 trở lên. Các lệnh sử dụng:

```powershell
day09 validate-inputs
day09 mcp-tools
day09 run
day09 validate
day09 package --output dist/submission.zip
```

ZIP nộp bài chứa `manifest.json`, `trace.jsonl` và các JSON trong `outputs/`. Không đưa `.env`, API key, input, mã nguồn hoặc thư mục `debug/` vào ZIP.