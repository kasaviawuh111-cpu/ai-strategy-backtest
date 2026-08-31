import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'

import App from './App'
import { AppProviders } from './app/AppProviders'
import { resolveInstrumentContext } from './shared/instrument-context'

const root = document.getElementById('root')
if (!root) throw new Error('Missing #root element')
const instrumentContext = resolveInstrumentContext(window.location.search)

createRoot(root).render(
  <StrictMode>
    <AppProviders>
      <App
        instrument={instrumentContext.instrument}
        instrumentContextError={instrumentContext.error}
      />
    </AppProviders>
  </StrictMode>,
)
