const bridge = window.AstrBotPluginPage;

const state = {
  category: null,
  offset: 0,
  limit: 24,
  total: 0,
  items: [],
  categories: [],
  selected: new Set(),
  includeBlocked: true,
};

const $ = (id) => document.getElementById(id);

const el = (tag, attrs = {}, children = []) => {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") node.className = v;
    else if (k === "text") node.textContent = v;
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else node.setAttribute(k, v);
  }
  for (const c of [].concat(children)) if (c) node.appendChild(c);
  return node;
};

/* ---------------------------- 轻量弹窗（iframe 内 prompt/confirm 被沙箱禁用） ---------------------------- */
function modal({ title, fields = [], confirmText = "确定", danger = false }) {
  return new Promise((resolve) => {
    const wrap = el("div", {}, []);
    Object.assign(wrap.style, {
      position: "sticky", bottom: "0", marginTop: "12px", background: "var(--panel)",
      border: "1px solid var(--line)", borderRadius: "10px", padding: "12px", zIndex: 20,
    });
    wrap.appendChild(el("div", { text: title, style: "font-weight:500;margin-bottom:8px" }));

    const inputs = {};
    for (const f of fields) {
      const row = el("div", { style: "display:flex;gap:8px;align-items:center;margin-bottom:6px" });
      row.appendChild(el("label", { text: f.label, style: "min-width:76px;color:var(--muted)" }));
      let input;
      if (f.type === "select") {
        input = el("select", {});
        for (const o of f.options) input.appendChild(el("option", { value: o.value, text: o.label }));
      } else {
        input = el("input", { type: "text", value: f.value || "" });
        Object.assign(input.style, {
          flex: "1", background: "var(--bg)", color: "var(--text)",
          border: "1px solid var(--line)", borderRadius: "8px", padding: "4px 8px",
        });
      }
      inputs[f.name] = input;
      row.appendChild(input);
      wrap.appendChild(row);
    }

    const actions = el("div", { style: "display:flex;gap:8px;justify-content:flex-end;margin-top:8px" });
    const cancel = el("button", { class: "mini ghost", text: "取消" });
    const ok = el("button", { class: `mini${danger ? " danger" : ""}`, text: confirmText });
    cancel.addEventListener("click", () => { wrap.remove(); resolve(null); });
    ok.addEventListener("click", () => {
      const out = {};
      for (const [k, node] of Object.entries(inputs)) out[k] = node.value.trim();
      wrap.remove();
      resolve(out);
    });
    actions.appendChild(cancel);
    actions.appendChild(ok);
    wrap.appendChild(actions);

    document.body.appendChild(wrap);
  });
}

let toastTimer = null;
function toast(msg) {
  const node = $("toast");
  node.textContent = msg;
  node.classList.remove("hidden");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => node.classList.add("hidden"), 2600);
}

function fail(err) {
  toast(`失败：${err && err.message ? err.message : err}`);
}

/* ---------------------------- 数据加载 ---------------------------- */
async function loadOverview() {
  const data = await bridge.apiGet("overview");
  const mb = (data.bytes / 1024 / 1024).toFixed(1);
  $("stats").replaceChildren(
    el("span", { class: "stat" }, [el("span", { text: "图片" }), el("b", { text: String(data.images) })]),
    el("span", { class: "stat" }, [el("span", { text: "已屏蔽" }), el("b", { text: String(data.blocked) })]),
    el("span", { class: "stat" }, [el("span", { text: "分类" }), el("b", { text: String(data.categories) })]),
    el("span", { class: "stat" }, [el("span", { text: "占用" }), el("b", { text: `${mb} MB` })]),
    el("span", { class: "stat" }, [el("span", { text: "黑名单" }), el("b", { text: String(data.blocked_hashes) })]),
  );
}

