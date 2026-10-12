# VietinBank Vietnamese ASR Recording Web Application 🎙️

Ứng dụng web thu thập dữ liệu giọng nói tiếng Việt phục vụ nghiên cứu và đánh giá mô hình nhận dạng tiếng nói (ASR/STT) cho ngân hàng VietinBank.

Dự án được xây dựng tối giản, tối ưu cho thiết bị di động, bảo mật và lưu trữ trực tiếp vào **Hugging Face Storage Bucket** thông qua giao thức S3-compatible với cơ chế ghi nguyên tử có điều kiện (**Conditional CAS Writes**).

---

## 📌 Tổng quan tính năng

- **Dành cho Người thu âm (Public `/`)**:
  - Giao diện tiếng Việt thân thiện, rõ ràng, tối ưu cho trình duyệt di động (iOS Safari / Android Chrome) và máy tính.
  - Hoàn toàn ẩn danh: Không yêu cầu tài khoản, email, mật khẩu hay mã mời.
  - Phân bổ câu thông minh: Chọn giới tính (Nam / Nữ) để nhận câu đọc phù hợp với đại từ xưng hô; cân bằng tự động nhóm 250 Nam / 250 Nữ.
  - Hiển thị kịch bản lớn, tách biệt rõ ràng phần **Hướng dẫn thể hiện (không đọc phần này)**.
  - Thu âm trực tiếp qua microphone (16kHz), nghe lại trước khi chủ động gửi.
  - Cơ chế **Reload Recovery**: Người dùng bấm F5 (tải lại trang) vẫn giữ nguyên phiên làm việc và câu đang thu, không lo bị mất lượt hoặc giữ trùng câu.
  - Cơ chế **Đổi câu / Bỏ qua**: Giải phóng khóa câu an toàn để chuyển cho người khác.
  - Dữ liệu thu được tính là **Đã thu (Recorded)** ngay khi tải lên thành công, không phụ thuộc vào việc admin đã duyệt hay chưa.

- **Dành cho Quản trị viên (Admin `/admin`)**:
  - Được bảo vệ bằng một mật khẩu dùng chung (`ADMIN_PASSWORD`), so sánh an toàn constant-time (`hmac.compare_digest`) kèm cơ chế khóa tạm thời khi nhập sai nhiều lần.
  - Không lộ đường dẫn trên menu người đọc; truy cập trực tiếp qua URL `/admin`.
  - **Tab Dashboard (Tổng quan)**: Theo dõi tiến độ theo thời gian thực (Tổng số câu, Đã thu, Chưa thu, Đang giữ, Chưa kiểm tra, Đã kiểm tra); thống kê theo giới tính và kịch bản (disfluency scenarios); tìm kiếm và nghe lại bản ghi trực tiếp từ Storage Bucket.
  - **Tab Review (Kiểm duyệt QA - Tùy chọn)**: Hỗ trợ 2-3 admin cùng làm việc đồng thời với khóa độc quyền ngắn hạn (`review_lease`). Hỗ trợ 3 quyết định:
    1. **Đạt**: Duyệt bản ghi chỉ với 1 click.
    2. **Đạt, sửa text**: Cho phép ghi nhận văn bản phát âm thực tế (lặp từ, vấp, từ đệm) mà không làm thay đổi kịch bản gốc.
    3. **Không đạt / Thu lại**: Chọn lý do lỗi (ồn, nhỏ, cắt đầu/đuôi, sai số, v.v.), tự động mở lại câu về trạng thái Chưa thu (Available) để cộng tác viên thu lại.
  - **Quản lý đa Dataset ([+] Thêm dataset)**:
    - Tạo namespace mới trong cùng một bucket thông qua upload file `.jsonl`.
    - Kiểm tra tính hợp lệ dữ liệu toàn diện (báo chi tiết từng dòng lỗi).
    - Chuyển đổi linh hoạt dataset đang thu (`active_dataset`) chỉ với 1 nút bấm.

---

## 🧱 Cấu trúc thư mục

