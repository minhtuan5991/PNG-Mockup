# Phát hành PNG Mockup

Chỉ phát hành khi chủ dự án đã duyệt repository và phạm vi công khai. Không
sử dụng repository của ứng dụng gốc. Không ký số nếu chưa được cho phép riêng.

## Chuẩn bị

1. Dùng repository riêng cho `png-mockup`. Updater cài trên máy khách không chứa
   token; kênh GitHub Releases hiện tại yêu cầu repository công khai. Source
   riêng tư với kênh phân phối riêng cần được thiết kế và duyệt riêng.
2. Điền `App Source/update-channel.json` với đúng `repository`, `product_id`
   bằng `png-mockup`, `schema_version` bằng `1`, `enabled` bằng `true`.
3. Tăng phiên bản ổn định trong `App Source/version.json`; thêm ghi chú tại
   `releases/vX.Y.Z.md`. Không thay file của một phiên bản đã phát hành.
4. Chạy kiểm thử, `Build Setup.bat`, rồi chạy lại test installer trên EXE mới.
   Cấu hình người dùng/ảnh/log không được đưa vào Git hoặc release.
5. Commit và push đúng mã nguồn vào repository đã duyệt, không force push.

## Tạo và xác minh assets

Chạy bằng Python riêng của dự án:

```powershell
& '.\App Source\tools\python.bat' 'tools\prepare_release.py' --repository OWNER/REPO
```

Lệnh chỉ tạo thư mục `build/release-v...`, không upload. Assets gồm:

- `PNG-Mockup-Setup.exe`: bản sao byte nguyên vẹn của bộ cài đã xác minh.
- `PNG-Mockup-Setup.exe.sha256`: mã băm của bộ cài.
- `png-mockup-update.json`: định danh, repo, phiên bản/tag, kích thước, SHA-256.

Tên asset dùng dấu gạch nối để ổn định trên GitHub; tên bộ cài bàn giao nội bộ
vẫn là `PNG Mockup Setup.exe`.

## GitHub

Đăng nhập Git Credential Manager trên máy phát triển. Công cụ đọc credential
trong bộ nhớ, không ghi token ra đĩa, log hoặc bộ cài. Nó không tự mở đăng nhập.

```powershell
& '.\App Source\tools\python.bat' 'tools\github_release.py' --account ACCOUNT --repository OWNER/REPO status
& '.\App Source\tools\python.bat' 'tools\github_release.py' --account ACCOUNT --repository OWNER/REPO create --visibility public
& '.\App Source\tools\python.bat' 'tools\github_release.py' --account ACCOUNT --repository OWNER/REPO release --prepared 'build\release-vX.Y.Z-ID' --commit COMMIT_SHA --publish
```

Lệnh `create` và `release --publish` là hành động phát hành, chỉ chạy sau phê
duyệt. Bỏ `--publish` để giữ release ở trạng thái draft. Công cụ upload rồi đọc
lại từng asset và đối chiếu SHA-256 trước khi chuyển draft thành bản chính thức.
Nếu thất bại, draft được giữ nguyên để kiểm tra; không tự xóa/ghi đè release cũ.

Sau khi phát hành, kiểm tra latest release không đăng nhập, tải lại installer,
đối chiếu hash và thử backend updater từ bản thấp hơn trong thư mục kiểm thử.
Không thử nâng cấp bằng cách đổi phiên bản/cấu hình ở thư mục đang làm việc.

## Tài liệu API

Triển khai dựa trên GitHub REST API chính thức:
[Releases](https://docs.github.com/en/rest/releases/releases),
[Release assets](https://docs.github.com/en/rest/releases/assets),
[Repositories](https://docs.github.com/en/rest/repos/repos).
