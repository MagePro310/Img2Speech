# Hướng dẫn triển khai Img2Speech trên Raspberry Pi

Tài liệu này hướng dẫn chạy `device_reader.py` với ba nút vật lý, Raspberry Pi
Camera, microphone và loa. Ví dụ sử dụng Raspberry Pi OS Bookworm, camera
`rpicam-still`, ALSA `arecord`/`aplay` và cách đánh số GPIO theo BCM.

Nếu thiết bị đã cài đặt xong và chỉ cần hướng dẫn sử dụng hằng ngày, xem
[RASPBERRY_PI_VAN_HANH_VI.md](RASPBERRY_PI_VAN_HANH_VI.md).

## 1. Phần cứng và sơ đồ nối nút

Phần cứng tối thiểu:

- Raspberry Pi có header GPIO 40 chân và kết nối Internet.
- Camera tương thích Raspberry Pi hoặc camera khác có lệnh chụp được ảnh JPEG.
- Microphone USB, audio HAT hoặc thiết bị thu âm ALSA tương thích.
- Loa USB, HDMI, jack audio hoặc audio HAT tương thích ALSA.
- Ba nút nhấn thường mở (momentary, normally open).

Ánh xạ chân mặc định của chương trình:

| Chức năng | GPIO BCM | Chân vật lý | Đầu còn lại |
|---|---:|---:|---|
| Button 1 — chụp/đọc tiếp; giữ để đổi trang | GPIO 17 | Pin 11 | GND |
| Button 2 — dừng/tóm tắt | GPIO 27 | Pin 13 | GND |
| Button 3 — hỏi/hoàn tất câu hỏi | GPIO 22 | Pin 15 | GND |
| Mass chung | GND | Pin 6 | Ba nút có thể dùng chung |

Mỗi nút nối giữa một GPIO và GND:

```text
BCM GPIO 17 ──[ Button 1 ]── GND
BCM GPIO 27 ──[ Button 2 ]── GND
BCM GPIO 22 ──[ Button 3 ]── GND
```

Chương trình bật điện trở pull-up nội bộ và chống rung 100 ms. Không nối nút
vào 5 V hoặc 3,3 V và không cần điện trở pull-up ngoài. Tắt nguồn Pi trước khi
đấu dây. Có thể chọn GPIO khác, nhưng ba số BCM phải khác nhau và không được
đang dùng bởi camera, audio HAT hoặc phần cứng khác.

## 2. Chuẩn bị Raspberry Pi OS

Cập nhật hệ thống và cài công cụ camera/audio:

```bash
sudo apt update
sudo apt install -y git alsa-utils rpicam-apps
```

Trên Raspberry Pi OS Bookworm, công cụ camera hiện hành có tên `rpicam-*`;
`libcamera-still` là tên cũ. Kiểm tra camera:

```bash
rpicam-hello --list-cameras
rpicam-still --nopreview --timeout 1500 --output /tmp/img2speech-camera.jpg
file /tmp/img2speech-camera.jpg
```

Liệt kê thiết bị phát và thu âm:

```bash
aplay -l
arecord -l
```

Thu thử năm giây rồi phát lại:

```bash
arecord -q -t wav -f S16_LE -r 16000 -c 1 -d 5 /tmp/img2speech-mic.wav
aplay /tmp/img2speech-mic.wav
```

Nếu máy có nhiều sound card, thêm `-D plughw:CARD=<ten>,DEV=<so>` vào cả lệnh
`arecord` và `aplay`. Lấy tên/card/device từ kết quả `arecord -l` và `aplay -l`.

## 3. Cài dự án

Cài `uv` bằng hướng dẫn chính thức của uv, sau đó đăng nhập lại nếu shell chưa
nhận lệnh. Sao chép dự án vào Pi, ví dụ `/home/pi/Img2Speech`:

```bash
cd /home/pi/Img2Speech
uv sync
```

Tạo `.env` trong cùng thư mục với `device_reader.py`:

