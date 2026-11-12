// The web dashboard has no Google Home page: this manifest exists to mount
// plugin_api.py, which the Desktop half calls through ctx.rest. The dashboard
// still loads the entry, so register an empty page.
;(function () {
  var plugins = window.__HERMES_PLUGINS__
  if (plugins && typeof plugins.register === 'function') {
    plugins.register('googlehome', function GoogleHomePlaceholder() {
      return null
    })
  }
})()
