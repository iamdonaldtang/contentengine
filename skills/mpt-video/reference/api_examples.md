# MPT API 调用模板速查

## 0. 一次性准备：通用调用脚本

把以下内容存到 `E:\AILife\MoneyPrinterTurbo\mpt_call.py`（首次使用时跑一次即可）：

```python
# E:\AILife\MoneyPrinterTurbo\mpt_call.py
import requests, json, sys

BASE = "http://localhost:8090/api/v1"

def call(method, path, body=None):
    url = f"{BASE}{path}"
    r = requests.request(method, url, json=body, timeout=60)
    r.raise_for_status()
    return r.json()

if __name__ == "__main__":
    method, path = sys.argv[1], sys.argv[2]
    body = json.loads(sys.stdin.read()) if not sys.stdin.isatty() else None
    print(json.dumps(call(method, path, body), ensure_ascii=False, indent=2))
```

需要 `pip install requests`（PowerShell：`pip install requests`）。

---

## 调用模板 1 · 生成文案 (POST /scripts)

PowerShell here-string 避免引号转义：

```powershell
@'
{
  "video_subject": "早起健身的好处",
  "video_language": "zh-CN",
  "paragraph_number": 1
}
'@ | python E:\AILife\MoneyPrinterTurbo\mpt_call.py POST /scripts
```

英文版：

```powershell
@'
{
  "video_subject": "Why early morning workouts change everything",
  "video_language": "en-US",
  "paragraph_number": 2
}
'@ | python E:\AILife\MoneyPrinterTurbo\mpt_call.py POST /scripts
```

---

## 调用模板 2 · 生成关键词 (POST /terms)

```powershell
@'
{
  "video_subject": "早起健身的好处",
  "video_script": "<把阶段1的 video_script 文本贴这里，注意转义>",
  "amount": 5
}
'@ | python E:\AILife\MoneyPrinterTurbo\mpt_call.py POST /terms
```

> ⚠️ video_script 包含中文双引号或换行时，PowerShell here-string 不需要额外转义；但若包含 `"`（半角双引号），需 `\"`。

---

## 调用模板 3 · 提交渲染任务 (POST /videos)

```powershell
@'
{
  "video_subject": "早起健身的好处",
  "video_script": "<阶段1文案>",
  "video_terms": ["sunrise jogging", "morning stretch", "running shoes close-up", "park sunrise", "fitness app screen"],
  "video_aspect": "9:16",
  "video_source": "pexels",
  "video_count": 1,
  "video_clip_duration": 4,
  "voice_name": "zh-CN-XiaoxiaoNeural-Female",
  "subtitle_enabled": true,
  "subtitle_position": "bottom",
  "font_size": 60,
  "bgm_type": "random",
  "bgm_volume": 0.2
}
'@ | python E:\AILife\MoneyPrinterTurbo\mpt_call.py POST /videos
```

返回里抓 `data.task_id`。

---

## 调用模板 4 · 轮询任务进度 (GET /tasks/{task_id})

存为 `E:\AILife\MoneyPrinterTurbo\mpt_poll.py`：

```python
# E:\AILife\MoneyPrinterTurbo\mpt_poll.py
import requests, json, sys, time

BASE = "http://localhost:8090/api/v1"
task_id = sys.argv[1]
max_wait_s = 1200  # 20 分钟
interval_s = 5

t0 = time.time()
while time.time() - t0 < max_wait_s:
    r = requests.get(f"{BASE}/tasks/{task_id}", timeout=30)
    r.raise_for_status()
    data = r.json().get("data", {})
    state = data.get("state")
    progress = data.get("progress", 0)
    elapsed = int(time.time() - t0)
    print(f"[{elapsed:>4}s] state={state} progress={progress}%")

    if state == 1:
        print("\n✅ 完成！videos:")
        print(json.dumps(data.get("videos", []), ensure_ascii=False, indent=2))
        sys.exit(0)
    if state == -1:
        print("\n❌ 失败：")
        print(json.dumps(data, ensure_ascii=False, indent=2))
        sys.exit(1)

    time.sleep(interval_s)

print("\n⏱️ 超时（20 分钟），自行登录 Web UI 检查：http://localhost:8501")
sys.exit(2)
```

调用：

```powershell
python E:\AILife\MoneyPrinterTurbo\mpt_poll.py <task_id>
```

---

## 调用模板 5 · 直接打开成片

```powershell
# 用默认播放器打开
start E:\AILife\MoneyPrinterTurbo\storage\tasks\<task_id>\final-1.mp4

# 或直接在浏览器流式播放
start http://localhost:8090/api/v1/stream/tasks/<task_id>/final-1.mp4
```

---

## 调用模板 6 · 一行命令重启 MPT API（troubleshoot 用）

```powershell
cd E:\AILife\MoneyPrinterTurbo
docker compose restart api
```

或完全重启：

```powershell
cd E:\AILife\MoneyPrinterTurbo
docker compose down
docker compose up -d
```

---

## TTS 语音 Voice Name 速查

```
中文女声：
  zh-CN-XiaoxiaoNeural-Female  ← 默认推荐，温暖自然
  zh-CN-XiaoyiNeural-Female    亲切朋友
  zh-CN-YunyangNeural-Female   新闻播报感

中文男声：
  zh-CN-YunxiNeural-Male       ← 默认推荐，磁性
  zh-CN-YunjianNeural-Male     稳重商务
  zh-CN-YunyiNeural-Male       温和讲解

英文女声：
  en-US-AriaNeural-Female      ← 默认推荐
  en-US-JennyNeural-Female     亲和力
  en-US-MichelleNeural-Female  播报感

英文男声：
  en-US-GuyNeural-Male         ← 默认推荐
  en-US-DavisNeural-Male       商务
  en-US-TonyNeural-Male        年轻有活力
```

完整 200+ 语音见 [edge-tts voices](https://github.com/rany2/edge-tts)。

---

## 字段速查表

| 字段 | 类型 | 取值 / 说明 |
|---|---|---|
| `video_aspect` | string | `"9:16"` / `"16:9"` / `"1:1"` |
| `video_source` | string | `"pexels"` / `"pixabay"`（不要用） / `"local"` |
| `video_count` | int | 一次生成几条（默认 1） |
| `video_clip_duration` | int | 单段素材时长，建议 3-5 秒 |
| `paragraph_number` | int | 文案段落数：1=30s 内 / 2=60s 内 / 3=>60s |
| `subtitle_position` | string | `"bottom"` / `"top"` / `"center"` |
| `font_size` | int | 60 = 9:16 默认；横屏建议 50 |
| `bgm_type` | string | `"random"` / `"none"` / 文件名 |
| `bgm_volume` | float | 0.0-1.0，默认 0.2（人声-12dB 时合适） |
