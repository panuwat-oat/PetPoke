# PetPoke — แจ้งเตือนอุปกรณ์ PetKit ผ่าน Telegram (แบบซ้ำๆ)

PetKit แอพเตือนแค่ครั้งเดียวตอนถังเต็ม / อาหารหมด / น้ำใกล้หมด ฯลฯ —
ออกจากบ้านทีกลับมาเจอน้องล้นกล่องหรือชามเปล่า 😩
โปรเจกต์นี้รัน script เล็กๆ ทุก 5 นาทีบน VPS (Docker container, loop ในตัว)
เพื่อเช็คสถานะอุปกรณ์ทุกตัว แล้วยิง Telegram เตือนซ้ำตามตาราง backoff จนกว่าปัญหาจะหาย

**รองรับ:** Pura MAX (ถังขยะ), Feeder ตระกูล YUMSHARE/D-series (อาหาร + ซองดูดความชื้น),
น้ำพุ EVERSWEET (น้ำ + filter + battery)

> ℹ️ **รันบน VPS (Docker)**: เดิมรันบน GitHub Actions cron (drift 20–45 นาที) → ย้ายไป
> Cloud Run + Cloud Scheduler → ตอนนี้ย้ายมา VPS ส่วนตัวด้วย Docker (`runner.py` loop poll
> ทุก 5 นาทีในตัว container เดียว ไม่ต้องพึ่ง scheduler ภายนอก) — logic ใน `notifier.py`
> ยังแยกจาก platform รันที่ไหนก็ได้ (local, Raspberry Pi ฯลฯ) ถ้าอยากย้ายอีกทีหลัง

## ฟีเจอร์

- 🔔 ส่ง Telegram ซ้ำๆ ทุกครั้งที่ยังเจอปัญหา
- 📉 Backoff schedule: 15 → 30 → 60 → 120 → 120 นาที (ไม่สแปม)
- ✅ ส่งข้อความ "กลับสู่ปกติแล้ว" ครั้งเดียวเมื่อปัญหาหาย
- 🤫 ปุ่ม **"แก้แล้ว"** ใต้ทุก alert — กดแล้วเงียบทันที ไม่ต้องรอ PetKit cloud refresh
- 🎯 หลาย alert type ต่ออุปกรณ์ — แต่ละ alert มี backoff/state แยกกัน
- 🔐 ใช้ secondary PetKit account ไม่ขัดกับแอพมือถือบน account หลัก
- 🆓 รันบน VPS ส่วนตัวที่มีอยู่แล้ว — container เดียว กิน RAM/CPU น้อยมาก (poll ทุก 15 นาที)

## Alert types ทั้งหมด

| Device | Alert | ตัวกระตุ้น (จาก PetKit API) |
|---|---|---|
| 🚨 **Pura MAX** | ถังขยะเต็ม | `state.box_full == true` |
| 🍽️ **Feeder** | อาหารหมด | `state.food == 0` |
| 🍽️ **Feeder** | อาหารใกล้หมด | `state.food == 1` |
| 🚰 **น้ำพุ** | น้ำใกล้หมด | `lack_warning > 0` |
| 🧽 **น้ำพุ** | filter ใกล้เปลี่ยน | `filter_warning > 0` หรือ `filter_percent < 10` |
| 🔋 **น้ำพุ** | แบตเตอรี่ต่ำ | `low_battery > 0` |
| ⚠️ **ทุก device** | พบ error ของอุปกรณ์ | `state.error_code` มีค่า หรือ `breakdown_warning > 0` |
| 📡 **ทุก device** | ออฟไลน์/ไม่ตอบสนอง | `state.offline_time` มีค่า |
| 🆘 **Pura MAX** | น้องอาจติดในกล่อง (เซ็นเซอร์ตรวจพบ) | `state.pet_error == true` |

## Backoff schedule (ต่อ alert type ต่อ device)

หลังเจอปัญหา (เช่น กล่องเต็ม / อาหารหมด) ครั้งแรก:

