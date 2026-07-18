/**
 * assets/js/auth.js
 * ──────────────────
 * Loaded as <script type="module"> so top-level await works.
 * Depends on ui.js being loaded first (for showToast).
 *
 * Responsibilities
 * ─────────────────
 * 1. Initialise Supabase client
 * 2. Restore session and update nav accordingly
 * 3. Expose doLogin / doSignup / subscribe to window
 *    (module functions aren't globally visible by default, so we
 *     assign them explicitly — this lets the inline onclick="" attributes
 *     in the HTML call them without changes)
 * 4. Load live stats and latest alert from the backend API on page load
 * 5. Implement the real filterCases() with debounce
 *
 * ── Replace the two constants below with your project values ──────
 */

import { createClient } from 'https://esm.sh/@supabase/supabase-js@2';

const SUPABASE_URL     = 'https://vyxaxoilyilrvrwgyfil.supabase.co';  // ← replace
const SUPABASE_ANON_KEY = 'eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InZ5eGF4b2lseWlscnZyd2d5ZmlsIiwicm9sZSI6ImFub24iLCJpYXQiOjE3ODI5MTQzODksImV4cCI6MjA5ODQ5MDM4OX0.ZBfLgdjd5OvaFq7PKtN17hQWh2s37s86WIPpqB0Wz3M';                    // ← replace

const supabase = createClient(SUPABASE_URL, SUPABASE_ANON_KEY);

/* ── Session restore ──────────────────────────────────────────── */

const { data: { session } } = await supabase.auth.getSession();
window._session  = session;
window._supabase = supabase;  // expose for console debugging in dev

if (session) {
  _updateNavForLoggedInUser(session.user);
}

// Keep nav in sync if the session changes in another tab
supabase.auth.onAuthStateChange((_event, newSession) => {
  window._session = newSession;
  if (newSession) {
    _updateNavForLoggedInUser(newSession.user);
  } else {
    _resetNav();
  }
});

function _updateNavForLoggedInUser(user) {
  const loginBtn = document.querySelector('.nav-login');
  if (!loginBtn) return;
  const displayName = user.user_metadata?.full_name?.split(' ')[0]
    || user.email.split('@')[0];
  loginBtn.textContent = displayName;
  loginBtn.title = 'Click to sign out';
  loginBtn.onclick = async () => {
    await supabase.auth.signOut();
    showToast('Signed out successfully.');
    setTimeout(() => location.reload(), 800);
  };
}

function _resetNav() {
  const loginBtn = document.querySelector('.nav-login');
  if (!loginBtn) return;
  loginBtn.textContent = 'Log in';
  loginBtn.title = '';
  loginBtn.onclick = () => openModal('login');
}

/* ── Login ────────────────────────────────────────────────────── */

window.doLogin = async function () {
  const email    = document.getElementById('login-email')?.value.trim() ?? '';
  const password = document.getElementById('login-password')?.value ?? '';
  const errEl    = document.getElementById('login-error');

  if (errEl) errEl.style.display = 'none';

  if (!email || !password) {
    _showFieldError('login-error', 'Email and password are required.');
    return;
  }

  const { error } = await supabase.auth.signInWithPassword({ email, password });

  if (error) {
    _showFieldError(
      'login-error',
      error.message.toLowerCase().includes('invalid')
        ? 'Incorrect email or password.'
        : error.message
    );
    return;
  }

  closeModal();
  showToast('Welcome back! You are now signed in.');
  setTimeout(() => location.reload(), 900);
};

/* ── Signup ───────────────────────────────────────────────────── */

window.doSignup = async function () {
  const name     = document.getElementById('signup-name')?.value.trim()     ?? '';
  const phone    = document.getElementById('signup-phone')?.value.trim()    ?? '';
  const email    = document.getElementById('signup-email')?.value.trim()    ?? '';
  const county   = document.getElementById('signup-county')?.value          ?? '';
  const password = document.getElementById('signup-password')?.value        ?? '';
  const confirmPassword = document.getElementById('signup-password-confirm')?.value ?? '';

  _showFieldError('signup-error', '');  // clear previous

  // Client-side validation (mirrors backend rules)
  if (!name || !phone || !email || !county || !password) {
    _showFieldError('signup-error', 'Please fill in all fields.');
    return;
  }
  if (!/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(email)) {
    _showFieldError('signup-error', 'Please enter a valid email address.');
    return;
  }
  if (password.length < 8 || !/\d/.test(password) || !/[A-Z]/.test(password)) {
    _showFieldError(
      'signup-error',
      'Password must be at least 8 characters with a number and uppercase letter.'
    );
    return;
  }
  if (password !== confirmPassword) {
    _showFieldError('signup-error', 'Passwords do not match.');
    return;
  }

  const { error } = await supabase.auth.signUp({
    email,
    password,
    options: {
      data: { full_name: name, phone, county },
      emailRedirectTo: `${window.location.origin}/index.html`,
    },
  });

  if (error) {
    _showFieldError('signup-error', error.message);
    return;
  }

  closeModal();
  showToast('Account created! Please check your email to confirm your address.');
};

