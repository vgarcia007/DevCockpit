document.querySelectorAll('a[href]').forEach(link => {
  if (new URL(link.href).hostname === 'github.com') {
    link.target = '_blank';
    link.relList.add('noopener', 'noreferrer');
  }
});

const syncButton = document.getElementById('sync-button');
const syncLabel = document.getElementById('sync-label');
if (syncButton) {
  syncButton.addEventListener('click', async () => {
    syncButton.disabled = true;
    try {
      const response = await fetch('/sync', {method: 'POST'});
      const data = await response.json();
      syncLabel.textContent = data.started ? 'Sync running…' : 'Sync already running…';
    } catch (_) {
      syncLabel.textContent = 'Sync request failed';
    } finally {
      setTimeout(() => { syncButton.disabled = false; }, 2500);
    }
  });
  setInterval(async () => {
    try {
      const response = await fetch('/sync/status');
      const data = await response.json();
      if (data.running) syncLabel.textContent = 'Sync running…';
      else if (data.last_success) syncLabel.textContent = 'Last sync ' + new Date(data.last_success).toLocaleString('de-DE');
      if (window._syncWasRunning && !data.running) window.location.reload();
      window._syncWasRunning = data.running;
    } catch (_) {}
  }, 10000);
}

const themeToggle = document.getElementById('theme-toggle');
if (themeToggle) {
  const updateThemeButton = () => {
    const dark = document.documentElement.dataset.bsTheme === 'dark';
    themeToggle.querySelector('use').setAttribute('href', '/static/lucide.svg#' + (dark ? 'sun' : 'moon'));
    themeToggle.querySelector('.visually-hidden').textContent = dark ? 'Light mode' : 'Dark mode';
    themeToggle.title = dark ? 'Light mode' : 'Dark mode';
    themeToggle.setAttribute('aria-pressed', String(dark));
  };
  updateThemeButton();
  themeToggle.addEventListener('click', () => {
    const theme = document.documentElement.dataset.bsTheme === 'dark' ? 'light' : 'dark';
    document.documentElement.dataset.bsTheme = theme;
    try { localStorage.setItem('cockpit-theme', theme); } catch (_) {}
    updateThemeButton();
  });
}

const briefCopyPrompt = document.getElementById('brief-copy-prompt');
if (briefCopyPrompt) {
  const field = document.getElementById('brief-export-text');
  const status = document.getElementById('brief-copy-status');
  const fallbackCopy = value => {
    const scratch = document.createElement('textarea');
    scratch.value = value;
    scratch.readOnly = true;
    scratch.style.cssText = 'position:fixed;top:0;left:0;width:1px;height:1px;opacity:0';
    document.body.appendChild(scratch);
    scratch.focus();
    scratch.select();
    try {
      return document.execCommand('copy');
    } finally {
      scratch.remove();
    }
  };
  briefCopyPrompt.addEventListener('click', async () => {
    let copied = false;
    if (navigator.clipboard?.writeText) {
      try {
        await navigator.clipboard.writeText(field.value);
        copied = true;
      } catch (_) {}
    }
    if (!copied) {
      try { copied = fallbackCopy(field.value); } catch (_) {}
    }
    status.textContent = copied ? 'Copied' : 'Copy blocked by browser';
    status.classList.toggle('is-error', !copied);
    briefCopyPrompt.focus();
  });
}

const teamSearch = document.getElementById('team-search');
if (teamSearch) {
  teamSearch.addEventListener('input', () => {
    const query = teamSearch.value.trim().toLowerCase();
    let visible = 0;
    document.querySelectorAll('.team-row').forEach(row => {
      row.hidden = !row.dataset.member.includes(query);
      if (!row.hidden) visible++;
    });
    document.getElementById('team-no-results').hidden = visible > 0;
  });
}

const sidebar = document.getElementById('app-sidebar');
const sidebarToggle = document.getElementById('sidebar-toggle');
const sidebarBackdrop = document.getElementById('sidebar-backdrop');
if (sidebar && sidebarToggle && sidebarBackdrop) {
  const setSidebarOpen = (open) => {
    sidebar.classList.toggle('is-open', open);
    sidebarBackdrop.hidden = !open;
    sidebarToggle.setAttribute('aria-expanded', String(open));
    sidebarToggle.setAttribute('aria-label', open ? 'Close navigation' : 'Open navigation');
  };
  sidebarToggle.addEventListener('click', () => setSidebarOpen(!sidebar.classList.contains('is-open')));
  sidebarBackdrop.addEventListener('click', () => setSidebarOpen(false));
  document.addEventListener('keydown', event => { if (event.key === 'Escape') setSidebarOpen(false); });
}

