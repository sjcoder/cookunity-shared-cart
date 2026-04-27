// Server-of-truth cart. `cartState` mirrors what CookUnity returned on the
// last GET /api/cart. Map<inv_id, {quantity, ...serverFields}>.
let cartState = new Map();
let cartTotal = 0;
let cartMinItems = 0;
const MENU_INDEX = window.MENU_INDEX || {};
const FAV_INDEX = window.FAV_INDEX || {};
const MENU_DATE = window.MENU_DATE;

function withDate(path) {
  const u = new URL(path, location.origin);
  u.searchParams.set('date', MENU_DATE);
  return u.pathname + u.search;
}

// Favorites are local-only, keyed by stable meal/bundle id so they survive
// across weeks when inventoryIds change. Shape: {key: {name,image,price,chef,addedAt,isBundle}}.
function loadFavs() {
  try { return JSON.parse(localStorage.getItem('cu_favs') || '{}'); }
  catch { return {}; }
}
function saveFavs(f) { localStorage.setItem('cu_favs', JSON.stringify(f)); }
let favs = loadFavs();

function isFav(key) { return Object.prototype.hasOwnProperty.call(favs, key); }

function toggleFav(key, snapshot) {
  if (isFav(key)) delete favs[key];
  else favs[key] = { ...snapshot, addedAt: new Date().toISOString() };
  saveFavs(favs);
  syncFavUI();
}

function syncFavUI() {
  document.querySelectorAll('.card').forEach(c => {
    c.classList.toggle('fav', isFav(c.dataset.key));
  });
  const count = Object.keys(favs).length;
  const countEl = document.getElementById('favs-count');
  if (countEl) {
    countEl.textContent = count;
    countEl.style.display = count ? '' : 'none';
  }
  if (document.body.classList.contains('view-favorites')) renderFavoritesView();
}

function renderFavoritesView() {
  const list = document.getElementById('favs-list');
  const empty = document.getElementById('favs-empty');
  list.innerHTML = '';
  const keys = Object.keys(favs).sort((a, b) => (favs[b].addedAt || '').localeCompare(favs[a].addedAt || ''));
  if (keys.length === 0) {
    empty.style.display = '';
    return;
  }
  empty.style.display = 'none';
  for (const key of keys) {
    const snap = favs[key];
    const current = FAV_INDEX[key];           // non-null = on this week's menu
    const row = document.createElement('div');
    row.className = 'fav-row';
    const img = (current && current.image) || snap.image || '';
    const name = (current && current.name) || snap.name || key;
    const price = (current && current.price) ?? snap.price;
    const chef = (current && current.chef) || snap.chef || '';
    const priceStr = typeof price === 'number' ? '$' + price.toFixed(2) : '';
    row.innerHTML = `
      <img src="${img}" alt="" onerror="this.style.visibility='hidden'">
      <div class="info">
        <div class="name">${name}</div>
        <div class="meta">${chef ? 'Chef ' + chef + ' · ' : ''}${priceStr}${!current ? " · <span class=\"unavail\">Not on this week's menu</span>" : ''}</div>
      </div>
      ${current ? `<button class="add" data-inv="${current.inventoryId}">Add to cart</button>` : ''}
      <button class="remove" data-key="${key}">Remove</button>
    `;
    const addBtn = row.querySelector('button.add');
    if (addBtn) addBtn.addEventListener('click', () => addToCart(addBtn.dataset.inv));
    row.querySelector('button.remove').addEventListener('click', () => {
      delete favs[key];
      saveFavs(favs);
      syncFavUI();
    });
    list.appendChild(row);
  }
}

function applyRoute() {
  const isFavs = location.hash === '#favorites';
  const isAuth = location.hash === '#auth';
  document.body.classList.toggle('view-favorites', isFavs);
  document.body.classList.toggle('view-auth', isAuth);
  document.getElementById('favs-link').style.display = (isFavs || isAuth) ? 'none' : '';
  document.getElementById('auth-link').style.display = (isFavs || isAuth) ? 'none' : '';
  document.getElementById('menu-link').style.display = (isFavs || isAuth) ? '' : 'none';
  if (isFavs) renderFavoritesView();
  if (isAuth) { loadCredsInfo(); checkAuth(); }
}

