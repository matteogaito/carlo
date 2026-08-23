export function registerPwa(onUpdate: (activate: () => void) => void) {
  if (!('serviceWorker' in navigator)) return
  void navigator.serviceWorker.register('/sw.js').then((registration) => {
    const offer = () => registration.waiting && onUpdate(() => registration.waiting?.postMessage('SKIP_WAITING'))
    offer()
    registration.addEventListener('updatefound', () => {
      registration.installing?.addEventListener('statechange', () => {
        if (registration.installing?.state === 'installed' && navigator.serviceWorker.controller) offer()
      })
    })
  })
  let refreshing = false
  navigator.serviceWorker.addEventListener('controllerchange', () => {
    if (!refreshing) { refreshing = true; location.reload() }
  })
}
