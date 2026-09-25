Phân chia công việc

Task 1: Router/Supervisor Agent (workflow.py)

- [ ] Phân tích case["customer_request"] để trích xuất:
  - order_id từ claimed_order_id hoặc candidate_order_ids
  - Loại khiếu nại (topic)
  - Claims (claim_id, topic)
- [ ] Quyết định thứ tự gọi Workers
- [ ] Tổng hợp kết quả từ các Workers
- [ ] Gọi gateway.call() đúng tool và collect evidence_ref

Task 2: Entity Resolution (trong workflow)

- [ ] Resolve đúng order từ candidates
- [ ] Confidence scoring cho entity resolution
- [ ] Trace: task_assigned, handoff

Task 3: Logistics Worker (trong workflow)

- [ ] Gọi get_order / get_order_items
- [ ] Đối soát ngày giao thực tế vs hẹn
- [ ] Xác định lỗi thuộc Shipper hay Seller
- [ ] Output: shipment_analysis verdict

Task 4: Financial Worker (trong workflow)

- [ ] Gọi get_payment, get_refund
- [ ] Deterministic calculation: số tiền hoàn
- [ ] Kiểm tra refund_pending, refund_failed
- [ ] Output: payment_analysis, financial_resolution

Task 5: Policy Worker (trong workflow)

- [ ] Gọi get_policy để tra cứu quy định
- [ ] Kiểm tra thời hiệu (7 ngày/30 ngày)
- [ ] Validate claims vs policy

Task 6: Conflict Resolution & Verifier

- [ ] Phát hiện conflict giữa sources
- [ ] Quyết định precedence (policy > payment > logistics)
- [ ] Output: data_conflicts, root_cause_analysis

Task 7: Output Builder

- [ ] Build đúng schema l3b-output-v2
- [ ] Populate all required fields
- [ ] Confidence calibration
- [ ] Trace: verification_completed, case_finalized