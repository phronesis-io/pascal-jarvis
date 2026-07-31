(function () {
  'use strict';

  if (window.__jarvisNavigationGuardInstalled) return;
  window.__jarvisNavigationGuardInstalled = true;
  document.documentElement.setAttribute(
    'data-jarvis-navigation-guard',
    'ready'
  );

  var navigating = false;
  var releaseTimer = null;

  function releaseNavigation() {
    navigating = false;
    if (releaseTimer !== null) {
      window.clearTimeout(releaseTimer);
      releaseTimer = null;
    }
    document.documentElement.classList.remove('jarvis-navigating');
    document.documentElement.removeAttribute('aria-busy');
  }

  window.addEventListener('pageshow', releaseNavigation);

  document.addEventListener('click', function (event) {
    var origin = event.target;
    if (!origin || typeof origin.closest !== 'function') return;

    var link = origin.closest('a[href]');
    if (!link || link.hasAttribute('download')) return;
    var linkTarget = link.getAttribute('target');
    if (linkTarget && linkTarget !== '_self') return;

    var destination;
    try {
      destination = new URL(link.href, window.location.href);
    } catch (_error) {
      return;
    }
    if (destination.origin !== window.location.origin) return;

    var sameDocument = (
      destination.pathname === window.location.pathname
      && destination.search === window.location.search
    );
    if (sameDocument) {
      if (destination.hash === window.location.hash) {
        event.preventDefault();
        event.stopImmediatePropagation();
      }
      return;
    }

    if (navigating) {
      event.preventDefault();
      event.stopImmediatePropagation();
      return;
    }

    navigating = true;
    document.documentElement.classList.add('jarvis-navigating');
    document.documentElement.setAttribute('aria-busy', 'true');
    releaseTimer = window.setTimeout(releaseNavigation, 8000);
  }, true);
}());
