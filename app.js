let menuData = null;
let activeFilter = "all"; // "all" | "cafeteria1" | "cafeteria2"

function renderTabs(cafeterias) {
  const tabsEl = document.getElementById("tabs");
  tabsEl.innerHTML = "";

  const defs = [
    { id: "all", label: "すべて表示" },
    ...cafeterias.map((c) => ({ id: c.id, label: c.name }))
  ];

  defs.forEach((def) => {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = `tab-btn ${activeFilter === def.id ? "is-active" : ""}`;
    btn.textContent = def.label;
    btn.addEventListener("click", () => {
      activeFilter = def.id;
      renderTabs(cafeterias);
      renderCafeterias();
    });
    tabsEl.appendChild(btn);
  });
}

function createTodayMenuCard(caf) {
  const parsed = caf.menu_parsed || {};
  const todayItems = Array.isArray(parsed.today_items) ? parsed.today_items : [];
  const weekly = parsed.weekly && typeof parsed.weekly === "object" ? parsed.weekly : null;
  const dayLabel = parsed.today_label ? `${parsed.today_label}曜日の献立` : "今日の献立";

  const card = document.createElement("article");
  card.className = "card today-card";

  const heading = document.createElement("h3");
  heading.textContent = dayLabel;
  card.appendChild(heading);

  if (todayItems.length > 0) {
    const ul = document.createElement("ul");
    ul.className = "today-list";
    todayItems.forEach((item) => {
      const li = document.createElement("li");
      li.textContent = item;
      ul.appendChild(li);
    });
    card.appendChild(ul);
  } else {
    const empty = document.createElement("p");
    empty.className = "muted-text";
    empty.textContent = "今日の抽出メニューはありません（下の献立表または公式ページをご確認ください）。";
    card.appendChild(empty);
  }

  if (weekly && Object.keys(weekly).length > 0) {
    const details = document.createElement("details");
    details.className = "weekly-details";

    const summary = document.createElement("summary");
    summary.textContent = "今週の献立（曜日別）を見る";
    details.appendChild(summary);

    const weekList = document.createElement("div");
    weekList.className = "weekly-grid";

    Object.entries(weekly).forEach(([day, items]) => {
      const row = document.createElement("div");
      row.className = "weekly-row";

      const dayEl = document.createElement("strong");
      dayEl.textContent = `${day}曜`;

      const menuText = document.createElement("span");
      menuText.textContent = Array.isArray(items) && items.length > 0 ? items.join(" / ") : "—";

      row.appendChild(dayEl);
      row.appendChild(menuText);
      weekList.appendChild(row);
    });

    details.appendChild(weekList);
    card.appendChild(details);
  }

  return card;
}

function createCafeteriaElement(caf) {
  const block = document.createElement("section");
  block.className = "cafeteria-block";

  const heading = document.createElement("div");
  heading.className = "cafeteria-heading";

  const h2 = document.createElement("h2");
  h2.textContent = caf.name;

  const srcLink = document.createElement("a");
  srcLink.className = "source-link";
  srcLink.href = caf.source_url;
  srcLink.target = "_blank";
  srcLink.rel = "noopener";
  srcLink.textContent = "公式ページへ";

  heading.appendChild(h2);
  heading.appendChild(srcLink);
  block.appendChild(heading);

  block.appendChild(createTodayMenuCard(caf));

  if (Array.isArray(caf.images) && caf.images.length > 0) {
    caf.images.forEach((img) => {
      const card = document.createElement("article");
      card.className = "card";

      const title = document.createElement("h3");
      title.textContent = img.alt || `${caf.name} 献立表`;

      const imageEl = document.createElement("img");
      imageEl.src = img.url;
      imageEl.alt = img.alt || caf.name;
      imageEl.className = "menu-img";
      imageEl.loading = "lazy";

      card.appendChild(title);
      card.appendChild(imageEl);
      block.appendChild(card);
    });
  }

  if (Array.isArray(caf.links) && caf.links.length > 0) {
    caf.links.forEach((link) => {
      const card = document.createElement("article");
      card.className = "card";

      const title = document.createElement("h3");
      title.textContent = link.title;

      const viewerUrl = `https://docs.google.com/gview?embedded=true&url=${encodeURIComponent(link.url)}`;
      const iframe = document.createElement("iframe");
      iframe.src = viewerUrl;
      iframe.className = "pdf-frame";
      iframe.title = link.title;

      const openBtn = document.createElement("a");
      openBtn.href = link.url;
      openBtn.target = "_blank";
      openBtn.rel = "noopener";
      openBtn.className = "btn";
      openBtn.textContent = "元ファイルを開く";

      card.appendChild(title);
      card.appendChild(iframe);
      card.appendChild(openBtn);
      block.appendChild(card);
    });
  }

  return block;
}

function renderCafeterias() {
  const app = document.getElementById("app");
  app.innerHTML = "";

  const targets = activeFilter === "all"
    ? menuData.cafeterias
    : menuData.cafeterias.filter((c) => c.id === activeFilter);

  targets.forEach((caf) => {
    app.appendChild(createCafeteriaElement(caf));
  });
}

async function init() {
  const app = document.getElementById("app");
  const updatedEl = document.getElementById("updated-at");

  try {
    const res = await fetch(`./menu.json?t=${Date.now()}`);
    if (!res.ok) throw new Error("データの取得に失敗しました");
    menuData = await res.json();

    updatedEl.textContent = `更新: ${menuData.updated_at}`;
    renderTabs(menuData.cafeterias || []);
    renderCafeterias();
  } catch (err) {
    updatedEl.textContent = "エラー";
    app.innerHTML = `<div class="card"><p>${err.message}</p></div>`;
  }
}

document.addEventListener("DOMContentLoaded", init);