async function loadCredsInfo() {
  try {
    const res = await fetch('/api/creds');
    const body = await res.json();
    const el = document.getElementById('creds-info');
    el.innerHTML = body.token
      ? `Current creds: token ends <code>…${body.token_tail}</code>, cart <code>${body.cart_id || '(none)'}</code>, source: <code>${body.source}</code>, saved: <code>${body.saved_at || '—'}</code>`
      : 'No credentials loaded yet.';
  } catch {}
}

async function saveCreds() {
  const btn = document.getElementById('auth-save');
  const status = document.getElementById('auth-status');
  status.className = 'status';
  status.textContent = '';
  btn.disabled = true;
  const orig = btn.textContent;
  btn.textContent = 'Saving…';
  try {
    const curl = document.getElementById('auth-curl').value.trim();
    if (!curl) throw new Error('Paste a curl command first.');
    const res = await fetch('/api/creds', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ curl }),
    });
    const body = await res.json();
    if (!res.ok) throw new Error(body.error || ('HTTP ' + res.status));
    status.className = 'status ok';
    status.textContent = `Saved. Token tail …${body.token_tail}. Cart ${body.cart_id || '(unchanged)'}. Reloading…`;
    setTimeout(() => { location.href = withDate('/'); }, 900);
  } catch (e) {
    status.className = 'status err';
    status.textContent = String(e.message || e);
    btn.disabled = false;
    btn.textContent = orig;
  }
}

function toast(msg, isErr=false) {
  const el = document.getElementById('toast');
  el.textContent = msg;
  el.classList.toggle('err', isErr);
  el.classList.add('show');
  clearTimeout(toast._t);
  toast._t = setTimeout(() => el.classList.remove('show'), 2500);
}

function fmtTime() {
  const d = new Date();
  return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
}

function cartTotalQuantity() {
  let n = 0;
  for (const v of cartState.values()) n += (v.quantity || 1);
  return n;
}

function renderCart() {
  const wrap = document.querySelector('#cart .items');
  const countEl = document.querySelector('#cart .count');
  const totalEl = document.querySelector('#cart .total');
  const planEl = document.querySelector('#cart .plan-progress');
  wrap.innerHTML = '';
  let count = 0;
  for (const [inv, item] of cartState) {
    count += (item.quantity || 1);
    const meta = MENU_INDEX[inv] || {};
    const name = meta.name || inv;
    const img = meta.image || '';
    const isExtra = !!item.is_extra;
    const unitPrice = typeof meta.price === 'number' ? meta.price : null;
    const linePrice = unitPrice !== null ? unitPrice * (item.quantity || 1) : null;
    const row = document.createElement('div');
    row.className = 'item';
    row.innerHTML = `
      <img src="${img}" alt="" onerror="this.style.visibility='hidden'">
      <div class="name">${name}${meta.chef ? ' <span style=\"color:var(--muted);font-size:11px\">· ' + meta.chef + '</span>' : ''}${isExtra ? ' <span class=\"extra-tag\">extra</span>' : ''}</div>
      <div class="row-stepper" data-inv="${inv}">
        <button class="qty-dec" aria-label="Remove one">−</button>
        <span class="qty">${item.quantity || 1}</span>
        <button class="qty-inc" aria-label="Add one">+</button>
      </div>
      <div class="price">${linePrice !== null ? '$' + linePrice.toFixed(2) : ''}</div>
    `;
    row.querySelector('.qty-dec').addEventListener('click', () => removeFromCart(inv));
    row.querySelector('.qty-inc').addEventListener('click', () => addToCart(inv));
    wrap.appendChild(row);
  }
  countEl.textContent = count;
  countEl.style.display = count ? '' : 'none';
  totalEl.textContent = cartTotal ? '$' + cartTotal.toFixed(2) : '';

  // Plan state: empty → hidden; short → red; met → green; extras → amber.
  planEl.classList.remove('short', 'met', 'extras');
  const extras = [...cartState.values()].filter(v => v.is_extra).reduce((a, v) => a + (v.quantity || 1), 0);
  if (!cartMinItems) {
    planEl.textContent = '';
  } else if (count < cartMinItems) {
    planEl.textContent = `${count} / ${cartMinItems} plan min`;
    planEl.classList.add('short');
  } else if (extras > 0) {
    planEl.textContent = `plan full ✓ · ${extras} extra${extras > 1 ? 's' : ''}`;
    planEl.classList.add('extras');
  } else {
    planEl.textContent = `plan full ✓`;
    planEl.classList.add('met');
  }

  // Decorate each card with its current tier's extras price when we're past the plan.
  const atPlan = cartMinItems && count >= cartMinItems;
  document.querySelectorAll('.card').forEach(c => {
    const inv = c.dataset.inv;
    const inCart = cartState.has(inv);
    c.classList.toggle('in-cart', inCart);
    const qtyEl = c.querySelector('.stepper .qty');
    if (qtyEl) qtyEl.textContent = inCart ? (cartState.get(inv).quantity || 1) : 0;
    // Extras price hint: price of the NEXT meal (position count+1).
    const hint = c.querySelector('.extras-hint');
    const meta = MENU_INDEX[inv];
    if (atPlan && meta && meta.boxPrices) {
      const nextIdx = String(count + 1);
      const nextPrice = meta.boxPrices[nextIdx];
      if (typeof nextPrice === 'number') {
        const text = `extras rate: $${nextPrice.toFixed(2)}`;
        if (hint) hint.textContent = text;
        else {
          const meta_el = c.querySelector('.meta');
          if (meta_el) {
            const span = document.createElement('span');
            span.className = 'extras-hint';
            span.textContent = text;
            meta_el.appendChild(span);
          }
        }
      } else if (hint) {
        hint.remove();
      }
    } else if (hint) {
      hint.remove();
    }
  });
}

