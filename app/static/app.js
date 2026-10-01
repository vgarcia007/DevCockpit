document.querySelectorAll('a[href]').forEach(link => {
  if (new URL(link.href).hostname === 'github.com') {
    link.target = '_blank';
    link.relList.add('noopener', 'noreferrer');
  }
});

const syncLabel = document.getElementById('sync-label');
if (syncLabel) {
  const renderedGitHubSync = syncLabel.dataset.lastSuccess || '';
  const renderedOtrsSync = syncLabel.dataset.otrsLastSuccess || '';
  const renderedZabbixRevision = syncLabel.dataset.zabbixRevision || '';
  let sources = JSON.parse(document.getElementById('sync-sources-data').textContent);
  const rows = new Map([...document.querySelectorAll('[data-sync-source]')].map(row => [row.dataset.syncSource, row]));
  let syncWasRunning = false;
  let unavailable = false;
  let serverOffsetMs = Date.parse(syncLabel.dataset.serverTime) - Date.now();
  const remaining = target => Math.max(0, Math.ceil((Date.parse(target) - Date.now() - serverOffsetMs) / 1000));
  const duration = seconds => {
    const hours = Math.floor(seconds / 3600);
    const minutes = Math.floor(seconds % 3600 / 60);
    const rest = seconds % 60;
    return hours ? `${hours}:${String(minutes).padStart(2, '0')}:${String(rest).padStart(2, '0')}` :
      `${String(minutes).padStart(2, '0')}:${String(rest).padStart(2, '0')}`;
  };
  const renderSources = () => {
    for (const source of sources) {
      const row = rows.get(source.id);
      if (!row) continue;
      const state = unavailable ? 'unavailable' : source.state;
      row.className = `sync-source is-${state}`;
      const labels = {running: 'Syncing…', paused: 'Paused', error: 'Error', unavailable: 'Unavailable'};
      row.querySelector('.sync-source-value').textContent = labels[state] ||
        (source.next_sync_at ? duration(remaining(source.next_sync_at)) : 'Waiting');
      row.querySelector('.sync-details').textContent = unavailable ?
        'Sync status unavailable. Showing last known details.\n' + source.details : source.details;
    }

  };
  const refreshSyncStatus = async () => {
    try {
      const response = await fetch('/sync/status', {cache: 'no-store'});
      if (!response.ok) throw new Error('Sync status unavailable');
      const data = await response.json();
      serverOffsetMs = Date.parse(data.server_time) - Date.now();
      sources = data.sources;
      unavailable = false;
      if ((data.last_success || '') !== renderedGitHubSync ||
          (data.otrs_last_success || '') !== renderedOtrsSync ||
          (data.zabbix_revision || '') !== renderedZabbixRevision ||
          (syncWasRunning && !data.running)) {
        window.location.reload();
        return;
      }
      syncWasRunning = data.running;
    } catch (_) { unavailable = true; }
    renderSources();
  };
  for (const row of rows.values()) {
    row.addEventListener('keydown', event => {
      if (event.key === 'Escape') row.dataset.tooltipDismissed = 'true';
    });
    for (const event of ['mouseenter', 'focus', 'blur', 'mouseleave']) {
      row.addEventListener(event, () => delete row.dataset.tooltipDismissed);
    }
  }
  renderSources();
  setInterval(refreshSyncStatus, 10000);
  setInterval(renderSources, 1000);
  refreshSyncStatus();
}

