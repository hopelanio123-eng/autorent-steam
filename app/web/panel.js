const byId = (id) => document.getElementById(id);
const { t, message: translateMessage, locale } = window.AutoRentI18n;
const labels = {
  free: "Свободен",
  busy: "В аренде",
  expired: "Просрочен",
  disabled: "Отключён",
  closed: "Закрыта",
};
const pages = {
  accounts: { title: "Аккаунты", table: "Список аккаунтов", empty: "Аккаунты не найдены" },
  lots: { title: "Лоты" },
  rentals: { title: "Аренда", table: "Открытые аренды", empty: "Открытых аренд нет" },
  history: { title: "История", table: "Закрытые аренды", empty: "Закрытых аренд нет" },
  reports: { title: "Отчёты" },
  notifications: { title: "Уведомления" },
  settings: { title: "Настройки" },
};
let currency;
let dateTime;
let chartDate;
let refreshVersion = 0;
let pendingAction;
let pendingSuccess;
let editedAccount;
let refundRental;
let searchTimer;
let editedLot;
let rentalLot;

function updateFormats() {
  currency = new Intl.NumberFormat(locale(), { style: "currency", currency: "RUB" });
  dateTime = new Intl.DateTimeFormat(locale(), { dateStyle: "short", timeStyle: "short" });
  chartDate = new Intl.DateTimeFormat(locale(), { day: "numeric", month: "short", timeZone: "UTC" });
}

async function api(path, { method = "GET", data } = {}) {
  const options = { method };
  if (data instanceof FormData) {
    options.body = data;
  } else if (data !== undefined) {
    options.headers = { "Content-Type": "application/json" };
    options.body = JSON.stringify(data);
  }
  const response = await fetch("/api" + path, options);
  const result = await response.json();
  if (!response.ok) {
    const message = Array.isArray(result.detail)
      ? result.detail.map((item) => item.msg).join("\n")
      : result.detail;
    throw Error(message || "Запрос не выполнен");
  }
  return result;
}

function node(tag, text, className) {
  const element = document.createElement(tag);
  if (text !== undefined) element.textContent = text;
  if (className) element.className = className;
  return element;
}

function notify(message, error = false) {
  const notice = byId("notice");
  notice.textContent = translateMessage(message);
  notice.classList.toggle("error", error);
  notice.hidden = !message;
}

function actionButton(text, action) {
  const button = node("button", t(text), "small");
  button.type = "button";
  button.addEventListener("click", action);
  return button;
}

function confirmAction(title, target, label, action, success, duration = false) {
  byId("action-title").textContent = t(title);
  byId("action-target").textContent = target;
  byId("action-submit").textContent = t(label);
  const labelElement = byId("duration-label");
  labelElement.hidden = !duration;
  labelElement.querySelector("input").disabled = !duration;
  labelElement.querySelector("input").value = 60;
  pendingAction = action;
  pendingSuccess = success;
  openDialog("action-dialog");
}

function statusBadge(status) {
  return node("span", t(labels[status]), "pill " + status);
}

function accountRow(account) {
  const row = node("tr");
  const name = node("td");
  name.append(node("strong", account.account_name || account.login));
  if (account.account_name) name.append(node("small", account.login));
  const game = node("td", account.game);
  game.append(node("small", [account.platform, account.game_subgroup].filter(Boolean).join(" · ")));
  const status = node("td");
  status.append(statusBadge(account.status));
  const buttons = node("td", undefined, "row-actions");
  buttons.append(actionButton("Изменить", () => openAccountForm(account)));
  if (!account.is_active) {
    const change = account.status === "disabled" ? "Включить" : "Отключить";
    const path = "/accounts/" + encodeURIComponent(account.login);
    buttons.append(
      actionButton(change, () => confirmAction(
        change === "Включить" ? "Включить аккаунт?" : "Отключить аккаунт?", account.login, change,
        () => api(path + "/toggle-disabled", { method: "POST" }),
        "Статус аккаунта обновлён"
      )),
      actionButton("В архив", () => confirmAction(
        "Перенести в архив?", account.login, "В архив",
        () => api(path, { method: "DELETE" }),
        "Аккаунт перенесён в архив"
      ))
    );
  }
  row.append(name, game, status, node("td", String(account.rentals_count)), buttons);
  return row;
}

