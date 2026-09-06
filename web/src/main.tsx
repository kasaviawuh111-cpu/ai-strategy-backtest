import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'

import App from './App'
import { AppProviders } from './app/AppProviders'
import { PortfolioReviewPage } from './portfolio-review/PortfolioReviewPage'
import { isPortfolioReviewPath } from './portfolio-review/route'
import { resolveInstrumentContext } from './shared/instrument-context'

const root = document.getElementById('root')
if (!root) throw new Error('Missing #root element')

const backtestApp = () => {
  const instrumentContext = resolveInstrumentContext(window.location.search)
  return (
    <AppProviders>
      <App
        instrument={instrumentContext.instrument}
        instrumentContextSource={instrumentContext.source}
        instrumentContextError={instrumentContext.error}
      />
    </AppProviders>
  )
}

createRoot(root).render(
  <StrictMode>
    {isPortfolioReviewPath(window.location.pathname)
      ? <PortfolioReviewPage />
      : backtestApp()}
  </StrictMode>,
)
