// Дашборд обращений 7000. Весь текст клиентов выводится через textContent — никакого innerHTML.
(function () {
  "use strict";

  var state = { from: "", to: "", today: "", source: "all", creative: "", report: null };
  var $ = function (id) { return document.getElementById(id); };

  function el(tag, opts, children) {
    var node = document.createElement(tag);
    opts = opts || {};
    if (opts.cls) node.className = opts.cls;
    if (opts.text != null) node.textContent = opts.text;
    if (opts.title) node.title = opts.title;
    if (opts.href && /^https?:\/\//i.test(opts.href)) {
      node.href = opts.href; node.target = "_blank"; node.rel = "noopener noreferrer";
    }
    (children || []).forEach(function (c) { if (c) node.appendChild(c); });
    return node;
  }

  function isoDate(d) {
    var m = String(d.getMonth() + 1).padStart(2, "0");
    var day = String(d.getDate()).padStart(2, "0");
    return d.getFullYear() + "-" + m + "-" + day;
  }
  function parseIso(s) { var p = s.split("-"); return new Date(+p[0], +p[1] - 1, +p[2]); }
  function addDays(s, n) { var d = parseIso(s); d.setDate(d.getDate() + n); return isoDate(d); }
  function ruDate(s) { var p = s.split("-"); return p[2] + "." + p[1] + "." + p[0]; }
  function pct(n, total) { return total ? Math.round(n * 100 / total) + "%" : "—"; }

  function formatPhone(waId) {
    var d = String(waId || "").replace(/\D/g, "");
    if (d.length === 11 && d[0] === "7") {
      return "+7 " + d.slice(1, 4) + " " + d.slice(4, 7) + " " + d.slice(7, 9) + " " + d.slice(9);
    }
    return d ? "+" + d : "";
  }

  function query(extra) {
    var p = new URLSearchParams();
    if (state.from) p.set("from", state.from);
    if (state.to) p.set("to", state.to);
    Object.keys(extra || {}).forEach(function (k) { if (extra[k]) p.set(k, extra[k]); });
    return p.toString();
  }

  function load() {
    $("status").textContent = "Загрузка…";
    fetch("/api/report?" + query(), { credentials: "same-origin" })
      .then(function (r) {
        if (r.status === 401) { location.href = "/login"; throw new Error("auth"); }
        if (!r.ok) throw new Error("HTTP " + r.status);
        return r.json();
      })
      .then(function (rep) {
        state.report = rep;
        state.from = rep.from; state.to = rep.to; state.today = rep.today;
        $("from").value = rep.from; $("to").value = rep.to;
        if (state.creative && !rep.creatives.some(function (c) { return c.key === state.creative; })) {
          state.creative = "";
        }
        render();
      })
      .catch(function (e) {
        if (e.message !== "auth") {
          $("status").textContent = "Не удалось загрузить данные: " + e.message;
          $("status").className = "small error";
        }
      });
  }

  function render() {
    var rep = state.report;
    var t = rep.totals;
    var st = rep.status;

    var parts = [st.demo ? "ДЕМО: все данные случайные" : "Номер 7000"];
    parts.push(st.last_message_at ? "последнее сообщение: " + st.last_message_at.replace(/^(\d+)-(\d+)-(\d+)/, "$3.$2.$1")
                                  : "сообщений ещё не было");
    parts.push({ on: "amoCRM: запись включена", "dry-run": "amoCRM: пробный режим (в amoCRM не пишем)",
                 demo: "amoCRM: демо-сделки" }[st.amo_mode]
               || "amoCRM: выключено");
    if (st.ads_total && !st.demo) {
      parts.push(st.ad_names_enabled
        ? "названия креативов из Meta: " + st.ads_named + " из " + st.ads_total
        : "названия креативов из Meta отключены — показываем заголовок рекламы");
    }
    $("status").textContent = parts.join(" · ");
    $("status").className = "muted small";

    $("k-all").textContent = t.all;
    $("k-period").textContent = rep.from === rep.to ? ruDate(rep.from) : ruDate(rep.from) + " — " + ruDate(rep.to);
    $("k-ad").textContent = t.ad;
    $("k-ad-share").textContent = t.all ? pct(t.ad, t.all) + " от всех" : "";
    $("k-organic").textContent = t.organic;
    $("k-organic-share").textContent = t.all ? pct(t.organic, t.all) + " от всех" : "";

    renderCreatives(rep);
    renderInquiries(rep);
    $("csv").href = "/api/report.csv?" + query({ source: state.source === "all" ? "" : state.source, creative: state.creative });

    document.querySelectorAll("[data-source]").forEach(function (b) {
      b.classList.toggle("active", b.getAttribute("data-source") === state.source);
    });
    markPreset();
  }

  function renderCreatives(rep) {
    var body = $("creatives").tBodies[0];
    body.textContent = "";
    if (!rep.creatives.length) {
      body.appendChild(el("tr", {}, [el("td", { cls: "empty", text: "За этот период обращений нет" })]));
      body.rows[0].cells[0].colSpan = 5;
      return;
    }
    rep.creatives.forEach(function (c) {
      var nameCell = el("td", {}, [el("div", { text: c.label })]);
      if (c.source === "ad") {
        var details = [];
        if (c.ad_name && c.headline && c.headline !== c.ad_name) details.push("Заголовок: " + c.headline);
        if (!c.ad_name && c.ad_id) details.push("название из Meta ещё не получено");
        if (c.ad_id) details.push("ID " + c.ad_id);
        if (details.length) nameCell.appendChild(el("div", { cls: "sub", text: details.join(" · ") }));
        if (c.source_url) nameCell.appendChild(el("div", { cls: "sub" }, [el("a", { href: c.source_url, text: "открыть рекламу" })]));
      }
      var camp = [c.campaign_name, c.adset_name].filter(Boolean).join(" / ");
      var share = el("div", { cls: "share" }, [
        el("div", { cls: "share-bar" }, [el("span")]),
        el("span", { cls: "share-num", text: pct(c.count, rep.totals.all) })
      ]);
      share.querySelector(".share-bar > span").style.width = (rep.totals.all ? c.count * 100 / rep.totals.all : 0) + "%";
      var platforms = Object.keys(c.platforms || {})
        .sort(function (a, b) { return c.platforms[b] - c.platforms[a]; })
        .map(function (p) { return Object.keys(c.platforms).length > 1 ? p + " " + c.platforms[p] : p; });
      var row = el("tr", { cls: "clickable" + (state.creative === c.key ? " selected" : "") }, [
        nameCell,
        el("td", { cls: "nowrap", text: c.source === "ad" ? (platforms.join(" · ") || "—") : "" }),
        el("td", { cls: "sub", text: c.source === "ad" ? (camp || "—") : "" }),
        el("td", { cls: "num", text: String(c.count) }),
        el("td", {}, [share])
      ]);
      row.addEventListener("click", function () {
        state.creative = state.creative === c.key ? "" : c.key;
        state.source = "all";
        render();
        if (state.creative) $("inquiries").scrollIntoView({ behavior: "smooth", block: "start" });
      });
      body.appendChild(row);
    });
  }

  function renderInquiries(rep) {
    var rows = rep.inquiries.filter(function (r) {
      if (state.source !== "all" && r.source !== state.source) return false;
      if (state.creative && r.creative_key !== state.creative) return false;
      return true;
    });

    var picked = state.creative && rep.creatives.filter(function (c) { return c.key === state.creative; })[0];
    $("filter-note").hidden = !picked;
    if (picked) $("filter-name").textContent = picked.label;

    var body = $("inquiries").tBodies[0];
    body.textContent = "";
    if (!rows.length) {
      body.appendChild(el("tr", {}, [el("td", { cls: "empty", text: "Нет обращений" })]));
      body.rows[0].cells[0].colSpan = 7;
    }
    rows.forEach(function (r) {
      var when = r.started_local.split(" ");
      var phone = formatPhone(r.wa_id);
      var client = el("td", { cls: "nowrap" }, [
        el("div", { text: r.profile_name || "Без имени" }),
        el("div", { cls: "sub" }, [el("a", { href: rep.status.demo ? "" : "https://wa.me/" + r.wa_id, text: phone })])
      ]);
      var creative = el("td", {}, r.source === "ad" ? [
        el("div", { text: r.creative }),
        r.campaign_name ? el("div", { cls: "sub", text: r.campaign_name }) : null
      ] : [el("span", { cls: "sub", text: "—" })]);
      body.appendChild(el("tr", {}, [
        el("td", { cls: "nowrap" }, [el("div", { text: ruDate(when[0]) }), el("div", { cls: "sub", text: when[1] })]),
        client,
        el("td", { cls: "nowrap" }, [
          el("span", { cls: "badge " + r.source, text: r.source === "ad" ? "Таргет" : "Органика" }),
          r.source === "ad" && (r.platform || r.media)
            ? el("div", { cls: "sub", text: [r.platform, r.media].filter(Boolean).join(" · ") }) : null
        ]),
        creative,
        el("td", { cls: "text", text: r.first_text || "" }),
        el("td", { cls: "num", text: String(r.messages) }),
        amoCell(r, rep.status.amo_domain)
      ]));
    });

    $("truncated").hidden = !rep.truncated;
    if (rep.truncated) {
      $("truncated").textContent = "Показаны последние " + rep.inquiries.length + " из " + rep.inquiries_total +
        " — сузьте период или скачайте CSV.";
    }
  }

  var AMO_STATES = {
    written: "Поставлено: Таргет",
    dry_run: "Пробно: поставили бы Таргет",
    already: "Таргет уже стоял",
    organic: "Органика — не трогаем"
  };

  function amoCell(r, domain) {
    if (!r.amo_lead_id) return el("td", { cls: "sub", text: "—" });
    var link = el("a", { href: domain ? "https://" + domain + "/leads/detail/" + r.amo_lead_id : "", text: "сделка " + r.amo_lead_id });
    var cell = el("td", { cls: "amo" }, [el("div", { cls: "nowrap" }, [link])]);
    var text = AMO_STATES[r.amo_state];
    if (text) cell.appendChild(el("div", { cls: "sub", text: text }));
    if (r.amo_source_before && r.amo_state !== "already" && r.amo_state !== "organic") {
      cell.appendChild(el("div", { cls: "sub", text: "было: " + r.amo_source_before }));
    }
    return cell;
  }

  function presetRange(name) {
    var today = state.today || isoDate(new Date());
    if (name === "today") return [today, today];
    if (name === "yesterday") { var y = addDays(today, -1); return [y, y]; }
    if (name === "7") return [addDays(today, -6), today];
    if (name === "30") return [addDays(today, -29), today];
    if (name === "month") return [today.slice(0, 8) + "01", today];
    return null;
  }

  function markPreset() {
    document.querySelectorAll("[data-preset]").forEach(function (b) {
      var r = presetRange(b.getAttribute("data-preset"));
      b.classList.toggle("active", !!r && r[0] === state.from && r[1] === state.to);
    });
  }

  document.querySelectorAll("[data-preset]").forEach(function (b) {
    b.addEventListener("click", function () {
      var r = presetRange(b.getAttribute("data-preset"));
      state.from = r[0]; state.to = r[1];
      load();
    });
  });
  $("apply").addEventListener("click", function () {
    state.from = $("from").value; state.to = $("to").value;
    load();
  });
  document.querySelectorAll("[data-source]").forEach(function (b) {
    b.addEventListener("click", function () {
      state.source = b.getAttribute("data-source");
      if (state.source === "organic") state.creative = "";
      if (state.report) render();
    });
  });
  $("clear-filter").addEventListener("click", function () { state.creative = ""; render(); });

  // Новые обращения подтягиваются сами, пока вкладка открыта.
  setInterval(function () { if (!document.hidden && state.report) load(); }, 120000);

  load();
})();