| ครั้งที่ | ส่งห่างจากครั้งก่อน |
|---|---|
| 1 (ครั้งแรก) | ทันที |
| 2 | + 15 นาที |
| 3 | + 30 นาที |
| 4 | + 60 นาที |
| 5 | + 120 นาที |
| 6+ | + 120 นาที (cap) |

ตัวเลขนี้เป็นเวลา backoff "ตามตาราง" ต่อ alert — แยกจากรอบ poll (ตั้งไว้ทุก 5 นาทีบน VPS) ที่เป็นแค่ความถี่ในการเช็คสถานะ

## ปุ่ม "แก้แล้ว" (ปิดแจ้งเตือนซ้ำ)

PetKit cloud cache สถานะเก่าไว้ — พอแก้ปัญหาจริง (เช่น เทกล่องทราย) cloud มักยังรายงานว่ายังเจอปัญหา จนกว่าจะเปิดแอพหลักให้มัน refresh ค่าจาก device ใหม่ ทำให้ PetPoke แจ้งซ้ำทั้งที่แก้แล้ว

ทุกข้อความ alert จึงมีปุ่ม **✅ แก้แล้ว (เงียบไว้)** กดแล้ว:

- PetPoke หยุดแจ้งซ้ำ alert นั้นทันที ไม่ต้องรอ cloud
- พอ cloud ยืนยันว่าหายจริงในรอบถัดๆ ไป → re-arm อัตโนมัติ (ไม่เด้งข้อความ "กลับสู่ปกติ" ซ้ำ เพราะคุณรู้อยู่แล้วว่าแก้ไปแล้ว)

**ข้อจำกัด:** การกดมีผลภายใน ≤15 นาที (รอบ poll ถัดไปที่อ่านปุ่มผ่าน `getUpdates`) ไม่ใช่ทันที และอาจไม่มี toast ยืนยันตอนกด — แต่ระบบรับ tap ไว้แล้ว เงียบแน่นอน

---

## สิ่งที่ต้องเตรียมก่อนเริ่ม

### 1. สร้าง PetKit secondary account แล้ว family-share กล่อง

ทำเรื่องนี้ก่อน ห้ามใช้ account หลัก ไม่งั้นเวลา script login แอพในมือถือจะโดน kick ออก

**ขั้นตอน:**

1. ในแอพ PetKit (account หลัก) → เมนู `Me` → `Family Management` (หรือ `My Family`)
2. กด `+` เพื่อเพิ่มสมาชิกครอบครัว → จดรหัส invite ไว้
3. ดาวน์โหลด PetKit ในมือถือเครื่องอื่น หรือใช้ logout-login บนเครื่องเดียวกัน
4. สมัคร account ใหม่ด้วย email อีกอันที่ไม่ได้ใช้ (เช่น `[ชื่อคุณ]+petpoke@gmail.com`)
5. เข้า PetKit ด้วย account ใหม่ → `Me` → `Family Management` → ใส่ invite code
6. รอ account หลัก approve
7. account ใหม่ควรเห็นกล่องในแอพแล้ว
8. **จดอีเมล + รหัสผ่าน** ของ secondary account ไว้ใส่เป็น secret
9. **อย่า logout** secondary account ทิ้งเฉยๆ (ขั้นนี้ใช้เฉพาะเช็คว่า invite สำเร็จ)

### 2. สร้าง Telegram Bot

