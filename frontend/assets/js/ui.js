/**
 * assets/js/ui.js
 * ────────────────
 * Synchronous UI helpers used across all pages.
 * No Supabase dependency — loads as a plain <script>, not a module,
 * so every function is globally available immediately (auth.js needs
 * showToast before the module finishes loading).
 *
 * Functions exported to window (implicitly, as top-level declarations):
 *   openModal(tab)       — open auth modal on 'login' or 'signup' tab
 *   closeModal(event?)   — close modal; optional event for overlay-click guard
 *   switchTab(tab)       — switch between login / signup panels
 *   showToast(msg, err)  — show bottom-right toast notification
 *   scrollToSection(id)  — smooth scroll to element by id
 *   viewCase(caseId)     — navigate to case detail page
 *   setFilter(btn, key)  — activate a filter tab (UI only; fetch handled by auth.js)
 *   toggleCounty(pill)   — toggle county pill selection for alert subscription
 *   toggleLanguage(btn)  — switch EN ↔ SW (stub; wire i18next in production)
 *   filterCases(query)   — stub wired to the search input; real fetch in auth.js
 */

/* ── Modal ────────────────────────────────────────────────────── */

function openModal(tab = 'login') {
  const overlay = document.getElementById('modal-overlay');
  if (!overlay) return;
  overlay.classList.add('open');
  switchTab(tab);
  document.body.style.overflow = 'hidden';

  // Focus first input for accessibility
  const firstInput = overlay.querySelector('input');
  if (firstInput) setTimeout(() => firstInput.focus(), 50);
}

function closeModal(e) {
  // If called from the overlay click handler, only close when clicking the
  // backdrop itself — not the modal box inside it.
  if (e && e.target !== document.getElementById('modal-overlay')) return;
  const overlay = document.getElementById('modal-overlay');
  if (!overlay) return;
  overlay.classList.remove('open');
  document.body.style.overflow = '';
}

function switchTab(tab) {
  const loginPanel  = document.getElementById('panel-login');
  const signupPanel = document.getElementById('panel-signup');
  const tabLogin    = document.getElementById('tab-login');
  const tabSignup   = document.getElementById('tab-signup');
  const title       = document.getElementById('modal-title');

  if (!loginPanel || !signupPanel) return;

  const isLogin = tab === 'login';

  loginPanel.style.display  = isLogin ? '' : 'none';
  signupPanel.style.display = isLogin ? 'none' : '';

  tabLogin.classList.toggle('active', isLogin);
  tabSignup.classList.toggle('active', !isLogin);
  tabLogin.setAttribute('aria-selected',  String(isLogin));
  tabSignup.setAttribute('aria-selected', String(!isLogin));

  if (title) title.textContent = isLogin ? 'Sign in to Nipate' : 'Create your account';
}

/* ── Toast ────────────────────────────────────────────────────── */

let _toastTimer = null;

function showToast(msg, isError = false) {
  const toast = document.getElementById('toast');
  const label = document.getElementById('toast-msg');
  if (!toast || !label) return;

  label.textContent = msg;
  toast.style.background = isError ? 'var(--red)' : 'var(--mint)';
  toast.classList.add('show');

  // Reset any existing timer so rapid calls don't stack
  clearTimeout(_toastTimer);
  _toastTimer = setTimeout(() => toast.classList.remove('show'), 4000);
}

/* ── Navigation helpers ───────────────────────────────────────── */

function scrollToSection(id) {
  const el = document.getElementById(id);
  if (el) el.scrollIntoView({ behavior: 'smooth' });
}

function viewCase(caseId) {
  window.location.href = `case.html?id=${encodeURIComponent(caseId)}`;
}

/* ── Filter tabs ──────────────────────────────────────────────── */

function setFilter(btn, filter) {
  document.querySelectorAll('.filter-tab').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
  // Dispatch a custom event so auth.js (or any future module) can react
  document.dispatchEvent(new CustomEvent('nipate:filter', { detail: { filter } }));
}

/* ── County pill toggle (alert subscription) ──────────────────── */

function toggleCounty(pill) {
  if (pill.dataset.county === 'all') {
    document.querySelectorAll('.county-pill').forEach(p => p.classList.remove('sel'));
    pill.classList.add('sel');
  } else {
    const allPill = document.querySelector('[data-county="all"]');
    if (allPill) allPill.classList.remove('sel');
    pill.classList.toggle('sel');
    // If nothing selected, fall back to 'all'
    const anySelected = document.querySelectorAll('.county-pill.sel').length > 0;
    if (!anySelected && allPill) allPill.classList.add('sel');
  }
}

/* ── Language toggle ──────────────────────────────────────────── */

function toggleLanguage(btn) {
  const goingSwahili = btn.textContent.trim() === 'EN';
  btn.textContent = goingSwahili ? 'SW' : 'EN';
  document.documentElement.lang = goingSwahili ? 'sw' : 'en';

  // Production: i18next.changeLanguage(goingSwahili ? 'sw' : 'en')
  showToast(
    goingSwahili ? 'Kubadilisha lugha hadi Kiswahili…' : 'Switching to English…'
  );
}

/* ── Search stub (real fetch wired in auth.js) ────────────────── */

function filterCases(query) {
  // Debounce + API call handled in auth.js once the module loads.
  // This stub prevents "function not defined" errors if the input
  // fires before the ES module finishes loading.
}

/* ── Global keyboard handler ──────────────────────────────────── */

document.addEventListener('keydown', e => {
  if (e.key === 'Escape') closeModal();
});
