/* Тарифы и тестовое оформление подписки. */
(() => {
  const sk = window.SK;
  const { icon } = sk;
  const escape = value => String(value).replace(/[&<>"]/g, character => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;",
  })[character]);
  const dateFormat = new Intl.DateTimeFormat("ru-RU", { dateStyle: "medium" });
  const checkoutIdPattern = /^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$/i;
  const statuses = {
    inactive: "Подписка не активна",
    active: "Подписка активна",
    past_due: "Ошибка продления",
    expired: "Подписка закончилась",
  };

  function priceLabel(plan) {
    if (plan.price_minor === null) return "Тестовая цена не задана";
    const format = new Intl.NumberFormat("ru-RU", { style: "currency", currency: plan.currency });
    const digits = format.resolvedOptions().maximumFractionDigits;
    return "Тестовая цена — " + format.format(plan.price_minor / 10 ** digits);
  }

  function mount(container, { onBack, onChange }) {
    const controller = new AbortController();
    const request = (path, options) => sk.api.request(path, { ...options, signal: controller.signal });
    let subscription = null;
    let plans = [];
    let checkout = null;
    let busy = false;
    let error = "";
    let notice = "";

    container.innerHTML = `
      <div class="view billing-view">
        <div class="screen-head">
          <button class="iconbtn" type="button" data-billing-action="back" aria-label="Назад">${icon("chevLeft", 20)}</button>
          <h1 class="page-title" tabindex="-1">Подписка</h1>
        </div>
        <div data-billing-content></div>
      </div>`;
    const content = container.querySelector("[data-billing-content]");
    container.querySelector("h1").focus();

    function render() {
      const disabled = busy ? "disabled" : "";
      const now = Date.now();
      const currentPeriod = subscription && Date.parse(subscription.period_start) <= now
        && now < Date.parse(subscription.period_end);
      const periodReserved = subscription && now < Date.parse(subscription.period_end);
      const selected = plans.find(plan => plan.code === subscription?.plan_code);
      const status = subscription ? statuses[subscription.status] ?? "Статус неизвестен" : "";
      const period = subscription?.period_start && subscription.period_end
        ? `<p class="muted small">Период с ${dateFormat.format(new Date(subscription.period_start))}
            по ${dateFormat.format(new Date(subscription.period_end))}</p>` : "";

      content.innerHTML = `
        <section class="callout blue">
          <div class="eyebrow">Тестовый режим оплаты</div>
          <p>Тестовое оформление активирует доступ без списания средств.</p>
        </section>
        <p data-billing-notice role="status" tabindex="-1">${escape(notice)}</p>
        ${error ? `<section class="card card-pad stack-sm">
          <p role="alert">${escape(error)}</p>
          ${!subscription ? `<button class="btn btn-ghost" type="button" data-billing-action="reload" ${disabled}>Повторить загрузку</button>` : ""}
        </section>` : ""}
        ${busy ? `<p class="empty" role="status">${subscription ? "Обновляем подписку…" : "Загружаем тарифы и подписку…"}</p>` : ""}
        ${subscription ? `
          <section class="card card-pad stack-sm">
            <h2 class="section-title">Текущий статус</h2>
            <div class="chiprow">
              <span class="chip ${currentPeriod ? "ok" : "linen"}">${status}</span>
              ${subscription.demonstration ? '<span class="chip warn">Тестовая</span>' : ""}
            </div>
            ${subscription.plan_code ? `<p>${escape(selected?.name ?? subscription.plan_code)}</p>` : ""}
            ${period}
            <div class="stat-grid">
              <div class="stat">
                <div class="n blue">${escape(subscription.checks_remaining)} из ${escape(subscription.checks_limit)}</div>
                <div class="l">Осталось проверок</div>
              </div>
              <div class="stat">
                <div class="n">${escape(subscription.max_pages ?? "—")}</div>
                <div class="l">Страниц в одной работе</div>
              </div>
            </div>
            ${subscription.cancel_at_period_end
              ? `<p class="muted small">${currentPeriod ? "Продление отменено. Доступ сохранён до конца периода." : "Продление отменено."}</p>`
              : currentPeriod && subscription.demonstration ? `
                <p class="muted small">После отмены продления доступ сохранится до конца периода.</p>
                <button class="btn btn-ghost" type="button" data-billing-action="cancel" ${disabled}>Отменить продление</button>` : ""}
          </section>
          ${checkout ? `<section class="card card-pad stack-sm">
            <h2 class="section-title">Тестовое оформление</h2>
            <p>Тариф «${escape(checkout.planName)}».</p>
            <button class="btn btn-primary" type="button" data-billing-action="confirm" ${disabled}>
              ${icon("check", 19)} Подтвердить тестовое оформление
            </button>
            <button class="btn btn-quiet" type="button" data-billing-action="dismiss" ${disabled}>К тарифам</button>
          </section>` : ""}
          <section class="card card-pad stack-sm">
            <h2 class="section-title">Тарифы</h2>
            ${periodReserved ? '<p class="muted small">Тариф можно оформить после окончания оплаченного периода.</p>' : ""}
            ${plans.length ? plans.map(plan => `
              <div class="stack-sm">
                <h3 class="section-title">${escape(plan.name)}</h3>
                <p class="muted small">До ${escape(plan.checks_limit)} проверок за период · до ${escape(plan.max_pages)} страниц на работу</p>
                <p class="muted small">${plan.allow_followups ? "Доступны уточнения и перепроверка" : "Тариф включает первичную проверку"}</p>
                ${plan.demonstration
                  ? `<p class="small">${escape(priceLabel(plan))}</p>
                    <button class="btn btn-primary" type="button" data-billing-action="checkout" data-plan="${escape(plan.code)}"
                      ${busy || periodReserved || checkout ? "disabled" : ""}>Оформить тестовую подписку</button>`
                  : '<p class="muted small">Оформление этого тарифа недоступно.</p>'}
              </div>`).join('<hr class="hr">') : '<p class="empty">Доступных тарифов пока нет.</p>'}
          </section>` : ""}`;
    }

    function focus(action) {
      content.querySelector(`[data-billing-action="${action}"]:not(:disabled)`)?.focus();
    }

    async function run(operation, focusOnError) {
      if (busy) return;
      busy = true;
      error = notice = "";
      render();
      let nextFocus;
      try {
        nextFocus = await operation();
      } catch (failure) {
        if (controller.signal.aborted) return;
        error = failure.message || "Не удалось обновить подписку.";
        nextFocus = focusOnError;
      } finally {
        busy = false;
        if (!controller.signal.aborted) {
          render();
          if (nextFocus) focus(nextFocus);
          else if (notice) content.querySelector("[data-billing-notice]")?.focus();
        }
      }
    }

    function load() {
      return run(async () => {
        const result = await Promise.all([
          request("/v1/plans", { auth: false }),
          request("/v1/subscription"),
        ]);
        if (!controller.signal.aborted) [plans, subscription] = result;
      }, "reload");
    }

    function startCheckout(planCode) {
      const plan = plans.find(item => item.code === planCode && item.demonstration);
      if (!plan) return;
      return run(async () => {
        const started = await request("/v1/subscription/checkout", {
          method: "POST", body: { plan_code: plan.code },
        });
        const path = `/v1/payments/fake/checkout/${started.checkout_id}`;
        if (!started.demonstration || !checkoutIdPattern.test(started.checkout_id)
            || started.checkout_url !== path) {
          throw new Error("Тестовое оформление сейчас недоступно.");
        }
        const details = await request(path);
        if (!details.demonstration || details.checkout_id !== started.checkout_id
            || details.plan_code !== plan.code || details.confirm_url !== `${path}/confirm`) {
          throw new Error("Получены неверные данные тестового оформления.");
        }
        if (!controller.signal.aborted) checkout = { ...details, planName: plan.name };
        return "confirm";
      }, "checkout");
    }

    function confirmCheckout() {
      if (!checkout) return;
      return run(async () => {
        const receipt = await request(checkout.confirm_url, { method: "POST" });
        if (!receipt.demonstration || !receipt.applied) {
          checkout = null;
          throw new Error("Тестовое оформление не подтверждено. Оформите подписку снова.");
        }
        const updated = await request("/v1/subscription");
        if (controller.signal.aborted) return;
        checkout = null;
        subscription = updated;
        notice = "Тестовое оформление подтверждено.";
        onChange(updated);
      }, "confirm");
    }

    function cancelRenewal() {
      return run(async () => {
        const updated = await request("/v1/subscription/cancel", { method: "POST" });
        if (controller.signal.aborted) return;
        subscription = updated;
        notice = "Продление отменено.";
        onChange(updated);
      }, "cancel");
    }

    function click(event) {
      const button = event.target.closest("[data-billing-action]");
      if (!button || button.disabled) return;
      const action = button.dataset.billingAction;
      if (action === "back") onBack();
      else if (busy) return;
      else if (action === "reload") void load();
      else if (action === "checkout") void startCheckout(button.dataset.plan);
      else if (action === "confirm") void confirmCheckout();
      else if (action === "cancel") void cancelRenewal();
      else if (action === "dismiss") {
        checkout = null;
        error = "";
        render();
        focus("checkout");
      }
    }

    container.addEventListener("click", click);
    void load();
    return () => {
      controller.abort();
      container.removeEventListener("click", click);
    };
  }

  sk.billing = { mount };
})();