let autoRedirected = false;  // only auto-redirect once per page load

function maybeAutoRedirect(orderPlaced) {
  // If the URL had no ?date= and the default landed on an already-ordered week,
  // jump forward to the next dropdown option. Explicit ?date= URLs are left
  // alone so revisiting an ordered week still works.
  if (autoRedirected || !orderPlaced) return;
  if (new URL(location.href).searchParams.has('date')) return;
  const opts = [...document.getElementById('date-picker').options].map(o => o.value);
  const idx = opts.indexOf(MENU_DATE);
  const next = opts.slice(idx + 1).find(Boolean);
  if (!next) return;
  autoRedirected = true;
  location.href = '/?date=' + encodeURIComponent(next);
}

function setAuthIndicator(state) {
  // state: 'ok' | 'expired' | 'unknown'
  const dot = document.getElementById('auth-dot');
  if (!dot) return;
  dot.dataset.state = state;
  dot.title = {
    ok: 'CookUnity auth OK',
    expired: 'CookUnity auth expired — click ⚙ Auth to refresh',
    unknown: 'Auth status unknown',
  }[state] || '';
}

async function syncCart(showToast=false) {
  try {
    const res = await fetch(withDate('/api/cart'), { cache: 'no-store' });
    if (res.status === 401 || res.status === 403) {
      setAuthIndicator('expired');
      throw new Error('Auth expired (HTTP ' + res.status + ')');
    }
    if (!res.ok) throw new Error('HTTP ' + res.status);
    const body = await res.json();
    setAuthIndicator('ok');
    const map = new Map();
    for (const p of body.products || []) {
      map.set(p.inventory_id, p);
    }
    cartState = map;
    cartTotal = (((body.metadata || {}).pricing || {}).total) || 0;
    cartMinItems = ((((body.metadata || {}).pricing || {}).base_plan || {}).min_items) || 0;
    const order = body.order || null;
    document.body.classList.toggle('ordered', !!order);
    renderOrderBanner(order);
    updateReviewButton(order);
    document.querySelector('#cart .synced').textContent = 'synced ' + fmtTime();
    renderCart();
    maybeAutoRedirect(!!order);
    if (showToast) toast('Cart synced');
  } catch (e) {
    document.querySelector('#cart .synced').textContent = 'sync failed';
    if (showToast) toast('Sync failed: ' + e.message, true);
  }
}

async function checkAuth() {
  const btn = document.getElementById('auth-test');
  const out = document.getElementById('auth-test-result');
  if (btn) btn.disabled = true;
  if (out) { out.className = 'status'; out.textContent = 'Testing…'; out.style.display = 'block'; }
  try {
    const res = await fetch('/api/auth/check');
    const body = await res.json();
    if (body.ok) {
      setAuthIndicator('ok');
      if (out) {
        out.className = 'status ok';
        out.textContent = `✓ Auth OK — tested against /cart/v2/${body.tested_date}`;
      }
    } else {
      setAuthIndicator(body.status === 401 || body.status === 403 ? 'expired' : 'unknown');
      if (out) {
        out.className = 'status err';
        out.textContent = `✗ ${body.message || 'Auth failed (HTTP ' + body.status + ')'}`;
      }
    }
  } catch (e) {
    if (out) { out.className = 'status err'; out.textContent = 'Test failed: ' + e.message; }
  } finally {
    if (btn) btn.disabled = false;
  }
}

