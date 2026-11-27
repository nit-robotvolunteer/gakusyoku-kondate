"use strict";

const DAY_KEYS = ["月", "火", "水", "木", "金", "土"];
const ALLERGENS = ["卵", "乳", "小麦", "えび", "かに", "そば", "落花生"];
const STORE_KEY = "kondate.avoid";

const state = {
  data: null,
  day: null,
  week: "this", // "this"（今週）か "next"（来週）
  avoid: new Set(loadAvoid()),
};

function loadAvoid() {
  try {
    return JSON.parse(localStorage.getItem(STORE_KEY) || "[]");
  } catch {
    return [];
  }
}
function saveAvoid() {
  try {
    localStorage.setItem(STORE_KEY, JSON.stringify([...state.avoid]));
  } catch {
    /* 保存できない環境では何もしない */
  }
}

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") node.className = v;
    else if (k === "text") node.textContent = v;
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else node.setAttribute(k, v === true ? "" : v);
  }
  for (const c of children.flat()) {
    if (c === null || c === undefined || c === false) continue;
    node.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return node;
}

function todayKey() {
  const idx = new Date().getDay(); // 0=日
  return idx === 0 ? null : DAY_KEYS[idx - 1];
}

function hasNextWeek() {
  return state.data.cafeterias.some((c) => c.next_week && c.next_week.days.length);
}

// 選択中の週のデータ（今週は食堂データそのもの、来週は next_week）
function weekOf(caf) {
  return state.week === "next" ? caf.next_week : caf;
}

function dateFor(key) {
  // 表が古いままの食堂より、正しい週のデータを持つ食堂の日付を優先する
  const cafs = [...state.data.cafeterias].sort((a, b) => (a.week_status === "stale") - (b.week_status === "stale"));
  for (const caf of cafs) {
    const w = weekOf(caf);
    const d = ((w && w.days) || []).find((x) => x.key === key);
    if (d && d.date) return d.date;
  }
  return "";
}

/* ---------- 曜日タブ ---------- */
function renderDayTabs() {
  const wrap = document.getElementById("day-tabs");
  wrap.replaceChildren();
  const tk = state.week === "this" ? todayKey() : null;
  DAY_KEYS.forEach((key) => {
    const selected = key === state.day;
    const date = dateFor(key);
    const btn = el(
      "button",
      {
        type: "button",
        role: "tab",
        class: "day-tab" + (key === tk ? " is-today" : ""),
        "aria-selected": selected ? "true" : "false",
        tabindex: selected ? "0" : "-1",
        "data-key": key,
        onclick: () => selectDay(key, true),
        onkeydown: onTabKey,
      },
      el("span", { class: "day-tab-key", text: key }),
      el("span", { class: "day-tab-date", text: date ? date.split("/")[1] : "" }),
      key === tk ? el("span", { class: "day-tab-today", text: "今日" }) : null
    );
    btn.setAttribute("aria-label", `${date || ""}（${key}）${key === tk ? " 今日" : ""}`);
    wrap.append(btn);
  });
}

function onTabKey(e) {
  const i = DAY_KEYS.indexOf(state.day);
  let next = null;
  if (e.key === "ArrowRight") next = DAY_KEYS[(i + 1) % DAY_KEYS.length];
  if (e.key === "ArrowLeft") next = DAY_KEYS[(i - 1 + DAY_KEYS.length) % DAY_KEYS.length];
  if (e.key === "Home") next = DAY_KEYS[0];
  if (e.key === "End") next = DAY_KEYS[DAY_KEYS.length - 1];
  if (!next) return;
  e.preventDefault();
  selectDay(next, true);
  document.querySelector(`.day-tab[data-key="${next}"]`)?.focus();
}

function selectDay(key, push) {
  state.day = key;
  if (push) {
    const hash = (state.week === "next" ? "来週" : "") + key;
    history.replaceState(null, "", `#${encodeURIComponent(hash)}`);
  }
  renderWeekSwitch();
  renderDayTabs();
  renderCafeterias();
}