```text
├── app.py                         # Điểm khởi chạy Streamlit (định tuyến / và /admin)
├── manifest.jsonl                 # Tập kịch bản gốc 500 câu (numeric-data-v1)
├── schemas.py                     # Định nghĩa Pydantic models (Manifest, State, Lease, Audio, Review)
├── requirements.txt               # Thư viện phụ thuộc
├── .gitignore                     # Loại trừ secrets, cache và virtualenv
├── .streamlit/
│   ├── config.toml                # Cấu hình giao diện và server Streamlit
│   └── secrets.toml.example       # Mẫu cấu hình thông tin đăng nhập bảo mật
├── services/
│   ├── storage.py                 # Lớp trừu tượng Storage (HuggingFaceS3Storage & MockStorage)
│   ├── manifest.py                # Phân tích, xác thực và nạp kịch bản JSONL
│   ├── dataset_registry.py        # Quản lý datasets.json và bootstrap khởi tạo
│   ├── tasks.py                   # Quản lý vòng đời câu thu, CAS reservation, upload audio
│   ├── audio.py                   # Chuẩn hóa âm thanh 16kHz PCM16 Mono WAV
│   ├── review.py                  # Quy trình kiểm duyệt QA, review lease và các quyết định
│   └── session.py                 # Phục hồi phiên duyệt trình (Reload Recovery) & Admin auth
├── views/
│   ├── reader_view.py             # Giao diện tiếng Việt cho người thu âm
│   └── admin_view.py              # Giao diện Dashboard & Review cho quản trị viên
└── tests/                         # Bộ kiểm thử tự động pytest (32 bài test toàn diện)
    ├── test_manifest.py           # Kiểm tra fixture 500 câu và các quy tắc xác thực
    ├── test_storage.py            # Kiểm tra cơ chế CAS của Storage và mock
    ├── test_audio.py              # Kiểm tra chuẩn hóa âm thanh WAV
    ├── test_concurrency_and_leases.py # Kiểm tra tranh chấp đồng thời 20 readers
    ├── test_reload_recovery.py    # Kiểm tra F5 phục hồi phiên và nhiều tab
    ├── test_admin_and_review.py   # Kiểm tra mật khẩu, dashboard, 3 quyết định QA
    └── test_datasets.py           # Kiểm tra thêm dataset mới và vòng đời số liệu
```

---

## 🚀 Hướng dẫn Triển khai Chi tiết