function updateReviewButton(order) {
  const btn = document.getElementById('review-order');
  const count = cartTotalQuantity();
  const canReview = !order && count > 0 && (!cartMinItems || count >= cartMinItems);
  btn.style.display = canReview ? '' : 'none';
}

function extractBreakdownRows(data) {
  // Server-driven response; find dollar amounts + labels in the usual nesting
  // without being too strict — fall back to a simple total + raw dump.
  const rows = [];
  function walk(node) {
    if (!node || typeof node !== 'object') return;
    if (Array.isArray(node)) { node.forEach(walk); return; }
    const attrs = node.attributes || node;
    const label = attrs.label || attrs.title || attrs.name;
    const value = attrs.value || attrs.amount || attrs.price;
    if (typeof label === 'string' && (typeof value === 'string' || typeof value === 'number')) {
      if (/\$[\d,.]+/.test(String(value)) || /total|subtotal|fee|tax|tip|discount|delivery|extras|plan/i.test(label)) {
        rows.push({ label, value: String(value) });
      }
    }
    for (const k of Object.keys(node)) walk(node[k]);
  }
  walk(data);
  // Dedupe adjacent duplicates
  const seen = new Set();
  return rows.filter(r => {
    const k = r.label + '|' + r.value;
    if (seen.has(k)) return false;
    seen.add(k);
    return true;
  });
}

function renderBreakdown(data) {
  const el = document.getElementById('breakdown');
  el.innerHTML = '';
  const rows = extractBreakdownRows(data);
  if (!rows.length) {
    el.innerHTML = '<div class="placeholder">No itemized breakdown returned — total below is authoritative.</div>';
  } else {
    for (const r of rows) {
      const div = document.createElement('div');
      div.className = 'row' + (/total/i.test(r.label) && !/subtotal/i.test(r.label) ? ' total' : '') + (/discount|save|promo/i.test(r.label) ? ' discount' : '');
      div.innerHTML = `<span>${r.label}</span><span>${r.value}</span>`;
      el.appendChild(div);
    }
  }
  if (typeof cartTotal === 'number' && cartTotal > 0) {
    const div = document.createElement('div');
    div.className = 'row total';
    div.innerHTML = `<span>Cart total</span><span>$${cartTotal.toFixed(2)}</span>`;
    el.appendChild(div);
  }
}

async function openReviewModal() {
  const modal = document.getElementById('order-modal');
  const status = document.getElementById('order-status');
  const confirmBtn = modal.querySelector('.modal-confirm');
  status.className = 'modal-status';
  status.textContent = '';
  confirmBtn.disabled = true;
  document.querySelector('.modal-meta').textContent = `Delivery ${MENU_DATE} · ${cartTotalQuantity()} meal${cartTotalQuantity() > 1 ? 's' : ''}`;
  document.getElementById('breakdown').innerHTML = '<div class="placeholder">Loading breakdown…</div>';
  modal.classList.add('open');
  modal.setAttribute('aria-hidden', 'false');
  try {
    const res = await fetch(withDate('/api/order/preview'), { method: 'POST', headers: { 'content-type': 'application/json' }, body: '{}' });
    const body = await res.json();
    if (!res.ok) throw new Error(body.error || ('HTTP ' + res.status));
    renderBreakdown(body);
    confirmBtn.disabled = false;
  } catch (e) {
    document.getElementById('breakdown').innerHTML = '';
    status.className = 'modal-status err';
    status.textContent = 'Preview failed: ' + e.message;
  }
}

function closeReviewModal() {
  const modal = document.getElementById('order-modal');
  modal.classList.remove('open');
  modal.setAttribute('aria-hidden', 'true');
}

async function placeOrder() {
  const status = document.getElementById('order-status');
  const confirmBtn = document.querySelector('#order-modal .modal-confirm');
  const cancelBtn = document.querySelector('#order-modal .modal-cancel');
  confirmBtn.disabled = true;
  cancelBtn.disabled = true;
  confirmBtn.textContent = 'Placing…';
  status.className = 'modal-status';
  status.textContent = '';
  try {
    const res = await fetch(withDate('/api/order/place'), { method: 'POST', headers: { 'content-type': 'application/json' }, body: '{}' });
    const body = await res.json();
    if (!res.ok) throw new Error(body.error || ('HTTP ' + res.status));
    const node = ((body.data || {}).createOrder) || body;
    const err = node && (node.error || node.__typename === 'OrderCreationError');
    if (err) {
      const oos = (node.outOfStockIds || []).join(', ');
      throw new Error((node.error || 'Order rejected') + (oos ? ' · out of stock: ' + oos : ''));
    }
    status.className = 'modal-status ok';
    status.textContent = `Order placed ✓ #${(node.id || '') || (body.id || '')} · syncing…`;
    await syncCart();
    setTimeout(closeReviewModal, 1200);
  } catch (e) {
    status.className = 'modal-status err';
    status.textContent = 'Order failed: ' + e.message;
    confirmBtn.disabled = false;
    confirmBtn.textContent = 'Place order';
    cancelBtn.disabled = false;
  }
}

