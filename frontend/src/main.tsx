import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'

import { App } from './App'
import { registerPwa } from './pwa'

createRoot(document.getElementById('root')!).render(
  <StrictMode><App /></StrictMode>,
)

registerPwa((activate) => window.dispatchEvent(new CustomEvent('carlo-update-ready', { detail: activate })))