async function loadCategories(keepSelection = true) {
  const data = await bridge.apiGet("categories");
  state.categories = data.items;
  const list = $("cats");
  list.replaceChildren();

  if (keepSelection && state.category && !data.items.some((c) => c.name === state.category)) {
    state.category = null;
  }
  if (!state.category && data.items.length) state.category = data.items[0].name;

  for (const c of data.items) {
    const rename = el("button", { class: "mini ghost", text: "改名" });
    rename.addEventListener("click", async (e) => {
      e.stopPropagation();
      const res = await modal({
        title: `分类改名：${c.name}`,
        fields: [{ name: "new", label: "新名称", value: c.name }],
      });
      if (!res || !res.new || res.new === c.name) return;
      try {
        const r = await bridge.apiPost("categories/rename", { old: c.name, new: res.new });
        if (state.category === c.name) state.category = r.name;
        toast(`已改名，迁移 ${r.moved} 张`);
        await refresh();
      } catch (err) { fail(err); }
    });

    const del = el("button", { class: "mini ghost danger", text: "删除" });
    del.addEventListener("click", async (e) => {
      e.stopPropagation();
      const res = await modal({
        title: `删除分类：${c.name}`,
        fields: [
          {
            name: "mode", label: "方式", type: "select",
            options: [
              { value: "move", label: "图片移入默认分类（安全）" },
              { value: "purge", label: `连同 ${c.cnt} 张图片一起删除（不可恢复）` },
            ],
          },
          { name: "target", label: "迁移到", value: "未分类" },
        ],
        confirmText: "删除",
        danger: true,
      });
      if (!res) return;
      try {
        const r = await bridge.apiPost("categories/delete", {
          name: c.name, mode: res.mode, target: res.target || "未分类",
        });
        if (state.category === c.name) { state.category = null; state.offset = 0; }
        toast(r.mode === "purge" ? `已删除分类及 ${r.removed} 张图片` : `已迁移 ${r.removed} 张到「${r.target}」`);
        await refresh();
      } catch (err) { fail(err); }
    });

    const aliases = el("button", { class: "mini ghost", text: "别名" });
    aliases.addEventListener("click", async (e) => {
      e.stopPropagation();
      await openAliases(c.name);
    });

    const li = el("li", {
      class: state.category === c.name ? "active" : "",
      onclick: () => { state.category = c.name; state.offset = 0; state.selected.clear(); refresh(); },
    }, [
      el("span", { class: "name", text: c.name }),
      el("span", { class: "cnt", text: c.blocked ? `${c.cnt}/${c.blocked}🚫` : String(c.cnt) }),
      c.aliases ? el("span", { class: "cnt", title: "已登记别名数", text: `别名${c.aliases}` }) : null,
      aliases, rename, del,
    ]);
    list.appendChild(li);
  }

  if (!data.items.length) {
    list.appendChild(el("li", { class: "ts", text: "暂无分类" }));
  }
}