function renderOrderBanner(order) {
  let el = document.getElementById('order-banner');
  if (!order) { if (el) el.remove(); return; }
  if (!el) {
    el = document.createElement('div');
    el.id = 'order-banner';
    document.querySelector('main.page').insertAdjacentElement('beforebegin', el);
  }
  const addr = order.address || {};
  const window_ = (order.time_start && order.time_end) ? `${order.time_start}–${order.time_end}` : '';
  const totalStr = typeof cartTotal === 'number' && cartTotal > 0 ? ' · $' + cartTotal.toFixed(2) : '';
  el.innerHTML = `
    <div class="order-banner-inner">
      <span class="order-ico">✓</span>
      <div class="order-text">
        <div class="order-title">Order placed for ${order.delivery_date || MENU_DATE}${totalStr}</div>
        <div class="order-sub">#${order.id}${window_ ? ' · window ' + window_ : ''}${addr.city ? ' · to ' + addr.city : ''}</div>
      </div>
    </div>
  `;
}

async function addToCart(inv) {
  const card = document.querySelector(`.card[data-inv="${inv}"]`);
  const btn = card && card.querySelector('.add-btn');
  if (btn) { btn.disabled = true; btn.textContent = 'Adding…'; }
  try {
    const res = await fetch(withDate('/api/cart/add'), {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ inventory_id: inv, quantity: 1 }),
    });
    if (!res.ok) {
      const body = await res.json().catch(() => ({}));
      throw new Error(body.error || ('HTTP ' + res.status));
    }
    const name = (MENU_INDEX[inv] && MENU_INDEX[inv].name) || inv;
    toast(`Added: ${name}`);
    await syncCart();
  } catch (e) {
    toast('Add failed: ' + e.message, true);
  } finally {
    if (btn) btn.disabled = false;
  }
}

async function removeFromCart(inv) {
  try {
    const res = await fetch(withDate('/api/cart/remove'), {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ inventory_id: inv }),
    });
    if (!res.ok) {
      const body = await res.json().catch(() => ({}));
      throw new Error(body.error || ('HTTP ' + res.status));
    }
    toast('Removed');
    await syncCart();
  } catch (e) {
    toast('Remove failed: ' + e.message, true);
  }
}

function applyFilters() {
  const q = document.getElementById('search').value.trim().toLowerCase();
  const cat = document.getElementById('cat').value;
  const onlyCart = document.getElementById('only-cart').checked;
  document.querySelectorAll('.card').forEach(card => {
    const matchQ = !q || card.dataset.search.includes(q);
    const matchCat = !cat || card.closest('section').dataset.cat === cat;
    const matchCart = !onlyCart || cartState.has(card.dataset.inv);
    card.classList.toggle('hidden', !(matchQ && matchCat && matchCart));
  });
  document.querySelectorAll('section.category').forEach(sec => {
    const anyVisible = [...sec.querySelectorAll('.card')].some(c => !c.classList.contains('hidden'));
    sec.classList.toggle('hidden', !anyVisible);
  });
}

function highResImage(url) {
  if (!url) return '';
  if (!url.includes('imgix.net')) return url;
  const base = url.split('?')[0];
  return base + '?w=1600&auto=format,compress';
}

function openLightbox(card) {
  const snap = JSON.parse(card.dataset.item || '{}');
  const img = snap.image || (card.querySelector('.thumb img') || {}).src || '';
  if (!img) return;
  const imgEl = document.getElementById('lightbox-img');
  imgEl.src = highResImage(img);
  const chef = (card.querySelector('.chef') || {}).textContent || '';
  const name = snap.name || '';
  document.getElementById('lightbox-caption').textContent = chef ? name + ' · ' + chef : name;
  const box = document.getElementById('lightbox');
  box.classList.add('open');
  box.setAttribute('aria-hidden', 'false');
}

