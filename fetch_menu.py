import json
import os
import re
import tempfile
from datetime import datetime, timedelta, timezone
from urllib.parse import urljoin

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
FALLBACK_IMAGE_HINTS = {
    "cafeteria1": ["1menu", "menu1", "dining", "hall"],
    "cafeteria2": ["2menu", "menu2", "trevi", "kitchen"],
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

URL_ATTRS = ["href", "src", "data-src", "data-fancybox-src", "data-href", "data-lazy-src"]


def clean_text(val: str | None) -> str:
    return re.sub(r"\s+", " ", (val or "").strip())


def extract_section_title(section, sec_id: str) -> str:
    """セクション内の見出し（h1〜h4）から食堂名を取得し、無ければ固定名を返す"""
    if section:
        heading = section.find(["h1", "h2", "h3", "h4"])
        if heading:
            text = clean_text(heading.get_text())
            if text:
                return text
    return FALLBACK_NAMES.get(sec_id, f"食堂 ({sec_id})")


def collect_media_from_container(container, default_title: str) -> tuple[list[dict], list[dict]]:
    """指定コンテナ内のリンク・画像候補（data-* 属性含む）を収集する"""
    links: list[dict] = []
    images: list[dict] = []
    seen_links: set[str] = set()
    seen_images: set[str] = set()

    if not container:
        return links, images

    for el in container.find_all(["a", "img", "button", "div", "figure"]):
        label = clean_text(el.get("alt") or el.get("data-caption") or el.get_text())
        for attr in URL_ATTRS:
            raw_val = el.get(attr)
            if not raw_val or not isinstance(raw_val, str):
                continue
            raw_val = raw_val.strip()
            if raw_val.startswith("#") or raw_val.lower().startswith("javascript:"):
                continue

            full_url = urljoin(TARGET_URL, raw_val)
            lower = full_url.lower()
            combined = f"{label} {full_url}".lower()

            if ".pdf" in lower:
                if full_url not in seen_links:
                    seen_links.add(full_url)
                    links.append({
                        "title": label or f"{default_title} 今週の献立表",
                        "url": full_url,
                    })
            elif any(ext in lower for ext in [".png", ".jpg", ".jpeg", ".webp"]) or "userdata/" in lower:
                if any(k in combined for k in ["menu", "kondate", "献立", "userdata", "lunch", "food", "trevi"]):
                    if full_url not in seen_images:
                        seen_images.add(full_url)
                        images.append({
                            "alt": label or default_title,
                            "url": full_url,
                        })

    return links, images


def find_section_element(soup: BeautifulSoup, sec_id: str, index: int):
    """セクション要素をID/アンカー/クラス順で探す"""
    section = soup.find(id=sec_id)
    if section:
        return section

    anchor = soup.find(attrs={"name": sec_id})
    if anchor and anchor.parent:
        return anchor.parent

    caf_blocks = soup.find_all("div", class_=re.compile(r"\bcafeteria\b", re.I))
    if index < len(caf_blocks):
        return caf_blocks[index]

    return None


def parse_single_cafeteria(soup: BeautifulSoup, sec_id: str, index: int) -> dict:
    """指定セクションIDの献立リンク・画像URLを抽出する"""
    section = find_section_element(soup, sec_id, index)
    title = extract_section_title(section, sec_id)

    links, images = collect_media_from_container(section, title)

    # セクション内で画像が見つからない場合、ページ全体から該当食堂らしい画像を補完
    if not images:
        _, page_images = collect_media_from_container(soup.body or soup, title)
        hints = FALLBACK_IMAGE_HINTS.get(sec_id, [])
        matched = [img for img in page_images if any(h in img["url"].lower() for h in hints)]
        if matched:
            images = matched
        elif index < len(page_images):
            images = [page_images[index]]

    # それでも見つからない場合の userdata/1menu.png, 2menu.png フォールバック
    if not images and not links:
        menu_num = index + 1
        fallback_img_url = urljoin(TARGET_URL, f"../userdata/{menu_num}menu.png")
        images.append({
            "alt": f"{title} 献立表",
            "url": fallback_img_url,
        })

    print(f"[INFO] {title}: links={len(links)}, images={[i['url'] for i in images]}")

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
    return [
        parse_single_cafeteria(soup, sec_id, idx)
        for idx, sec_id in enumerate(SECTION_IDS)
    ]


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

        if not extracted.get("weekly"):
            for img in caf.get("images", []):
                img_url = img.get("url", "")
                if not img_url:
                    continue
                try:
                    r = requests.get(img_url, headers=HEADERS, timeout=25)
                    r.raise_for_status()
                    ctype = r.headers.get("Content-Type", "image/jpeg").split(";")[0].strip()
                    if not ctype.startswith("image/"):
                        ctype = "image/png" if ".png" in img_url.lower() else "image/jpeg"
                    extracted = parse_menu_with_gemini(caf["name"], r.content, ctype, today_key)
                    if extracted.get("weekly"):
                        break
                except Exception as exc:
                    print(f"[WARN] 画像取得失敗 ({caf['name']} / {img_url}): {exc}")

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
