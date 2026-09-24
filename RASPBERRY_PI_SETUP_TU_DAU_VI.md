# Cài đặt và sử dụng Img2Speech trên Raspberry Pi mới từ đầu

Tài liệu này đưa một Raspberry Pi mới từ bước ghi hệ điều hành đến khi thiết bị
tự khởi động Img2Speech và vận hành bằng ba nút. Các lệnh giả định repository
nằm tại `~/Img2Speech`; không giả định tên tài khoản là `pi`.

Sau khi cài xong, có thể dùng bản rút gọn tại
[RASPBERRY_PI_VAN_HANH_VI.md](RASPBERRY_PI_VAN_HANH_VI.md). Thông tin kỹ thuật
chi tiết hơn nằm trong
[RASPBERRY_PI_DEPLOY_VI.md](RASPBERRY_PI_DEPLOY_VI.md).

## 1. Chuẩn bị

Phần cứng tối thiểu:

- Raspberry Pi có header GPIO 40 chân. Khuyến nghị Pi 3, Pi 4, Pi 5 hoặc
  Zero 2 W và **Raspberry Pi OS 64-bit**.
- Thẻ microSD, nguồn đúng chuẩn và kết nối Internet ổn định.
- Raspberry Pi Camera hoặc camera khác có lệnh xuất JPEG.
- Microphone và loa tương thích ALSA.
- Ba nút nhấn thường mở, dây nối và một chân GND chung.
- OpenAI API key của project có quyền dùng các model mặc định, đặc biệt
  `gpt-5.6-sol`.

Raspberry Pi OS 64-bit là lựa chọn an toàn nhất cho các Python wheel trong
workspace. Không khuyến nghị Pi Zero đời đầu/ARMv6 hoặc hệ điều hành 32-bit cho
bản cài mới.

Raspberry Pi 5 không có jack âm thanh 3,5 mm. Với model này, chuẩn bị loa qua
HDMI, USB hoặc audio HAT; microphone thường dùng USB hoặc audio HAT.

Ảnh trang sách, âm thanh câu hỏi và văn bản nhận dạng được gửi tới OpenAI và
có thể phát sinh chi phí. Không dùng tài liệu nhạy cảm nếu chưa được phép.

## 2. Ghi Raspberry Pi OS và khởi động lần đầu