(() => {
  const trigger = document.getElementById('notification-trigger');
  if (!trigger) return;
  const panel = document.getElementById('notification-panel');
  const badge = document.getElementById('notification-count');
  const list = document.getElementById('notification-list');
  const status = document.getElementById('notification-status');
  const more = document.getElementById('notification-more');
  const desktopButton = document.getElementById('notification-desktop');
  const desktopStatus = document.getElementById('notification-desktop-status');
  const toast = document.getElementById('notification-toast');
  const keys = {read: 'cockpit-notifications-read-at', alerted: 'cockpit-notifications-alerted-at', desktop: 'cockpit-desktop-notifications'};
  const memory = {};
  const get = key => { try { return localStorage.getItem(key) || memory[key] || null; } catch (_) { return memory[key] || null; } };
  const set = (key, value) => { memory[key] = value; try { localStorage.setItem(key, value); } catch (_) {} };
  let nextBefore = null;
  let checking = false;
  let lastCheck = 0;
  let toastTimer;
  let otrsOrigin = null;

  const countLabel = n => `${n} new change${n === 1 ? '' : 's'}`;
  const updateBadge = n => {
    badge.hidden = n === 0;
    badge.textContent = n > 99 ? '99+' : String(n);
    trigger.setAttribute('aria-label', n ? `Notifications, ${countLabel(n)}` : 'Notifications');
  };
  const updateDesktop = () => {
    const available = 'Notification' in window && window.isSecureContext;
    const enabled = available && Notification.permission === 'granted' && get(keys.desktop) === 'true';
    desktopButton.textContent = enabled ? 'Turn off browser alerts' : 'Enable browser alerts';
    desktopButton.disabled = !available || (!enabled && Notification.permission === 'denied');
    desktopStatus.textContent = !available ? 'Browser alerts are unavailable here.' :
      Notification.permission === 'denied' ? 'Browser alerts are blocked in browser settings.' :
        enabled ? 'One grouped alert for new changes while this tab is in the background.' :
          'Optional. Alerts work while this tab is open.';
  };
  const render = (events, append = false) => {
    if (!append) list.replaceChildren();
    if (!events.length && !append) {
      const empty = document.createElement('p');
      empty.className = 'notification-status';
      empty.textContent = 'No changes in the last 30 days.';
      list.append(empty);
    }
    for (const event of events) {
      const item = document.createElement('a');
      item.className = 'notification-item';
      let safeUrl;
      try { safeUrl = new URL(event.url); } catch (_) {}
      if (safeUrl?.protocol === 'https:' &&
          ((event.source === 'otrs' && safeUrl.origin === otrsOrigin) ||
           (event.source !== 'otrs' && safeUrl.hostname === 'github.com'))) {
        item.href = safeUrl.href;
        item.target = '_blank';
        item.rel = 'noopener noreferrer';
      }
      const heading = document.createElement('strong');
      heading.textContent = event.title;
      const detail = document.createElement('small');
      detail.textContent = `${event.source === 'otrs' ? 'OTRS · ' : ''}${event.repository} · ${event.detail} · ${new Date(event.observed_at).toLocaleString('de-DE')}`;
      item.append(heading, detail);
      list.append(item);
    }
  };
  const readEvents = async before => {
    const params = new URLSearchParams();
    if (get(keys.read)) params.set('since', get(keys.read));
    if (get(keys.alerted)) params.set('alert_since', get(keys.alerted));
    if (before) params.set('before', before);
    const response = await fetch(`/notifications?${params}`, {cache: 'no-store'});
    if (!response.ok) throw new Error('Notifications unavailable');
    const data = await response.json();
    otrsOrigin = data.otrs_origin;
    return data;
  };
  const showToast = n => {
    document.getElementById('notification-toast-text').textContent = countLabel(n) + ' since the last update';
    toast.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { toast.hidden = true; }, 10000);
  };
  const alertOnce = async (since, asOf, count) => {
    const claim = async () => {
      if (get(keys.alerted) !== since) return;
      set(keys.alerted, asOf);
      if (document.visibilityState === 'visible') {
        if (panel.hidden) showToast(count);
      } else if ('Notification' in window && Notification.permission === 'granted' && get(keys.desktop) === 'true') {
        try {
          const notice = new Notification('Dev-Cockpit', {body: countLabel(count) + ' since the last update', tag: 'gitdash-changes'});
          notice.onclick = () => { window.focus(); openPanel(); notice.close(); };
        } catch (_) {}
      }
    };
    if (navigator.locks?.request) {
      await navigator.locks.request('gitdash-notification-alert', claim);
    } else {
      // Claim the alert in shared storage so only the winning tab displays it.
      if (get(keys.alerted) !== since) return;
      const claimKey = 'cockpit-notifications-alert-claim';
      const token = `${Date.now()}-${Math.random()}`;
      set(claimKey, token);
      set(keys.alerted, asOf);
      await new Promise(resolve => setTimeout(resolve, 60));
      if (get(keys.alerted) === asOf && get(claimKey) === token) {
        if (document.visibilityState === 'visible') { if (panel.hidden) showToast(count); }
        else if ('Notification' in window && Notification.permission === 'granted' && get(keys.desktop) === 'true') {
          try { new Notification('Dev-Cockpit', {body: countLabel(count) + ' since the last update', tag: 'gitdash-changes'}); } catch (_) {}
        }
      }
    }
  };
  const check = async (force = false) => {
    if (checking || (!force && Date.now() - lastCheck < 5000)) return;
    checking = true;
    try {
      const read = get(keys.read);
      const alerted = get(keys.alerted);
      const data = await readEvents();
      lastCheck = Date.now();
      if (!read || !alerted) {
        if (!get(keys.read)) set(keys.read, data.as_of);
        if (!get(keys.alerted)) set(keys.alerted, data.as_of);
        updateBadge(0);
      } else {
        if (get(keys.read) === read) updateBadge(data.unread_count);
        if (data.new_count && get(keys.alerted) === alerted &&
            (document.visibilityState === 'visible' || ('Notification' in window && Notification.permission === 'granted' && get(keys.desktop) === 'true'))) {
          const syncResponse = await fetch('/sync/status', {cache: 'no-store'});
          if (syncResponse.ok) {
            const syncState = await syncResponse.json();
            if (!syncState.running && !syncState.otrs_running) {
              await alertOnce(alerted, data.as_of, data.new_count);
            }
          }
        }
      }
      if (!panel.hidden) {
        render(data.events);
        nextBefore = data.next_before;
        more.hidden = !nextBefore;
        set(keys.read, data.as_of);
        updateBadge(0);
        status.textContent = `${data.events.length} recent changes`;
      }
    } catch (_) {
      if (!panel.hidden) status.textContent = 'Could not load notifications.';
    } finally { checking = false; }
  };
  const openPanel = async () => {
    panel.hidden = false;
    trigger.setAttribute('aria-expanded', 'true');
    toast.hidden = true;
    status.textContent = 'Loading…';
    updateDesktop();
    try {
      const data = await readEvents();
      render(data.events);
      nextBefore = data.next_before;
      more.hidden = !nextBefore;
      set(keys.read, data.as_of);
      updateBadge(0);
      status.textContent = `${data.events.length} recent changes`;
    } catch (_) { status.textContent = 'Could not load notifications.'; }
  };
  const closePanel = () => { panel.hidden = true; trigger.setAttribute('aria-expanded', 'false'); };
  trigger.addEventListener('click', () => panel.hidden ? openPanel() : closePanel());
  document.getElementById('notification-close').addEventListener('click', closePanel);
  document.addEventListener('click', event => { if (!panel.hidden && !event.target.closest('.notification-control')) closePanel(); });
  document.addEventListener('keydown', event => { if (event.key === 'Escape' && !panel.hidden) { closePanel(); trigger.focus(); } });
  more.addEventListener('click', async () => {
    if (!nextBefore) return;
    more.disabled = true;
    try {
      const data = await readEvents(nextBefore);
      render(data.events, true);
      nextBefore = data.next_before;
      more.hidden = !nextBefore;
    } catch (_) { status.textContent = 'Could not load older notifications.'; }
    finally { more.disabled = false; }
  });
  desktopButton.addEventListener('click', async () => {
    if (!('Notification' in window)) return;
    if (get(keys.desktop) === 'true') set(keys.desktop, 'false');
    else {
      try { if ((await Notification.requestPermission()) === 'granted') set(keys.desktop, 'true'); } catch (_) {}
    }
    updateDesktop();
  });
  document.getElementById('notification-toast-open').addEventListener('click', openPanel);
  document.getElementById('notification-toast-close').addEventListener('click', () => { toast.hidden = true; });
  window.addEventListener('storage', event => { if (event.key === keys.read || event.key === keys.alerted) check(true); if (event.key === keys.desktop) updateDesktop(); });
  document.addEventListener('visibilitychange', () => { if (document.visibilityState === 'visible') check(true); });
  window.addEventListener('focus', () => check());
  setInterval(() => check(true), 30000);
  updateDesktop();
  check(true);
})();

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