function closeLightbox() {
  const box = document.getElementById('lightbox');
  box.classList.remove('open');
  box.setAttribute('aria-hidden', 'true');
  document.getElementById('lightbox-img').src = '';
}

document.addEventListener('click', (e) => {
  const favBtn = e.target.closest('.fav-btn');
  if (favBtn) {
    const card = favBtn.closest('.card');
    const snap = JSON.parse(card.dataset.item || '{}');
    toggleFav(card.dataset.key, {
      name: snap.name,
      image: snap.image,
      price: snap.price,
      chef: (card.querySelector('.chef') || {}).textContent?.replace(/^Chef\s+/, '') || '',
      isBundle: !!snap.isBundle,
    });
    return;
  }
  const addBtn = e.target.closest('.add-btn');
  if (addBtn) {
    addToCart(addBtn.closest('.card').dataset.inv);
    return;
  }
  const incBtn = e.target.closest('.card .stepper .qty-inc');
  if (incBtn) {
    addToCart(incBtn.closest('.card').dataset.inv);
    return;
  }
  const decBtn = e.target.closest('.card .stepper .qty-dec');
  if (decBtn) {
    removeFromCart(decBtn.closest('.card').dataset.inv);
    return;
  }
  const thumb = e.target.closest('.card .thumb');
  if (thumb) {
    openLightbox(thumb.closest('.card'));
    return;
  }
  if (e.target.closest('.lightbox')) {
    closeLightbox();
  }
});

document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape') closeLightbox();
});

async function refresh() {
  const btn = document.getElementById('refresh');
  btn.disabled = true;
  const orig = btn.textContent;
  btn.textContent = 'Refreshing…';
  try {
    const res = await fetch(withDate('/api/refresh'), { method: 'POST' });
    const body = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(body.error || ('HTTP ' + res.status));
    toast('Menu refreshed — reloading');
    setTimeout(() => location.reload(), 400);
  } catch (e) {
    toast('Refresh failed: ' + e.message, true);
    btn.disabled = false;
    btn.textContent = orig;
  }
}

document.addEventListener('DOMContentLoaded', () => {
  document.getElementById('search').addEventListener('input', applyFilters);
  document.getElementById('cat').addEventListener('change', applyFilters);
  document.getElementById('only-cart').addEventListener('change', applyFilters);
  document.getElementById('refresh').addEventListener('click', refresh);
  document.getElementById('cart-reload').addEventListener('click', (e) => { e.stopPropagation(); syncCart(true); });
  document.getElementById('review-order').addEventListener('click', (e) => { e.stopPropagation(); openReviewModal(); });
  document.querySelector('#order-modal .modal-close').addEventListener('click', closeReviewModal);
  document.querySelector('#order-modal .modal-cancel').addEventListener('click', closeReviewModal);
  document.querySelector('#order-modal .modal-confirm').addEventListener('click', placeOrder);
  document.getElementById('order-modal').addEventListener('click', (e) => {
    if (e.target.id === 'order-modal') closeReviewModal();
  });
  document.addEventListener('keydown', (e) => { if (e.key === 'Escape') closeReviewModal(); });
  document.getElementById('cart-toggle').addEventListener('click', () => {
    const cart = document.getElementById('cart');
    cart.classList.toggle('collapsed');
    document.body.classList.toggle('cart-open', !cart.classList.contains('collapsed'));
  });
  document.getElementById('date-picker').addEventListener('change', (e) => {
    const d = e.target.value;
    const hash = location.hash || '';
    location.href = '/?date=' + encodeURIComponent(d) + hash;
  });
  document.getElementById('auth-save').addEventListener('click', saveCreds);
  const authTest = document.getElementById('auth-test');
  if (authTest) authTest.addEventListener('click', checkAuth);
  document.getElementById('favs-clear').addEventListener('click', () => {
    if (Object.keys(favs).length === 0) return;
    if (!confirm('Clear all ' + Object.keys(favs).length + ' favorites?')) return;
    favs = {};
    saveFavs(favs);
    syncFavUI();
  });
  window.addEventListener('hashchange', applyRoute);
  syncFavUI();
  applyRoute();
  syncCart();
  // Re-sync when the tab regains focus and every 30s while visible.
  document.addEventListener('visibilitychange', () => { if (!document.hidden) syncCart(); });
  setInterval(() => { if (!document.hidden) syncCart(); }, 30000);
});
