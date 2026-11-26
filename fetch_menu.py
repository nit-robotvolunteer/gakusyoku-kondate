import json
import os
import tempfile
from datetime import datetime, timedelta, timezone

import requests
from google import genai
from google.genai import types
from pypdf import PdfReader

PAGE_URL = "https://nitsc.co.jp/cafeteria/"
CAFETERIAS = [
    {
        "id": "cafeteria1",
        "name": "ダイニングホール",
        "source_url": f"{PAGE_URL}#cafeteria1",
        "image_url": "https://nitsc.co.jp/userdata/1menu.png",
    },
    {
        "id": "cafeteria2",
        "name": "キッチン＆カフェトレビ",
        "source_url": f"{PAGE_URL}#cafeteria2",
        "image_url": "https://nitsc.co.jp/userdata/2menu.png",
    },
]

DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

JST = timezone(timedelta(hours=+9), "JST")
WEEKDAY_MAP = {0: "月", 1: "火", 2: "水", 3: "木", 4: "金", 5: "土", 6: "日"}
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )
}


def extract_pdf_text_if_available(pdf_bytes: bytes) -> str:
    """PDFに埋め込みテキストがある場合に抽出する"""
    try:
        with tempfile.NamedTemporaryFile(suffix=".pdf") as tmp:
            tmp.write(pdf_bytes)
            tmp.flush()
            reader = PdfReader(tmp.name)
            texts = [page.extract_text() or "" for page in reader.pages]
            return "\n".join(texts).strip()
    except Exception:
        return ""


def parse_menu_with_gemini(caf_name: str, file_bytes: bytes, mime_type: str, today_key: str) -> dict:
    """Gemini APIで献立表画像から曜日別・今日のメニューを抽出する"""
    if not GEMINI_API_KEY:
        return {}

    client = genai.Client(api_key=GEMINI_API_KEY)
    prompt = f"""
あなたは学食の献立表を読み取るアシスタントです。
対象食堂: {caf_name}
添付された献立表画像から、曜日ごと（月〜金、必要なら土）のメニュー名を抽出してください。
休売・休業などの記載がある場合はそれも簡潔に含めてください。
余計な解説は省き、次のJSON形式のみで出力してください。

{{
  "weekly": {{
    "月": ["メニュー1", "メニュー2"],
    "火": ["メニュー1", "メニュー2"],
    "水": ["メニュー1", "メニュー2"],
    "木": ["メニュー1", "メニュー2"],
    "金": ["メニュー1", "メニュー2"]
  }}
}}
"""
    parts = [
        types.Part.from_bytes(data=file_bytes, mime_type=mime_type),
        prompt,
    ]

    if mime_type == "application/pdf":
        raw_text = extract_pdf_text_if_available(file_bytes)
        if len(raw_text) > 30:
            parts.append(f"\n参考テキスト:\n{raw_text[:8000]}")

    try:
        response = client.models.generate_content(
            model="gemini-3.8-flash",
            contents=parts,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                temperature=0.1,
            ),
        )
        parsed = json.loads(response.text)
        weekly = parsed.get("weekly") if isinstance(parsed, dict) else {}
        if not isinstance(weekly, dict):
            weekly = {}

        today_items = weekly.get(today_key, [])
        if not isinstance(today_items, list):
            today_items = []

        cleaned_items = [str(item).strip() for item in today_items if str(item).strip()]
        print(f"[INFO] {caf_name} ({today_key}曜) 抽出件数: {len(cleaned_items)}")

        return {
            "today_label": today_key,
            "today_items": cleaned_items,
            "weekly": weekly,
        }
    except Exception as exc:
        print(f"[WARN] Gemini抽出失敗 ({caf_name}): {exc}")
        return {}


def fetch_all_cafeterias(today_key: str) -> list[dict]:
    """各食堂の献立画像を取得し、Geminiで今日の献立テキストを付与する"""
    results: list[dict] = []

    for base in CAFETERIAS:
        img_url = base["image_url"]
        extracted: dict = {}

        try:
            r = requests.get(img_url, headers=HEADERS, timeout=25)
            r.raise_for_status()
            ctype = r.headers.get("Content-Type", "image/png").split(";")[0].strip()
            if not ctype.startswith("image/"):
                ctype = "image/png"

            if GEMINI_API_KEY:
                extracted = parse_menu_with_gemini(base["name"], r.content, ctype, today_key)
            else:
                print("[INFO] GEMINI_API_KEY 未設定のため文字抽出をスキップします。")
        except Exception as exc:
            print(f"[WARN] 画像取得失敗 ({base['name']} / {img_url}): {exc}")

        results.append({
            "id": base["id"],
            "name": base["name"],
            "source_url": base["source_url"],
            "links": [],
            "images": [{"alt": f"{base['name']} 今週の献立表", "url": img_url}],
            "menu_parsed": extracted,
        })

    return results


def format_today_description(caf: dict) -> str | None:
    """Discord Embedのdescription用に今日の献立テキストを整形する"""
    parsed = caf.get("menu_parsed") or {}
    items = parsed.get("today_items") or []
    if not items:
        return None

    day_label = parsed.get("today_label")
    header = f"**{day_label}曜日の献立**" if day_label else "**今日の献立**"
    lines = [f"・{item}" for item in items]
    body = f"{header}\n" + "\n".join(lines)
    return body if len(body) <= 3900 else body[:3897] + "..."


def build_discord_embed(caf: dict, updated_at: str, today_str: str) -> dict:
    """各食堂のDiscord Embedオブジェクトを組み立てる"""
    embed = {
        "title": f"🍽️ {today_str} {caf['name']} 今週の献立",
        "url": caf["source_url"],
        "color": 0x0F766E if caf["id"] == "cafeteria1" else 0x2563EB,
        "fields": [],
        "footer": {"text": f"最終更新: {updated_at}"},
    }

    today_desc = format_today_description(caf)
    if today_desc:
        embed["description"] = today_desc
    else:
        embed["description"] = "本日の献立テキストを抽出できませんでした（休止日または画像未反映の可能性あり）。"

    return embed


def send_discord(data: dict) -> None:
    """Discord Webhookへ献立Embedを送信する"""
    if not DISCORD_WEBHOOK_URL:
        print("[INFO] DISCORD_WEBHOOK_URL 未設定のため通知をスキップします。")
        return

    now_jst = datetime.now(JST)
    today_str = f"{now_jst.strftime('%m/%d')} ({WEEKDAY_MAP[now_jst.weekday()]})"
    embeds = [
        build_discord_embed(caf, data["updated_at"], today_str)
        for caf in data.get("cafeterias", [])
    ]

    res = requests.post(
        DISCORD_WEBHOOK_URL,
        json={"embeds": embeds[:10]},
        timeout=15,
    )
    res.raise_for_status()
    print("[INFO] Discord Webhook 通知を送信しました。")


def main() -> None:
    now_jst = datetime.now(JST)
    today_key = WEEKDAY_MAP[now_jst.weekday()]

    cafeterias = fetch_all_cafeterias(today_key)

    payload = {
        "updated_at": now_jst.strftime("%Y-%m-%d %H:%M"),
        "source_url": PAGE_URL,
        "cafeterias": cafeterias,
    }

    with open("menu.json", "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    print("[INFO] menu.json を更新しました。")

    send_discord(payload)


if __name__ == "__main__":
    main()