/* ── Alert subscription ───────────────────────────────────────── */

window.subscribe = async function () {
  const emailInput = document.getElementById('sub-email');
  const email = emailInput?.value.trim() ?? '';

  if (!email || !email.includes('@')) {
    showToast('Please enter a valid email address.', true);
    return;
  }

  const selectedCounties = [...document.querySelectorAll('.county-pill.sel')]
    .map(p => p.dataset.county);

  try {
    const res = await fetch('/api/alerts/subscribe', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ email, counties: selectedCounties }),
    });

    if (res.ok) {
      showToast('Subscribed! You will receive alerts for your selected counties.');
      if (emailInput) emailInput.value = '';
    } else {
      const d = await res.json().catch(() => ({}));
      showToast(d.error || 'Subscription failed. Please try again.', true);
    }
  } catch {
    showToast('Could not connect to the server. Please try again.', true);
  }
};

/* ── Live stats ───────────────────────────────────────────────── */

async function loadStats() {
  try {
    const res = await fetch('/api/cases/stats');
    if (!res.ok) return;
    const stats = await res.json();

    // Hero quick-stat boxes
    _setText('stat-active', stats.active?.toLocaleString() ?? '—');
    _setText('stat-found',  stats.found_safe?.toLocaleString() ?? '—');
    _setText('stat-tips',   stats.tips?.toLocaleString() ?? '—');

    // Stats bar
    const bars = document.querySelectorAll('.stat-item .num');
    if (bars[0]) bars[0].textContent = stats.active?.toLocaleString() ?? '—';
    if (bars[1]) bars[1].textContent = stats.found_safe?.toLocaleString() ?? '—';
    if (bars[2]) bars[2].textContent = stats.investigating?.toLocaleString() ?? '—';
    if (bars[3]) bars[3].textContent = stats.tips?.toLocaleString() ?? '—';

    // Browse CTA mini-stats
    _setText('cta-active', stats.active?.toLocaleString() ?? '—');
    _setText('cta-found',  stats.found_safe?.toLocaleString() ?? '—');
    _setText('cta-inv',    stats.investigating?.toLocaleString() ?? '—');
    _setText('cta-tips',   stats.tips?.toLocaleString() ?? '—');
    _setText('browse-count', stats.active?.toLocaleString() ?? '247');
  } catch {
    // silently keep static fallback numbers
  }
}

/* ── Latest alert strip ───────────────────────────────────────── */

async function loadLatestAlert() {
  try {
    const res = await fetch('/api/cases?status=reported&risk_level=urgent&limit=1&is_public=true');
    if (!res.ok) return;
    const { cases } = await res.json();
    if (!cases?.length) return;

    const c = cases[0];
    const stripP = document.querySelector('.alert-strip p');
    const stripA = document.querySelector('.alert-strip a');

    if (stripP) {
      const days = c.days_missing ?? '?';
      stripP.innerHTML =
        `<strong>ACTIVE ALERT:</strong> ${c.full_name} (${c.gender === 'female' ? 'Female' : 'Male'}, ${c.age}) ` +
        `missing in ${c.last_seen_location} for ${days} day${days !== 1 ? 's' : ''}. ` +
        `If you have information, please submit a tip immediately.`;
    }
    if (stripA) stripA.href = `tip.html?case=${c.id}`;
  } catch {
    // keep static placeholder
  }
}

/* ── Search stub (homepage hero search navigates to cases.html) ── */

window.filterCases = function (query = '') {
  // Hero search navigates directly to the cases page with the query
  if (query.length > 1) {
    window.location.href = `cases.html?q=${encodeURIComponent(query)}`;
  }
};

/* ── Helpers ──────────────────────────────────────────────────── */

function _setText(id, text) {
  const el = document.getElementById(id);
  if (el) el.textContent = text;
}

function _showFieldError(id, msg) {
  const el = document.getElementById(id);
  if (!el) return;
  el.textContent  = msg;
  el.style.display = msg ? 'block' : 'none';
}

/* ── Initialise on load ───────────────────────────────────────── */

loadStats();
loadLatestAlert();

// Auto-open the login modal if we arrived here via a "Go to log in" link
// from reset.html (?login=1) — saves an extra click after resetting a password.
if (new URLSearchParams(window.location.search).get('login') === '1' && !window._session) {
  openModal('login');
}
