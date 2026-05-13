# PetPoke — แจ้งเตือนอุปกรณ์ PetKit ผ่าน Telegram (แบบซ้ำๆ)

PetKit แอพเตือนแค่ครั้งเดียวตอนถังเต็ม / อาหารหมด / น้ำใกล้หมด ฯลฯ —
ออกจากบ้านทีกลับมาเจอน้องล้นกล่องหรือชามเปล่า 😩
โปรเจกต์นี้รัน script เล็กๆ ทุก ~15 นาทีบน GitHub Actions เพื่อเช็คสถานะ
อุปกรณ์ทุกตัว แล้วยิง Telegram เตือนซ้ำตามตาราง backoff จนกว่าปัญหาจะหาย

**รองรับ:** Pura MAX (ถังขยะ), Feeder ตระกูล YUMSHARE/D-series (อาหาร + ซองดูดความชื้น),
น้ำพุ EVERSWEET (น้ำ + filter + battery)

> ⚠️ **GitHub Actions cron lag**: `cron: '*/15 * * * *'` ตามสเปกจะรันทุก 15 นาที
> แต่ในทางปฏิบัติ GitHub Actions มี delay 5–30 นาที (ช่วง peak อาจถึง 60+ นาที)
> ฉะนั้นรอบจริงอาจห่าง 20–45 นาที ถ้ารับไม่ได้ ให้ย้ายไป self-hosted (Raspberry Pi)
> หรือ Cloudflare Workers Cron ทีหลัง — logic ใน `notifier.py` ย้าย platform ง่าย

## ฟีเจอร์

- 🔔 ส่ง Telegram ซ้ำๆ ทุกครั้งที่ยังเจอปัญหา
- 📉 Backoff schedule: 15 → 30 → 60 → 120 → 120 นาที (ไม่สแปม)
- 🌙 Quiet hours (default 23:00–07:00 Asia/Bangkok) ไม่ส่งกลางดึก
- ✅ ส่งข้อความ "กลับสู่ปกติแล้ว" ครั้งเดียวเมื่อปัญหาหาย
- 🎯 หลาย alert type ต่ออุปกรณ์ — แต่ละ alert มี backoff/state แยกกัน
- 🔐 ใช้ secondary PetKit account ไม่ขัดกับแอพมือถือบน account หลัก
- 🆓 ฟรี 100% (GitHub Actions free tier)

## Alert types ทั้งหมด

| Device | Alert | ตัวกระตุ้น (จาก PetKit API) |
|---|---|---|
| 🚨 **Pura MAX** | ถังขยะเต็ม | `state.box_full == true` |
| 🍽️ **Feeder** | อาหารหมด | `state.food == 0` |
| 🍽️ **Feeder** | อาหารใกล้หมด | `state.food == 1` |
| 🚰 **น้ำพุ** | น้ำใกล้หมด | `lack_warning > 0` |
| 🧽 **น้ำพุ** | filter ใกล้เปลี่ยน | `filter_warning > 0` หรือ `filter_percent < 10` |
| 🔋 **น้ำพุ** | แบตเตอรี่ต่ำ | `low_battery > 0` |

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

ตัวเลขนี้เป็นเวลา "ตามตาราง" จริงต้องบวก GHA cron lag

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

### A) ทดสอบใน local ก่อน (แนะนำ — ดีกว่ารอ GHA debug)

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

### B) Deploy ไป GitHub Actions

1. สร้าง repo ใหม่บน GitHub (private แนะนำ)
2. push code ขึ้น:
   ```bash
   git init
   git add .
   git commit -m "feat: initial PetPoke setup"
   git branch -M main
   git remote add origin git@github.com:<you>/<repo>.git
   git push -u origin main
   ```
3. ไปที่ repo → `Settings` → `Secrets and variables` → `Actions`
4. ใต้แท็บ **Secrets** กด `New repository secret` แล้วใส่ทีละตัว:

   | Secret name | Value |
   |---|---|
   | `PETKIT_USERNAME` | email ของ secondary PetKit account |
   | `PETKIT_PASSWORD` | password ของ secondary account |
   | `TELEGRAM_BOT_TOKEN` | token จาก @BotFather |
   | `TELEGRAM_CHAT_ID` | chat ID ของคุณ |

5. (ทางเลือก) ใต้แท็บ **Variables** เพิ่มตัวพวกนี้ถ้าอยากปรับ:

   | Variable | Default | ความหมาย |
   |---|---|---|
   | `PETKIT_REGION` | `TH` | country code ของ account |
   | `PETKIT_TIMEZONE` | `Asia/Bangkok` | timezone สำหรับเวลาใน message |
   | `QUIET_HOURS_ENABLED` | `true` | ปิดแจ้งเตือนช่วงดึก |
   | `QUIET_HOURS_START` | `23:00` | เริ่ม quiet (24h format) |
   | `QUIET_HOURS_END` | `07:00` | จบ quiet |
   | `DEBUG_LOG_RAW` | `false` | ตั้ง `true` ดูสถานะ raw รอบแรก |

6. ไปที่แท็บ `Actions` → เลือก workflow `PetKit Poll` → กด `Run workflow` เพื่อทดสอบ manual ก่อน
7. ดู log ว่า login + ส่ง Telegram ทำงานปกติ → จากนั้น cron จะวิ่งทุก 15 นาทีอัตโนมัติ

---

## Troubleshooting

| อาการ | สาเหตุที่น่าจะเป็น | วิธีแก้ |
|---|---|---|
| มือถือถูก logout PetKit | ดัน login ด้วย account หลักลง script | เปลี่ยนเป็น secondary account |
| `PetKit fetch failed: ... login` | password/region ผิด | ตรวจ `PETKIT_REGION` ตามประเทศจริง (TH, US, EU, ...) |
| `Telegram returned 403` | ยังไม่ได้กด Start ทักทาย bot | เปิดแชท bot แล้วส่ง `/start` |
| `Telegram returned 400 chat not found` | `TELEGRAM_CHAT_ID` ผิด | เช็คอีกครั้งจาก `getUpdates` |
| ไม่มี alert ทั้งๆ ที่กล่องเต็ม | field `boxFull` ใน pypetkitapi เปลี่ยน | ตั้ง `DEBUG_LOG_RAW=true` ดู log แล้วแจ้ง issue |
| Workflow ขึ้นแต่ไม่ commit state | repo permissions ไม่พอ | ตรวจ `permissions: contents: write` ใน `poll.yml` |
| commit state รก git history | ปกติ ทุก 15 นาทีถ้ามี change | ไม่ใช่ปัญหา — `[skip ci]` ป้องกัน loop แล้ว |

## โครงสร้างโปรเจกต์

```
PetPoke/
├── .github/workflows/poll.yml   # cron job (ทุก 15 นาที)
├── notifier.py                  # ตัว script หลัก
├── requirements.txt             # pypetkitapi + aiohttp
├── state.json                   # state (auto-commit โดย GHA)
├── .env.example                 # template สำหรับ local
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
- State file commit กลับ repo ผ่าน GHA — ถ้า private repo ปัญหาน้อย แต่ถ้าทำ
  public ระวังไม่มีอะไร sensitive (มีแค่ timestamp + counter)

## License

MIT — ใช้ได้ตามสะดวก
