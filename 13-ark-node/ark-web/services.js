/* services.js - the one list of what this node serves, and how this browser
   finds out whether each one is answering. Read by index.html (the Services
   menu) and home.html (the services page), so the two cannot drift apart.
   Added 2026-09-27; the design and its reasons are PLAN-services-home and
   DECISIONS.md 2026-09-27.

   EVERY ADDRESS IS BUILT FROM THE ONE THE PAGE WAS OPENED WITH, never from
   "localhost". On the node's own screen the two are the same machine. On a phone
   across the room "localhost" is the phone, and a link built that way leads
   nowhere - which is exactly what every citation link did until this file
   existed (fixHosts, below).

   REACHABILITY IS ASKED FROM THIS BROWSER, not reported by the node, for the
   same reason the map pane asks the tile server itself: the only question that
   matters is whether the reader's device can reach the port. A no-cors fetch
   resolves if anything answers and rejects if nothing does; its body is opaque
   and is not needed.

   THE TWO MODEL PAGES ARE BOUND TO 127.0.0.1 ON PURPOSE (bin/ark.py). From
   another device they cannot be opened, so they are shown as "this computer
   only" with the node's own report of whether they are up, and not as a link
   that cannot work. They are also drawn apart from the archive services: they
   answer from the model's memory with no lookup and no citation, which is the
   thing the rest of the surface exists to prevent. Manual chapter 5, the one
   rule.

   No framework, no build step, no external request. */
