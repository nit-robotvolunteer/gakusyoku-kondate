"""学食献立の取得・構造化・通知スクリプト。

処理の流れ
  1. 献立画像を取得し SHA-256 を計算
  2. 罫線からメニューセルを検出（menu_grid.detect_grid）
  3. 画像が前回と同じで、前回 Gemini 解析に成功していれば → キャッシュをそのまま使う
     （献立表は週1回しか変わらないので、Gemini 呼び出しは実質週1回）
  4. 新しい画像なら画像全体を Gemini に渡し「1週間分」を構造化 JSON で取得
  5. Gemini が使えないときはセル単位の簡易 OCR ＋ 料理名辞書補正にフォールバック
  6. menu.json を書き出し、Discord に今日の分を通知
"""
from __future__ import annotations

import argparse
import difflib
import hashlib
import io
import json
import os
import re
import time
from datetime import date, datetime, timedelta, timezone

import requests
from PIL import Image

from menu_grid import DAY_KEYS, Cell, MenuGrid, cell_image_for_ocr, detect_grid, stitch_cells

PARSER_VERSION = 3  # 週データの形式を変えたら上げる（キャッシュ無効化）
PAGE_URL = "https://nitsc.co.jp/cafeteria/"
CAFETERIAS = [
    {
        "id": "cafeteria1",
        "name": "ダイニングホール",
        "short": "ダイニング",
        "source_url": f"{PAGE_URL}#cafeteria1",
        "image_url": "https://nitsc.co.jp/userdata/1menu.png",
        "local_name": "1menu.png",
        "color": 0x0F766E,
        "categories": ["スペシャルランチ", "日替わりランチ", "日工大ランチ"],
        "closed_days": [],
        "extra_categories": [],
    },
    {
        "id": "cafeteria2",
        "name": "キッチン＆カフェ トレビ",
        "short": "トレビ",
        "source_url": f"{PAGE_URL}#cafeteria2",
        "image_url": "https://nitsc.co.jp/userdata/2menu.png",
        "local_name": "2menu.png",
        "color": 0x2563EB,
        "categories": ["日替わりランチ（並）", "鉄板ランチ"],
        "closed_days": ["土"],  # 土曜は休業
        "extra_categories": ["週替わりパスタA", "週替わりパスタB", "週替わりパスタ夏季"],
    },
]

# 改行・カンマ・空白区切りで複数指定できる（テスト用と本番用の両方に送る等）
DISCORD_WEBHOOK_URLS = [u for u in re.split(r"[\s,]+", os.environ.get("DISCORD_WEBHOOK_URL") or "") if u]
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
# 公開ページ（GitHub Pages 等）のURL。設定すると通知にリンクが付く
SITE_URL = os.environ.get("SITE_URL", "").strip()
# 先頭から順に試す。404（廃止）は即座に次へ、503/429 のみ1回だけ短く再試行
GEMINI_MODELS = [
    m.strip()
    for m in (
        os.environ.get("GEMINI_MODELS") or "gemini-3.5-flash-lite,gemini-3.8-flash,gemini-3.5-flash,gemini-flash-latest"
    ).split(",")
    if m.strip()
]
GEMINI_TIMEOUT_MS = int(os.environ.get("GEMINI_TIMEOUT_MS") or "45000")
# 1食堂あたりGeminiに使ってよい合計秒数。超えたら即OCRへ切り替えてワークフローを速く終わらせる
GEMINI_BUDGET_S = float(os.environ.get("GEMINI_BUDGET_S") or "90")
# "fallback"（既定）: OCRフォールバック時のみ原本の欄を添付 / "always" / "never"
DISCORD_ATTACH = os.environ.get("DISCORD_ATTACH_IMAGE") or "fallback"

JST = timezone(timedelta(hours=+9), "JST")
WEEKDAY_MAP = {0: "月", 1: "火", 2: "水", 3: "木", 4: "金", 5: "土", 6: "日"}
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )
}
DISHES_PATH = "dishes.json"
MENU_PATH = "menu.json"

STATUS_RE = re.compile(r"^(休売|休止|休み|休業|定休日?|販売なし)$")
NOTE_RE = re.compile(r"(致します|いたします|より販売|出来ません|できません|ございます|ください|下さい|予定)")


# ---------------------------------------------------------------------------
# 共通: セクションの正規化
# ---------------------------------------------------------------------------
def empty_section(category: str) -> dict:
    return {"category": category, "items": [], "status": None, "notes": [], "kcal": None, "allergens": []}


def normalize_section(sec: dict, category: str) -> dict:
    out = empty_section(category)
    items = [re.sub(r"\s+", " ", str(i)).strip() for i in (sec.get("items") or []) if str(i).strip()]
    notes = [str(n).strip() for n in (sec.get("notes") or []) if str(n).strip()]
    status = (sec.get("status") or "").strip() or None

    kept = []
    for it in items:
        if STATUS_RE.match(it):
            status = status or it
        elif NOTE_RE.search(it):
            notes.append(it)
        else:
            kept.append(it)
    out["items"] = kept
    out["notes"] = notes
    out["status"] = status if not kept else None

    kcal = sec.get("kcal")
    try:
        kcal = int(str(kcal).replace(",", "")) if kcal not in (None, "", 0) else None
    except ValueError:
        kcal = None
    out["kcal"] = kcal if kcal and 100 <= kcal <= 3000 else None
    out["allergens"] = [str(a).strip() for a in (sec.get("allergens") or []) if str(a).strip()]
    return out


