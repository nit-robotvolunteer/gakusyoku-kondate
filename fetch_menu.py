import json
import os
import re
import tempfile
from datetime import datetime, timedelta, timezone

import requests
from bs4 import BeautifulSoup
from google import genai
from google.genai import types
from pypdf import PdfReader

TARGET_URL = "https://nitsc.co.jp/cafeteria/"
SECTION_IDS = ["cafeteria1", "cafeteria2"]
FALLBACK_NAMES = {
    "cafeteria1": "ダイニングホール",
    "cafeteria2": "キッチン＆カフェトレビ",
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


def extract_section_title(section, sec_id: str) -> str:
    """セクション内の見出し（h1〜h4）から食堂名を取得し、無ければ固定名を返す"""
    if section:
        heading = section.find(["h1", "h2", "h3", "h4"])
        if heading:
            text = re.sub(r"\s+", " ", heading.get_text(strip=True))
            if text:
                return text
    return FALLBACK_NAMES.get(sec_id, f"食堂 ({sec_id})")


def parse_single_cafeteria(soup: BeautifulSoup, sec_id: str) -> dict:
    """指定セクションIDの献立リンク・画像URLを抽出する"""
    section = soup.find(id=sec_id)
    if not section:
        anchor = soup.find(attrs={"name": sec_id})
        section = anchor.parent if anchor else None

    title = extract_section_title(section, sec_id)
    links = []
    images = []
    seen_links = set()
    seen_images = set()

    if section:
        for a in section.find_all("a", href=True):
            href = requests.compat.urljoin(TARGET_URL, a["href"])
            text = re.sub(r"\s+", " ", a.get_text(strip=True))
            if any(k in (text + href).lower() for k in ["献立", "メニュー", "menu", "kondate", ".pdf"]):
                if href not in seen_links:
                    seen_links.add(href)
                    links.append({
                        "title": text or f"{title} 今週の献立表",
                        "url": href,
                    })

        for img in section.find_all("img", src=True):
            src = requests.compat.urljoin(TARGET_URL, img["src"])
            alt = re.sub(r"\s+", " ", img.get("alt", ""))
            if any(k in (alt + src).lower() for k in ["献立", "menu", "kondate", "lunch", "food", "trevi"]):
                if src not in seen_images:
                    seen_images.add(src)
                    images.append({
                        "alt": alt or title,
                        "url": src,
                    })

    return {
        "id": sec_id,
        "name": title,
        "source_url": f"{TARGET_URL}#{sec_id}",
        "links": links,
        "images": images,
        "menu_parsed": {},
    }


def fetch_cafeteria_sections() -> list[dict]:
    """対象ページを取得して各食堂セクションをパースする"""
    res = requests.get(TARGET_URL, headers=HEADERS, timeout=20)
    res.raise_for_status()
    res.encoding = res.apparent_encoding
    soup = BeautifulSoup(res.text, "html.parser")
    return [parse_single_cafeteria(soup, sec_id) for sec_id in SECTION_IDS]


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
    """Gemini APIで献立表（PDF/画像）から曜日別・今日のメニューを抽出する"""
    if not GEMINI_API_KEY:
        return {}

    client = genai.Client(api_key=GEMINI_API_KEY)
    prompt = f"""
あなたは学食の献立表を読み取るアシスタントです。
対象食堂: {caf_name}
添付された献立表（PDFまたは画像）から、曜日ごと（月〜金、必要なら土）のメニュー名を抽出してください。
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
            model="gemini-2.5-flash",
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

        return {
            "today_label": today_key,
            "today_items": [str(item).strip() for item in today_items if str(item).strip()],
            "weekly": weekly,
        }
    except Exception as exc:
        print(f"[WARN] Gemini抽出失敗 ({caf_name}): {exc}")
        return {}


def attach_extracted_menus(cafeterias: list[dict], today_key: str) -> list[dict]:
    """各食堂のPDFまたは画像を取得し、今日の献立テキストを付与する"""
    if not GEMINI_API_KEY:
        print("[INFO] GEMINI_API_KEY 未設定のため文字抽出をスキップします。")
        return cafeterias

    for caf in cafeterias:
        extracted = {}

        for link in caf.get("links", []):
            url = link.get("url", "")
            if url.lower().endswith(".pdf"):
                try:
                    r = requests.get(url, headers=HEADERS, timeout=25)
                    r.raise_for_status()
                    extracted = parse_menu_with_gemini(caf["name"], r.content, "application/pdf", today_key)
                    if extracted.get("weekly"):
                        break
                except Exception as exc:
                    print(f"[WARN] PDF取得失敗 ({caf['name']}): {exc}")

        if not extracted.get("weekly") and caf.get("images"):
            img_url = caf["images"][0]["url"]
            try:
                r = requests.get(img_url, headers=HEADERS, timeout=25)
                r.raise_for_status()
                ctype = r.headers.get("Content-Type", "image/jpeg").split(";")[0].strip()
                if not ctype.startswith("image/"):
                    ctype = "image/png" if img_url.lower().endswith(".png") else "image/jpeg"
                extracted = parse_menu_with_gemini(caf["name"], r.content, ctype, today_key)
            except Exception as exc:
                print(f"[WARN] 画像取得失敗 ({caf['name']}): {exc}")

        caf["menu_parsed"] = extracted

    return cafeterias


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

    for link in caf.get("links", []):
        embed["fields"].append({
            "name": link["title"][:250],
            "value": f"[献立表を開く]({link['url']})"[:1000],
            "inline": False,
        })

    if not today_desc and not caf.get("links") and not caf.get("images"):
        embed["description"] = f"献立リンク・画像が未検出です。[ページを確認]({caf['source_url']})"

    if caf.get("images"):
        embed["image"] = {"url": caf["images"][0]["url"]}

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

    cafeterias = fetch_cafeteria_sections()
    cafeterias = attach_extracted_menus(cafeterias, today_key)

    payload = {
        "updated_at": now_jst.strftime("%Y-%m-%d %H:%M"),
        "source_url": TARGET_URL,
        "cafeterias": cafeterias,
    }

    with open("menu.json", "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    print("[INFO] menu.json を更新しました。")

    send_discord(payload)


if __name__ == "__main__":
    main()