1. Cài [Raspberry Pi Imager](https://www.raspberrypi.com/software/) trên máy
   tính và ghi bản Raspberry Pi OS 64-bit mới nhất vào thẻ microSD. Bản Lite
   đủ dùng nếu vận hành qua SSH; bản Desktop thuận tiện hơn khi kiểm tra camera.
2. Trong phần tùy chỉnh của Imager, đặt hostname, tạo user thường, cấu hình
   Wi-Fi, múi giờ và bật SSH nếu cần điều khiển từ xa.
3. Tắt nguồn Pi trước khi gắn camera CSI. Cắm đúng chiều cáp, lắp thẻ và bật
   nguồn.
4. Đăng nhập trực tiếp hoặc qua SSH, sau đó cập nhật hệ thống:

   ```bash
   sudo apt update
   sudo apt full-upgrade -y
   sudo reboot
   ```

Sau khi Pi khởi động lại, đăng nhập lại. Không dùng `rpi-update` cho một bản cài
thông thường và không bật chế độ camera legacy.

## 3. Đấu ba nút

Tắt nguồn trước khi đấu hoặc sửa dây:

```bash
sudo poweroff
```

Đấu từng nút giữa GPIO và GND:

| Chức năng | GPIO BCM | Chân vật lý | Đầu còn lại |
|---|---:|---:|---|
| Nút 1 — chụp/đọc tiếp/đổi trang | GPIO17 | Pin 11 | GND |
| Nút 2 — tóm tắt | GPIO27 | Pin 13 | GND |
| Nút 3 — hỏi/hoàn tất câu hỏi | GPIO22 | Pin 15 | GND |
| GND chung | GND | Pin 6 | Ba nút dùng chung |

```text
GPIO17 (pin 11) ──[ Nút 1 ]──┐
GPIO27 (pin 13) ──[ Nút 2 ]──┼── GND (pin 6)
GPIO22 (pin 15) ──[ Nút 3 ]──┘
```

Chương trình dùng pull-up nội và số chân **BCM**. Không nối nút với 3,3 V hoặc
5 V. Nếu dùng audio HAT, kiểm tra HAT không chiếm GPIO17, GPIO27 hoặc GPIO22.

## 4. Cài công cụ hệ thống

Bật lại Pi và cài các công cụ lõi cùng những gói cần để xây dựng GPIO backend:

```bash
sudo apt update
sudo apt install -y git curl ca-certificates alsa-utils file build-essential \
  swig liblgpio-dev
```

Kiểm tra camera trước vì Raspberry Pi OS thường đã cài sẵn công cụ này:

```bash
command -v rpicam-still
```

Chỉ khi lệnh trên không trả về đường dẫn, cài **một** trong hai lựa chọn phù
hợp với bản OS:

```bash
# Raspberry Pi OS Lite:
sudo apt install -y rpicam-apps-lite

# Hoặc Raspberry Pi OS Desktop:
sudo apt install -y rpicam-apps
```

Kiểm tra các lệnh cần dùng:

```bash
command -v rpicam-still
command -v aplay
command -v arecord
```

Từ Raspberry Pi OS Bookworm trở đi, lệnh camera có tiền tố `rpicam-*`; không
dùng tên cũ `libcamera-still` trong cấu hình mới.

## 5. Kiểm tra camera, loa và microphone độc lập

### Camera

```bash
rpicam-hello --list-cameras
rpicam-still --nopreview --timeout 1500 \
  --output /tmp/img2speech-camera.jpg
file /tmp/img2speech-camera.jpg
```

Kết quả cuối phải là ảnh JPEG. Nếu không thấy camera, tắt nguồn rồi kiểm tra lại
cáp CSI và đúng cổng camera.

### Loa và microphone

```bash
aplay -l
arecord -l
arecord -q -t wav -f S16_LE -r 16000 -c 1 -d 5 \
  /tmp/img2speech-mic.wav
aplay /tmp/img2speech-mic.wav
```

Nếu máy có nhiều sound card, ghi lại tên `CARD` và số `DEV` từ `aplay -l` và
`arecord -l`. Có thể chọn thiết bị bằng cú pháp
`plughw:CARD=<ten>,DEV=<so>`. Dùng `alsamixer` để chỉnh âm lượng.

Không chuyển sang bước chạy ứng dụng cho tới khi camera tạo JPEG, microphone
thu được WAV và loa phát lại được file thử.

## 6. Tải workspace và cài Python dependency

Cài `uv` bằng installer chính thức dưới user hiện tại, không dùng `sudo`:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Mở terminal mới. Nếu terminal hiện tại chưa nhận `uv`, nạp file môi trường mà
installer vừa tạo, rồi kiểm tra:

```bash
[ ! -f "$HOME/.local/bin/env" ] || source "$HOME/.local/bin/env"
command -v uv
uv --version
```

Clone workspace và đồng bộ đúng dependency từ `uv.lock`:

```bash
cd "$HOME"
git clone https://github.com/baokhanhle123/Img2Speech.git
cd "$HOME/Img2Speech"
uv sync --locked
```

Nếu bạn đã chép workspace bằng USB/SCP thay vì Git, chỉ cần `cd` tới đúng thư
mục rồi chạy `uv sync --locked`. Python yêu cầu phiên bản 3.10 trở lên; `uv` sẽ
quản lý môi trường `.venv` của dự án.

### GPIO backend

`gpiozero` cần một pin factory thực sự. Trên Pi 5, hỗ trợ qua `pip` cần
`gpiozero` từ `2.0.1.post1` và backend `lgpio`. Cài lớp tương thích phần cứng
vào môi trường dự án:

```bash
uv pip install 'gpiozero>=2.0.1.post1' lgpio
GPIOZERO_PIN_FACTORY=lgpio .venv/bin/python -c \
  "from gpiozero import Button; b=Button(17); print('GPIO OK'); b.close()"
```

`lgpio` cần quyền với `/dev/gpiochip*`. Hai package trên hiện là lớp tương thích
Pi nằm ngoài phiên bản được khóa trong `uv.lock`; `uv sync --locked` về sau có
thể đặt lại môi trường. Vì vậy, sau mỗi lần sync, hãy chạy lại lệnh
`uv pip install 'gpiozero>=2.0.1.post1' lgpio`.

Sau khi cài lớp tương thích này, các mục dưới chạy trực tiếp
`.venv/bin/python` để `uv run` không tự đồng bộ lại và gỡ package bổ sung.

### Phương án pip nếu không thể dùng uv

Đây là phương án thay thế độc lập; không trộn hai cách cài trong cùng một môi
trường. Repository có sẵn `requirements.txt`:

```bash
sudo apt install -y python3-full python3-venv
cd "$HOME/Img2Speech"
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m pip install 'gpiozero>=2.0.1.post1' lgpio
```

Các mục dưới đã dùng trực tiếp `.venv/bin/python`, nên cùng một lệnh chạy được
cho cả hai phương án. Không dùng `sudo pip`.

## 7. Cấu hình OpenAI API key

Tạo `.env` cạnh `device_reader.py` mà không sửa hoặc chia sẻ file mẫu:

```bash
cd "$HOME/Img2Speech"
install -m 600 .env.example .env
nano .env
```

Nội dung cần có:

```dotenv
OPENAI_API_KEY=sk-...
```

Không dùng `cat .env`, không đưa khóa vào log, Git hoặc tin nhắn. Model mặc
định hiện tại:

- OCR: `gpt-5.6-sol`, ảnh `detail: "high"`, reasoning `max`;
- đọc nội dung và thông báo: `gpt-4o-mini-tts`, giọng `marin`;
- tóm tắt/hỏi đáp: `gpt-4o-mini`;
- nhận dạng câu hỏi: `gpt-4o-mini-transcribe`.

Ứng dụng không tự fallback nếu project không có quyền dùng model OCR. Có thể
chủ động thêm `--ocr-model <model-id>` vào lệnh chạy, nhưng model đó phải hỗ trợ
ảnh. Cấu hình chất lượng cao mặc định có thể tăng chi phí và thời gian chờ.

## 8. Kiểm tra phần mềm offline

Chạy test trước khi gọi camera hoặc API:

```bash
cd "$HOME/Img2Speech"
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python device_reader.py --help
```

Các test được mock và không gọi OpenAI hoặc yêu cầu phần cứng. Nếu test model
không pass, dừng tại đây và xử lý lỗi trước khi thử phần cứng hoặc API thật.

## 9. Chạy thủ công lần đầu

Bảo đảm chưa có service Img2Speech khác chạy, sau đó dùng:

```bash
cd "$HOME/Img2Speech"
GPIOZERO_PIN_FACTORY=lgpio .venv/bin/python device_reader.py \
  --capture-command 'rpicam-still --nopreview --timeout 1500 --output {output}' \
  --record-command 'arecord -q -t wav -f S16_LE -r 16000 -c 1 {output}'
```

Khi thấy `Three-button reader ready. Press Ctrl+C to stop.`, thử theo thứ tự:

1. Đặt trang trước camera, nhấn ngắn Nút 1 và chờ “Đang chụp ảnh”, “Đang xử lý
   ảnh”, “Bắt đầu đọc”. Ảnh được OCR và đọc tự động; không cần bấm lần nữa.
2. Đang đọc, nhấn Nút 2 để nghe tóm tắt. Sau khi tóm tắt xong, nhấn ngắn Nút 1
   để đọc tiếp từ đầu câu bị ngắt.
3. Nhấn Nút 3, chờ một beep rồi mới nói. Nhấn Nút 3 lần hai; hai beep xác nhận
   đã dừng ghi, sau đó hệ thống trả lời. Câu hỏi tự dừng sau 60 giây nếu không
   bấm lần hai.
4. Đặt trang mới trước camera rồi giữ Nút 1 khoảng 1,5 giây. Trang cũ bị xóa,
   ảnh mới được chụp và đọc tự động; thao tác này không thể hoàn tác.

Nhấn `Ctrl+C` để dừng bản chạy thủ công. Nếu phải chọn ALSA card cụ thể, chạy:

```bash
GPIOZERO_PIN_FACTORY=lgpio .venv/bin/python device_reader.py \
  --capture-command 'rpicam-still --nopreview --timeout 1500 --output {output}' \
  --record-command 'arecord -q -D plughw:CARD=Device,DEV=0 -t wav -f S16_LE -r 16000 -c 1 {output}' \
  --player 'aplay -q -D plughw:CARD=Device,DEV=0 -f S16_LE -r 24000 -c 1 -t raw -'
```

## 10. Tạo service tự chạy khi bật Pi

Chỉ cấu hình service sau khi lệnh thủ công chạy ổn. Lấy các giá trị thật:

```bash
cd "$HOME/Img2Speech"
whoami
pwd
```

Trong ví dụ dưới đây, thay `<USER>` và `<PROJECT_DIR>` bằng đúng kết quả trên.
Tạo service:

```bash
sudo nano /etc/systemd/system/img2speech.service
```

Nội dung:

```ini
[Unit]
Description=Img2Speech three-button reader
After=network-online.target sound.target
Wants=network-online.target

[Service]
Type=simple
User=<USER>
SupplementaryGroups=audio video gpio
WorkingDirectory=<PROJECT_DIR>
Environment=PYTHONUNBUFFERED=1
Environment=GPIOZERO_PIN_FACTORY=lgpio
ExecStart=<PROJECT_DIR>/.venv/bin/python <PROJECT_DIR>/device_reader.py --button1-pin 17 --button2-pin 27 --button3-pin 22 --capture-command "rpicam-still --nopreview --timeout 1500 --output {output}" --record-command "arecord -q -t wav -f S16_LE -r 16000 -c 1 {output}"
Restart=on-failure
RestartSec=3

[Install]
WantedBy=multi-user.target
```

Không để nguyên dấu `<...>` và không chạy service bằng root. Cấp group phần
cứng cho user rồi kích hoạt service:

```bash
sudo usermod -aG audio,video,gpio <USER>
sudo systemctl daemon-reload
sudo systemctl enable --now img2speech.service
systemctl status img2speech.service --no-pager
```

Sau khi thêm group, nếu service báo thiếu quyền, reboot Pi một lần:

```bash
sudo reboot
```

Xem log:

```bash
journalctl -u img2speech.service -n 100 --no-pager
journalctl -u img2speech.service -f
```

`Ctrl+C` chỉ thoát màn hình log. Không chạy lệnh thủ công cùng lúc với service
vì hai tiến trình sẽ tranh GPIO, camera, microphone và loa.

## 11. Vận hành hằng ngày

Kiểm tra service:

```bash
systemctl status img2speech.service --no-pager
```

| Thao tác | Kết quả |
|---|---|
| Nhấn ngắn Nút 1 khi chưa có trang/đang đọc/đã đọc xong | Chụp trang mới và tự đọc |
| Nhấn ngắn Nút 1 sau tóm tắt hoặc trả lời | Đọc tiếp từ đầu câu bị ngắt |
| Giữ Nút 1 khoảng 1,5 giây | Luôn bỏ trang hiện tại, chụp và đọc trang mới |
| Nhấn Nút 2 | Dừng âm thanh và tóm tắt phần đã nghe |
| Nhấn Nút 3, chờ một beep rồi nói | Bắt đầu ghi câu hỏi |
| Nhấn Nút 3 lần hai | Dừng ghi; hai beep rồi xử lý và trả lời |

Ba beep thấp báo lỗi. Lần đầu phát một câu thông báo có thể chậm vì phải tạo
TTS; các lần sau dùng cache RAM. Tránh bấm liên tục khi thiết bị đang thông báo
hoặc xử lý.

## 12. Cập nhật workspace

Trước khi cập nhật, dừng service:

```bash
sudo systemctl stop img2speech.service
cd "$HOME/Img2Speech"
git status --short
git pull --ff-only
# Nếu cài bằng uv:
uv sync --locked
uv pip install 'gpiozero>=2.0.1.post1' lgpio
.venv/bin/python -m unittest discover -s tests -v
sudo systemctl start img2speech.service
systemctl status img2speech.service --no-pager
```

Nếu đã chọn phương án pip ở mục 6, thay hai lệnh `uv` bằng:

```bash
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m pip install 'gpiozero>=2.0.1.post1' lgpio
```

Nếu `git status --short` có thay đổi cục bộ, không chạy `git pull` cho tới khi
đã sao lưu hoặc xử lý các thay đổi đó. Không ghi đè `.env`.

## 13. Xử lý lỗi nhanh

### Service không chạy

```bash
systemctl status img2speech.service --no-pager
journalctl -u img2speech.service -n 100 --no-pager
```

- `203/EXEC`: kiểm tra đường dẫn tuyệt đối tới `.venv/bin/python` và
  `device_reader.py` trong `ExecStart`.
- `No such file or directory`: kiểm tra `WorkingDirectory` và đã tạo `.venv`.
- Lỗi quyền: kiểm tra group bằng `groups` và quyền `/dev/gpiochip*`.

### Nút không phản hồi

- Xác nhận nối GPIO17/27/22 xuống GND theo số BCM.
- Kiểm tra `lgpio`:

  ```bash
  cd "$HOME/Img2Speech"
  .venv/bin/python -c "import lgpio; print('lgpio OK')"
  ```

- Không dùng `sudo` để chạy ứng dụng; sửa quyền/group thay vì chạy bằng root.

### Camera, loa hoặc microphone lỗi

Chạy lại từng bài kiểm tra độc lập ở mục 5. Camera của chương trình phải xuất
JPEG; microphone phải xuất WAV `RIFF/WAVE`; player phải nhận raw PCM 24 kHz,
mono, signed 16-bit.

### Ba beep thấp hoặc lỗi model

- Kiểm tra Internet và quyền truy cập `gpt-5.6-sol` của OpenAI project.
- Kiểm tra `.env` tồn tại và không rỗng mà không in khóa:

  ```bash
  test -s "$HOME/Img2Speech/.env" && echo '.env tồn tại và không rỗng'
  ```

- Xem `journalctl`; ứng dụng không tự chuyển sang model khác.

## 14. Dừng và tắt an toàn

Dừng ứng dụng rồi tắt hệ điều hành:

```bash
sudo systemctl stop img2speech.service
sudo poweroff
```

Chờ Pi tắt hoàn toàn rồi mới ngắt nguồn. Khởi động lại, mất điện hoặc restart
service sẽ làm mất trang, tiến độ đọc và ngữ cảnh hỏi đáp đang giữ trong RAM.

## Tài liệu chính thức tham khảo

- [Cài Raspberry Pi OS](https://www.raspberrypi.com/documentation/computers/getting-started.html)
- [Raspberry Pi OS và cập nhật hệ thống](https://www.raspberrypi.com/documentation/computers/os.html)
- [Phần mềm camera Raspberry Pi](https://www.raspberrypi.com/documentation/computers/camera_software.html)
- [Cài đặt uv](https://docs.astral.sh/uv/getting-started/installation/)
- [GPIO Zero pin factories](https://gpiozero.readthedocs.io/en/stable/api_pins.html)
- [OpenAI GPT-5.6 Sol](https://developers.openai.com/api/docs/models/gpt-5.6-sol)
