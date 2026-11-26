import io
import json
import os
import re
import time
from datetime import datetime, timedelta, timezone

import numpy as np
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
        "categories": ["スペシャルランチ", "日替わりランチ", "日工大ランチ"],
    },
    {
        "id": "cafeteria2",
        "name": "キッチン＆カフェトレビ",
        "source_url": f"{PAGE_URL}#cafeteria2",
        "image_url": "https://nitsc.co.jp/userdata/2menu.png",
        "categories": [],
    },
]

# 曜日ごとの横方向クロップ比率（x_start, x_end）
DAY_X_RATIOS = {
    "月": (0.14, 0.30),
    "火": (0.29, 0.45),
    "水": (0.44, 0.60),
    "木": (0.59, 0.75),
    "金": (0.74, 0.90),
    "土": (0.88, 1.00),
}
Y_RATIO = (0.15, 0.96)

# ローカルOCRのよくある誤読補正辞書
REPLACE_MAP = {
    "加以重": "カツ重",
    "黒胡椒加弓": "黒胡椒からあげ",
    "温宝": "温玉",
    "琉球木一": "琉球ポーク",
    "力口[kcal]": "",
    "力ロー[kcal]": "",
    "蛋白質[g]": "",
    "脂質[g]": "",
    "塩分[g]": "",
}

NOISE_EXACT = {
    "アレルゲン",
    "カロリー",
    "蛋白質",
    "脂質",
    "塩分",
    "kcal",
    "g",
    "分",
    "幼千",
    "乳小麦",
    "用乳小",
    "図乳小",
}

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
        enlarged = cropped.resize(
            (cropped.width * 2, cropped.height * 2),
            Image.Resampling.LANCZOS,
        )
        gray = ImageOps.grayscale(enlarged)
        enhanced = ImageOps.autocontrast(gray)

        out = io.BytesIO()
        enhanced.save(out, format="PNG")
        return out.getvalue()


def clean_ocr_lines(raw_lines: list[str]) -> list[str]:
    """OCRの行リストから数値・栄養表記・アレルゲン系ノイズを削り、よく出る誤字を補正する"""
    cleaned: list[str] = []
    for line in raw_lines:
        s = line.strip()
        if not s:
            continue

        for bad, good in REPLACE_MAP.items():
            s = s.replace(bad, good).strip()

        if not s or s in NOISE_EXACT:
            continue

        if any(k in s for k in ["kcal", "蛋白質", "脂質", "塩分", "アレルゲン", "乳小"]):
            continue

        if re.fullmatch(r"[\[\]().\-+0-9,.\s]+g?", s):
            continue
        if s in {"[", "]", "(", ")", "[g]", "g]"}:
            continue

        cleaned.append(s)
    return cleaned


def run_local_ocr(ocr_engine: RapidOCR, caf_name: str, crop_png_bytes: bytes, today_key: str) -> list[str]:
    """ローカルOCRで切り抜き画像から今日のメニュー行を抽出する"""
    try:
        with Image.open(io.BytesIO(crop_png_bytes)) as im:
            arr = np.array(im)
        result, _ = ocr_engine(arr)
        if not result:
            return []
        raw_lines = [
            str(item[1]).strip()
            for item in result
            if item and len(item) >= 2 and str(item[1]).strip()
        ]
        items = clean_ocr_lines(raw_lines)
        print(f"[INFO] ローカルOCR {caf_name} ({today_key}曜) 抽出件数: {len(items)}")
        return items
    except Exception as exc:
        print(f"[WARN] ローカルOCR失敗 ({caf_name}): {exc}")
        return []


