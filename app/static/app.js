/* Happy Birthday Mini App — zero-build vanilla JS, no third-party UI dependencies. */
(() => {
  "use strict";
  const tg = window.Telegram && window.Telegram.WebApp;
  const $ = (id) => document.getElementById(id);
  const months = ["января","февраля","марта","апреля","мая","июня","июля","августа","сентября","октября","ноября","декабря"];
  const monthLabels = ["Январь","Февраль","Март","Апрель","Май","Июнь","Июль","Август","Сентябрь","Октябрь","Ноябрь","Декабрь"];
  const state = { data:null, chatId:null, page:"home", loading:false };
  let toastTimer;

  if (tg) {
    tg.ready();
    tg.expand();
    try { tg.setHeaderColor("#151225"); tg.setBackgroundColor("#100f20"); } catch (_) {}
  }

  function getStartParam() {
    const fromQuery = new URLSearchParams(window.location.search).get("tgWebAppStartParam");
    return (tg && tg.initDataUnsafe && tg.initDataUnsafe.start_param) || fromQuery || "";
  }

  function announce(message, bad=false) {
    const el = $("toast");
    el.textContent = message;
    el.style.background = bad ? "#63354f" : "#3c3452";
    el.classList.add("visible");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => el.classList.remove("visible"), 3100);
    if (tg && tg.HapticFeedback) {
      try { tg.HapticFeedback.notificationOccurred(bad ? "error" : "success"); } catch (_) {}
    }
  }

  function notice(message) {
    const el = $("notice");
    el.hidden = !message;
    el.textContent = message || "";
  }

  async function request(path, options={}) {
    if (!tg || !tg.initData) throw new Error("Открой приложение через Telegram.");
    if (!state.chatId) throw new Error("Открой Mini App из праздничной панели своей группы.");
    const result = await fetch(path + (path.includes("?") ? "&" : "?") + "chat_id=" + encodeURIComponent(state.chatId), {
      method: options.method || "GET",
      headers: { "Content-Type":"application/json", "X-Telegram-Init-Data":tg.initData },
      body: options.body === undefined ? undefined : JSON.stringify(options.body),
      cache:"no-store"
    });
    let data;
    try { data = await result.json(); } catch (_) { throw new Error("Сервер временно недоступен"); }
    if (!result.ok) {
      const msg = typeof data.detail === "string" ? data.detail : "Не получилось выполнить действие";
      throw new Error(msg);
    }
    return data;
  }

  function openPage(name) {
    if (!["home","birthday","calendar","wishes","settings"].includes(name)) return;
    if (name === "settings" && !(state.data && state.data.is_admin)) return;
    state.page = name;
    document.querySelectorAll(".page").forEach(el => el.classList.toggle("active", el.id === "page-" + name));
    document.querySelectorAll("[data-go].tab").forEach(el => el.classList.toggle("active", el.dataset.go === name));
    if (name === "settings") document.querySelectorAll(".tab").forEach(el => el.classList.remove("active"));
    window.scrollTo({top:0,behavior:"smooth"});
    if (tg && tg.HapticFeedback) {
      try { tg.HapticFeedback.selectionChanged(); } catch (_) {}
    }
  }

  function dateLabel(day, month) { return String(day) + " " + months[month-1]; }
  function daysLabel(days) {
    if (days === 0) return "Сегодня!";
    if (days === 1) return "Завтра";
    const n = days % 100;
    let ending = "дней";
    if (n < 11 || n > 14) {
      if (days % 10 === 1) ending = "день";
      else if (days % 10 >= 2 && days % 10 <= 4) ending = "дня";
    }
    return "Через " + days + " " + ending;
  }

  function makePerson(person) {
    const row = document.createElement("div");
    row.className = "person-row";
    const avatar = document.createElement("div");
    avatar.className = "person-avatar";
    avatar.textContent = (person.first_name || "?").trim().slice(0,1).toUpperCase();
    const info = document.createElement("div");
    info.className = "person-info";
    const name = document.createElement("div");
    name.className = "person-name";
    name.textContent = person.first_name || "Участник";
    const meta = document.createElement("div");
    meta.className = "person-meta";
    meta.textContent = dateLabel(person.day, person.month);
    if (person.birth_year) meta.textContent += " · " + person.birth_year + " г.";
    info.append(name,meta);
    const badge = document.createElement("div");
    badge.className = "countdown-pill" + (person.days_until===0 ? " today" : "");
    badge.textContent = daysLabel(person.days_until);
    row.append(avatar,info,badge);
    return row;
  }

  function render() {
    if (!state.data) return;
    const data = state.data;
    const firstName = (data.me.first_name || "Друг").trim().split(/\s+/)[0];
    $("welcome").textContent = "Привет, " + firstName + "! ✌️";
    $("chat-name").textContent = "Чат «" + data.chat.title + "»";
    if (data.me.photo_url) {
      const img = document.createElement("img");
      img.src = data.me.photo_url;
      img.alt = firstName;
      img.referrerPolicy = "no-referrer";
      $("avatar").replaceChildren(img);
    } else {
      $("avatar").textContent = firstName.slice(0,1).toUpperCase();
    }
    const my = data.birthday;
    if (my) {
      $("hero-text").textContent = "🎉 " + dateLabel(my.day,my.month);
      const upcomingSelf = data.birthdays.find(b => b.user_id === data.me.id);
      $("hero-caption").textContent = upcomingSelf ? daysLabel(upcomingSelf.days_until) : "Твой день обязательно наступит!";
      $("hero-action").firstChild.textContent = "Изменить дату ";
    } else {
      $("hero-text").textContent = "У каждого есть\nособенный день ✨";
      $("hero-caption").textContent = "Сохрани дату и жди поздравления!";
      $("hero-action").firstChild.textContent = "Указать день рождения ";
    }
    $("upcoming").replaceChildren();
    const next = data.birthdays.slice(0,3);
    if (!next.length) {
      const empty = document.createElement("div"); empty.className="empty-mini";
      empty.textContent = "Добавь свою дату первым — пусть праздники начнутся! 🥳";
      $("upcoming").append(empty);
    } else next.forEach(b => $("upcoming").append(makePerson(b)));
    $("calendar-count").textContent = String(data.birthdays.length);
    $("calendar-list").replaceChildren();
    if (!data.birthdays.length) {
      const empty = document.createElement("div"); empty.className="empty-mini";
      empty.textContent = "Пока нет добавленных дат. Начни с себя 💜";
      $("calendar-list").append(empty);
    } else data.birthdays.forEach(b => $("calendar-list").append(makePerson(b)));
    $("day").value = my ? String(my.day) : "";
    $("month").value = my ? String(my.month) : "";
    $("year").value = my && my.birth_year ? String(my.birth_year) : "";
    $("show-age").checked = !!(my && my.show_age);
    $("visible").checked = my ? my.visible : true;
    $("celebrate").checked = my ? my.celebrate : true;
    $("delete-birthday").hidden = !my;
    $("admin-link").hidden = !data.is_admin;
    $("timezone").value = data.settings.tz;
    $("greeting-time").value = data.settings.greeting_time;
    $("pin-enabled").checked = data.settings.pin_enabled;
  }

  async function reload() {
    state.data = await request("/api/bootstrap");
    render();
  }

  function updateBusy(button,busy) {
    state.loading = busy;
    button.disabled = busy;
    document.querySelectorAll("button[type=submit]").forEach(el => el.disabled=busy);
  }

  async function submitBirthday(event) {
    event.preventDefault();
    if (state.loading) return;
    const day = Number($("day").value), month = Number($("month").value);
    const yearText = $("year").value.trim();
    if (!day || !month) return announce("Выбери день и месяц 🎂",true);
    const year = yearText ? Number(yearText) : null;
    if (year !== null && (year < 1900 || year > new Date().getFullYear()))
      return announce("Проверь год рождения",true);
    const payload = {
      day,month,birth_year:year,show_age:$("show-age").checked,
      visible:$("visible").checked,celebrate:$("celebrate").checked
    };
    const button = $("save-birthday");
    try {
      updateBusy(button,true);
      await request("/api/birthday",{method:"PUT",body:payload});
      await reload();
      announce("🎉 День рождения сохранён!");
      openPage("home");
    } catch (e) { announce(e.message,true); }
    finally { updateBusy(button,false); }
  }

  async function deleteBirthday() {
    const approved = await new Promise(resolve => {
      if (tg && tg.showConfirm) tg.showConfirm("Удалить дату рождения из этого чата?",resolve);
      else resolve(window.confirm("Удалить дату рождения из этого чата?"));
    });
    if (!approved) return;
    try {
      $("delete-birthday").disabled = true;
      await request("/api/birthday",{method:"DELETE"});
      await reload();
      announce("Дата рождения удалена");
      openPage("home");
    } catch(e) { announce(e.message,true); }
    finally { $("delete-birthday").disabled = false; }
  }

  async function submitSettings(event) {
    event.preventDefault();
    if (state.loading) return;
    const button = $("save-settings");
    try {
      updateBusy(button,true);
      await request("/api/settings",{method:"PUT",body:{
        tz:$("timezone").value.trim(),greeting_time:$("greeting-time").value,
        pin_enabled:$("pin-enabled").checked
      }});
      await reload();
      announce("Настройки группы сохранены ✨");
      openPage("home");
    } catch(e) { announce(e.message,true); }
    finally { updateBusy(button,false); }
  }

  function fillSelections() {
    for (let day=1;day<=31;day++) {
      const option=document.createElement("option");option.value=String(day);option.textContent=String(day);
      $("day").append(option);
    }
    monthLabels.forEach((label,index)=>{
      const option=document.createElement("option");option.value=String(index+1);option.textContent=label;
      $("month").append(option);
    });
    $("year").max=String(new Date().getFullYear());
  }

  async function init() {
    fillSelections();
    document.querySelectorAll("[data-go]").forEach(button=>
      button.addEventListener("click",()=>openPage(button.dataset.go)));
    $("hero-action").addEventListener("click",()=>openPage("birthday"));
    $("avatar").addEventListener("click",()=>openPage("birthday"));
    $("birthday-form").addEventListener("submit",submitBirthday);
    $("delete-birthday").addEventListener("click",deleteBirthday);
    $("settings-form").addEventListener("submit",submitSettings);

    const match = getStartParam().match(/^c(-?\d+)(?:_([bcw]))?$/);
    if (!match) {
      notice("✨ Открой приложение через кнопку в сообщении «Праздники нашего чата». Если панели ещё нет, попроси администратора написать /holidays в группе.");
      $("chat-name").textContent = "Выбери чат через праздничную панель";
      return;
    }
    state.chatId = Number(match[1]);
    const target = {b:"birthday",c:"calendar",w:"wishes"}[match[2]] || "home";
    try {
      await reload();
      notice("");
      openPage(target);
    } catch (e) {
      notice("Не удалось открыть данные группы: " + e.message + ". Проверь, что бот — администратор группы и ты открыл Mini App из её панели.");
    }
  }

  document.addEventListener("DOMContentLoaded", init);
})();