def norm_date(s: str) -> str:
    m = re.search(r"(\d{1,2})\s*[/月]\s*(\d{1,2})", s or "")
    return f"{int(m.group(1))}/{int(m.group(2))}" if m else ""


def week_dates(today: date) -> dict[str, str]:
    monday = today - timedelta(days=today.weekday())
    return {k: f"{(monday + timedelta(days=i)).month}/{(monday + timedelta(days=i)).day}" for i, k in enumerate(DAY_KEYS)}


# ---------------------------------------------------------------------------
# Gemini: 画像全体 → 1週間分の構造化
# ---------------------------------------------------------------------------
def build_gemini_prompt(caf: dict) -> str:
    cats = "、".join(caf["categories"])
    extra = (
        f"\n- 表の下に曜日と無関係の枠（{'、'.join(caf['extra_categories'])}）がある。これは \"weekly\" に入れる。"
        if caf["extra_categories"]
        else "\n- \"weekly\" は空配列にする。"
    )
    return f"""これは大学食堂「{caf['name']}」の1週間分の献立表（フォントは創英角ポップ体）です。
表を正確に読み取り、指定のJSONだけを出力してください。

表の構造:
- 列: 月〜土の6日分。最上段に「11月23日(月)」のような日付がある。
- 行（区分）: 上から {cats}。左端に縦書きで区分名がある。{extra}
- 各欄は「料理名（太字）」→「アレルゲン：（赤字）」→「カロリー・蛋白質・脂質・塩分」の順。

出力ルール:
- days は月〜土の6要素。key は "月"〜"土"、date は "11/26" の形式。
- sections は区分ごと（上記の順）。category は上記の区分名をそのまま使う。
- items: 料理を1品ずつ。主菜を先頭、副菜（温玉・サラダ等）を後ろに。
  改行で折り返されているだけの料理名は1品につなげる（例: 「ジャンダレチキン」「ステーキ」→「ジャンダレチキンステーキ」、「極太つけ麺」「魚介とんこつ（麺400g）」→「極太つけ麺 魚介とんこつ（麺400g）」）。
  「＆パスタ」「＆ポテト」のような付け合わせは直前の料理名に含める。
- status: 「休売」「休止」「休み」などと書かれている欄はその語を入れ、items は空配列。
- notes: 「11時半より販売致します」「大盛は出来ません」のような注意書きは items でなく notes へ。
- kcal: カロリー欄の整数（空欄なら null）。allergens: 赤字のアレルゲン（例 ["卵","乳","小麦"]、無ければ []）。
- 欄が完全に空白なら items=[], status=null。
- 読めない文字を推測で埋めない。漢字・カタカナは日本語の表記で（簡体字にしない）。
"""


_SECTION_SCHEMA = {
    "type": "object",
    "properties": {
        "category": {"type": "string"},
        "items": {"type": "array", "items": {"type": "string"}},
        "status": {"type": "string", "description": "休売・休止・休み など。無ければ空文字"},
        "notes": {"type": "array", "items": {"type": "string"}},
        "kcal": {"type": "integer", "description": "空欄なら 0"},
        "allergens": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["category", "items", "status", "notes", "kcal", "allergens"],
}
GEMINI_SCHEMA = {
    "type": "object",
    "properties": {
        "days": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "key": {"type": "string", "enum": DAY_KEYS},
                    "date": {"type": "string"},
                    "sections": {"type": "array", "items": _SECTION_SCHEMA},
                },
                "required": ["key", "date", "sections"],
            },
        },
        "weekly": {"type": "array", "items": _SECTION_SCHEMA},
    },
    "required": ["days", "weekly"],
}