```dotenv
OPENAI_API_KEY=sk-...
```

Bảo vệ file khóa API:

```bash
chmod 600 .env
```

Kiểm tra Python nhìn thấy GPIO Zero:

```bash
uv run python -c "from gpiozero import Button; print('gpiozero OK')"
```

GPIO Zero cần một pin factory có quyền đọc/ghi `/dev/gpiochip*`. Nếu gặp
`BadPinFactory`, cài backend `lgpio` vào môi trường dự án rồi kiểm tra lại:

```bash
uv pip install lgpio
GPIOZERO_PIN_FACTORY=lgpio uv run python -c "from gpiozero import Button; b=Button(17); print('GPIO OK'); b.close()"
```

Nếu dùng `GPIOZERO_PIN_FACTORY=lgpio` khi kiểm tra, thêm cùng biến môi trường
vào lệnh chạy thật hoặc service systemd ở phần 7.

## 4. Chạy chương trình ba nút

Lệnh khuyến nghị với các chân mặc định trong sơ đồ:

```bash
cd /home/pi/Img2Speech
uv run device_reader.py \
  --capture-command 'rpicam-still --nopreview --timeout 1500 --output {output}' \
  --record-command 'arecord -q -t wav -f S16_LE -r 16000 -c 1 {output}'
```

Không cần truyền tham số GPIO cho sơ đồ mặc định: Button 1 dùng BCM 17,
Button 2 dùng BCM 27 và Button 3 dùng BCM 22. Muốn đổi chân, truyền
`--button1-pin`, `--button2-pin` và `--button3-pin`; ba giá trị phải khác nhau.

Giữ nguyên chuỗi `{output}` trong cả hai command. Chương trình thay chuỗi này
bằng file tạm, tách tham số bằng `shlex` và không chạy qua shell.

Yêu cầu đối với command:

- Camera phải tạo JPEG hợp lệ trong tối đa 30 giây.
- Recorder phải chạy liên tục và tạo WAV hợp lệ khi nhận `SIGINT`.
- Câu hỏi tự dừng sau 60 giây nếu người dùng không nhấn Button 3 lần hai.
- Player mặc định là `aplay`, nhận PCM mono, 16-bit, 24 kHz qua stdin.

Ví dụ chọn thiết bị ALSA cụ thể:

```bash
uv run device_reader.py \
  --capture-command 'rpicam-still --nopreview --timeout 1500 --output {output}' \
  --record-command 'arecord -q -D plughw:CARD=Device,DEV=0 -t wav -f S16_LE -r 16000 -c 1 {output}' \
  --player 'aplay -q -D plughw:CARD=Device,DEV=0 -f S16_LE -r 24000 -c 1 -t raw -'
```

## 5. Hành vi của ba nút

### Button 1 — chụp ảnh hoặc đọc tiếp

- Nhấn ngắn giữ hành vi hiện tại: chụp ảnh khi cần trang mới hoặc đọc tiếp trang
  đang dở sau khi tóm tắt/trả lời.
- Nhấn giữ khoảng 1,5 giây ở bất kỳ trạng thái nào: dừng audio/ghi âm, hủy và xóa
  trang hiện tại, chụp ảnh mới rồi tự động OCR và đọc từ đầu. Khi nhả nút, hệ
  thống không đồng thời thực hiện thao tác nhấn ngắn.
- Khi chưa có ảnh: loa báo “Đang chụp ảnh”, chụp JPEG, báo “Đang xử lý
  ảnh”, OCR, rồi báo “Bắt đầu đọc” trước câu đầu tiên.
- Khi đang đọc: hủy ảnh hiện tại, chụp ảnh mới và đọc từ đầu ảnh mới.
- Sau khi tóm tắt hoặc trả lời, nếu còn nội dung: báo “Tiếp tục đọc” và đọc
  tiếp từ đầu câu bị ngắt.
- Khi đã đọc hết: chụp ảnh mới.
- Khi nội dung kết thúc tự nhiên: loa báo “Đã đọc xong”. Nếu audio bị ngắt thì
  không phát câu hoàn tất này.