### Bước 1: Tạo Hugging Face Storage Bucket riêng tư (Private Bucket)
1. Đăng nhập vào tài khoản [Hugging Face](https://huggingface.co/).
2. Truy cập mục **Settings** -> **Storage Buckets** (hoặc truy cập `https://huggingface.co/settings/buckets`).
3. Nhấn **Create new bucket**:
   - Tên Bucket: `vtb-recording-data`
   - Quyền riêng tư: Chọn **Private** (bắt buộc để đảm bảo dữ liệu không bị công khai).
   - Namespace: Chọn tài khoản cá nhân của bạn hoặc Organization của nhóm.

### Bước 2: Tạo Token và Lấy S3 Credentials
1. Truy cập **Settings** -> **Access Tokens** -> **Create new token**.
2. Chọn loại **Fine-grained Token**:
   - Đặt tên token (VD: `vtb-storage-token`).
   - Cấp quyền đọc/ghi (`Read and write access`) vào bucket `vtb-recording-data`.
3. Sau khi tạo token, nhấp vào menu hành động (icon 3 chấm hoặc nút dropdown cạnh token) -> Chọn **Create S3 Credentials** (hoặc xem hướng dẫn tại [HF Storage Buckets S3 API](https://huggingface.co/docs/hub/storage-buckets-s3)).
4. Lưu lại hai thông tin quan trọng:
   - **Access Key ID**
   - **Secret Access Key**

### Bước 3: Cấu hình Secrets cục bộ để kiểm tra
1. Tạo tệp `.streamlit/secrets.toml` từ mẫu:
   ```bash
   cp .streamlit/secrets.toml.example .streamlit/secrets.toml
   ```
2. Mở tệp `.streamlit/secrets.toml` và điền các giá trị:
   ```toml
   HF_NAMESPACE = "tên-username-hoặc-org-của-bạn"
   HF_BUCKET_NAME = "vtb-recording-data"
   HF_S3_ACCESS_KEY_ID = "điền-access-key-id-từ-bước-2"
   HF_S3_SECRET_ACCESS_KEY = "điền-secret-access-key-từ-bước-2"
   ADMIN_PASSWORD = "ilovevtb"
   MOCK_STORAGE = false
   ```

> **Lưu ý**: Nếu bạn chưa có tài khoản Hugging Face hoặc muốn chạy thử offline hoàn toàn, chỉ cần đặt `MOCK_STORAGE = true`. Ứng dụng sẽ tự động kích hoạt Mock Backend với đầy đủ ngữ nghĩa CAS nguyên tử!

### Bước 4: Chạy thử nghiệm và chạy bộ kiểm thử tự động
1. Cài đặt các thư viện phụ thuộc:
   ```bash
   pip install -r requirements.txt
   ```
2. Chạy toàn bộ 32 bài kiểm thử tự động:
   ```bash
   pytest -v
   ```
   *Tất cả bài test đều kiểm tra toàn diện các tình huống tranh chấp đồng thời, hết hạn lease, xác thực audio và quy trình QA.*

3. Khởi chạy ứng dụng Streamlit cục bộ:
   ```bash
   streamlit run app.py
   ```
4. Mở trình duyệt:
   - Trang người thu âm: `http://localhost:8501/`
   - Trang quản trị: `http://localhost:8501/admin` (nhập mật khẩu quản trị để đăng nhập).

---

## 🌐 Triển khai lên Streamlit Community Cloud

1. **Đẩy mã nguồn lên GitHub**:
   ```bash
   git init
   git add .
   git commit -m "feat: complete VietinBank ASR recording web app"
   git remote add origin https://github.com/<your-username>/<your-repo>.git
   git push -u origin main
   ```
   *(Tệp `.gitignore` đã bảo vệ để không bao giờ đẩy `.streamlit/secrets.toml` lên GitHub).*

2. **Tạo ứng dụng trên Streamlit Cloud**:
   - Truy cập [share.streamlit.io](https://share.streamlit.io/).
   - Nhấn **New app** -> Chọn Repository, Branch (`main`), và Main file path (`app.py`).
   - Nhấn **Advanced Settings** -> **Secrets**.
   - Dán toàn bộ nội dung cấu hình từ `.streamlit/secrets.toml`:
     ```toml
     HF_NAMESPACE = "..."
     HF_BUCKET_NAME = "vtb-recording-data"
     HF_S3_ACCESS_KEY_ID = "..."
     HF_S3_SECRET_ACCESS_KEY = "..."
     ADMIN_PASSWORD = "mật-khẩu-bảo-mật-của-bạn"
     MOCK_STORAGE = false
     ```
   - Nhấn **Deploy**.

3. **Lấy liên kết chính thức**:
   - Streamlit Cloud sẽ cung cấp liên kết công khai (VD: `https://vtb-recording.streamlit.app`).
   - Gửi liên kết này cho người tham gia thu âm.
   - Quản trị viên truy cập vào `https://vtb-recording.streamlit.app/admin` để quản lý.

---

## 📱 Kịch bản Kiểm thử Thực tế (Smoke Test Plan)

Sau khi triển khai lên Streamlit Cloud, thực hiện kiểm tra thực tế:

1. **Kiểm tra Tranh chấp Giữ Câu (2 trình duyệt / điện thoại)**:
   - Mở 2 cửa sổ trình duyệt (hoặc 2 điện thoại) cùng vào trang chủ.
   - Chọn cùng giới tính (VD: Nam).
   - Hệ thống tự động phân bổ 2 câu khác nhau, không bao giờ giao cùng 1 câu cho 2 người đọc đồng thời.
2. **Kiểm tra Phục hồi khi Reload (F5)**:
   - Đang ở câu đọc, nhấn F5 trên trình duyệt.
   - Trang tải lại và giữ nguyên câu đọc và thời hạn giữ câu (lease), không cấp câu mới hay làm mất trạng thái.
3. **Kiểm tra Gửi Bản Ghi & Tăng Số Lượng**:
   - Người thứ nhất đọc, bấm "Gửi bản ghi".
   - Hệ thống kiểm tra âm thanh, đẩy vào `audio/HNUM-xxxx/<uuid>.wav`, cập nhật trạng thái thành `recorded`.
   - Vào `/admin` -> Dashboard: Số lượng **Đã thu (Recorded)** tăng lên ngay lập tức.
4. **Kiểm tra Quy trình Kiểm duyệt & Thu lại**:
   - Trong `/admin` -> Tab Review -> Chọn câu vừa thu.
   - Nhấn nghe lại bản ghi.
   - Bấm "Không đạt / Thu lại", tích chọn lý do "Ồn" và xác nhận.
   - Quay lại Dashboard: Số lượng đã thu giảm đi 1, câu trở về trạng thái Chưa thu (Available).
   - Người đọc trên trang chủ tải câu tiếp theo sẽ có thể nhận lại câu vừa bị từ chối để thu lại chuẩn hơn.

---

## 📂 Hướng dẫn Thêm Dataset Mới (`.jsonl`) trong Tương lai

Khi cần thu âm đợt mới (VD: `numeric-data-v2`):
1. Đăng nhập vào `/admin`.
2. Nhấn nút **➕ Thêm dataset** ở thanh điều khiển trên cùng.
3. Điền:
   - **Mã định danh (slug)**: Chữ thường và gạch nối (VD: `numeric-data-v2`).
   - **Tên hiển thị**: Tên tiếng Việt rõ nghĩa (VD: `Tập kịch bản thẻ tín dụng đợt 2`).
   - **Tệp manifest (.jsonl)**: Tải lên tệp JSONL của bạn.
4. Nhấn **Tạo Dataset**:
   - Hệ thống tự động kiểm tra cú pháp, trường bắt buộc, trùng mã task, giới tính hợp lệ.
   - Nếu có lỗi, hệ thống chỉ rõ dòng lỗi cụ thể và dừng lại an toàn.
   - Nếu hợp lệ, dataset được tạo thành công bên trong bucket mà **không** cần khởi động lại ứng dụng hay can thiệp vào GitHub!
5. Khi sẵn sàng cho người đọc thu tập mới, chỉ cần chọn dataset trong danh sách và bấm **⭐ Đặt làm dataset đang thu**.