def parse_with_gemini(caf: dict, img_bytes: bytes) -> tuple[dict | None, str]:
    """成功時 (parsed_dict, model_name)、失敗時 (None, 理由)"""
    if not GEMINI_API_KEY:
        return None, "GEMINI_API_KEY 未設定"
    try:
        from google import genai
        from google.genai import types
    except ImportError as exc:
        return None, f"google-genai 未インストール: {exc}"

    # gemini-flash-latest などの別名は v1beta にしか無いので明示する
    client = genai.Client(
        api_key=GEMINI_API_KEY,
        http_options=types.HttpOptions(api_version="v1beta", timeout=GEMINI_TIMEOUT_MS),
    )
    parts = [types.Part.from_bytes(data=img_bytes, mime_type="image/png"), build_gemini_prompt(caf)]
    def make_config(with_schema: bool):
        kw = {"response_mime_type": "application/json"}
        if with_schema:
            kw["response_json_schema"] = GEMINI_SCHEMA
        # 表の書き起こしに長い思考は不要。思考を減らすと 504（時間切れ）が起きにくく速い
        for thinking in (
            lambda: types.ThinkingConfig(thinking_level="low"),
            lambda: types.ThinkingConfig(thinking_budget=512),
            None,
        ):
            try:
                if thinking is None:
                    return types.GenerateContentConfig(**kw)
                return types.GenerateContentConfig(thinking_config=thinking(), **kw)
            except Exception:
                continue
        return None

    configs = [c for c in (make_config(True), make_config(False)) if c is not None]
    started = time.time()

    reasons = []
    for model in GEMINI_MODELS:
        cfg_i, attempt = 0, 0
        while cfg_i < len(configs):
            if time.time() - started > GEMINI_BUDGET_S:
                reasons.append(f"時間切れ（{GEMINI_BUDGET_S:.0f}秒）")
                return None, " / ".join(reasons)
            t0 = time.time()
            try:
                resp = client.models.generate_content(model=model, contents=parts, config=configs[cfg_i])
                data = json.loads(resp.text)
                if not isinstance(data, dict) or not isinstance(data.get("days"), list) or not data["days"]:
                    raise ValueError("JSONに days がありません")
                print(f"[INFO] Gemini成功 {caf['name']} model={model} ({time.time() - t0:.1f}s)")
                return data, model
            except Exception as exc:
                msg = str(exc)
                short = msg.splitlines()[0][:160] if msg else type(exc).__name__
                print(f"[WARN] Gemini失敗 model={model}: {short}")
                # thinking_level 非対応モデルは 400 になるので、思考設定なしで作り直して再試行
                if "thinking" in msg.lower() and "400" in msg:
                    configs = [types.GenerateContentConfig(**{k: v for k, v in c.model_dump(exclude_none=True).items() if k != "thinking_config"}) for c in configs]
                    continue
                transient = any(k in msg for k in ("503", "UNAVAILABLE", "429", "RESOURCE_EXHAUSTED"))
                if transient and attempt == 0:
                    attempt += 1
                    time.sleep(2)
                    continue
                if ("400" in msg or "INVALID_ARGUMENT" in msg or isinstance(exc, ValueError)) and cfg_i + 1 < len(configs):
                    cfg_i += 1  # スキーマ非対応などはスキーマ無しで1回だけ再試行
                    continue
                reasons.append(f"{model}: {short}")
                break  # 404（廃止・存在しない）などは即座に次のモデルへ
    return None, " / ".join(reasons)


def assemble_from_gemini(caf: dict, data: dict, grid: MenuGrid, today: date) -> dict:
    fallback_dates = week_dates(today)
    by_key = {str(d.get("key")): d for d in data.get("days", []) if isinstance(d, dict)}
    days = []
    for ri_day, key in enumerate(DAY_KEYS):
        src = by_key.get(key, {})
        src_secs = [s for s in (src.get("sections") or []) if isinstance(s, dict)]
        secs = []
        for ri, cat in enumerate(caf["categories"]):
            match = next((s for s in src_secs if str(s.get("category", "")).strip() == cat), None)
            if match is None and ri < len(src_secs):
                match = src_secs[ri]
            sec = normalize_section(match or {}, cat)
            # 罫線検出で「完全な空欄」と分かっている欄は、AIの出力より優先して空にする（幻覚対策）
            if grid.ok and ri < len(grid.rows) and grid.rows[ri][key].is_blank:
                sec = empty_section(cat)
            secs.append(sec)
        date_str = norm_date(str(src.get("date") or "")) or fallback_dates[key]
        days.append({"key": key, "date": date_str, "sections": secs})

    weekly_src = [s for s in (data.get("weekly") or []) if isinstance(s, dict)]
    weekly = []
    for i, cat in enumerate(caf["extra_categories"]):
        match = next((s for s in weekly_src if str(s.get("category", "")).strip() == cat), None)
        if match is None and i < len(weekly_src):
            match = weekly_src[i]
        sec = normalize_section(match or {}, cat)
        if i < len(grid.extras) and grid.extras[i].is_blank:
            sec = empty_section(cat)
        weekly.append(sec)
    return {"days": days, "weekly": weekly}


# ---------------------------------------------------------------------------
# 簡易OCR（フォールバック）
# ---------------------------------------------------------------------------
# 簡体字モデル由来の字形を日本語に戻す（文脈に依らず安全なものだけ）
SIMPLIFIED_TO_JA = str.maketrans({
    "黑": "黒", "贩": "販", "壳": "売", "卖": "売", "调": "週", "鸡": "鶏", "鱼": "魚", "烧": "焼",
    "风": "風", "盐": "塩", "汤": "湯", "饺": "餃", "杂": "雑", "酱": "醤", "肠": "腸", "猪": "豚",
    "饭": "飯", "锅": "鍋", "铁": "鉄", "边": "辺", "时": "時", "图": "図",
})
REPLACE_MAP = {
    "加以重": "カツ重", "黒胡椒加弓": "黒胡椒からあげ", "琉球木一": "琉球ポーク", "人定食": "おでん定食", "温宝": "温玉", "甘亏夕": "サラダ", "廿亏夕": "サラダ", "三二小么": "ミニオムレツ",
}