- Nạp ảnh mới xóa transcript, con trỏ, summary và câu hỏi/trả lời gần nhất của
  ảnh cũ.

### Button 2 — dừng và tóm tắt

- Dừng bài đọc, câu trả lời hoặc summary đang phát.
- Báo “Bắt đầu tóm tắt” trước khi gọi model; trong lúc xử lý hệ thống giữ im
  lặng, không lặp thông báo.
- Tóm tắt toàn bộ các câu gốc đã đọc hoàn chỉnh; nếu bị ngắt trong câu đầu tiên
  thì dùng câu đầu tiên làm fallback.
- Nhấn lại sẽ tạo summary mới từ cùng phần văn bản gốc tích lũy. Summary cũ
  không được nhập ngược vào nguồn.
- Sau nội dung summary, loa báo “Tóm tắt xong”. Nhấn Button 1 để đọc tiếp nếu
  còn nội dung.

### Button 3 — đặt câu hỏi

- Lần nhấn thứ nhất: dừng audio hiện tại, báo “Bắt đầu ghi câu hỏi”, phát một
  tiếng beep rồi mới bắt đầu ghi. Lời báo và beep không được thu vào câu hỏi.
- Lần nhấn thứ hai: dừng recorder trước, phát hai tiếng beep, báo “Đã ghi xong.
  Đang xử lý câu hỏi”, rồi mới chạy STT và tạo câu trả lời.
- Khi có kết quả: báo “Bắt đầu trả lời”, đọc câu trả lời, rồi báo “Đã trả lời
  xong”.
- Câu trả lời chỉ dựa trên văn bản gốc đã đọc. Nếu nguồn không đủ, hệ thống phải
  nói rõ thay vì suy đoán.
- Mỗi câu hỏi độc lập; câu hỏi và câu trả lời trước không được gửi lại model.
- Sau câu trả lời, Button 1 đọc tiếp; Button 2 tạo summary; Button 3 bắt đầu câu
  hỏi mới.

Các trạng thái không hợp lệ cũng được báo qua loa, ví dụ “Vui lòng chụp ảnh
trước”, “Chưa có nội dung để thực hiện” hoặc “Đang xử lý câu hỏi. Vui lòng
chờ”. Ba tiếng beep thấp kèm lời báo ngắn được dùng cho lỗi camera, microphone,
model hoặc player. Nếu TTS thông báo thất bại thì chỉ phát ba beep thấp. Mỗi
câu thông báo sử dụng cùng model/voice và được cache trong RAM sau lần tạo đầu.
Mặc định nội dung sách, bản tóm tắt, câu trả lời và mọi thông báo nói đều dùng
giọng `marin`; tham số `--voice` sẽ đổi đồng bộ toàn bộ các phần này.
Khi có lỗi, xem log terminal hoặc `journalctl` nếu chạy bằng systemd.

## 6. Kiểm tra nghiệm thu trước khi đóng hộp

Thực hiện theo thứ tự:

1. Khởi động chương trình và thấy dòng `Three-button reader ready`.
2. Nhấn Button 1: nghe lần lượt thông báo chụp, xử lý, bắt đầu đọc rồi nội dung.
3. Giữa một câu, nhấn Button 2: audio dừng, nghe thông báo bắt đầu, summary và
   thông báo hoàn tất; nhấn Button 1 và
   xác nhận câu bị ngắt được đọc lại từ đầu.
4. Khi đang đọc, nhấn Button 3: nghe lời báo và một beep rồi nói câu hỏi; nhấn
   lại và nghe hai beep, lời báo xử lý, lời mở đầu, câu trả lời và lời hoàn tất.
5. Nhấn Button 1 sau câu trả lời: bài đọc tiếp tục, không chụp ảnh mới nếu còn
   nội dung.
