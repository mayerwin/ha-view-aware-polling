// View-Aware Polling - global frontend module.
// Refreshes watched entities (homeassistant.update_entity) ONLY while they are on
// screen, and only while the tab is visible. Each target is refreshed at its OWN
// native polling interval (read from the integration's coordinator on the backend).
//
// Per unit `scope`:
//   any     : refresh whenever any Lovelace dashboard is the active view.
//   visible : refresh on a dashboard only if that view actually contains the entity.
// A more-info dialog open for the entity, or its device page, always refreshes it.
(function () {
  "use strict";
  const WS_TYPE = "view_aware_polling/config";
  const TICK_MS = 1000; // how often we check per-target timers
  const REFETCH_MS = 30000; // re-pull config (native intervals settle, option edits)

  let cfg = null; // {units, eid_rep, device_reps, has_visible, repInterval:{rep:ms}, fallback:ms}
  let moreInfo = null;
  let viewCache = { key: null, set: new Set() };
  const lastRefresh = {}; // rep -> ms timestamp

  function root() {
    return document.querySelector("home-assistant");
  }
  function hass() {
    const r = root();
    return r && r.hass;
  }
  function onLovelace(h) {
    const seg = location.pathname.split("/")[1] || "lovelace";
    const panel = h.panels && h.panels[seg];
    return !!(panel && panel.component_name === "lovelace");
  }

  function collect(node, set) {
    if (!node) return;
    if (typeof node === "string") {
      if (/^[a-z_]+\.[a-z0-9_]+$/.test(node)) set.add(node);
      return;
    }
    if (Array.isArray(node)) {
      node.forEach((n) => collect(n, set));
      return;
    }
    if (typeof node === "object") {
      for (const k in node) collect(node[k], set);
    }
  }

  async function refreshViewSet(h) {
    if (!cfg || !cfg.has_visible || !onLovelace(h)) {
      viewCache = { key: "__none__", set: new Set() };
      return;
    }
    const parts = location.pathname.split("/").filter(Boolean);
    const urlPath = parts[0];
    const viewSeg = parts[1];
    const key = urlPath + "|" + (viewSeg || "");
    if (viewCache.key === key) return;
    try {
      const lc = await h.connection.sendMessagePromise({
        type: "lovelace/config",
        url_path: urlPath === "lovelace" ? null : urlPath,
      });
      const views = lc.views || [];
      let view;
      if (viewSeg && /^\d+$/.test(viewSeg)) view = views[parseInt(viewSeg, 10)];
      else if (viewSeg) view = views.find((v) => v.path === viewSeg);
      if (!view) view = views[0];
      const set = new Set();
      collect(view, set);
      viewCache = { key, set };
    } catch (e) {
      viewCache = { key, set: new Set() };
    }
  }

  function viewedReps(h) {
    const out = new Set();
    if (moreInfo && cfg.eid_rep[moreInfo]) out.add(cfg.eid_rep[moreInfo]);
    const m = location.pathname.match(/\/config\/devices\/device\/([^/?#]+)/);
    if (m && cfg.device_reps[m[1]]) out.add(cfg.device_reps[m[1]]);
    if (onLovelace(h)) {
      for (const u of cfg.units) {
        if (u.scope === "any") out.add(u.rep);
        else if (u.entities.some((e) => viewCache.set.has(e))) out.add(u.rep);
      }
    }
    return out;
  }

  function repMs(rep) {
    return cfg.repInterval[rep] || cfg.fallback;
  }

  function tick(force) {
    if (document.visibilityState !== "visible") return;
    const h = hass();
    if (!h || !cfg) return;
    const reps = viewedReps(h);
    const now = Date.now();
    const due = [];
    for (const rep of reps) {
      if (force || now - (lastRefresh[rep] || 0) >= repMs(rep)) {
        due.push(rep);
        lastRefresh[rep] = now;
      }
    }
    if (due.length) {
      h.callService("homeassistant", "update_entity", { entity_id: due });
    }
  }

  window.addEventListener("hass-more-info", (e) => {
    moreInfo = (e.detail && e.detail.entityId) || null;
    if (moreInfo) tick(true);
  });
  window.addEventListener("dialog-closed", (e) => {
    if (e.detail && e.detail.dialog === "ha-more-info-dialog") moreInfo = null;
  });
  window.addEventListener("location-changed", async () => {
    moreInfo = null;
    const h = hass();
    if (h) await refreshViewSet(h);
    tick(true);
  });
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible") tick(true);
  });

  function applyConfig(c) {
    const units = (c.units || []).map((u) => ({
      rep: u.rep,
      scope: u.scope,
      entities: u.entities || [],
      interval: u.interval,
    }));
    const fallback = (c.fallback_interval || 30) * 1000;
    const repInterval = {};
    units.forEach((u) => {
      repInterval[u.rep] = (u.interval || c.fallback_interval || 30) * 1000;
    });
    cfg = {
      units,
      eid_rep: c.eid_rep || {},
      device_reps: c.device_reps || {},
      has_visible: !!c.has_visible,
      repInterval,
      fallback,
    };
  }

  async function fetchConfig() {
    const h = hass();
    if (!h || !h.connection) return false;
    try {
      applyConfig(await h.connection.sendMessagePromise({ type: WS_TYPE }));
      const hh = hass();
      if (hh) await refreshViewSet(hh);
      return true;
    } catch (e) {
      return false;
    }
  }

  async function boot() {
    if (!(await fetchConfig())) return false;
    setInterval(() => tick(false), TICK_MS);
    setInterval(fetchConfig, REFETCH_MS);
    tick(true);
    // eslint-disable-next-line no-console
    console.info(
      "[view_aware_polling] active: " + cfg.units.length + " units (each at its native interval)"
    );
    return true;
  }

  const bootTimer = setInterval(async () => {
    if (await boot()) clearInterval(bootTimer);
  }, 1500);
})();