function rentalRow(rental, history) {
  const row = node("tr");
  const name = node("td");
  name.append(node("strong", rental.login), node("small", rental.game));
  const order = node("td", rental.id_order || "—");
  if (rental.lot_title) order.append(node("small", rental.lot_title));
  if (rental.marketplace !== "manual") {
    order.append(node("small", rental.marketplace === "funpay" ? "FunPay" : "Playerok"));
  }
  const date = history ? rental.closed_at : rental.expires_at;
  const time = node("td", dateTime.format(new Date(date)));
  if (!history) {
    const minutes = Math.max(0, Math.ceil((Date.parse(date) - Date.now()) / 60_000));
    const remaining = t("{hours} ч {minutes} мин", {
      hours: Math.floor(minutes / 60), minutes: String(minutes % 60).padStart(2, "0"),
    });
    time.append(node("small", rental.status === "expired" ? t("Просрочена") : remaining));
  }
  const price = node("td", currency.format(rental.price));
  if (rental.refund_amount) {
    price.append(node("small", t("Возврат {amount}", { amount: currency.format(rental.refund_amount) })));
  }
  row.append(name, order, time, price);
  const buttons = node("td", undefined, "row-actions");
  if (history) {
    if (!rental.refund_amount && rental.price > 0) {
      buttons.append(actionButton("Возврат", () => openRefundForm(rental)));
    }
  } else {
    const path = "/rentals/" + encodeURIComponent(rental.login);
    if (rental.status === "busy") {
      buttons.append(actionButton("Портал", () => showBuyerLink(rental.id)));
    }
    buttons.append(
      actionButton("+30 мин", async () => {
        try {
          await api(path + "/add-time?minutes=30&rental_id=" + rental.id, { method: "POST" });
          notify("Добавлено 30 минут");
          await refresh();
        } catch (error) { notify(error.message, true); }
      }),
      actionButton("Срок", () => confirmAction(
        "Установить срок аренды", rental.login, "Сохранить",
        (data) => api(path + "/set-time?" + new URLSearchParams({ minutes: data.get("minutes"), rental_id: rental.id }), { method: "POST" }),
        "Срок аренды обновлён", true
      )),
      actionButton("Закрыть", () => confirmAction(
        "Закрыть аренду?", rental.login, "Закрыть",
        () => api(path + "/free?rental_id=" + rental.id, { method: "POST" }),
        "Аренда закрыта"
      ))
    );
  }
  row.append(buttons);
  return row;
}

function openAccountForm(account) {
  const form = byId("account-form");
  form.reset();
  editedAccount = account?.login;
  byId("account-title").textContent = t(account ? "Изменить аккаунт" : "Добавить аккаунт");
  byId("account-submit").textContent = t(account ? "Сохранить" : "Добавить");
  byId("account-login-label").hidden = Boolean(account);
  form.elements.namedItem("login").disabled = Boolean(account);
  if (account) {
    for (const key of ["account_name", "game", "platform", "game_subgroup", "purchase_cost", "extra_expenses"]) {
      form.elements.namedItem(key).value = account[key];
    }
  }
  openDialog("account-dialog");
}

function openRefundForm(rental) {
  refundRental = rental.id;
  byId("refund-target").textContent = [rental.login, rental.id_order].filter(Boolean).join(" · ");
  const input = byId("refund-form").elements.namedItem("amount");
  input.max = rental.price;
  input.value = rental.price.toFixed(2);
  openDialog("refund-dialog");
}

async function showBuyerLink(rentalId) {
  try {
    const { path } = await api("/rentals/" + rentalId + "/buyer-link", { method: "POST" });
    const url = new URL(path, location.origin).href;
    byId("buyer-link-url").value = url;
    byId("buyer-link-open").href = url;
    byId("buyer-link-dialog").showModal();
    byId("buyer-link-url").select();
  } catch (error) {
    notify(error.message, true);
  }
}

