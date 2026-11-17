const info = document.getElementById("buyer-info");
const status = document.getElementById("buyer-status");
const help = document.getElementById("buyer-help");
const { t, message, locale } = window.AutoRentI18n;
const token = location.pathname.split("/").at(-1);
let expiresAt = 0;

function showStatus(source) {
  expiresAt = 0;
  status.textContent = message(source);
  status.hidden = false;
  info.hidden = true;
  help.hidden = true;
}

function updateTime() {
  if (!expiresAt) return;
  const minutes = Math.ceil((expiresAt - Date.now()) / 60_000);
  if (minutes <= 0) {
    showStatus("Срок доступа закончился");
    return;
  }
  const duration = t("{hours} ч {minutes} мин", {
    hours: Math.floor(minutes / 60), minutes: String(minutes % 60).padStart(2, "0"),
  });
  document.getElementById("buyer-remaining").textContent = duration;
}

async function loadRental() {
  try {
    const response = await fetch("/b/api/" + encodeURIComponent(token) + "/info");
    const data = await response.json();
    if (!response.ok) throw Error(data.detail || "Аренда недоступна");
    expiresAt = Date.parse(data.expires_at);
    document.getElementById("buyer-game").textContent = data.game;
    document.getElementById("buyer-order").textContent = data.order_id || "—";
    const dateTime = new Intl.DateTimeFormat(locale(), { dateStyle: "long", timeStyle: "short" });
    document.getElementById("buyer-expires").textContent = dateTime.format(expiresAt);
    help.textContent = data.help;
    help.hidden = !data.help;
    status.hidden = true;
    info.hidden = false;
    updateTime();
  } catch (error) {
    showStatus(error.message);
  }
}

window.addEventListener("autorent-language", loadRental);
loadRental();
setInterval(updateTime, 10_000);
setInterval(loadRental, 30_000);