async function loadThumbs() {
  if (!state.category) {
    $("grid").replaceChildren();
    $("hint").textContent = "左侧还没有分类，先创建一个，或在群里用 /加图 收录图片。";
    $("page-info").textContent = "";
    return;
  }
  const data = await bridge.apiGet("thumbs", {
    category: state.category,
    offset: state.offset,
    limit: state.limit,
    include_blocked: state.includeBlocked ? 1 : 0,
  });
  state.items = data.items;
  state.total = data.total;
  // limit 的权威在服务端（它有硬上限）：用它校正翻页步进与分页文案，
  // 否则"请求 96、只返回 60"会让中间的图被静默跳过。
  if (typeof data.limit === "number" && data.limit > 0 && data.limit !== state.limit) {
    state.limit = data.limit;
    const sel = $("page-size");
    if (sel && Array.from(sel.options).some((o) => Number(o.value) === state.limit)) {
      sel.value = String(state.limit);
    }
  }
  state.selected.clear();

  const grid = $("grid");
  grid.replaceChildren();
  for (const it of state.items) {
    const pick = el("input", { type: "checkbox", class: "pick" });
    pick.addEventListener("change", () => {
      if (pick.checked) state.selected.add(it.seq);
      else state.selected.delete(it.seq);
      card.classList.toggle("sel", pick.checked);
    });

    const preview = it.thumb
      ? el("img", { class: "thumb", src: it.thumb, loading: "lazy", alt: `#${it.seq}` })
      : el("div", { class: "noimg", text: "无预览" });

    const blockBtn = el("button", { class: "mini", text: it.blocked ? "解屏" : "屏蔽" });
    blockBtn.addEventListener("click", async () => {
      try {
        await bridge.apiPost("images/block", {
          category: state.category, seq: it.seq, blocked: !it.blocked,
        });
        await refresh();
      } catch (err) { fail(err); }
    });

    const delBtn = el("button", { class: "mini danger", text: "删除" });
    delBtn.addEventListener("click", async () => {
      const res = await modal({
        title: `删除「${state.category}」#${it.seq}？`,
        fields: [],
        confirmText: "删除",
        danger: true,
      });
      if (!res) return;
      try {
        await bridge.apiPost("images/delete", { category: state.category, seq: it.seq });
        toast(`已删除 #${it.seq}`);
        await refresh();
      } catch (err) { fail(err); }
    });

    const dlBtn = el("button", { class: "mini ghost", text: "下载" });
    dlBtn.addEventListener("click", () => {
      bridge.download("raw", { category: state.category, seq: it.seq }).catch(fail);
    });

    const mvBtn = el("button", { class: "mini ghost", text: "改分类" });
    mvBtn.addEventListener("click", () => {
      transferItems([{ category: state.category, seq: it.seq }], "move");
    });

    const card = el("div", { class: `card${it.blocked ? " blocked" : ""}`, "data-seq": String(it.seq) }, [
      pick, preview,
      el("div", { class: "meta" }, [
        el("span", { text: `#${it.seq}` }),
        el("span", { class: "badge", text: it.blocked ? "已屏蔽" : "" }),
      ]),
      el("div", { class: "ops" }, [blockBtn, mvBtn, delBtn]),
      el("div", { class: "ops" }, [dlBtn]),
    ]);
    grid.appendChild(card);
  }

  $("hint").textContent = state.items.length
    ? `「${state.category}」共 ${state.total} 张`
    : `「${state.category}」暂无图片`;
  const from = state.total ? state.offset + 1 : 0;
  const to = Math.min(state.offset + state.limit, state.total);
  $("page-info").textContent = `${from}-${to} / ${state.total}`;
  $("prev").disabled = state.offset <= 0;
  $("next").disabled = state.offset + state.limit >= state.total;
  $("select-all").checked = false;
}

async function refresh() {
  try {
    await Promise.all([loadOverview(), loadCategories()]);
    await loadThumbs();
  } catch (err) { fail(err); }
}

/* ---------------------------- 重新分类（移动/复制） ---------------------------- */
async function transferItems(items, mode) {
  if (!items.length) { toast("先勾选图片"); return; }
  const others = state.categories
    .map((c) => c.name)
    .filter((n) => n !== state.category);
  if (!others.length) {
    toast("还没有其他分类，先在左侧「新建」一个");
    return;
  }
  const res = await modal({
    title: `${mode === "copy" ? "复制" : "移动"} ${items.length} 张图片到…`,
    fields: [
      {
        name: "target", label: "目标分类", type: "select",
        options: others.map((n) => ({ value: n, label: n })),
      },
      { name: "newTarget", label: "或新建", value: "" },
    ],
    confirmText: mode === "copy" ? "复制" : "移动",
  });
  if (!res) return;
  const target = (res.newTarget || "").trim() || res.target;
  if (!target) { toast("请选择或填写目标分类"); return; }
  try {
    const r = await bridge.apiPost("images/transfer", { items, target, mode });
    const verb = mode === "copy" ? "复制" : "移动";
    toast(
      `${verb}完成：成功 ${r.done} 张` +
      (r.skipped ? `，跳过 ${r.skipped} 张（已在目标分类或不存在）` : "") +
      ` → 「${r.target}」`,
    );
    await refresh();
  } catch (err) { fail(err); }
}