function renderReport(revenue, profit) {
  const max = Math.max(1, ...revenue.chart.map((day) => Math.abs(day.net)));
  const chart = revenue.chart.map((day) => {
    const row = node("div", undefined, "chart-row");
    const label = chartDate.format(new Date(day.date + "T00:00:00Z"));
    const bar = node("span", undefined, day.net < 0 ? "bar negative" : "bar");
    bar.setAttribute("aria-hidden", "true");
    bar.style.setProperty("--fill", (Math.abs(day.net) / max * 100) + "%");
    row.append(node("span", label), bar, node("strong", currency.format(day.net)));
    return row;
  });
  byId("revenue-chart").replaceChildren(...chart);
  const details = [
    ["Выручка до возвратов", profit.gross_revenue],
    ["Возвраты", profit.refunds.amount],
    ["Покупка аккаунтов", profit.costs.purchase_cost],
    ["Дополнительные расходы", profit.costs.extra_expenses],
    ["Чистая прибыль", profit.net_profit],
  ];
  byId("profit-summary").replaceChildren(...details.map(([label, amount]) => {
    const item = node("div");
    item.append(node("dt", t(label)), node("dd", currency.format(amount)));
    return item;
  }));
}

function renderNotifications(data) {
  byId("notifications-unread").textContent = t("Непрочитанных: {count}", { count: data.unread });
  byId("notifications-empty").hidden = data.notifications.length > 0;
  const rows = data.notifications.map((entry) => {
    const item = node("li");
    const info = node("div");
    info.append(
      node("strong", entry.login + " · " + entry.game),
      node("small", t(entry.kind === "overdue" ? "Просрочена" : "Скоро закончится")
        + " · " + dateTime.format(new Date(entry.expires_at * 1000)))
    );
    item.append(info);
    if (entry.is_read) {
      item.append(node("small", t("Прочитано")));
    } else {
      item.append(actionButton("Отметить прочитанным", async () => {
        try {
          await api("/notifications/" + entry.rental_id + "/" + entry.kind + "/read?expires_at=" + entry.expires_at,
            { method: "POST" });
          await refresh();
        } catch (error) {
          notify(error.message, true);
        }
      }));
    }
    return item;
  });
  byId("notification-list").replaceChildren(...rows);
}

function renderSettings(settings) {
  const form = byId("settings-form");
  for (const [name, value] of Object.entries(settings)) {
    const field = form.elements.namedItem(name);
    if (field.type === "checkbox") field.checked = value;
    else field.value = value;
  }
}

function renderLots(data) {
  byId("lots-empty").hidden = data.lots.length > 0;
  byId("lots-table").replaceChildren(...data.lots.map((lot) => {
    const row = node("tr");
    const title = node("td");
    title.append(node("strong", lot.title), node("small", lot.game + (lot.is_enabled ? "" : " · " + t("Отключён"))));
    const actions = node("td", undefined, "row-actions");
    if (lot.is_enabled && lot.available > 0) {
      actions.append(actionButton("Оформить", () => openRentalForm(lot)));
    }
    actions.append(
      actionButton("Изменить", () => openLotForm(lot)),
      actionButton("Удалить", () => confirmAction(
        "Удалить лот?", lot.title, "Удалить",
        () => api("/lots/" + lot.id, { method: "DELETE" }), "Лот удалён"
      ))
    );
    row.append(title, node("td", String(lot.available)), node("td", t("{count} мин", { count: lot.minutes })),
      node("td", currency.format(lot.price)), actions);
    return row;
  }));
}

async function openLotForm(lot) {
  try {
    const { games } = await api("/accounts/games");
    if (lot && !games.includes(lot.game)) games.push(lot.game);
    if (!games.length) {
      notify("Сначала добавь аккаунт с игрой для лота", true);
      return;
    }
    const form = byId("lot-form");
    form.reset();
    editedLot = lot?.id;
    form.elements.namedItem("game").replaceChildren(...games.map((game) => {
      const option = node("option", game);
      option.value = game;
      return option;
    }));
    if (lot) {
      for (const key of ["title", "game", "minutes", "price"]) form.elements.namedItem(key).value = lot[key];
      form.elements.namedItem("is_enabled").checked = lot.is_enabled;
    }
    byId("lot-title").textContent = t(lot ? "Изменить лот" : "Создать лот");
    openDialog("lot-dialog");
  } catch (error) {
    notify(error.message, true);
  }
}

function currentPage() {
  const hash = location.hash.slice(1);
  return Object.hasOwn(pages, hash) ? hash : "accounts";
}