const releaseNodes = document.querySelectorAll('[data-release-node]');
if (releaseNodes.length) {
  let selected = null;
  for (const node of releaseNodes) {
    node.addEventListener('click', () => {
      if (selected) {
        selected.setAttribute('aria-expanded', 'false');
        document.getElementById(selected.getAttribute('aria-controls')).hidden = true;
      }
      if (selected === node) {
        selected = null;
        return;
      }
      selected = node;
      node.setAttribute('aria-expanded', 'true');
      const notes = document.getElementById(node.getAttribute('aria-controls'));
      notes.hidden = false;
      notes.scrollIntoView({block: 'nearest', behavior: 'smooth'});
    });
  }
}

const visitFeed = document.getElementById('visit-feed');
if (visitFeed) {
  const keys = {
    baseline: 'cockpit-session-visit-baseline',
    started: 'cockpit-session-visit-started',
    hidden: 'cockpit-session-visit-hidden',
    previous: 'cockpit-last-visit',
    left: 'cockpit-last-left',
    completed: 'cockpit-completed-changes',
  };
  const idleThresholdMs = 30 * 60 * 1000;
  const rows = [...visitFeed.querySelectorAll('[data-change-key]')];
  const visitBody = document.getElementById('visit-body');
  const completedSection = document.getElementById('visit-completed');
  const completedFeed = document.getElementById('visit-completed-feed');
  const completedToggle = document.getElementById('visit-completed-toggle');
  const actionStatus = document.getElementById('visit-action-status');
  const storageWarning = document.getElementById('visit-storage-warning');
  const empty = document.getElementById('visit-empty');
  const intro = document.getElementById('visit-intro');
  const count = document.getElementById('visit-count');
  const more = document.getElementById('visit-more');
  let visitBaseline = '';
  let storageAvailable = true;
  let completionStorageAvailable = true;
  let matching = [];
  let expanded = false;
  let completedExpanded = false;
  let completed = new Set();

  const validTimestamp = value => value && Number.isFinite(Date.parse(value));
  const loadCompleted = () => {
    try {
      const stored = JSON.parse(localStorage.getItem(keys.completed) || '[]');
      const cutoff = Date.now() - 30 * 24 * 60 * 60 * 1000;
      completed = new Set(Array.isArray(stored) ? stored.filter(key =>
        typeof key === 'string' && /^\d+:\d+$/.test(key) && Number(key.split(':')[1]) >= cutoff) : []);
      completionStorageAvailable = true;
    } catch (_) { completionStorageAvailable = false; }
    storageWarning.hidden = completionStorageAvailable;
  };
  const saveCompleted = () => {
    try { localStorage.setItem(keys.completed, JSON.stringify([...completed])); completionStorageAvailable = true; }
    catch (_) { completionStorageAvailable = false; }
    storageWarning.hidden = completionStorageAvailable;
  };

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
    const open = matching.filter(row => !completed.has(row.dataset.changeKey));
    const done = matching.filter(row => completed.has(row.dataset.changeKey));
    rows.forEach(row => { row.hidden = true; });
    rows.forEach(row => {
      const isDone = completed.has(row.dataset.changeKey);
      const button = row.querySelector('[data-visit-action]');
      button.dataset.visitAction = isDone ? 'restore' : 'complete';
      button.textContent = isDone ? 'Restore' : 'Done';
      button.setAttribute('aria-label', `${isDone ? 'Restore' : 'Mark'} ${row.querySelector('strong').textContent}${isDone ? '' : ' as completed'}`);
      (isDone ? completedFeed : visitFeed).append(row);
    });
    open.slice(0, expanded ? undefined : 6).forEach(row => { row.hidden = false; });
    if (completedExpanded) done.forEach(row => { row.hidden = false; });
    count.textContent = firstVisit ? '—' : String(open.length);
    intro.textContent = !storageAvailable ? 'Visit comparison is unavailable in this browser.' :
      firstVisit ? 'Visit comparison starts after this visit.' :
        'Observed since ' + new Date(since).toLocaleString('de-DE');
    empty.textContent = !storageAvailable ?
      'Browser storage is blocked, so previous visits cannot be remembered.' :
      firstVisit ?
        'This browser has no previous visit yet. Changes observed by GitHub sync will appear after you return.' :
        done.length ? 'All changes since your last visit are completed.' :
          'No relevant changes observed by GitHub sync since your last visit.';
    empty.hidden = !firstVisit && open.length > 0;
    more.hidden = open.length <= 6;
    more.setAttribute('aria-expanded', String(expanded));
    more.textContent = expanded ? 'Show fewer changes' : 'Show ' + (open.length - 6) + ' more changes';
    completedSection.hidden = done.length === 0;
    completedFeed.hidden = !completedExpanded || done.length === 0;
    completedToggle.textContent = `Completed ${done.length}`;
    completedToggle.setAttribute('aria-expanded', String(completedExpanded && done.length > 0));
  };

  loadCompleted();
  visitBody.addEventListener('click', event => {
    const button = event.target.closest('[data-visit-action]');
    if (!button) return;
    const row = button.closest('[data-change-key]');
    const wasCompleted = completed.has(row.dataset.changeKey);
    if (wasCompleted) completed.delete(row.dataset.changeKey);
    else completed.add(row.dataset.changeKey);
    saveCompleted();
    renderVisitFeed();
    actionStatus.textContent = `${row.querySelector('strong').textContent} ${wasCompleted ? 'restored' : 'completed'}.`;
    if (wasCompleted) (row.hidden ? more : button).focus();
    else completedToggle.focus();
  });
  more.addEventListener('click', () => {
    expanded = !expanded;
    renderVisitFeed();
  });
  completedToggle.addEventListener('click', () => {
    completedExpanded = !completedExpanded;
    renderVisitFeed();
  });
  window.addEventListener('storage', event => {
    if (event.key === keys.completed) { loadCompleted(); renderVisitFeed(); }
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