/* ---------- 今週／来週 ---------- */
function renderWeekSwitch() {
  const wrap = document.getElementById("week-switch");
  if (!hasNextWeek()) {
    wrap.hidden = true;
    return;
  }
  wrap.hidden = false;
  wrap.replaceChildren(
    ...[
      ["this", "今週"],
      ["next", "来週"],
    ].map(([w, label]) =>
      el("button", {
        type: "button",
        class: "week-btn",
        "aria-pressed": state.week === w ? "true" : "false",
        text: label,
        onclick: () => {
          if (state.week === w) return;
          state.week = w;
          // 来週に切り替えたら月曜から、今週に戻したら今日から
          selectDay(w === "next" ? "月" : todayKey() || "月", true);
        },
      })
    )
  );
}

/* ---------- 食券カード ---------- */
// 縦書きの半券では半角英数字が横倒しになるので全角にする（「週替わりパスタA」→「週替わりパスタＡ」）
function toFullWidth(text) {
  return text.replace(/[A-Za-z0-9]/g, (c) => String.fromCharCode(c.charCodeAt(0) + 0xfee0));
}

function ticket(sec, cafId) {
  const hits = sec.allergens.filter((a) => state.avoid.has(a));
  const off = Boolean(sec.status);
  const cls = ["ticket", off ? "is-off" : "", hits.length ? "is-avoided" : ""].join(" ").trim();

  const stub = el("div", { class: "ticket-stub", "aria-hidden": "true" }, el("span", { text: toFullWidth(sec.category) }));

  const body = el("div", { class: "ticket-body" });
  body.append(el("p", { class: "ticket-cat", text: sec.category }));

  if (off) {
    body.append(el("p", { class: "ticket-off", text: sec.status }));
  } else {
    const [main, ...sides] = sec.items;
    body.append(el("p", { class: "ticket-main", text: main }));
    if (sides.length) {
      body.append(el("ul", { class: "ticket-sides" }, sides.map((s) => el("li", { text: s }))));
    }
    sec.notes.forEach((n) => body.append(el("p", { class: "ticket-note", text: n })));

    const meta = el("div", { class: "ticket-meta" });
    if (sec.kcal) meta.append(el("span", { class: "kcal" }, el("b", { text: sec.kcal.toLocaleString("ja-JP") }), " kcal"));
    if (sec.allergens.length) {
      meta.append(
        el(
          "span",
          { class: "allergens", "aria-label": `アレルゲン: ${sec.allergens.join("、")}` },
          sec.allergens.map((a) => el("span", { class: "allergen" + (state.avoid.has(a) ? " is-hit" : ""), text: a }))
        )
      );
    }
    if (meta.childNodes.length) body.append(meta);
    if (hits.length) body.append(el("p", { class: "ticket-warn", text: `${hits.join("・")}を含みます` }));
  }

  return el("article", { class: cls, "data-caf": cafId }, stub, body);
}