6. Hỏi hai câu liên tiếp và xác nhận câu thứ hai không phụ thuộc hội thoại cũ.
7. Đọc hết ảnh rồi nhấn Button 1: camera phải chụp ảnh mới.
8. Sau khi tóm tắt hoặc trả lời, nhấn giữ Button 1 khoảng 1,5 giây: xác nhận
   camera chụp trang mới và trang cũ không được đọc tiếp khi nhả nút.

## 7. Tự chạy khi Raspberry Pi khởi động

Tạo `/etc/systemd/system/img2speech.service`. Thay `pi` và đường dẫn nếu tài
khoản hoặc thư mục dự án của bạn khác:

```ini
[Unit]
Description=Img2Speech three-button reader
After=network-online.target sound.target
Wants=network-online.target

[Service]
Type=simple
User=pi
Group=pi
SupplementaryGroups=audio video gpio
WorkingDirectory=/home/pi/Img2Speech
Environment=PYTHONUNBUFFERED=1
# Bỏ dấu # ở dòng dưới nếu Pi cần backend lgpio:
# Environment=GPIOZERO_PIN_FACTORY=lgpio
ExecStart=/home/pi/.local/bin/uv run device_reader.py --button1-pin 17 --button2-pin 27 --button3-pin 22 --capture-command "rpicam-still --nopreview --timeout 1500 --output {output}" --record-command "arecord -q -t wav -f S16_LE -r 16000 -c 1 {output}"
Restart=on-failure
RestartSec=3

[Install]
WantedBy=multi-user.target
```

Không chạy service bằng root. Đảm bảo user chạy service thuộc các group cần
thiết; đăng xuất/khởi động lại sau khi thêm group:

```bash
sudo usermod -aG audio,video,gpio pi
sudo systemctl daemon-reload
sudo systemctl enable --now img2speech.service
systemctl status img2speech.service
```

Xem log trực tiếp:

```bash
journalctl -u img2speech.service -f
```

Sau khi sửa service:

```bash
sudo systemctl daemon-reload
sudo systemctl restart img2speech.service
```

## 8. Xử lý lỗi thường gặp

### Nhấn nút không có phản hồi

- Xác nhận dùng số BCM, không dùng số chân vật lý trong tham số CLI.
- Kiểm tra một chân nút nối GPIO, chân còn lại nối GND.
- Kiểm tra quyền `/dev/gpiochip*`, group `gpio` và pin factory GPIO Zero.
- Chạy bằng terminal trước khi chuyển sang systemd để thấy lỗi đầy đủ.

### Camera timeout hoặc không tạo JPEG

- Chạy riêng command `rpicam-still` ở phần 2.
- Kiểm tra cáp cắm đúng cổng CSI và đúng chiều.
- Command phải giữ `{output}` và xuất JPEG; `device_reader.py` không nhận PNG từ
  camera dù giao diện web local có hỗ trợ PNG.

### Không nghe tiếng

- Chạy `aplay -l`, kiểm tra sound card mặc định hoặc truyền `--player` có `-D`.
- Kiểm tra âm lượng bằng `alsamixer`.
- User của systemd phải thuộc group `audio`.

### Không nhận được câu hỏi

- Thu/phát thử WAV như phần 2 và chọn đúng thiết bị `arecord -D`.
- Không dùng recorder tự thoát ngay; nó phải ghi đến khi nhận `SIGINT`.
- WAV phải có header `RIFF/WAVE`; command mẫu `arecord -t wav` đáp ứng yêu cầu.

### Có ba beep thấp

Đây là tín hiệu lỗi. Kiểm tra kết nối Internet, `OPENAI_API_KEY`, camera,
microphone, loa và log:

```bash
journalctl -u img2speech.service -n 100 --no-pager
```

## Tài liệu tham khảo

- [Raspberry Pi camera software](https://www.raspberrypi.com/documentation/computers/camera_software.html)
- [Raspberry Pi audio documentation](https://www.raspberrypi.com/documentation/accessories/audio.html)
- [GPIO Zero pin factories](https://gpiozero.readthedocs.io/en/stable/api_pins.html)
