# PetPoke — แจ้งเตือนอุปกรณ์ PetKit ผ่าน Telegram (แบบซ้ำๆ)

PetKit แอพเตือนแค่ครั้งเดียวตอนถังเต็ม / อาหารหมด / น้ำใกล้หมด ฯลฯ —
ออกจากบ้านทีกลับมาเจอน้องล้นกล่องหรือชามเปล่า 😩
โปรเจกต์นี้รัน script เล็กๆ ทุก 15 นาทีบน Google Cloud Run (สั่งโดย Cloud Scheduler)
เพื่อเช็คสถานะอุปกรณ์ทุกตัว แล้วยิง Telegram เตือนซ้ำตามตาราง backoff จนกว่าปัญหาจะหาย

**รองรับ:** Pura MAX (ถังขยะ), Feeder ตระกูล YUMSHARE/D-series (อาหาร + ซองดูดความชื้น),
น้ำพุ EVERSWEET (น้ำ + filter + battery)

> ℹ️ **รันบน Cloud Run + Cloud Scheduler**: เดิมโปรเจกต์รันบน GitHub Actions cron
> แต่ GHA มี delay 5–30 นาที (peak 60+) ทำให้รอบจริงห่าง 20–45 นาที จึงย้ายมา Cloud Run
> ที่ Cloud Scheduler ยิง `/poll` ตรงเวลาทุก 15 นาที — logic ใน `notifier.py` แยกจาก platform
> ยังรันที่ไหนก็ได้ (local, Raspberry Pi, Cloudflare Workers) ถ้าอยากย้ายอีกทีหลัง

## ฟีเจอร์

- 🔔 ส่ง Telegram ซ้ำๆ ทุกครั้งที่ยังเจอปัญหา
- 📉 Backoff schedule: 15 → 30 → 60 → 120 → 120 นาที (ไม่สแปม)
- ✅ ส่งข้อความ "กลับสู่ปกติแล้ว" ครั้งเดียวเมื่อปัญหาหาย
- 🎯 หลาย alert type ต่ออุปกรณ์ — แต่ละ alert มี backoff/state แยกกัน
- 🔐 ใช้ secondary PetKit account ไม่ขัดกับแอพมือถือบน account หลัก
- 🆓 ฟรี 100% (GCP free tier — Cloud Run + Scheduler + GCS อยู่ในโควต้าฟรีที่ปริมาณนี้)

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

ตัวเลขนี้เป็นเวลา "ตามตาราง" — Cloud Scheduler ยิงตรงเวลา รอบจริงห่างตามนี้

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

### B) Deploy ไป Google Cloud Run