function cafeteriaBlock(caf) {
  const w = weekOf(caf) || { days: [], weekly: [] };
  const day = (w.days || []).find((d) => d.key === state.day);
  const secs = [...(day ? day.sections : []), ...(w.weekly || [])].filter(
    (s) => s.items.length || s.status || s.notes.length
  );
  const serving = secs.filter((s) => s.items.length);
  const off = secs.filter((s) => !s.items.length);

  const head = el(
    "header",
    { class: "caf-head" },
    el("h2", { class: "caf-name", text: caf.name }),
    el("a", {
      class: "caf-source",
      href: caf.image_url,
      target: "_blank",
      rel: "noopener",
      // 公開中の献立表がすでに来週分なら、リンク名もそれに合わせる
      text: caf.next_week && caf.next_week.image_hash && caf.next_week.image_hash !== caf.image_hash ? "来週の献立" : "今週の献立",
    })
  );

  const block = el("section", { class: "caf", "data-caf": caf.id, "aria-label": caf.name }, head);

  if ((caf.closed_days || []).includes(state.day)) {
    block.append(el("p", { class: "caf-empty", text: `${state.day}曜日は休業日です。` }));
    return block;
  }

  if (!w.days.length) {
    const text =
      state.week === "next"
        ? "来週の献立表はまだ掲載されていません。"
        : caf.week_status === "missing"
          ? "公開中の献立表がすでに来週分のため、今週の献立を表示できません。「来週」から来週分を見られます。"
          : caf.week_status === "pending"
            ? "今週の献立はまだ取得できていません。毎朝8時ごろに更新されます。"
            : "今週の献立を取得できませんでした。";
    block.append(el("p", { class: "caf-empty", text }));
    return block;
  }

  if (w.method && w.method !== "gemini") {
    block.append(
      el(
        "p",
        { class: "caf-caution" },
        "簡易読み取りのため、料理名に誤字があるかもしれません。",
        el("a", { href: caf.image_url, target: "_blank", rel: "noopener", text: "献立表で確認する" })
      )
    );
  }
  if (state.week === "this" && caf.week_status === "stale" && state.day === todayKey()) {
    block.append(el("p", { class: "caf-caution", text: "献立表が今週分に更新されていない可能性があります。" }));
  }

  if (!serving.length && !off.length) {
    block.append(
      el(
        "p",
        { class: "caf-empty" },
        state.day === "土" ? "この日の提供情報はありません。" : "この日のメニューを読み取れませんでした。",
        " ",
        el("a", { href: caf.image_url, target: "_blank", rel: "noopener", text: "献立表を開く" })
      )
    );
    return block;
  }

  const list = el("div", { class: "tickets" });
  serving.forEach((s) => list.append(ticket(s, caf.id)));
  block.append(list);

  if (off.length) {
    const byStatus = new Map();
    off.forEach((s) => {
      const k = s.status || "お知らせあり";
      byStatus.set(k, [...(byStatus.get(k) || []), s.category]);
    });
    const text = [...byStatus].map(([st, cats]) => `${cats.join("・")}は${st}`).join("、");
    block.append(el("p", { class: "caf-closed", text: `${text}です。` }));
  }
  return block;
}

function renderCafeterias() {
  const app = document.getElementById("app");
  app.replaceChildren(...state.data.cafeterias.map(cafeteriaBlock));
}

/* ---------- アレルゲン絞り込み ---------- */
function renderFilter() {
  const opts = document.getElementById("allergen-options");
  opts.replaceChildren(
    ...ALLERGENS.map((a) => {
      const id = `avoid-${a}`;
      return el(
        "label",
        { class: "chip", for: id },
        el("input", {
          type: "checkbox",
          id,
          checked: state.avoid.has(a),
          onchange: (e) => {
            e.target.checked ? state.avoid.add(a) : state.avoid.delete(a);
            saveAvoid();
            updateFilterCount();
            renderCafeterias();
          },
        }),
        el("span", { text: a })
      );
    })
  );
  updateFilterCount();
  if (state.avoid.size) document.getElementById("allergen-filter").open = true;
}
function updateFilterCount() {
  document.getElementById("filter-count").textContent = state.avoid.size ? `（${state.avoid.size}件）` : "";
}

/* ---------- 起動 ---------- */
function showNotice(text) {
  const n = document.getElementById("notice");
  n.textContent = text;
  n.hidden = false;
}

function normalize(data) {
  // 旧形式(menu_parsed)のデータが来た場合も落ちないようにする
  const normWeek = (w) => ({
    ...w,
    days: (w.days || []).map((d) => ({ ...d, sections: (d.sections || []).map(normSec) })),
    weekly: (w.weekly || []).map(normSec),
  });
  const thisMon = mondayISO(new Date());
  const ranThisWeek = (data.updated_at || "").slice(0, 10) >= thisMon;
  data.cafeterias = (data.cafeterias || []).map((c) => assignWeeks(normWeek(c), c.next_week ? normWeek(c.next_week) : null, thisMon, ranThisWeek));
  return data;
}