/* ---------------------------- 交互绑定 ---------------------------- */
function bind() {
  $("new-cat").addEventListener("click", async () => {
    const res = await modal({
      title: "新建分类",
      fields: [{ name: "name", label: "分类名" }],
      confirmText: "创建",
    });
    if (!res || !res.name) return;
    try {
      await bridge.apiPost("categories/create", { name: res.name });
      state.category = res.name;
      state.offset = 0;
      toast(`已创建「${res.name}」`);
      await refresh();
    } catch (err) { fail(err); }
  });

  $("select-all").addEventListener("change", (e) => {
    state.selected.clear();
    for (const card of document.querySelectorAll(".card")) {
      const box = card.querySelector("input.pick");
      box.checked = e.target.checked;
      card.classList.toggle("sel", e.target.checked);
      if (e.target.checked) state.selected.add(Number(card.dataset.seq));
    }
  });

  const batch = async (blocked) => {
    if (!state.selected.size) { toast("先勾选图片"); return; }
    const seqs = [...state.selected];
    let ok = 0;
    for (const seq of seqs) {
      try {
        await bridge.apiPost("images/block", { category: state.category, seq, blocked });
        ok += 1;
      } catch (err) { /* 单张失败继续 */ }
    }
    toast(`${blocked ? "屏蔽" : "解屏"} ${ok}/${seqs.length} 张`);
    await refresh();
  };
  $("batch-block").addEventListener("click", () => batch(true));
  $("batch-unblock").addEventListener("click", () => batch(false));

  const selectedItems = () =>
    [...state.selected].map((seq) => ({ category: state.category, seq }));
  $("batch-move").addEventListener("click", () =>
    transferItems(selectedItems(), "move"));
  $("batch-copy").addEventListener("click", () =>
    transferItems(selectedItems(), "copy"));

  $("batch-delete").addEventListener("click", async () => {
    if (!state.selected.size) { toast("先勾选图片"); return; }
    const seqs = [...state.selected];
    const res = await modal({
      title: `确认删除 ${seqs.length} 张图片？（不可恢复）`,
      fields: [], confirmText: "删除", danger: true,
    });
    if (!res) return;
    let ok = 0;
    for (const seq of seqs) {
      try {
        await bridge.apiPost("images/delete", { category: state.category, seq });
        ok += 1;
      } catch (err) { /* 继续 */ }
    }
    toast(`已删除 ${ok}/${seqs.length} 张`);
    await refresh();
  });

  $("with-blocked").addEventListener("change", (e) => {
    state.includeBlocked = e.target.checked;
    state.offset = 0;
    loadThumbs().catch(fail);
  });

  $("page-size").addEventListener("change", (e) => {
    state.limit = Number(e.target.value);
    state.offset = 0;
    loadThumbs().catch(fail);
  });

  $("prev").addEventListener("click", () => {
    state.offset = Math.max(0, state.offset - state.limit);
    loadThumbs().catch(fail);
  });
  $("next").addEventListener("click", () => {
    if (state.offset + state.limit < state.total) {
      state.offset += state.limit;
      loadThumbs().catch(fail);
    }
  });

  $("toggle-bans").addEventListener("click", async () => {
    const panel = $("bans-panel");
    if (!panel.classList.contains("hidden")) { panel.classList.add("hidden"); return; }
    const list = $("bans");
    list.replaceChildren();
    panel.classList.remove("hidden");
    try {
      const data = await bridge.apiGet("overview");
      list.appendChild(el("li", { class: "ts", text: `当前黑名单共 ${data.blocked_hashes} 条。在图片卡片上点「屏蔽」即可新增，或在此按 sha256 解除。` }));
      const addRow = el("li", {}, []);
      const input = el("input", { type: "text", placeholder: "粘贴 sha256 解除拉黑" });
      Object.assign(input.style, {
        flex: "1", background: "var(--bg)", color: "var(--text)",
        border: "1px solid var(--line)", borderRadius: "8px", padding: "4px 8px",
      });
      const btn = el("button", { class: "mini", text: "解除" });
      btn.addEventListener("click", async () => {
        const sha = input.value.trim().toLowerCase();
        if (sha.length !== 64) { toast("sha256 应为 64 位"); return; }
        try {
          const r = await bridge.apiPost("hashes/unblock", { sha256: sha });
          toast(`已解除，恢复 ${r.affected} 张`);
          input.value = "";
          await refresh();
        } catch (err) { fail(err); }
      });
      addRow.appendChild(input);
      addRow.appendChild(btn);
      list.appendChild(addRow);
    } catch (err) { fail(err); }
  });

  $("close-bans").addEventListener("click", () => $("bans-panel").classList.add("hidden"));

  $("toggle-aliases").addEventListener("click", async () => {
    const panel = $("aliases-panel");
    if (!panel.classList.contains("hidden")) { panel.classList.add("hidden"); return; }
    await openAliases(null);
  });
  $("close-aliases").addEventListener("click", () => $("aliases-panel").classList.add("hidden"));
}