(function () {
  'use strict';
  var H = location.hostname;
  var LOCAL = H === 'localhost' || H === '127.0.0.1' || H === '[::1]' || H === '::1';

  function at(port, path) {
    return location.protocol + '//' + H + ':' + port + (path || '/');
  }

  /* THE PORTS COME FROM THE NODE (/api/ports.js, loaded just before this
     file), because ark.toml can move any server. A node that predates that
     script leaves ARK_PORTS undefined and every value below is the old one. */
  var P = { archive: 8080, tiles: 8081, node: 8090, primary: 8091, crosscheck: 8092 };
  var GIVEN = window.ARK_PORTS || {};
  Object.keys(P).forEach(function (k) {
    if (typeof GIVEN[k] === 'number') P[k] = GIVEN[k];
  });

  var LIST = [
    { id: 'search', group: 'archive', name: 'Ask the archive', href: '/',
      port: ':' + P.node, icon: 'search', newTab: false,
      desc: 'Search the archive in English or Spanish. Every answer cites the passage it used, and every passage opens its source.' },
    { id: 'library', group: 'archive', name: 'Library', href: at(P.archive, '/'),
      port: ':' + P.archive, icon: 'books', newTab: true, probe: at(P.archive, '/'),
      desc: 'Every book on the drive, page by page, in Kiwix, including the ones search does not reach.' },
    { id: 'collections', group: 'archive', name: 'Collections', href: '/library/',
      port: '/library/', icon: 'shelves', newTab: false,
      desc: 'What the library holds: which collections answers can cite, which you can only browse, and how to add the rest.' },
    { id: 'map', group: 'archive', name: 'Map', href: '/map/',
      port: ':' + P.node + ' + :' + P.tiles, icon: 'map', newTab: true,
      probe: at(P.tiles, '/20260825/0/0/0.mvt'),
      desc: 'The whole planet with terrain, and a gazetteer that finds a place by its name.' },
    { id: 'primary', group: 'model', name: 'Qwen, direct', href: at(P.primary, '/'),
      port: ':' + P.primary, icon: 'chat', newTab: true, localOnly: true,
      probe: at(P.primary, '/health'), role: 'primary',
      desc: 'The primary model on its own. Fast, on the graphics card.' },
    { id: 'crosscheck', group: 'model', name: 'Gemma, direct', href: at(P.crosscheck, '/'),
      port: ':' + P.crosscheck, icon: 'chat2', newTab: true, localOnly: true,
      probe: at(P.crosscheck, '/health'), role: 'crosscheck',
      desc: 'The cross-check model on its own, a separate lineage. Slow: it runs without a graphics card.' },
    { id: 'status', group: 'node', name: 'Node status', href: '/home/#status',
      port: '/api/health', icon: 'pulse', newTab: false,
      desc: 'What is running, what the index holds, and anything degraded.' }
  ];

  var ICONS = {
    search: '<circle cx="14" cy="14" r="8"/><path d="M20 20l8 8"/>',
    books: '<path d="M6 6v22M11 6v22M16 8l5 20M24 6h4v22h-4z"/>',
    map: '<path d="M4 8l8-3 10 3 8-3v21l-8 3-10-3-8 3z"/><path d="M12 5v21M22 8v21"/>',
    chat: '<path d="M5 7h24v15H14l-6 5v-5H5z"/><path d="M11 14h12"/>',
    chat2: '<path d="M5 7h24v15H14l-6 5v-5H5z"/><path d="M11 12h12M11 17h8"/>',
    pulse: '<path d="M3 18h7l3-8 5 15 4-10 2 3h7"/>',
    shelves: '<path d="M4 15h26M4 28h26"/><path d="M7 6v9M11 6v9M15 8v7M20 5l4 10M8 19v9M12 19v9M18 21v7M23 19v9M27 20v8"/>'
  };
  function icon(name, cls) {
    return '<svg class="' + (cls || 'ic') + '" viewBox="0 0 34 34" aria-hidden="true">'
      + (ICONS[name] || '') + '</svg>';
  }

  function probe(url, ms) {
    return new Promise(function (resolve) {
      var done = false;
      var t = setTimeout(function () { if (!done) { done = true; resolve(false); } }, ms || 2500);
      fetch(url, { mode: 'no-cors', cache: 'no-store' }).then(function () {
        if (!done) { done = true; clearTimeout(t); resolve(true); }
      }).catch(function () {
        if (!done) { done = true; clearTimeout(t); resolve(false); }
      });
    });
  }

  function health() {
    return fetch('/api/health', { cache: 'no-store' })
      .then(function (r) { return r.json(); })
      .catch(function () { return null; });
  }

  /* THE MODEL'S OWN NAME, without the quantisation tag: "Phi-4-mini Q4_K_M"
     becomes "Phi-4-mini". The 8gb profile does not answer with Qwen. */
  function shortName(n) {
    return String(n || '').replace(/\s+\S*Q\d\S*$/i, '').replace(/\s+(B?F16|F32)$/i, '');
  }

  /* One pass over every service. Resolves to { health, state: {id: {level,
     text, name?}} } where level is 'up', 'down', 'far' (running, but only on
     the node's own screen) or 'off' (this kit does not install it: the starter
     has no maps and no cross-check model, and drawing those red told a
     stranger a correct install was broken, 2026-10-03). */
  function check() {
    return health().then(function (h) {
      var off = (h && h.not_installed) || [];
      var jobs = LIST.map(function (s) {
        if (off.indexOf(s.role || s.id) >= 0) {
          return Promise.resolve({ level: 'off', text: 'not installed' });
        }
        if (s.id === 'search') {
          return Promise.resolve(h ? { level: 'up', text: 'running' }
                                   : { level: 'down', text: 'not answering' });
        }
        /* THE LIBRARY'S OWN COUNTS (corpus.py via /api/health). Broken is the
           only fault: not installed is a choice, browsable-only is a state. */
        if (s.id === 'collections') {
          var c = h && h.corpus;
          if (!c || c.error) return Promise.resolve({ level: h ? 'warn' : 'down',
            text: h ? 'unknown' : 'node not answering' });
          return Promise.resolve(c.red
            ? { level: 'warn', text: c.red + ' broken · ' + c.green + ' citable' }
            : { level: 'up', text: c.green + ' citable · ' + c.amber + ' browsable' });
        }
        if (s.id === 'status') {
          if (!h) return Promise.resolve({ level: 'down', text: 'node not answering' });
          var n = (h.degraded || []).length;
          return Promise.resolve(n ? { level: 'warn', text: n + ' degraded' }
                                   : { level: 'up', text: 'nothing degraded' });
        }
        if (s.localOnly && !LOCAL) {
          var m = h && h.models && h.models[s.role];
          return Promise.resolve(m && m.ok
            ? { level: 'far', text: 'running · this computer only' }
            : { level: 'down', text: (m ? 'not running' : 'unknown') + ' · this computer only' });
        }
        return probe(s.probe).then(function (ok) {
          return ok ? { level: 'up', text: 'running' } : { level: 'down', text: 'not answering' };
        });
      });
      return Promise.all(jobs).then(function (res) {
        var state = {};
        LIST.forEach(function (s, i) {
          var m = s.role && h && h.models && h.models[s.role];
          if (m && m.name) res[i].name = shortName(m.name) + ', direct';
          state[s.id] = res[i];
        });
        return { health: h, state: state };
      });
    });
  }

  /* CITATION LINKS NAME localhost:8080 BECAUSE THE SERVER BUILDS THEM
     (store.py, ARK_KIWIX). On the node that works; on a phone it points at the
     phone. Rewrite any link that names localhost to the address this page was
     opened with, and its visible text if the text was the address. Runs on
     everything already on the page and on everything added later. Does
     nothing at all on the node's own screen. */
  var LOOPBACK = { 'localhost': 1, '127.0.0.1': 1, '[::1]': 1, '::1': 1 };
  function fixOne(a) {
    var href = a.getAttribute('href');
    if (!href || href.indexOf('//') < 0) return;
    var u;
    try { u = new URL(href, location.href); } catch (e) { return; }
    if (!LOOPBACK[u.hostname]) return;
    var before = u.href;
    u.hostname = H;
    a.setAttribute('href', u.href);
    if (a.textContent === href || a.textContent === before) a.textContent = u.href;
  }
  function fixHosts(root) {
    if (LOCAL || !window.MutationObserver) return;
    (root || document).querySelectorAll('a[href]').forEach(fixOne);
    new MutationObserver(function (muts) {
      muts.forEach(function (m) {
        m.addedNodes.forEach(function (n) {
          if (n.nodeType !== 1) return;
          if (n.tagName === 'A') fixOne(n);
          n.querySelectorAll && n.querySelectorAll('a[href]').forEach(fixOne);
        });
      });
    }).observe(root || document.body, { childList: true, subtree: true });
  }

  window.ARK_SERVICES = { list: LIST, icon: icon, check: check, fixHosts: fixHosts,
                          local: LOCAL, host: H, at: at, ports: P };
})();
