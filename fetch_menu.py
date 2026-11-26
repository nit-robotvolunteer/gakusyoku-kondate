import io
import json
import os
import time
from datetime import datetime, timedelta, timezone

import requests
from google import genai
from google.genai import types
from PIL import Image, ImageOps
from rapidocr_onnxruntime import RapidOCR

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

DAY_X_RATIOS = {
    "月": (0.14, 0.30),
    "火": (0.29, 0.45),
    "水": (0.44, 0.60),
    "木": (0.59, 0.75),
    "金": (0.74, 0.90),
    "土": (0.88, 1.00),
}
Y_RATIO = (0.15, 0.96)

# ノイズになりやすい栄養表記などの除外ワード（必要に応じて調整）
NOISE_EXACT = {"アレルゲン", "カロリー", "蛋白質", "脂質", "塩分", "kcal", "g"}

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


def prepare_day_crop_bytes(img_bytes: bytes, day_key: str) -> bytes:
    """元画像から今日の曜日列を切り抜き、OCR向けに前処理したPNGバイト列を返す"""
    with Image.open(io.BytesIO(img_bytes)) as im:
        im.load()
        w, h = im.size
        x0_r, x1_r = DAY_X_RATIOS.get(day_key, (0.14, 0.95))
        y0_r, y1_r = Y_RATIO

        box = (
            max(0, int(w * x0_r)),
            max(0, int(h * y0_r)),
            min(w, int(w * x1_r)),
            min(h, int(h * y1_r)),
        )
        cropped = im.crop(box)
        enlarged = cropped.resize((cropped.width * 2, cropped.height * 2), Image.Resampling.LANCZOS)
        gray = ImageOps.grayscale(enlarged)
        enhanced = ImageOps.autocontrast(gray)

        out = io.BytesIO()
        enhanced.save(out, format="PNG")
        return out.getvalue()


def clean_ocr_lines(raw_lines: list[str]) -> list[str]:
    """OCRの行リストから明らかに栄養ラベルだけの行を軽く除外する"""
    cleaned: list[str] = []
    for line in raw_lines:
        s = line.strip()
        if not s:
            continue
        if s in NOISE_EXACT:
            continue
        if s.endswith("カロリー") or s.endswith("蛋白質") or s.endswith("脂質") or s.endswith("塩分"):
            continue
        cleaned.append(s)
    return cleaned


def parse_menu_with_local_ocr(ocr_engine: RapidOCR, caf_name: str, crop_png_bytes: bytes, today_key: str) -> list[str]:
    """ローカルOCRで切り抜き画像から今日のメニュー行を抽出する"""
    try:
        with Image.open(io.BytesIO(crop_png_bytes)) as im:
            import numpy as np
            arr = np.array(im)
        result, _ = ocr_engine(arr)
        if not result:
            return []
        raw_lines = [str(item[1]).strip() for item in result if item and len(item) >= 2 and str(item[1]).strip()]
        items = clean_ocr_lines(raw_lines)
        print(f"[INFO] ローカルOCR {caf_name} ({today_key}曜) 抽出件数: {len(items)}")
        return items
    except Exception as exc:
        print(f"[WARN] ローカルOCR失敗 ({caf_name}): {exc}")
        return []


def parse_menu_with_gemini(caf_name: str, file_bytes: bytes, mime_type: str, today_key: str) -> list[str]:
    """保険用：Gemini APIで献立表画像から今日のメニューを抽出する"""
    if not GEMINI_API_KEY:
        return []

    client = genai.Client(api_key=GEMINI_API_KEY)
    prompt = f"""
あなたは学食の献立表を読み取るアシスタントです。
対象食堂: {caf_name}
添付された献立表から、"{today_key}"曜日のメニュー名を抽出してください。
休売・休業などの記載がある場合はそれも簡潔に含めてください。
余計な解説は省き、次のJSON形式のみで出力してください。

{{
  "today_items": ["メニュー1", "メニュー2"]
}}
"""
    parts = [
        types.Part.from_bytes(data=file_bytes, mime_type=mime_type),
        prompt,
    ]

    models_to_try = ["gemini-3.8-flash", "gemini-3.8-flash-lite"]
    last_exc = None

    for model_name in models_to_try:
        for attempt in range(3):
            try:
                response = client.models.generate_content(
                    model=model_name,
                    contents=parts,
                    config=types.GenerateContentConfig(
                        response_mime_type="application/json",
                        temperature=0.1,
                    ),
                )
                parsed = json.loads(response.text)
                items = parsed.get("today_items") if isinstance(parsed, dict) else []
                if not isinstance(items, list):
                    items = []
                cleaned = [str(item).strip() for item in items if str(item).strip()]
                print(f"[INFO] Gemini保険 {caf_name} ({today_key}曜) 抽出件数: {len(cleaned)} [model={model_name}]")
                return cleaned
            except Exception as exc:
                last_exc = exc
                msg = str(exc)
                is_transient = ("503" in msg) or ("UNAVAILABLE" in msg) or ("429" in msg)
                if is_transient and attempt < 2:
                    wait_sec = 2 * (2 ** attempt)
                    print(f"[INFO] {caf_name} ({model_name}) 一時エラーのため {wait_sec}s 待機して再試行します...")
                    time.sleep(wait_sec)
                    continue
                break

    print(f"[WARN] Gemini保険抽出失敗 ({caf_name}): {last_exc}")
    return []


def fetch_all_cafeterias(today_key: str) -> list[dict]:
    """各食堂の献立画像を取得し、ローカルOCR（＋必要時Gemini保険）で今日の献立を付与する"""
    results: list[dict] = []
    ocr_engine = RapidOCR()

    for base in CAFETERIAS:
        img_url = base["image_url"]
        today_items: list[str] = []
        source_method = "none"

        try:
            r = requests.get(img_url, headers=HEADERS, timeout=25)
            r.raise_for_status()
            img_bytes = r.content

            crop_bytes = prepare_day_crop_bytes(img_bytes, today_key)
            ocr_items = parse_menu_with_local_ocr(ocr_engine, base["name"], crop_bytes, today_key)

            if len(ocr_items) >= 3:
                today_items = ocr_items
                source_method = "local_ocr"
            else:
                # ローカルOCRで十分に取れなかった場合のみGeminiへフォールバック
                gem_items = parse_menu_with_gemini(base["name"], crop_bytes, "image/png", today_key)
                if gem_items:
                    today_items = gem_items
                    source_method = "gemini_fallback"
                elif ocr_items:
                    today_items = ocr_items
                    source_method = "local_ocr_partial"

        except Exception as exc:
            print(f"[WARN] 画像取得失敗 ({base['name']} / {img_url}): {exc}")

        results.append({
            "id": base["id"],
            "name": base["name"],
            "source_url": base["source_url"],
            "links": [],
            "images": [{"alt": f"{base['name']} 今週の献立表", "url": img_url}],
            "menu_parsed": {
                "today_label": today_key,
                "today_items": today_items,
                "method": source_method,
            },
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