/* ---------------------------- 标签别名 ---------------------------- */
async function openAliases(target) {
  const panel = $("aliases-panel");
  const list = $("aliases");
  list.replaceChildren();
  panel.classList.remove("hidden");

  const render = async () => {
    list.replaceChildren();
    let data;
    try {
      data = await bridge.apiGet("aliases", target ? { target } : {});
    } catch (err) { fail(err); return; }

    if (!data.items.length) {
      list.appendChild(el("li", { class: "ts", text: "还没有别名。一个别名只能指向一个分类；同一个词被两个分类声明时会拒绝后一个（可用 force 接管）。" }));
    }
    for (const it of data.items) {
      const del = el("button", { class: "mini ghost danger", text: "删除" });
      del.addEventListener("click", async () => {
        try {
          const r = await bridge.apiPost("aliases/delete", { alias: it.alias_text });
          toast(r.message);
          await refresh();
          await render();
        } catch (err) { fail(err); }
      });
      list.appendChild(el("li", {}, [
        el("span", { class: "name", text: `${it.alias_text} → ${it.canonical}` }),
        del,
      ]));
    }

    const aliasInput = el("input", { type: "text", placeholder: "别名（单个词，不含空格/数字）" });
    const targetSelect = el("select", {});
    for (const c of state.categories) {
      targetSelect.appendChild(el("option", { value: c.name, text: c.name }));
    }
    if (target) targetSelect.value = target;
    const forceBox = el("input", { type: "checkbox" });
    const forceLabel = el("label", { class: "chk" }, [forceBox, el("span", { text: "接管已有别名" })]);
    const add = el("button", { class: "mini", text: "添加" });
    add.addEventListener("click", async () => {
      const alias = aliasInput.value.trim();
      if (!alias) { toast("请填别名"); return; }
      if (!targetSelect.value) { toast("请先建立目标分类"); return; }
      try {
        const r = await bridge.apiPost("aliases/add", {
          alias, target: targetSelect.value, force: forceBox.checked,
        });
        toast(r.message);
        await refresh();
        await render();
      } catch (err) { fail(err); }
    });
    list.appendChild(el("li", {}, [aliasInput, targetSelect, add]));
    list.appendChild(el("li", {}, [forceLabel]));
  };

  await render();
}

/* ---------------------------- 启动 ---------------------------- */
(async () => {
  try {
    const ctx = await bridge.ready();
    const title = bridge.t ? bridge.t("pages.manage.title", "图库管理") : "图库管理";
    document.title = title;
    $("title").textContent = title;
    if (ctx && ctx.isDark) document.documentElement.setAttribute("data-theme", "dark");
    bridge.onContext?.((c) => {
      if (c && c.isDark) document.documentElement.setAttribute("data-theme", "dark");
      else document.documentElement.removeAttribute("data-theme");
    });
  } catch (err) {
    /* 上下文非必需，忽略 */
  }
  bind();
  await refresh();
})();