async function refresh() {
  const version = ++refreshVersion;
  const page = currentPage();
  const isAccounts = page === "accounts";
  const lots = page === "lots";
  const history = page === "history";
  const reports = page === "reports";
  const notifications = page === "notifications";
  const settings = page === "settings";
  const info = pages[page];
  byId("page-title").textContent = t(info.title);
  byId("table-panel").hidden = reports || notifications || settings || lots;
  byId("lots-panel").hidden = !lots;
  byId("new-lot").hidden = !lots;
  byId("reports-panel").hidden = !reports;
  byId("notifications-panel").hidden = !notifications;
  byId("settings-panel").hidden = !settings;
  byId("table-title").textContent = t(info.table || "");
  byId("status-label").hidden = !isAccounts;
  byId("import-accounts").hidden = !isAccounts;
  byId("export-accounts").hidden = !isAccounts;
  for (const link of document.querySelectorAll("nav a")) {
    if (link.hash === "#" + page) link.setAttribute("aria-current", "page");
    else link.removeAttribute("aria-current");
  }
  const query = new URLSearchParams();
  if (byId("search").value.trim()) query.set("q", byId("search").value.trim());
  if (isAccounts && byId("status-filter").value) query.set("status_filter", byId("status-filter").value);
  if (history) query.set("include_closed", "true");
  const dataPath = (isAccounts ? "/accounts" : "/rentals") + (query.size ? "?" + query : "");
  try {
    let detail;
    if (reports) {
      const reportDays = byId("report-days").value;
      detail = Promise.all([api("/stats/revenue-chart?days=" + reportDays), api("/dashboard/profit-loss")]);
    } else if (notifications) {
      detail = api("/notifications");
    } else if (settings) {
      detail = api("/settings");
    } else if (lots) {
      detail = api("/lots");
    } else {
      detail = api(dataPath);
    }
    const [stats, data] = await Promise.all([api("/dashboard/stats"), detail]);
    if (refreshVersion !== version) return;
    for (const name of ["total", "free", "busy", "expired"]) byId(name).textContent = stats.accounts[name];
    byId("earned").textContent = currency.format(stats.revenue.total_earned);
    if (reports) {
      renderReport(...data);
      return;
    }
    if (notifications) {
      renderNotifications(data);
      return;
    }
    if (settings) {
      renderSettings(data);
      return;
    }
    if (lots) {
      renderLots(data);
      return;
    }
    const headings = isAccounts
      ? ["Аккаунт", "Игра", "Статус", "Аренд", "Действия"]
      : ["Аккаунт", "Заказ", history ? "Закрыта" : "До", "Сумма", "Действия"];
    const header = node("tr");
    header.append(...headings.map((label) => node("th", t(label))));
    byId("table-head").replaceChildren(header);
    const entries = isAccounts ? data.accounts : data.rentals.filter((rental) => !history || rental.closed_at);
    byId("table-body").replaceChildren(...entries.map((entry) => isAccounts ? accountRow(entry) : rentalRow(entry, history)));
    byId("table-count").textContent = t("Записей: {count}", { count: entries.length });
    byId("empty").hidden = entries.length > 0;
    byId("empty").textContent = t(info.empty);
  } catch (error) {
    if (refreshVersion === version) notify(error.message, true);
  }
}

function showFormError(form, message) {
  const error = form.querySelector(".form-error");
  error.textContent = translateMessage(message);
  error.hidden = !message;
}

function openDialog(id) {
  const dialog = byId(id);
  showFormError(dialog.querySelector("form"), "");
  dialog.showModal();
}

function registerForm(id, submit) {
  const form = byId(id);
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const button = form.querySelector("[type=submit]");
    button.disabled = true;
    showFormError(form, "");
    try {
      const message = await submit(new FormData(form));
      form.closest("dialog")?.close();
      notify(message);
      await refresh();
    } catch (error) {
      showFormError(form, error.message);
    } finally {
      button.disabled = false;
    }
  });
}