const WEEK_FIELDS = ["week_start", "image_hash", "method", "model", "parsed_at", "days", "weekly"];

// ブラウザの今日の日付を基準に「今週」「来週」を振り分け直す。
// （月曜の朝、その週の自動更新が走る前に開いても、先週分を今週として見せないため）
function assignWeeks(cur, next, thisMon, ranThisWeek) {
  const slots = [cur, next].filter((s) => s && s.days.length);
  if (!slots.some((s) => s.week_start)) return { ...cur, next_week: next }; // 古い形式のデータ
  const meta = { ...cur };
  WEEK_FIELDS.forEach((k) => delete meta[k]);
  const empty = { days: [], weekly: [] };
  const thisWeek = slots.find((s) => s.week_start === thisMon);
  const nextWeek = slots.find((s) => s.week_start && s.week_start > thisMon) || null;
  if (thisWeek) return { ...meta, ...thisWeek, week_status: "ok", next_week: nextWeek };
  const older = slots.find((s) => s.week_start && s.week_start < thisMon);
  if (nextWeek) return { ...meta, ...empty, week_status: "missing", next_week: nextWeek };
  if (older && ranThisWeek) return { ...meta, ...older, week_status: "stale", next_week: null };
  // 今週の自動更新がまだ（月曜の早朝など）
  return { ...meta, ...empty, week_status: "pending", next_week: null };
}

function mondayISO(d) {
  const m = new Date(d.getFullYear(), d.getMonth(), d.getDate() - ((d.getDay() + 6) % 7));
  const pad = (n) => String(n).padStart(2, "0");
  return `${m.getFullYear()}-${pad(m.getMonth() + 1)}-${pad(m.getDate())}`;
}

function normSec(s) {
  return {
    category: s.category || "メニュー",
    items: s.items || [],
    status: s.status || null,
    notes: s.notes || [],
    kcal: s.kcal || null,
    allergens: s.allergens || [],
  };
}

async function init() {
  const updated = document.getElementById("updated-at");
  try {
    const res = await fetch(`./menu.json?t=${Date.now()}`);
    if (!res.ok) throw new Error(`menu.json を取得できませんでした（${res.status}）`);
    state.data = normalize(await res.json());
    if (!state.data.cafeterias.some((c) => c.days.length || (c.next_week && c.next_week.days.length))) {
      throw new Error("メニューのデータがまだありません。次の自動更新をお待ちください。");
    }

    const [d, t] = (state.data.updated_at || "").split(" ");
    updated.textContent = d ? `${d.slice(5).replace("-", "/")} ${t || ""} 更新` : "";

    let fromHash = decodeURIComponent(location.hash.slice(1));
    if (fromHash.startsWith("来週") && hasNextWeek()) {
      state.week = "next";
      fromHash = fromHash.slice(2);
    }
    const tk = todayKey();
    if (DAY_KEYS.includes(fromHash)) {
      state.day = fromHash;
    } else if (tk) {
      state.day = tk;
    } else if (hasNextWeek()) {
      state.week = "next";
      state.day = "月";
      showNotice("今日は日曜日です。来週月曜日のメニューを表示しています。");
    } else {
      state.day = "月";
      showNotice("今日は日曜日です。今週月曜日のメニューを表示しています。");
    }

    renderFilter();
    selectDay(state.day, false);
  } catch (err) {
    updated.textContent = "";
    document.getElementById("app").replaceChildren(
      el(
        "div",
        { class: "load-error" },
        el("p", { text: err.message }),
        el("a", { href: "https://nitsc.co.jp/cafeteria/", target: "_blank", rel: "noopener", text: "公式の献立表を開く" })
      )
    );
  }
}

document.addEventListener("DOMContentLoaded", init);