const filterFocusKey = 'cockpit-auto-filter-focus';
for (const form of document.querySelectorAll('form[data-auto-filter]')) {
  let timer;
  let submitting = false;
  const rememberPosition = source => {
    try {
      const input = source instanceof HTMLInputElement && source.type !== 'hidden' ? source : null;
      sessionStorage.setItem(filterFocusKey, JSON.stringify({
        path: location.pathname,
        name: input?.name || '',
        cursor: input?.selectionStart ?? null,
        scrollY: window.scrollY,
        savedAt: Date.now(),
      }));
    } catch (_) {}
  };
  const apply = source => {
    if (submitting) return;
    clearTimeout(timer);
    submitting = true;
    rememberPosition(source);
    form.requestSubmit();
  };
  form.addEventListener('change', event => {
    if (event.target.matches('select, input:not([type="hidden"])')) apply(event.target);
  });
  form.addEventListener('input', event => {
    if (!event.target.matches('input:not([type="hidden"])')) return;
    clearTimeout(timer);
    timer = setTimeout(() => apply(event.target), 500);
  });
  form.addEventListener('submit', () => {
    if (!submitting) rememberPosition(document.activeElement);
  });
}
try {
  const saved = JSON.parse(sessionStorage.getItem(filterFocusKey) || 'null');
  sessionStorage.removeItem(filterFocusKey);
  if (saved?.path === location.pathname && Date.now() - saved.savedAt < 10000) {
    requestAnimationFrame(() => {
      const form = document.querySelector('form[data-auto-filter]');
      const input = form?.elements.namedItem(saved.name);
      if (input instanceof HTMLInputElement) {
        input.focus({preventScroll: true});
        if (saved.cursor !== null) input.setSelectionRange(saved.cursor, saved.cursor);
      }
      const previousBehavior = document.documentElement.style.scrollBehavior;
      document.documentElement.style.scrollBehavior = 'auto';
      window.scrollTo(0, saved.scrollY);
      document.documentElement.style.scrollBehavior = previousBehavior;
    });
  }
} catch (_) {}

const visitFeed = document.getElementById('visit-feed');
if (visitFeed) {
  const keys = {
    baseline: 'cockpit-session-visit-baseline',
    started: 'cockpit-session-visit-started',
    hidden: 'cockpit-session-visit-hidden',
    previous: 'cockpit-last-visit',
    left: 'cockpit-last-left',
  };
  const idleThresholdMs = 30 * 60 * 1000;
  const rows = [...visitFeed.querySelectorAll('[data-observed-ms]')];
  const empty = document.getElementById('visit-empty');
  const intro = document.getElementById('visit-intro');
  const count = document.getElementById('visit-count');
  const more = document.getElementById('visit-more');
  let visitBaseline = '';
  let storageAvailable = true;
  let matching = [];
  let expanded = false;

  const validTimestamp = value => value && Number.isFinite(Date.parse(value));

  const openVisit = () => {
    const now = new Date();
    const nowIso = now.toISOString();
    try {
      const started = sessionStorage.getItem(keys.started);
      const hidden = sessionStorage.getItem(keys.hidden);
      const savedBaseline = sessionStorage.getItem(keys.baseline);
      const newDay = validTimestamp(started) && new Date(started).toDateString() !== now.toDateString();
      const returned = validTimestamp(hidden) && now.getTime() - Date.parse(hidden) >= idleThresholdMs;

      if (!validTimestamp(started) || newDay || returned) {
        const previous = returned ? hidden :
          newDay ? localStorage.getItem(keys.previous) :
            localStorage.getItem(keys.left) || localStorage.getItem(keys.previous);
        visitBaseline = validTimestamp(previous) ? previous : '';
        sessionStorage.setItem(keys.baseline, visitBaseline);
        sessionStorage.setItem(keys.started, nowIso);
        localStorage.setItem(keys.previous, nowIso);
      } else {
        visitBaseline = validTimestamp(savedBaseline) ? savedBaseline : started;
        sessionStorage.setItem(keys.baseline, visitBaseline);
      }
      sessionStorage.removeItem(keys.hidden);
    } catch (_) {
      storageAvailable = false;
      visitBaseline = '';
    }
  };

  const renderVisitFeed = () => {
    const since = validTimestamp(visitBaseline) ? Date.parse(visitBaseline) : NaN;
    const firstVisit = !Number.isFinite(since);
    matching = firstVisit ? [] : rows.filter(row => Number(row.dataset.observedMs) > since);
    rows.forEach(row => { row.hidden = true; });
    matching.slice(0, expanded ? undefined : 6).forEach(row => { row.hidden = false; });
    count.textContent = firstVisit ? '—' : String(matching.length);
    intro.textContent = !storageAvailable ? 'Visit comparison is unavailable in this browser.' :
      firstVisit ? 'Visit comparison starts after this visit.' :
        'Observed since ' + new Date(since).toLocaleString('de-DE');
    empty.textContent = !storageAvailable ?
      'Browser storage is blocked, so previous visits cannot be remembered.' :
      firstVisit ?
        'This browser has no previous visit yet. Changes observed by GitHub sync will appear after you return.' :
        'No relevant changes observed by GitHub sync since your last visit.';
    empty.hidden = !firstVisit && matching.length > 0;
    more.hidden = matching.length <= 6;
    more.setAttribute('aria-expanded', String(expanded));
    more.textContent = expanded ? 'Show fewer changes' : 'Show ' + (matching.length - 6) + ' more changes';
  };

  more.addEventListener('click', () => {
    expanded = !expanded;
    renderVisitFeed();
  });
  const rememberLeaving = () => {
    try {
      const nowIso = new Date().toISOString();
      sessionStorage.setItem(keys.hidden, nowIso);
      localStorage.setItem(keys.left, nowIso);
    } catch (_) {}
  };
  window.addEventListener('pagehide', rememberLeaving);
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'hidden') {
      rememberLeaving();
    } else {
      expanded = false;
      openVisit();
      renderVisitFeed();
    }
  });
  if (document.visibilityState === 'visible') {
    openVisit();
  } else {
    try { visitBaseline = sessionStorage.getItem(keys.baseline) || ''; }
    catch (_) { storageAvailable = false; }
  }
  renderVisitFeed();
}