class LocalOCR:
    """rapidocr(v3, PP-OCRv5: 日本語対応) を優先し、使えなければ既定モデル → 旧 rapidocr_onnxruntime の順に使う。

    rapidocr はモデルを初回の読み取り時に読み込むため、設定ミスが後から例外になる。
    read_lines() 側で捕まえて既定モデルに切り替え、ワークフロー全体は止めない。
    """

    def __init__(self) -> None:
        self.engine = None
        self.kind = "none"
        self._factory = None
        try:
            from rapidocr import RapidOCR  # type: ignore

            def v5():
                import rapidocr as ro  # type: ignore
                from rapidocr.utils import typings as ty  # type: ignore

                LangDet = getattr(ro, "LangDet", None) or ty.LangDet
                LangRec = getattr(ro, "LangRec", None) or ty.LangRec
                ModelType = getattr(ro, "ModelType", None) or ty.ModelType
                OCRVersion = getattr(ro, "OCRVersion", None) or ty.OCRVersion

                return RapidOCR(params={
                    "Det.ocr_version": OCRVersion.PPOCRV5,
                    "Det.lang_type": LangDet.CH,
                    "Det.model_type": ModelType.MOBILE,
                    "Rec.ocr_version": OCRVersion.PPOCRV5,
                    "Rec.lang_type": LangRec.CH,  # PP-OCRv5 の ch モデルは日本語も学習済み
                    "Rec.model_type": ModelType.MOBILE,
                })

            self._fallbacks = [("rapidocr-ppocrv5", v5), ("rapidocr-default", lambda: RapidOCR())]
        except ImportError:
            def legacy():
                from rapidocr_onnxruntime import RapidOCR  # type: ignore

                return RapidOCR()

            self._fallbacks = [("rapidocr_onnxruntime", legacy)]
        self._next_engine()

    def _next_engine(self) -> bool:
        while self._fallbacks:
            kind, factory = self._fallbacks.pop(0)
            try:
                self.engine = factory()
                self.kind = kind
                print(f"[INFO] OCRエンジン: {kind}")
                return True
            except Exception as exc:
                print(f"[WARN] OCRエンジン {kind} を初期化できません: {exc}")
        self.engine = None
        self.kind = "none"
        print("[WARN] 使えるOCRエンジンがありません")
        return False

    def _run(self, arr):
        while self.engine is not None:
            try:
                return self.engine(arr)
            except Exception as exc:
                print(f"[WARN] OCRエンジン {self.kind} の実行に失敗、次の候補に切り替えます: {exc}")
                self._next_engine()
        return None

    def read_lines(self, img: Image.Image) -> list[tuple[float, float, float, str, float]]:
        """(x0, y_center, x1, text, score) のリスト"""
        if self.engine is None:
            return []
        import numpy as np

        arr = np.array(img.convert("RGB"))
        res = self._run(arr)
        if res is None:
            return []
        out = []
        if hasattr(res, "txts"):  # rapidocr v2/v3
            boxes, txts, scores = res.boxes, res.txts, res.scores
            if boxes is None or txts is None:
                return []
            triples = zip(boxes, txts, scores)
        else:  # rapidocr_onnxruntime: (result, elapse)
            result = res[0] if isinstance(res, tuple) else res
            triples = ((r[0], r[1], r[2]) for r in (result or []))
        for box, txt, score in triples:
            xs = [p[0] for p in box]
            ys = [p[1] for p in box]
            out.append((min(xs), sum(ys) / len(ys), max(xs), str(txt).strip(), float(score)))
        return out


def join_wrapped_lines(lines: list[tuple[float, float, float, str, float]], cell_w: float) -> list[str]:
    """字下げされた行を前行の続きとみなして連結する（「ジャンダレチキン / 　ステーキ」→1品）"""
    lines = [l for l in lines if l[3] and l[4] >= 0.45]
    if not lines:
        return []
    lines.sort(key=lambda l: l[1])
    left = min(l[0] for l in lines)
    items: list[str] = []
    for x0, _yc, _x1, txt, _s in lines:
        indented = (x0 - left) > cell_w * 0.06
        if items and (indented or txt[:1] in "（(＆&"):
            items[-1] += txt
        else:
            items.append(txt)
    return items


def clean_ocr_text(s: str) -> str:
    s = s.translate(SIMPLIFIED_TO_JA)
    for bad, good in REPLACE_MAP.items():
        s = s.replace(bad, good)
    s = s.replace("(", "（").replace(")", "）").replace("&", "＆")
    return s.strip(" 　・.,")


def load_dish_dictionary() -> list[str]:
    try:
        with open(DISHES_PATH, encoding="utf-8") as f:
            return list(json.load(f).get("dishes", []))
    except Exception:
        return []