def format_menu_with_gemini(
    caf: dict,
    crop_png_bytes: bytes,
    ocr_lines: list[str],
    today_key: str,
) -> list[dict]:
    """切り抜き画像＋OCR下書きテキストをGeminiに渡し、誤字補正と区分ごとの構造化を行う"""
    if not GEMINI_API_KEY:
        return []

    caf_name = caf["name"]
    categories = caf.get("categories") or []
    cat_hint = (
        f"想定されるメニュー区分（上から順）: {', '.join(categories)}"
        if categories
        else "区分見出しがあれば区分ごとに、無ければ「本日のメニュー」という1グループにまとめてください。"
    )
    ocr_hint = "\n".join(f"- {x}" for x in ocr_lines) if ocr_lines else "(なし)"

    client = genai.Client(api_key=GEMINI_API_KEY)
    prompt = f"""
あなたは大学の学食献立表を見やすく整理するアシスタントです。
対象食堂: {caf_name}（{today_key}曜日）
{cat_hint}

添付画像は献立表から{today_key}曜日の列を切り抜いたものです。
参考としてローカルOCRの抽出結果も載せますが、誤字や改行の分断が含まれています。
画像を見て正しい日本語の料理名に補正し、見やすいグループ形式でJSON出力してください。

ルール:
- カロリー・蛋白質・脂質・塩分・アレルゲン表記（卵・乳・小麦など）は除外する
- 1つの定食・セット内の主菜と副菜は「／」でつなぐか、わかりやすい単位にまとめる（例: 「桐生ソースカツ重／温玉／サラダ」）
- 「休売」「休業」などの記載がある場合はそのまま簡潔に入れる
- 余計な説明文は書かず、以下のJSON形式のみを出力する

{{
  "sections": [
    {{
      "category": "スペシャルランチ",
      "items": ["桐生ソースカツ重／温玉／サラダ"]
    }}
  ]
}}

参考OCRテキスト:
{ocr_hint}
"""

    parts = [
        types.Part.from_bytes(data=crop_png_bytes, mime_type="image/png"),
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
                sections = parsed.get("sections") if isinstance(parsed, dict) else []
                if not isinstance(sections, list):
                    sections = []

                normalized: list[dict] = []
                for sec in sections:
                    if not isinstance(sec, dict):
                        continue
                    cat = str(sec.get("category", "")).strip() or "本日のメニュー"
                    raw_items = sec.get("items", [])
                    if not isinstance(raw_items, list):
                        continue
                    clean_items = [str(i).strip() for i in raw_items if str(i).strip()]
                    if clean_items:
                        normalized.append({"category": cat, "items": clean_items})

                if normalized:
                    print(f"[INFO] Gemini整形成功 {caf_name} ({today_key}曜) [model={model_name}]")
                    return normalized
            except Exception as exc:
                last_exc = exc
                msg = str(exc)
                is_transient = ("503" in msg) or ("UNAVAILABLE" in msg) or ("429" in msg)
                if is_transient and attempt < 2:
                    wait_sec = 2 * (2 ** attempt)
                    print(f"[INFO] {caf_name} ({model_name}) 一時エラーのため {wait_sec}s 待機して再試行...")
                    time.sleep(wait_sec)
                    continue
                break

    print(f"[WARN] Gemini整形スキップ ({caf_name}): {last_exc}")
    return []


def fetch_all_cafeterias(today_key: str) -> list[dict]:
    """各食堂の献立画像を取得し、ローカルOCR＋Gemini整形で構造化データを生成する"""
    results: list[dict] = []
    ocr_engine = RapidOCR()

    for base in CAFETERIAS:
        img_url = base["image_url"]
        sections: list[dict] = []
        fallback_items: list[str] = []
        source_method = "none"

        try:
            r = requests.get(img_url, headers=HEADERS, timeout=25)
            r.raise_for_status()
            img_bytes = r.content

            crop_bytes = prepare_day_crop_bytes(img_bytes, today_key)
            ocr_lines = run_local_ocr(ocr_engine, base["name"], crop_bytes, today_key)

            # 1. Geminiで切り抜き画像＋OCRテキストを自然なメニュー表に整形（503時はリトライ）
            formatted_sections = format_menu_with_gemini(base, crop_bytes, ocr_lines, today_key)
            if formatted_sections:
                sections = formatted_sections
                source_method = "ocr_plus_gemini"
            elif ocr_lines:
                # 2. Geminiが503等で落ちた場合のフォールバック（ローカルOCRのノイズ除去済み行）
                fallback_items = ocr_lines
                sections = [{"category": "抽出メニュー（簡易OCR）", "items": ocr_lines}]
                source_method = "local_ocr_only"

        except Exception as exc:
            print(f"[WARN] 画像取得失敗 ({base['name']} / {img_url}): {exc}")

        results.append({
            "id": base["id"],
            "name": base["name"],
            "source_url": base["source_url"],
            "image_url": img_url,
            "menu_parsed": {
                "today_label": today_key,
                "sections": sections,
                "fallback_items": fallback_items,
                "method": source_method,
            },
        })

    return results


def build_discord_embed(caf: dict, updated_at: str, today_str: str) -> dict:
    """各食堂のDiscord Embedオブジェクトを視認性高く組み立てる"""
    parsed = caf.get("menu_parsed") or {}
    sections = parsed.get("sections") or []
    img_url = caf.get("image_url", "")

    embed = {
        "title": f"🍽️ {today_str} {caf['name']}",
        "url": img_url or caf["source_url"],
        "color": 0x0F766E if caf["id"] == "cafeteria1" else 0x2563EB,
        "fields": [],
        "footer": {"text": f"最終更新: {updated_at} ｜ タイトルクリックで献立画像を表示"},
    }

    if not sections:
        embed["description"] = "本日の献立を抽出できませんでした（休止日または画像未更新の可能性があります）。"
        return embed

    # 区分ごとにDiscord EmbedのFieldで見やすく表示
    for sec in sections:
        cat = sec.get("category", "メニュー")
        items = sec.get("items", [])
        if not items:
            continue
        value_lines = [f"・{item}" for item in items]
        value_text = "\n".join(value_lines)
        embed["fields"].append({
            "name": f"▸ {cat}"[:250],
            "value": value_text[:1000],
            "inline": False,
        })

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
