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
    if (name === "wishes") void loadWishes();
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


  // --- New Year wishlists — all actions are private Mini App requests. ---
  const wishState = {tab:"mine", data:{mine:[], friends:[], max_active:12}, pickup:{}, busy:false};
  const rubles = new Intl.NumberFormat("ru-RU");
  function element(tag, className="", content) {
    const el = document.createElement(tag);
    if (className) el.className = className;
    if (content !== undefined) el.textContent = String(content);
    return el;
  }

  function wishButton(text, kind, handler) {
    const button = element("button", "gift-action " + (kind || ""), text);
    button.type = "button";
    button.addEventListener("click", async () => {
      if (wishState.busy) return;
      wishState.busy = true;
      button.disabled = true;
      try { await handler(); }
      catch (e) { announce(e.message || "Не удалось выполнить действие", true); }
      finally { wishState.busy = false; button.disabled = false; }
    });
    return button;
  }

  function buildWishCard(wish, mine) {
    const card = element("article", "wish-item" + (wish.status === "fulfilled" ? " gift-fulfilled" : ""));
    const top = element("div", "wish-item-top");
    const media = element("div", "wish-item-photo");
    if (wish.image_url) {
      const image = document.createElement("img");
      image.src = wish.image_url;
      image.loading = "lazy";
      image.referrerPolicy = "no-referrer";
      image.alt = wish.title;
      image.addEventListener("error", () => media.replaceChildren(element("span", "", "🎁")), {once:true});
      media.append(image);
    } else media.append(element("span", "", "🎁"));
    const desc = element("div", "wish-item-copy");
    if (!mine) desc.append(element("div", "wish-owner", "✨ " + wish.owner_name));
    desc.append(element("h3", "wish-name", wish.title));
    if (wish.description) desc.append(element("p", "wish-description", wish.description));
    const tags = element("div", "gift-tags");
    if (wish.price_rub) tags.append(element("span", "gift-tag", rubles.format(wish.price_rub) + " ₽"));
    if (wish.status === "fulfilled") tags.append(element("span", "gift-tag success", "💚 Исполнено"));
    if (!mine && wish.reserved && !wish.reserved_by_me) tags.append(element("span", "gift-tag reserved", "🔒 Уже выбрано"));
    if (!mine && wish.reserved_by_me) tags.append(element("span", "gift-tag selected", "💝 Твой подарок"));
    desc.append(tags);
    top.append(media,desc);
    card.append(top);
    const actions = element("div", "gift-actions");
    if (wish.product_url) {
      const link = element("a", "gift-action link", "🛍️ Открыть товар ↗");
      link.href = wish.product_url;
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      link.addEventListener("click", (event) => {
        if (tg && tg.openLink) {
          event.preventDefault();
          tg.openLink(wish.product_url);
        }
      });
      actions.append(link);
    }
    if (mine) {
      if (wish.status === "active") {
        actions.append(wishButton("✏️ Изменить", "ghost", async () => openWishEditor(wish)));
        actions.append(wishButton("✅ Получено", "softgreen", async () => {
          if (!await confirmWish("Этот подарок уже получен?")) return;
          await request("/api/wishes/"+wish.id+"/received",{method:"POST"});
          announce("🎉 Желание исполнено!");
          await loadWishes();
        }));
      }
      actions.append(wishButton("Удалить", "subtle", async () => {
        if (!await confirmWish("Удалить желание? Бронь другого участника также исчезнет.")) return;
        await request("/api/wishes/"+wish.id,{method:"DELETE"});
        announce("Желание удалено");
        await loadWishes();
      }));
    } else if (wish.reserved_by_me) {
      actions.append(wishButton("📦 Адрес ПВЗ", "primary", async () => openWishDelivery(wish.id)));
      actions.append(wishButton("Отменить бронь", "subtle", async () => {
        if (!await confirmWish("Освободить подарок для других участников?")) return;
        await request("/api/wishes/"+wish.id+"/reserve",{method:"DELETE"});
        announce("Бронь отменена");
        await loadWishes();
      }));
    } else if (wish.reserved) {
      const disabled = wishButton("🔒 Уже забронировано", "disabled", () => {});
      disabled.disabled = true;
      actions.append(disabled);
    } else {
      actions.append(wishButton("🎁 Хочу подарить", "primary", async () => {
        if (!await confirmWish("Забронировать подарок для " + wish.owner_name + "? Это не оформит заказ автоматически.")) return;
        await request("/api/wishes/"+wish.id+"/reserve",{method:"POST"});
        announce("🎁 Подарок забронирован!");
        await loadWishes();
        await openWishDelivery(wish.id);
      }));
    }
    card.append(actions);
    return card;
  }

  async function confirmWish(text) {
    return new Promise(resolve => {
      if (tg && tg.showConfirm) tg.showConfirm(text, result => resolve(!!result));
      else resolve(window.confirm(text));
    });
  }

  function updateWishTab(name) {
    wishState.tab = name;
    $("wish-mine-panel").hidden = name !== "mine";
    $("wish-friends-panel").hidden = name !== "friends";
    document.querySelectorAll("[data-wish-tab]").forEach(btn => {
      const selected = btn.dataset.wishTab === name;
      btn.classList.toggle("selected", selected);
      btn.setAttribute("aria-selected", String(selected));
    });
  }

  function emptyWish(container, icon, label, subtitle="") {
    const empty = element("div", "wish-empty");
    empty.append(element("div", "wish-empty-icon", icon));
    empty.append(element("strong", "", label));
    if (subtitle) empty.append(element("p", "", subtitle));
    container.append(empty);
  }

  function renderWishes() {
    const data = wishState.data;
    const mine = data.mine || [], friends = data.friends || [];
    $("wish-my-counter").textContent = mine.filter(w => w.status === "active").length + " из " + data.max_active;
    $("wish-friends-counter").textContent = String(friends.length);
    $("my-wish-list").replaceChildren();
    $("friends-wish-list").replaceChildren();
    if (!mine.length) emptyWish($("my-wish-list"), "🌟", "Твой список пока пуст",
      "Нажми «Добавить» и расскажи друзьям, о каком подарке мечтаешь.");
    else mine.forEach(w => $("my-wish-list").append(buildWishCard(w,true)));
    if (!friends.length) emptyWish($("friends-wish-list"), "🎄", "Пока никто не загадал желание",
      "Поделись своим первым, чтобы вдохновить друзей!");
    else friends.forEach(w => $("friends-wish-list").append(buildWishCard(w,false)));
    $("pickup-wb").value = wishState.pickup.wb || "";
    $("pickup-ozon").value = wishState.pickup.ozon || "";
    const locations = [wishState.pickup.wb,wishState.pickup.ozon].filter(Boolean).length;
    $("pickup-edit").lastElementChild.textContent = locations ? "✓ " + locations + " ПВЗ" : "→";
  }

  async function loadWishes() {
    if (!state.chatId || !state.data) return;
    try {
      const [data, pickup] = await Promise.all([request("/api/wishes"), request("/api/pickup")]);
      wishState.data = data;
      wishState.pickup = pickup;
      renderWishes();
    } catch (e) {
      announce("Желания: " + e.message, true);
      if (!wishState.data.mine.length && !wishState.data.friends.length) {
        const loading = $("my-wish-list");
        loading.replaceChildren();
        emptyWish(loading,"⚠️","Не удалось загрузить желания","Проверь соединение и попробуй ещё раз.");
      }
    }
  }

  function openWishEditor(wish=null) {
    $("wish-form").hidden = false;
    $("wish-form").dataset.editId = wish ? String(wish.id) : "";
    $("wish-form-title").textContent = wish ? "✏️ Изменить желание" : "✨ Новое желание";
    $("wish-submit").textContent = wish ? "💜 Сохранить изменения" : "🎄 Сохранить желание";
    $("wish-title").value = wish ? wish.title : "";
    $("wish-description").value = wish ? wish.description : "";
    $("wish-url").value = wish ? (wish.product_url || "") : "";
    $("wish-image").value = wish ? (wish.image_url || "") : "";
    $("wish-price").value = wish && wish.price_rub ? String(wish.price_rub) : "";
    $("wish-form").scrollIntoView({behavior:"smooth",block:"center"});
    setTimeout(() => $("wish-title").focus(), 250);
  }

  function closeWishEditor() {
    $("wish-form").reset();
    $("wish-form").dataset.editId = "";
    $("wish-form").hidden = true;
  }

  async function saveWish(event) {
    event.preventDefault();
    if (wishState.busy) return;
    const title=$("wish-title").value.trim();
    if (title.length < 2) return announce("Напиши название подарка (от 2 символов)",true);
    const editId = $("wish-form").dataset.editId;
    const priceText = $("wish-price").value.trim();
    const payload = {
      title,description:$("wish-description").value.trim(),
      product_url:$("wish-url").value.trim()||null,
      image_url:$("wish-image").value.trim()||null,
      price_rub:priceText ? Number(priceText) : null
    };
    wishState.busy = true;
    $("wish-submit").disabled = true;
    try {
      await request(editId ? "/api/wishes/"+editId : "/api/wishes",{
        method:editId ? "PUT" : "POST",body:payload
      });
      closeWishEditor();
      announce("✨ Желание сохранено!");
      await loadWishes();
    } catch(e) { announce(e.message,true); }
    finally { wishState.busy=false; $("wish-submit").disabled=false; }
  }

  async function savePickup(event) {
    event.preventDefault();
    if (wishState.busy) return;
    wishState.busy=true;
    $("pickup-submit").disabled=true;
    try {
      await request("/api/pickup",{method:"PUT",body:{
        wb:$("pickup-wb").value.trim() || null,
        ozon:$("pickup-ozon").value.trim() || null
      }});
      $("pickup-form").hidden=true;
      announce("🔒 Адреса ПВЗ сохранены");
      await loadWishes();
    } catch(e) { announce(e.message,true); }
    finally { wishState.busy=false; $("pickup-submit").disabled=false; }
  }

  async function openWishDelivery(wishId) {
    const info = await request("/api/wishes/"+wishId+"/delivery");
    $("wish-delivery-heading").textContent = "Подарок для " + info.owner_name + " 🎁";
    $("wish-delivery-points").replaceChildren();
    for (const [key,label,emoji] of [["wb","Wildberries","🟣"],["ozon","Ozon","🔵"]]) {
      if (!info.pickup[key]) continue;
      const row=element("div","wish-delivery-row");
      row.append(element("strong","",emoji+" "+label));
      const text=element("p","",info.pickup[key]);
      row.append(text);
      const copy=element("button","gift-action ghost","📋 Скопировать адрес");
      copy.type="button";
      copy.addEventListener("click", async () => {
        try {
          await navigator.clipboard.writeText(info.pickup[key]);
          announce("📋 Адрес скопирован");
        } catch(e) { announce("Выдели и скопируй адрес вручную",true); }
      });
      row.append(copy);
      $("wish-delivery-points").append(row);
    }
    if (!$("wish-delivery-points").children.length) {
      emptyWish($("wish-delivery-points"),"📍","Пункт выдачи не указан",
        "Можно связаться с получателем и уточнить удобный способ передачи подарка.");
    }
    $("wish-delivery-note").textContent=info.note || "";
    $("wish-delivery-modal").hidden=false;
    document.body.style.overflow="hidden";
  }
  function closeWishDelivery() {
    $("wish-delivery-modal").hidden=true;
    document.body.style.overflow="";
  }

  function setupWishes() {
    document.querySelectorAll("[data-wish-tab]").forEach(btn =>
      btn.addEventListener("click",()=>updateWishTab(btn.dataset.wishTab)));
    $("wish-add-toggle").addEventListener("click",()=> {
      if ($("wish-form").hidden) openWishEditor(); else closeWishEditor();
    });
    $("wish-form-close").addEventListener("click",closeWishEditor);
    $("wish-form").addEventListener("submit",saveWish);
    $("pickup-edit").addEventListener("click",()=>{
      $("pickup-form").hidden=!$("pickup-form").hidden;
    });
    $("pickup-form").addEventListener("submit",savePickup);
    $("wish-refresh").addEventListener("click",loadWishes);
    $("wish-delivery-close").addEventListener("click",closeWishDelivery);
    $("wish-delivery-done").addEventListener("click",closeWishDelivery);
    $("wish-delivery-modal").addEventListener("click",event=>{
      if(event.target===$("wish-delivery-modal")) closeWishDelivery();
    });
    window.addEventListener("keydown",e=>{
      if(e.key==="Escape"&&!$("wish-delivery-modal").hidden)closeWishDelivery();
    });
    updateWishTab("mine");
  }

  async function init() {
    fillSelections();
    setupWishes();
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
