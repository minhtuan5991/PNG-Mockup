# PNG Mockup

Ứng dụng Windows độc lập để ghép thiết kế PNG vào mockup PSD/PSB bằng Photoshop.
Mã nguồn, bộ chạy, cấu hình và dữ liệu của dự án này không dùng chung với app gốc.

## Bản chạy và bộ cài

Tải bộ cài chính thức tại [GitHub Releases](https://github.com/minhtuan5991/PNG-Mockup/releases/latest).

- `PNG Mockup Setup.exe`: bộ cài offline để chuyển sang máy khác.
- `App Source/PNG Mockup.exe`: mở bản chạy trong dự án.
- `App Source/README.md`: hướng dẫn sử dụng đi kèm bộ cài.
- `verification/`: kết quả kiểm thử và đối chiếu bản đóng gói.

Bộ cài kèm Python/Tk, Pillow và oxipng riêng. Không cần cài sẵn Python, app cũ,
trình duyệt hoặc các model AI. Máy đích vẫn cần Adobe Photoshop có Automation/COM
hoạt động. Không có bản quyền hoặc bộ cài Photoshop đi kèm.

## Cấu trúc phát triển

`App Source` là nguồn chính. `installer` và `build_setup.ps1` tạo bộ cài. `tests`
chứa kiểm thử PNG/PSD, xử lý ảnh, giao diện và đóng gói. Không dùng script đồng bộ,
runtime, cấu hình hoặc kho cập nhật của dự án cũ.

Khi thử bộ cài trên máy phát triển, chọn thư mục cha khác với checkout hiện tại.
Bộ cài không cho cài đè vào thư mục mã nguồn hoặc `App Source`.

Chỉ có một phong cách giao diện và năm bảng màu. Từ 1.1.0, bộ cài tạo thư mục
`PNG Mockup` dưới ổ đĩa/thư mục cha đã chọn, ẩn file kỹ thuật và kèm công cụ
hiện/ẩn cùng Uninstall giữ dữ liệu.

Updater kiểm tra kênh `minhtuan5991/PNG-Mockup` mỗi lần mở app đã cài và tải nền,
không ngắt job. Bản chạy từ checkout mã nguồn không tự cập nhật đè mã nguồn.
Cài cập nhật cần người dùng chọn khi app rảnh, rồi chờ phiên hiện tại đóng.
Kênh chưa được cấu hình sẽ không kết nối mạng; không dùng kho của ứng dụng gốc.

## Mã nguồn và phát hành

Cấu hình cá nhân, ảnh, log, runtime và EXE sinh ra không nằm trong Git. Bản cài
đóng gói cấu hình mới từ `payload-template/config.json`, không lấy cài đặt cá nhân
trong `App Source/config.json`.

Để chạy một checkout mới, sao chép template thành `App Source/config.json`,
chuẩn bị runtime CPython 3.12 64-bit có Tk/Pillow bằng `tools/prepare_runtime.ps1`,
sau đó chạy `Build Setup.bat`. Runtime chỉ được đưa vào release asset.

Xem [quy trình phát hành](docs/RELEASING.md). Không lưu token GitHub vào nguồn,
runtime hoặc bộ cài; công cụ phát hành dùng Git Credential Manager của máy phát triển.