ต้องมี [gcloud CLI](https://cloud.google.com/sdk/docs/install) + บัญชี GCP ที่ผูก billing แล้ว
(free tier ครอบคลุม ไม่เสียเงินที่ปริมาณนี้) สมมติชื่อ project = `petpoke-notifier`, region = `us-central1`

```bash
# 0. login + ตั้ง project (project id ต้อง unique ทั้งโลก เปลี่ยนได้)
gcloud auth login
gcloud projects create petpoke-notifier --name=petpoke
gcloud billing projects link petpoke-notifier --billing-account=<YOUR_BILLING_ID>
gcloud config set project petpoke-notifier

# 1. เปิด API ที่ใช้
gcloud services enable run.googleapis.com cloudbuild.googleapis.com \
  artifactregistry.googleapis.com cloudscheduler.googleapis.com \
  secretmanager.googleapis.com storage.googleapis.com

# 2. สร้าง bucket เก็บ state
gcloud storage buckets create gs://petpoke-notifier-state \
  --location=us-central1 --uniform-bucket-level-access
# (ถ้าย้ายมาจาก runtime เดิม seed state ก่อน กัน alert เด้งซ้ำ)
gcloud storage cp state.json gs://petpoke-notifier-state/state.json

# 3. สร้าง secret 4 ตัว (ใส่ค่าแบบไม่โผล่จอ/ไม่ลง history)
for s in PETKIT_USERNAME PETKIT_PASSWORD TELEGRAM_BOT_TOKEN TELEGRAM_CHAT_ID; do
  gcloud secrets create "$s" --replication-policy=automatic
  printf "Enter %s: " "$s"; read -rs val; echo
  printf %s "$val" | gcloud secrets versions add "$s" --data-file=-
done; unset val

# 4. ให้สิทธิ์ default compute service account (= identity ที่ service + scheduler ใช้)
PROJNUM=$(gcloud projects describe petpoke-notifier --format='value(projectNumber)')
SA="$PROJNUM-compute@developer.gserviceaccount.com"
for s in PETKIT_USERNAME PETKIT_PASSWORD TELEGRAM_BOT_TOKEN TELEGRAM_CHAT_ID; do
  gcloud secrets add-iam-policy-binding "$s" \
    --member="serviceAccount:$SA" --role=roles/secretmanager.secretAccessor
done
gcloud storage buckets add-iam-policy-binding gs://petpoke-notifier-state \
  --member="serviceAccount:$SA" --role=roles/storage.objectAdmin

# 5. deploy (Cloud Build buildpacks — ไม่ต้องมี Dockerfile)
gcloud run deploy petpoke --source . --region=us-central1 \
  --no-allow-unauthenticated --memory=512Mi --timeout=120 --max-instances=1 \
  --set-env-vars=STATE_BUCKET=petpoke-notifier-state,STATE_OBJECT=state.json,PETKIT_REGION=TH,PETKIT_TIMEZONE=Asia/Bangkok \
  --set-secrets=PETKIT_USERNAME=PETKIT_USERNAME:latest,PETKIT_PASSWORD=PETKIT_PASSWORD:latest,TELEGRAM_BOT_TOKEN=TELEGRAM_BOT_TOKEN:latest,TELEGRAM_CHAT_ID=TELEGRAM_CHAT_ID:latest

# 6. ให้ scheduler เรียก service ได้ + สร้าง cron job ทุก 15 นาที (OIDC auth)
URL=$(gcloud run services describe petpoke --region=us-central1 --format='value(status.url)')
gcloud run services add-iam-policy-binding petpoke --region=us-central1 \
  --member="serviceAccount:$SA" --role=roles/run.invoker
gcloud scheduler jobs create http petpoke-poll --location=us-central1 \
  --schedule="*/15 * * * *" --time-zone="Asia/Bangkok" \
  --uri="$URL/poll" --http-method=POST \
  --oidc-service-account-email="$SA" --oidc-token-audience="$URL" \
  --attempt-deadline=300s

# 7. ทดสอบยิงเลย 1 รอบ แล้วดู log
gcloud scheduler jobs run petpoke-poll --location=us-central1
gcloud logging read 'resource.type=cloud_run_revision AND resource.labels.service_name=petpoke' \
  --limit=20 --freshness=10m --format='value(timestamp,textPayload)'
```

> **Note**: service เป็น **private** (`--no-allow-unauthenticated`) — เปิดให้เฉพาะ Cloud Scheduler
> เรียกผ่าน OIDC เท่านั้น อย่าทำเป็น public เพราะ `/poll` จะ trigger PetKit login + ส่ง Telegram
> ใครก็ยิงได้ = abuse ได้ ค่า secret อยู่ใน Secret Manager (`--set-secrets`) ไม่ฝังในโค้ด

---

## Troubleshooting

| อาการ | สาเหตุที่น่าจะเป็น | วิธีแก้ |
|---|---|---|
| มือถือถูก logout PetKit | ดัน login ด้วย account หลักลง script | เปลี่ยนเป็น secondary account |
| `PetKit fetch failed: ... login` | password/region ผิด | ตรวจ `PETKIT_REGION` ตามประเทศจริง (TH, US, EU, ...) |
| `Telegram returned 403` | ยังไม่ได้กด Start ทักทาย bot | เปิดแชท bot แล้วส่ง `/start` |
| `Telegram returned 400 chat not found` | `TELEGRAM_CHAT_ID` ผิด | เช็คอีกครั้งจาก `getUpdates` |
| ไม่มี alert ทั้งๆ ที่กล่องเต็ม | field `boxFull` ใน pypetkitapi เปลี่ยน | ตั้ง `DEBUG_LOG_RAW=true` ดู log แล้วแจ้ง issue |
| `/poll` คืน 403 | Scheduler ไม่มีสิทธิ์เรียก service | ตรวจ `run.invoker` ของ compute SA บน service |
| state ไม่ถูกเขียนใน GCS | SA ไม่มีสิทธิ์ bucket | ตรวจ `storage.objectAdmin` บน `gs://petpoke-notifier-state` |
| log ขึ้น `State unchanged; skipping write` | ปกติ — ไม่มี change รอบนั้น | ไม่ใช่ปัญหา (กัน write ฟุ่มเฟือยให้อยู่ใน free tier) |

## โครงสร้างโปรเจกต์

```
PetPoke/
├── notifier.py        # ตัว script หลัก (poll logic — แยกจาก platform)
├── main.py            # Flask entrypoint สำหรับ Cloud Run (/ health, /poll)
├── Procfile           # gunicorn process สำหรับ Cloud Run buildpacks
├── requirements.txt   # pypetkitapi + aiohttp + flask + gunicorn + google-cloud-storage
├── state.json         # state เดิม (historical/seed — ของจริงอยู่ใน GCS)
├── .gcloudignore      # ตัดไฟล์ออกตอน deploy --source
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
- State เก็บใน GCS bucket (`gs://petpoke-notifier-state/state.json`) ไม่ commit กลับ repo
  อีกแล้ว — `state.json` ใน git เป็นแค่ของเก่า/seed สำหรับ local run (มีแค่ timestamp +
  counter ไม่ sensitive) ตอนรัน local โดยไม่ตั้ง `STATE_BUCKET` จะใช้ไฟล์ local ตามปกติ

## License

MIT — ใช้ได้ตามสะดวก