1. เปิด Telegram → ค้นหา [@BotFather](https://t.me/BotFather)
2. ส่งคำสั่ง `/newbot` → ตั้งชื่อ display + username (ลงท้าย `bot`)
3. BotFather จะคืน **token** หน้าตาแบบ `123456:ABCdef...` → **เก็บไว้**
4. เปิดแชทกับ bot ที่เพิ่งสร้าง → กด `Start` หรือพิมพ์ `/start` (ขั้นนี้สำคัญ — ถ้าไม่ทักก่อน bot จะส่งหาคุณไม่ได้)

### 3. หา Chat ID

วิธีง่ายสุด:

1. ส่งข้อความอะไรก็ได้ให้ bot สัก 1 ข้อความ
2. เปิด browser ไปที่:
   ```
   https://api.telegram.org/bot<TOKEN ของคุณ>/getUpdates
   ```
3. มองหา `"chat":{"id":123456789,` → เลขนั้นคือ chat ID

หรือใช้บ็อต [@userinfobot](https://t.me/userinfobot) ทักไปก็บอก ID เลย

---

## วิธี Setup (ขั้นตอนหลัก)

### A) ทดสอบใน local ก่อน (แนะนำ — เร็วกว่า debug บน cloud)

```bash
# 1. clone โปรเจกต์
git clone <your-repo-url> petpoke
cd petpoke

# 2. สร้าง venv และติดตั้ง dependencies
python -m venv .venv
# Windows:
.venv\Scripts\activate
# macOS/Linux:
# source .venv/bin/activate
pip install -r requirements.txt

# 3. ก๊อปไฟล์ env แล้วใส่ค่าจริง
copy .env.example .env     # Windows
# cp .env.example .env     # macOS/Linux
# แก้ .env ใส่ค่าทั้ง 4 ตัว: PETKIT_USERNAME, PETKIT_PASSWORD, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID

# 4. load env แล้วรัน (Windows PowerShell)
Get-Content .env | ForEach-Object {
  if ($_ -match '^\s*([^#][^=]+)=(.*)$') {
    [Environment]::SetEnvironmentVariable($matches[1].Trim(), $matches[2].Trim())
  }
}
python notifier.py

# macOS/Linux:
# set -a; source .env; set +a; python notifier.py
```

ถ้าทุกอย่างถูก คุณควรเห็น log ประมาณนี้:
```
INFO petpoke: Pura MAX still full; next alert at 15:30 14/05/2026
```
หรือถ้าตอนนั้นกล่องเต็ม → ได้รับ Telegram message

> **Tip**: ครั้งแรกอยากเห็นว่า PetKit return field อะไรบ้าง → ตั้ง `DEBUG_LOG_RAW=true`
> ใน `.env` แล้วรันใหม่ จะมี dump device state ใน log

### B) Deploy ขึ้น VPS ด้วย Docker

ต้องมี VPS ที่ลง Docker + Docker Compose แล้ว logic ใน `notifier.py` แยกจาก platform —
`runner.py` เป็น loop เรียก poll ทุก 5 นาทีในตัว (แทน Cloud Scheduler) container เดียวจบ
state เก็บเป็นไฟล์ใน mounted volume (`./data/state.json`) ไม่มี HTTP endpoint เปิดออก

```bash
# 1. copy ไฟล์ runtime ขึ้น VPS (เฉพาะที่ต้องใช้ — ไม่เอา CLAUDE.md / .claude ขึ้น server)
ssh <vps> 'mkdir -p /opt/petpoke/data'
tar czf - Dockerfile docker-compose.yml requirements.txt notifier.py runner.py .dockerignore \
  | ssh <vps> 'tar xzf - -C /opt/petpoke'

# 2. สร้าง .env (4 ค่า required) — mode 600 ไม่ฝังในโค้ด/ไม่ลง git
ssh <vps> 'umask 077; cat > /opt/petpoke/.env' <<'EOF'
PETKIT_USERNAME=you@example.com
PETKIT_PASSWORD=your-petkit-password
TELEGRAM_BOT_TOKEN=123456:ABC-your-bot-token
TELEGRAM_CHAT_ID=123456789
EOF

# 3. (ถ้าย้ายมาจาก runtime เดิม) seed state ก่อน กัน alert เด้งซ้ำ
scp state.json <vps>:/opt/petpoke/data/state.json

# 4. build + run (restart: unless-stopped → ขึ้นเองหลัง VPS reboot)
ssh <vps> 'cd /opt/petpoke && docker compose up -d --build'

# 5. ดู log (poll รอบแรกยิงทันทีตอน container start)
ssh <vps> 'docker logs -f petpoke'
```

> **Note**: ปรับรอบ poll ได้ด้วย `POLL_INTERVAL_MINUTES` ใน `docker-compose.yml` (ตั้งไว้ 5 นาทีบน VPS, default ใน `runner.py` = 15)
> ค่า non-secret (region, timezone, interval, STATE_FILE) อยู่ใน `docker-compose.yml` —
> secret 4 ตัวอยู่ใน `.env` (mode 600) แยกออกมา ไม่มี endpoint เปิดออก (เป็น loop ปิดในตัว)
> ไม่เหมือน Cloud Run จึงไม่ต้องกังวลเรื่อง public abuse

---

## Troubleshooting

| อาการ | สาเหตุที่น่าจะเป็น | วิธีแก้ |
|---|---|---|
| มือถือถูก logout PetKit | ดัน login ด้วย account หลักลง script | เปลี่ยนเป็น secondary account |
| `PetKit fetch failed: ... login` | password/region ผิด | ตรวจ `PETKIT_REGION` ตามประเทศจริง (TH, US, EU, ...) |
| `Telegram returned 403` | ยังไม่ได้กด Start ทักทาย bot | เปิดแชท bot แล้วส่ง `/start` |
| `Telegram returned 400 chat not found` | `TELEGRAM_CHAT_ID` ผิด | เช็คอีกครั้งจาก `getUpdates` |
| ไม่มี alert ทั้งๆ ที่กล่องเต็ม | field `boxFull` ใน pypetkitapi เปลี่ยน | ตั้ง `DEBUG_LOG_RAW=true` ดู log แล้วแจ้ง issue |
| container ไม่ start / exit | `.env` ไม่ครบ 4 ค่า หรือ build พัง | `docker logs petpoke` ดู error, ตรวจ `/opt/petpoke/.env` |
| state หาย/รีเซ็ตหลัง redeploy | ลืม mount volume หรือ seed | ตรวจ `./data:/data` ใน compose + ไฟล์ `data/state.json` |
| log ขึ้น `State unchanged; skipping write` | ปกติ — ไม่มี change รอบนั้น | ไม่ใช่ปัญหา (เขียนไฟล์เฉพาะตอน state เปลี่ยน) |

## โครงสร้างโปรเจกต์

```
PetPoke/
├── notifier.py        # ตัว script หลัก (poll logic — แยกจาก platform)
├── runner.py          # loop driver สำหรับ VPS/Docker (poll ทุก 5 นาทีในตัว)
├── Dockerfile         # image: python:3.11-slim + requirements + runner.py
├── docker-compose.yml # service petpoke (env_file .env, volume ./data, restart)
├── .dockerignore      # ตัดไฟล์ออกตอน build (รวม CLAUDE.md/.claude)
├── requirements.txt   # pypetkitapi + aiohttp + tzdata
├── state.json         # state seed (ของจริงอยู่ใน volume ./data/state.json บน VPS)
├── .env.example       # template สำหรับ local
├── .gitignore
└── README.md
```

## ข้อจำกัด & หมายเหตุ

- `pypetkitapi` เป็น unofficial library — ถ้า PetKit เปลี่ยน API แรงๆ อาจพัง
  เราล็อค version range ไว้ (`>=1.15.0,<2.0.0`) แต่ถ้าวันหนึ่ง login เริ่ม fail
  ลอง bump version ใน `requirements.txt` ดูก่อน
- รองรับ device class ที่ pypetkitapi คืนมาเป็น `Litter`, `Feeder`,
  `WaterFountain` (จาก class name โดยตรง). ถ้ามี Purifier/Spray ตัวอื่นๆ
  ต้องเพิ่ม extractor function ใน `notifier.py`
- State เก็บเป็นไฟล์บน VPS (`/opt/petpoke/data/state.json` ใน mounted volume) ไม่ commit
  กลับ repo — `state.json` ใน git เป็นแค่ของเก่า/seed (มีแค่ timestamp + counter ไม่ sensitive)
  ตอนรัน local จะใช้ไฟล์ `state.json` ตามค่า `STATE_FILE` (default `state.json`)
  หมายเหตุ: `notifier.py` ยังมี GCS backend อยู่ (เปิดเมื่อ set `STATE_BUCKET`) เผื่อย้าย runtime

## License

MIT — ใช้ได้ตามสะดวก