for (const button of document.querySelectorAll("[data-close]")) {
  button.addEventListener("click", () => button.closest("dialog").close());
}
byId("new-account").addEventListener("click", () => openAccountForm());
byId("import-accounts").addEventListener("click", () => {
  byId("csv-form").reset();
  openDialog("csv-dialog");
});
async function openRentalForm(lot) {
  const button = byId("new-rental");
  button.disabled = true;
  try {
    const query = lot ? "?game=" + encodeURIComponent(lot.game) : "";
    const data = await api("/rentals/available-accounts" + query);
    const accounts = lot ? data.accounts.filter((account) => account.game === lot.game) : data.accounts;
    if (accounts.length === 0) {
      notify("Нет свободных аккаунтов", true);
      return;
    }
    const form = byId("rental-form");
    form.reset();
    rentalLot = lot;
    byId("manual-rental-fields").hidden = Boolean(lot);
    form.elements.namedItem("hours").disabled = Boolean(lot);
    form.elements.namedItem("price").disabled = Boolean(lot);
    const summary = byId("rental-lot-info");
    summary.hidden = !lot;
    summary.textContent = lot ? t("{title} · {minutes} мин · {amount} за аккаунт", {
      title: lot.title, minutes: lot.minutes, amount: currency.format(lot.price),
    }) : "";
    byId("rental-accounts").replaceChildren(...accounts.map((account) => {
      const label = node("label", undefined, "choice");
      const input = node("input");
      input.type = "checkbox";
      input.name = "login";
      input.value = account.login;
      label.append(input, node("span", account.login + " · " + account.game));
      return label;
    }));
    openDialog("rental-dialog");
  } catch (error) {
    notify(error.message, true);
  } finally {
    button.disabled = false;
  }
}
byId("new-rental").addEventListener("click", () => openRentalForm());
byId("new-lot").addEventListener("click", () => openLotForm());

registerForm("account-form", async (data) => {
  const fields = Object.fromEntries(data);
  if (editedAccount) {
    await api("/accounts/" + encodeURIComponent(editedAccount), { method: "PUT", data: fields });
    return "Данные аккаунта сохранены";
  }
  await api("/accounts", { method: "POST", data: fields });
  location.hash = "#accounts";
  byId("status-filter").value = "";
  byId("search").value = "";
  return "Аккаунт добавлен";
});
registerForm("rental-form", async (data) => {
  const logins = data.getAll("login");
  if (!logins.length) throw Error("Выберите хотя бы один аккаунт");
  const rental = { logins, order_id: data.get("order_id"), marketplace: data.get("marketplace") };
  if (rentalLot) {
    rental.lot_id = rentalLot.id;
    rental.lot_revision = rentalLot.revision;
  } else {
    rental.hours = data.get("hours");
    rental.price = data.get("price");
  }
  await api("/rentals/manual-assign", { method: "POST", data: rental });
  location.hash = "#rentals";
  return "Аренда оформлена";
});
registerForm("lot-form", async (data) => {
  await api(editedLot ? "/lots/" + editedLot : "/lots", {
    method: editedLot ? "PUT" : "POST",
    data: {
      title: data.get("title"), game: data.get("game"), minutes: Number(data.get("minutes")),
      price: data.get("price"), is_enabled: data.has("is_enabled"),
    },
  });
  return "Лот сохранён";
});
registerForm("csv-form", async (data) => {
  const result = await api("/accounts/upload-csv", { method: "POST", data });
  const summary = t("Добавлено {added} · Дубликатов {skipped} · Ошибок {errors}", result);
  return [summary, ...result.error_messages].join("\n");
});
registerForm("refund-form", async (data) => {
  await api("/orders/" + refundRental + "/refund", { method: "POST", data: { amount: data.get("amount") } });
  return "Возврат записан";
});
registerForm("settings-form", async (data) => {
  await api("/settings", {
    method: "POST",
    data: {
      buyer_portal_enabled: data.has("buyer_portal_enabled"),
      buyer_portal_ttl_hours: Number(data.get("buyer_portal_ttl_hours")),
      buyer_portal_help: data.get("buyer_portal_help"),
      rental_reminder_minutes: Number(data.get("rental_reminder_minutes")),
    },
  });
  return "Настройки сохранены";
});
registerForm("action-form", async (data) => {
  await pendingAction(data);
  return pendingSuccess;
});
window.addEventListener("hashchange", () => {
  byId("search").value = "";
  refresh();
});
byId("status-filter").addEventListener("change", refresh);
byId("report-days").addEventListener("change", refresh);
byId("search").addEventListener("input", () => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(refresh, 180);
});
window.addEventListener("autorent-language", () => {
  updateFormats();
  notify("");
  if (currentPage() === "settings") byId("page-title").textContent = t(pages.settings.title);
  else refresh();
});
updateFormats();
refresh();
window.setInterval(() => {
  if (!document.hidden && currentPage() !== "settings" && !document.querySelector("dialog[open]")) refresh();
}, 30_000);