def save_dish_dictionary(names: list[str]) -> None:
    current = load_dish_dictionary()
    merged = sorted(set(current) | {n for n in names if 2 <= len(n) <= 40})
    if merged != sorted(current):
        with open(DISHES_PATH, "w", encoding="utf-8") as f:
            json.dump({"dishes": merged}, f, ensure_ascii=False, indent=2)
        print(f"[INFO] 料理名辞書を更新: {len(current)} → {len(merged)} 件")


def correct_with_dictionary(s: str, dictionary: list[str]) -> str:
    """過去にGeminiで正しく読めた料理名と似ていれば、そちらに寄せる"""
    if not dictionary or len(s) < 2:
        return s
    best, best_r = s, 0.0
    for cand in dictionary:
        if abs(len(cand) - len(s)) > max(3, len(cand) // 2):
            continue
        r = difflib.SequenceMatcher(None, s, cand).ratio()
        if r > best_r:
            best, best_r = cand, r
    return best if best_r >= 0.62 else s


def ocr_cell(ocr: LocalOCR, img: Image.Image, cell: Cell, category: str, dictionary: list[str]) -> dict:
    if cell.is_blank:
        return empty_section(category)
    pre = cell_image_for_ocr(img, cell)
    lines = ocr.read_lines(pre)
    raw_items = join_wrapped_lines(lines, pre.width)
    items = []
    for t in raw_items:
        t = clean_ocr_text(t)
        if not t or re.fullmatch(r"[\W\d_a-zA-Z]{1,2}", t):
            continue
        items.append(correct_with_dictionary(t, dictionary))
    return normalize_section({"items": items}, category)


def ocr_header_dates(ocr: LocalOCR, img: Image.Image, grid: MenuGrid) -> dict[str, str]:
    """表の最上段（日付行）をOCRして '11/26' 形式に"""
    out: dict[str, str] = {}
    if not grid.ok:
        return out
    h = grid.height
    for key, cell in grid.rows[0].items():
        box = (cell.x0, max(0, cell.y0 - int(h * 0.034)), cell.x1, cell.y0 - 2)
        crop = img.crop(box).convert("L")
        crop = crop.resize((crop.width * 3, crop.height * 3), Image.Resampling.LANCZOS)
        text = "".join(l[3] for l in ocr.read_lines(crop))
        m = re.search(r"(\d{1,2})\s*月\s*(\d{1,2})\s*日", text)
        if m:
            out[key] = f"{int(m.group(1))}/{int(m.group(2))}"
    return out


def parse_with_local_ocr(caf: dict, img: Image.Image, grid: MenuGrid, ocr: LocalOCR, today: date) -> dict:
    dictionary = load_dish_dictionary()
    dates = {**week_dates(today), **ocr_header_dates(ocr, img, grid)}
    days = []
    for key in DAY_KEYS:
        secs = []
        for ri, cat in enumerate(caf["categories"]):
            if ri < len(grid.rows):
                secs.append(ocr_cell(ocr, img, grid.rows[ri][key], cat, dictionary))
            else:
                secs.append(empty_section(cat))
        days.append({"key": key, "date": dates[key], "sections": secs})
    weekly = [
        ocr_cell(ocr, img, grid.extras[i], cat, dictionary) if i < len(grid.extras) else empty_section(cat)
        for i, cat in enumerate(caf["extra_categories"])
    ]
    return {"days": days, "weekly": weekly}


# ---------------------------------------------------------------------------
# 取得・キャッシュ
# ---------------------------------------------------------------------------
def load_previous(path: str = MENU_PATH) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return {c["id"]: c for c in data.get("cafeterias", []) if c.get("id")}
    except Exception:
        return {}


def get_image_bytes(caf: dict, local_dir: str | None) -> bytes:
    if local_dir:
        with open(os.path.join(local_dir, caf["local_name"]), "rb") as f:
            return f.read()
    r = requests.get(caf["image_url"], headers=HEADERS, timeout=15)
    r.raise_for_status()
    return r.content


WEEK_KEYS = ("week_start", "image_hash", "method", "model", "parser_version", "parsed_at", "days", "weekly")


def monday_of(d: date) -> date:
    return d - timedelta(days=d.weekday())


def infer_week_start(days: list[dict], today: date) -> date | None:
    """各日の 'M/D' から、その表の週の月曜日を求める（年は今日にいちばん近いものを採用）"""
    for i, day in enumerate(days or []):
        m = re.fullmatch(r"(\d{1,2})/(\d{1,2})", str(day.get("date") or ""))
        if not m:
            continue
        mon, dd = int(m.group(1)), int(m.group(2))
        cands = []
        for y in (today.year - 1, today.year, today.year + 1):
            try:
                cands.append(date(y, mon, dd))
            except ValueError:
                pass
        if not cands:
            continue
        d = min(cands, key=lambda c: abs((c - today).days))
        return monday_of(d)
    return None


def week_slot(src: dict | None, today: date) -> dict | None:
    """menu.json の食堂データ（または next_week）から週データだけを取り出す"""
    if not src or not src.get("days"):
        return None
    slot = {k: src.get(k) for k in WEEK_KEYS}
    ws = slot.get("week_start")
    try:
        slot["week_start"] = date.fromisoformat(ws) if ws else infer_week_start(slot["days"], today)
    except ValueError:
        slot["week_start"] = infer_week_start(slot["days"], today)
    return slot


def parse_sheet(caf: dict, today: date, prev_caf: dict, ocr_holder: dict, local_dir: str | None) -> tuple[dict | None, bytes, str | None]:
    """献立表画像を取得・解析して (週データ, 今日の列の原本画像, エラー) を返す。

    週データは画像ハッシュが同じなら前回の結果（今週分・来週分どちらでも）を再利用する。
    """
    try:
        img_bytes = get_image_bytes(caf, local_dir)
    except Exception as exc:
        print(f"[WARN] 画像取得失敗 {caf['name']}: {exc}")
        return None, b"", f"画像取得失敗: {exc}"

    img_hash = hashlib.sha256(img_bytes).hexdigest()
    img = Image.open(io.BytesIO(img_bytes)).convert("RGB")
    grid = detect_grid(img)
    print(f"[INFO] {caf['name']}: 区分行 {len(grid.rows)} / 追加枠 {len(grid.extras)} を検出")

    today_key = WEEKDAY_MAP[today.weekday()]
    today_cells: list[Cell] = []
    if today_key in DAY_KEYS and grid.rows:
        today_cells = [r[today_key] for r in grid.rows] + list(grid.extras[: len(caf["extra_categories"])])
    crop_png = stitch_cells(img, today_cells) if today_cells else b""

    force = bool(os.environ.get("FORCE_REPARSE"))
    cached = None
    for cand in (prev_caf, (prev_caf or {}).get("next_week")):
        if cand and cand.get("image_hash") == img_hash and cand.get("parser_version") == PARSER_VERSION and cand.get("days"):
            cached = week_slot(cand, today)
            break
    if cached and not force and cached.get("method") == "gemini":
        print(f"[INFO] キャッシュ再利用 {caf['name']}（Gemini解析済み・画像変更なし）")
        return cached, crop_png, None

    error = None
    data, info = parse_with_gemini(caf, img_bytes)
    if data:
        parsed = assemble_from_gemini(caf, data, grid, today)
        method, model = "gemini", info
        save_dish_dictionary([i for d in parsed["days"] for s in d["sections"] for i in s["items"]]
                             + [i for s in parsed["weekly"] for i in s["items"]])
    else:
        error = f"Gemini不可: {info}"
        if cached and not force and cached.get("method") == "local_ocr":
            print(f"[INFO] 簡易OCR結果のキャッシュを再利用 {caf['name']}")
            return cached, crop_png, error
        if "ocr" not in ocr_holder:
            ocr_holder["ocr"] = LocalOCR()
        parsed = parse_with_local_ocr(caf, img, grid, ocr_holder["ocr"], today)
        method, model = ("local_ocr" if ocr_holder["ocr"].engine else "none"), None

    sheet = {
        "image_hash": img_hash,
        "method": method,
        "model": model,
        "parser_version": PARSER_VERSION,
        "parsed_at": datetime.now(JST).strftime("%Y-%m-%d %H:%M"),
        "days": parsed["days"],
        "weekly": parsed["weekly"],
    }
    sheet["week_start"] = infer_week_start(sheet["days"], today)
    return sheet, crop_png, error


def place_weeks(caf: dict, sheet: dict | None, crop: bytes, prev_caf: dict, today: date, error: str | None) -> tuple[dict, bytes]:
    """新しく読んだ表を「今週」「来週」に振り分ける。

    金曜などに来週分の表が掲載されても、今週分は前回の結果を使い続ける。
    """
    this_mon = monday_of(today)
    prev_cur = week_slot(prev_caf, today)
    prev_next = week_slot((prev_caf or {}).get("next_week"), today)

    fresh_ws = sheet.get("week_start") if sheet else None
    if sheet and fresh_ws is None:
        sheet["week_start"] = fresh_ws = this_mon  # 日付が読めなかった表は今週分とみなす

    current = nxt = None
    current_is_fresh = False
    # 今週分: 新しい表 → 前回の来週分（月曜に繰り上げ）→ 前回の今週分 の順で探す
    for cand, fresh in ((sheet, True), (prev_next, False), (prev_cur, False)):
        if cand and cand["week_start"] == this_mon:
            current, current_is_fresh = cand, fresh
            break
    # 来週分
    for cand in (sheet, prev_next, prev_cur):
        if cand and cand["week_start"] and cand["week_start"] > this_mon:
            nxt = cand
            break

    status = "ok"
    if current is None:
        older = [c for c in (sheet, prev_cur) if c and c["week_start"] and c["week_start"] < this_mon]
        if older and not nxt:
            current, current_is_fresh = older[0], older[0] is sheet
            status = "stale"  # 献立表が今週分に更新されていない
        else:
            status = "missing"  # 来週分に差し替わっていて今週分が無い
    if status == "ok" and current and sheet and current is not sheet and nxt is sheet:
        print(f"[INFO] {caf['name']}: 来週分の献立表を検出（{fresh_ws}〜）。今週分は前回の結果を使用")

    def dump(slot: dict | None) -> dict | None:
        if not slot:
            return None
        out = dict(slot)
        out["week_start"] = slot["week_start"].isoformat() if slot.get("week_start") else None
        return out

    result = {
        "id": caf["id"],
        "name": caf["name"],
        "short": caf["short"],
        "source_url": caf["source_url"],
        "image_url": caf["image_url"],
        "error": error,
        "closed_days": caf.get("closed_days", []),
        "week_status": status,
        "stale": status == "stale",
        **(dump(current) or {k: None for k in WEEK_KEYS}),
        "next_week": dump(nxt),
    }
    result["days"] = result.get("days") or []
    result["weekly"] = result.get("weekly") or []
    return result, (crop if current_is_fresh else b"")


# ---------------------------------------------------------------------------
# Discord
# ---------------------------------------------------------------------------
WARN_MARK = "\u26a0\ufe0e"  # ⚠︎（絵文字ではなく文字として表示させる）


def format_section_value(sec: dict) -> str:
    if sec["status"]:
        return f"*本日{sec['status']}*" if not sec["status"].startswith("本日") else f"*{sec['status']}*"
    lines = []
    main, *sides = sec["items"]
    lines.append(f"**{main}**")
    if sides:
        lines.append("＋ " + " ・ ".join(sides))
    for n in sec["notes"]:
        lines.append(f"・{n}")
    meta = []
    if sec["kcal"]:
        meta.append(f"`{sec['kcal']:,} kcal`")
    if sec["allergens"]:
        meta.append(f"{WARN_MARK} " + "・".join(sec["allergens"]))
    if meta:
        lines.append(" ".join(meta))
    return "\n".join(lines)[:1024]


def today_sections(caf: dict, today_key: str) -> list[dict]:
    day = next((d for d in caf.get("days") or [] if d["key"] == today_key), None)
    secs = list(day["sections"]) if day else []
    secs += caf.get("weekly") or []
    return [s for s in secs if s["items"] or s["status"]]


def build_discord_payload(data: dict, today: date, crops: dict[str, bytes]) -> tuple[dict, list[tuple[str, bytes]]]:
    today_key = WEEKDAY_MAP[today.weekday()]
    date_label = f"{today.month}/{today.day}（{today_key}）"
    embeds, files = [], []
    for caf in data["cafeterias"]:
        secs = today_sections(caf, today_key)
        has_note_only = [s for s in secs if s["notes"] and not s["items"] and not s["status"]]
        embed = {
            "title": f"{caf['name']}",
            "url": caf["image_url"],
            "color": next((c["color"] for c in CAFETERIAS if c["id"] == caf["id"]), 0x64748B),
            "fields": [],
        }
        if today_key in caf.get("closed_days", []):
            embed["description"] = f"**{date_label}** は休業日です。"
            embed["footer"] = {"text": "タイトルから献立表を確認できます。"}
            embeds.append(embed)
            continue
        desc = [f"**{date_label}** のメニュー"]
        if caf.get("week_status") == "stale":
            desc.append(f"{WARN_MARK} 献立表が今週分に更新されていない可能性があります")
        if caf.get("week_status") == "missing":
            desc.append("献立表がすでに来週分に差し替わっていたため、今日のメニューを取得できませんでした。")
            secs = []
        embed["description"] = "\n".join(desc)

        serving = [s for s in secs if s["items"]]
        if not secs and caf.get("week_status") == "missing":
            pass
        elif not secs:
            if caf.get("method") == "gemini":
                embed["description"] += "\n\n本日の提供情報はありません。"
            else:
                embed["description"] += "\n\nメニューを読み取れませんでした。下の画像（献立表の今日の欄）をご確認ください。"
        for sec in secs:
            if sec in has_note_only:
                continue
            embed["fields"].append({
                "name": f"【{sec['category']}】"[:256],
                "value": format_section_value(sec),
                "inline": False,
            })
        if secs and not serving:
            embed["description"] += "\n\n本日はすべて休止です。"

        method = caf.get("method")
        attach = DISCORD_ATTACH == "always" or (DISCORD_ATTACH == "fallback" and method != "gemini")
        crop = crops.get(caf["id"])
        if attach and crop:
            fname = f"today_{caf['id']}.png"
            embed["image"] = {"url": f"attachment://{fname}"}
            files.append((fname, crop))
        if caf.get("week_status") == "missing":
            # 公開中の献立表はすでに来週分（今週分の読み取り結果が無い）
            footer = "タイトルから来週の献立表を確認できます。献立表を自動で読み取っています。"
        elif method == "gemini":
            footer = "タイトルから献立表を確認できます。献立表を自動で読み取っています。"
        else:
            footer = "タイトルから献立表を確認できます。簡易読み取りのため誤字があるかもしれません（下の画像が原本です）。"
        embed["footer"] = {"text": footer}
        embeds.append(embed)

    payload: dict = {"embeds": embeds[:10]}
    if SITE_URL:
        payload["content"] = f"今週の献立: <{SITE_URL}>"
    return payload, files


def send_discord(payload: dict, files: list[tuple[str, bytes]]) -> None:
    if not DISCORD_WEBHOOK_URLS:
        print("[INFO] DISCORD_WEBHOOK_URL 未設定のため通知をスキップ")
        return
    failed = 0
    for i, url in enumerate(DISCORD_WEBHOOK_URLS, 1):
        try:
            if files:
                multipart = {f"files[{j}]": (name, blob, "image/png") for j, (name, blob) in enumerate(files)}
                res = requests.post(
                    url, data={"payload_json": json.dumps(payload, ensure_ascii=False)}, files=multipart, timeout=20
                )
            else:
                res = requests.post(url, json=payload, timeout=20)
            res.raise_for_status()
            print(f"[INFO] Discord 通知を送信しました（{i}/{len(DISCORD_WEBHOOK_URLS)}）")
        except Exception as exc:
            failed += 1
            # URLはログに出さない（Webhook URLを知られると誰でも投稿できるため）
            body = getattr(getattr(exc, "response", None), "text", "")[:200]
            print(f"[WARN] Discord 送信失敗（{i}/{len(DISCORD_WEBHOOK_URLS)}）: {type(exc).__name__} {body}")
    if failed == len(DISCORD_WEBHOOK_URLS):
        raise RuntimeError("すべての Discord Webhook への送信に失敗しました")


# ---------------------------------------------------------------------------
def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--local-dir", help="画像をURLではなくこのフォルダから読む（1menu.png / 2menu.png）")
    ap.add_argument("--date", help="今日の日付を上書き（YYYY-MM-DD）")
    ap.add_argument("--no-discord", action="store_true")
    ap.add_argument("--discord-preview", help="送信内容をJSONファイルに書き出す")
    ap.add_argument("--save-crops", help="今日の欄の画像を書き出すフォルダ")
    args = ap.parse_args()

    now = datetime.now(JST)
    today = date.fromisoformat(args.date) if args.date else now.date()
    prev = load_previous()
    try:
        with open(MENU_PATH, encoding="utf-8") as f:
            last_notified = json.load(f).get("last_notified")
    except Exception:
        last_notified = None
    # 本番チャンネルへの送信かどうか（ワークフローが prod / test を渡す）
    to_prod = (os.environ.get("NOTIFY_TARGET") or "prod") == "prod"
    is_schedule = os.environ.get("GITHUB_EVENT_NAME") == "schedule"
    ocr_holder: dict = {}

    cafeterias, crops = [], {}
    for caf in CAFETERIAS:
        prev_caf = prev.get(caf["id"]) or {}
        try:
            sheet, crop, err = parse_sheet(caf, today, prev_caf, ocr_holder, args.local_dir)
        except Exception as exc:  # 1食堂の失敗で通知全体を止めない
            import traceback

            traceback.print_exc()
            sheet, crop, err = None, b"", f"処理失敗: {exc}"
        res, crop = place_weeks(caf, sheet, crop, prev_caf, today, err)
        cafeterias.append(res)
        crops[res["id"]] = crop
        if args.save_crops and crop:
            os.makedirs(args.save_crops, exist_ok=True)
            with open(os.path.join(args.save_crops, f"today_{res['id']}.png"), "wb") as f:
                f.write(crop)

    payload = {
        "schema_version": 2,
        "updated_at": now.strftime("%Y-%m-%d %H:%M"),
        "source_url": PAGE_URL,
        "cafeterias": cafeterias,
        "last_notified": last_notified,  # 本番チャンネルに最後に通知した日（二重通知の防止用）
    }

    def write_menu() -> None:
        with open(MENU_PATH, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)

    write_menu()
    print("[INFO] menu.json を更新しました")

    if today.weekday() == 6:
        print("[INFO] 日曜日のため通知しません")
        return
    discord_payload, files = build_discord_payload(payload, today, crops)
    if args.discord_preview:
        with open(args.discord_preview, "w", encoding="utf-8") as f:
            json.dump({"payload": discord_payload, "files": [n for n, _ in files]}, f, ensure_ascii=False, indent=2)
    if args.no_discord:
        return
    # Cloudflare Worker からの起動と GitHub の予備の定期実行が両方動いても、本番への通知は1日1回だけ
    skip_if_notified = is_schedule or bool(os.environ.get("SKIP_IF_NOTIFIED"))
    if skip_if_notified and last_notified == today.isoformat():
        print("[INFO] 今日はすでに本番へ通知済みのため送信しません")
        return
    send_discord(discord_payload, files)
    if to_prod and DISCORD_WEBHOOK_URLS:
        payload["last_notified"] = today.isoformat()
        write_menu()


if __name__ == "__main__":
    main()
