import { useEffect, useRef, type CSSProperties } from 'react'

import portfolioReviewHtml from './portfolio-review.html?raw'

const frameStyle: CSSProperties = {
  position: 'fixed',
  inset: 0,
  display: 'block',
  width: '100%',
  height: '100dvh',
  border: 0,
  background: '#0a0c0b',
}

export function PortfolioReviewPage() {
  const frameRef = useRef<HTMLIFrameElement>(null)

  useEffect(() => {
    const previousTitle = document.title
    const themeColor = document.querySelector<HTMLMetaElement>('meta[name="theme-color"]')
    const previousThemeColor = themeColor?.content

    document.title = '高光复盘'
    if (themeColor) themeColor.content = '#0a0c0b'
    // Assign after mount so browsers execute the prototype's inline replay
    // engine. Rendering a script-bearing srcDoc as a React attribute leaves
    // those script nodes inert in Chromium.
    if (frameRef.current) frameRef.current.srcdoc = portfolioReviewHtml

    return () => {
      document.title = previousTitle
      if (themeColor && previousThemeColor !== undefined) {
        themeColor.content = previousThemeColor
      }
    }
  }, [])

  return (
    <iframe
      ref={frameRef}
      title="账户高光复盘"
      style={frameStyle}
      referrerPolicy="no-referrer"
    />
  )
}
