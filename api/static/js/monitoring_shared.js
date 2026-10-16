/* AlfaWay — helpers PARTAGÉS des boards de supervision (vanilla, aucune dépendance).
   Exposé en global `window.MShared`. Portée strictement présentation :
   - formatters date/heure/octets/durée + échappement HTML (mêmes conventions
     d'affichage sur les trois boards, cf. décision §5) ;
   - menu de layout (thème visuel black/generali) persisté en localStorage ;
   - dictionnaire i18n des libellés STRUCTURELS communs (nav croisée) EN/FR/IT,
     consommé par les trois boards (décision §4).
   Le vocabulaire MÉTIER propre à chaque board reste dans son template. */
(function () {
  'use strict';

  // Libellés structurels communs (nav croisée). Les boards métier ne toggle qu'EN/FR ;
  // `it` est fourni pour le board fichier (EN/FR/IT). Repli sur `en`.
  var NAV_I18N = {
    en: { nav_files: 'Files',    nav_business: 'Business', nav_expected: 'NAV expected',  nav_admin: '⚙ Admin' },
    fr: { nav_files: 'Fichiers', nav_business: 'Métier',   nav_expected: 'VNI attendues', nav_admin: '⚙ Admin' },
    it: { nav_files: 'File',     nav_business: 'Business',  nav_expected: 'NAV attese',    nav_admin: '⚙ Admin' },
  };

  // Échappement HTML (variante stricte : échappe aussi `"`, sûre en contenu ET attribut).
  function esc(s) {
    return (s == null ? '' : String(s)).replace(/[&<>"]/g,
      function (c) { return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]; });
  }

  // Heure puis date, locale courante (convention historique du board fichier).
  function fmtTime(iso, locale) {
    if (!iso) return '—';
    var d = new Date(iso);
    return d.toLocaleTimeString(locale) + ' ' + d.toLocaleDateString(locale);
  }

  // Jour seul (date ISO `YYYY-MM-DD`), ancré à minuit local pour éviter le décalage TZ.
  function fmtDay(iso, locale) {
    if (!iso) return '—';
    return new Date(iso + 'T00:00:00').toLocaleDateString(locale);
  }

  // Taille lisible. `labels` = { bytes_unit, units:[K,M,G,T] } fournis par le board
  // (traduits) ; repli sur des unités anglaises si absents.
  function fmtBytes(n, locale, labels) {
    if (n == null) return '—';
    var unit = (labels && labels.bytes_unit) || 'B';
    var units = (labels && labels.units) || ['KB', 'MB', 'GB', 'TB'];
    if (n < 1024) return n + ' ' + unit;
    var i = -1;
    do { n /= 1024; i++; } while (n >= 1024 && i < units.length - 1);
    return n.toLocaleString(locale, { minimumFractionDigits: 1, maximumFractionDigits: 1 })
      + ' ' + units[i];
  }

  // Durée de transfert (ms) → ms / s / min s.
  function fmtDuration(ms, locale) {
    if (ms == null) return '—';
    var num = function (v, d) {
      return v.toLocaleString(locale, { minimumFractionDigits: d, maximumFractionDigits: d });
    };
    if (ms < 1000) return ms + ' ms';
    if (ms < 60000) return num(ms / 1000, 2) + ' s';
    return Math.floor(ms / 60000) + ' min ' + Math.round((ms % 60000) / 1000) + ' s';
  }

  // Localise la nav croisée : remplit les `[data-i18n-nav]` du partial commun.
  function localizeNav(lang) {
    var dict = NAV_I18N[lang] || NAV_I18N.en;
    document.querySelectorAll('[data-i18n-nav]').forEach(function (el) {
      var k = el.getAttribute('data-i18n-nav');
      if (dict[k]) el.textContent = dict[k];
    });
  }

  // Layouts visuels disponibles (thèmes CSS, cf. bloc `data-layout` du CSS partagé).
  // Choix persisté côté navigateur, commun aux trois boards. `generali` = défaut (nouveau navigateur).
  var LAYOUTS = ['black', 'generali'];
  var LAYOUT_KEY = 'alfaway_layout';

  function currentLayout() {
    var v;
    try { v = localStorage.getItem(LAYOUT_KEY); } catch (e) { v = null; }
    return LAYOUTS.indexOf(v) >= 0 ? v : 'generali';
  }

  // Pose <html data-layout> + synchronise le menu ; `persist` enregistre le choix.
  function applyLayout(name, persist) {
    if (LAYOUTS.indexOf(name) < 0) name = 'generali';
    document.documentElement.setAttribute('data-layout', name);
    if (persist) { try { localStorage.setItem(LAYOUT_KEY, name); } catch (e) { /* privé */ } }
    document.querySelectorAll('[data-layout-pick]').forEach(function (el) { el.value = name; });
  }

  // Menu de layout de la nav croisée : script chargé en fin de <body>, le DOM est là.
  applyLayout(currentLayout(), false);
  document.querySelectorAll('[data-layout-pick]').forEach(function (el) {
    el.addEventListener('change', function () { applyLayout(el.value, true); });
  });

  window.MShared = {
    LAYOUTS: LAYOUTS,
    applyLayout: applyLayout,
    NAV_I18N: NAV_I18N,
    esc: esc,
    fmtTime: fmtTime,
    fmtDay: fmtDay,
    fmtBytes: fmtBytes,
    fmtDuration: fmtDuration,
    localizeNav: localizeNav,
  };
})();
