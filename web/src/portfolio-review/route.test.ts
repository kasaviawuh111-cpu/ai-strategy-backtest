import { describe, expect, it } from 'vitest'

import portfolioReviewHtml from './portfolio-review.html?raw'
import { isPortfolioReviewPath } from './route'

describe('portfolio review route', () => {
  it.each(['/portfolio-review', '/portfolio-review/'])(
    'matches the exact supported path %s',
    (pathname) => {
      expect(isPortfolioReviewPath(pathname)).toBe(true)
    },
  )

  it.each(['/', '/portfolio', '/portfolio-review/more', '/portfolio-review//'])(
    'does not become a catch-all for %s',
    (pathname) => {
      expect(isPortfolioReviewPath(pathname)).toBe(false)
    },
  )
})

describe('portfolio review experience contract', () => {
  it('starts with broker ledger upload and keeps three holding fallbacks', () => {
    const entry = portfolioReviewHtml.match(
      /<div class="entry" id="entry">([\s\S]*?)<div class="app" id="review-app"/,
    )?.[1]

    expect(entry).toBeDefined()
    expect(entry).not.toContain('<button class="back"')
    expect(entry?.match(/data-way=/g)).toHaveLength(3)
    expect(entry).toContain('id="entry-upload"')
    expect(entry).toContain('上传成交 / 交割流水')
    expect(entry).toContain('持仓截图')
    expect(entry).toContain('持仓表格')
    expect(entry).toContain('手工补录')
    expect(entry).toContain('id="btn-demo"')
    expect(portfolioReviewHtml).toContain(
      '<div class="app" id="review-app" hidden>',
    )
    expect(entry).toContain('成交、资金、持仓结余和记录时间')
    expect(entry).not.toContain('净值和行情时间点')
    expect(portfolioReviewHtml).toContain('当前 OCR 服务未连接')
  })

  it('keeps the Claude replay and story animation surface', () => {
    expect(portfolioReviewHtml).toContain('id="btn-replay"')
    expect(portfolioReviewHtml).toContain('id="story" hidden')
    expect(portfolioReviewHtml).toContain('function openStory(k)')
    expect(portfolioReviewHtml).toContain('const dur=RM?1:6500')
    expect(portfolioReviewHtml).toContain('camera zoom')
  })

  it('uses parse, explicit confirmation, then structured analysis', () => {
    expect(portfolioReviewHtml).toContain(
      "const PARSE_ENDPOINT='/api/v1/portfolio-reviews/imports/parse'",
    )
    expect(portfolioReviewHtml).toContain(
      "const ANALYZE_ENDPOINT='/api/v1/portfolio-reviews/analyze'",
    )
    expect(portfolioReviewHtml).toContain('rows:decodedLedgerRows')
    expect(portfolioReviewHtml).toContain(
      'current_holding_rows:decodedHoldingRows',
    )
    expect(portfolioReviewHtml).toContain(
      'import_metadata:{...pendingImport.import_metadata,confirmed:true}',
    )
    expect(portfolioReviewHtml).not.toContain('buildApiPayload')
    expect(portfolioReviewHtml).toContain('window.PortfolioReviewBridge')
    expect(portfolioReviewHtml).toContain("method:'POST'")
  })

  it('runs the downloadable demo through the same parse path', () => {
    expect(portfolioReviewHtml).toContain(
      'decodedLedgerRows=objectRows(parseCSV(DEMO_LEDGER_CSV))',
    )
    expect(portfolioReviewHtml).toContain(
      "await submitDecodedRows('csv_xlsx'",
    )
    expect(portfolioReviewHtml).toContain(
      "a.download='portfolio-review-demo-ledger.csv'",
    )
  })

  it('replaces each decoded upload batch instead of appending duplicate rows', () => {
    expect(portfolioReviewHtml).toContain(
      "const intent=uploadIntent==='holding'?'holding':'ledger', ledgerBatch=[], holdingBatch=[]",
    )
    expect(portfolioReviewHtml).toContain(
      "if(intent==='holding') decodedHoldingRows=holdingBatch",
    )
    expect(portfolioReviewHtml).toContain(
      'if(ledgerBatch.length) decodedLedgerRows=ledgerBatch',
    )
    expect(portfolioReviewHtml).not.toContain(
      'decodedLedgerRows.push(...rows)',
    )
    expect(portfolioReviewHtml).not.toContain(
      'decodedHoldingRows.push(...rows)',
    )
    expect(portfolioReviewHtml).toContain(
      "document.addEventListener('drop',e=>{ e.preventDefault(); const f=e.dataTransfer.files[0]; if(!f) return; openSheet(); handleFile(f); })",
    )
  })

  it('keeps verified copy inside the evidence actually covered', () => {
    expect(portfolioReviewHtml).toContain(
      "performanceGrade=review.data_capabilities?.performance?.grade",
    )
    expect(portfolioReviewHtml).toContain('仅覆盖实际净值观察区间')
    expect(portfolioReviewHtml).toContain(
      'performanceStart=recordDate(performancePoints[0]?.at)',
    )
    expect(portfolioReviewHtml).toContain(
      "method==='fifo_derived'?'FIFO 成本法推算':'券商已实现盈亏'",
    )
    expect(portfolioReviewHtml).toContain('subtitle:item.subtitle||')
    expect(portfolioReviewHtml).toContain('只识别到 <b class="num">${flows}</b> 条资金流水')
  })

  it('renders the imported operation timeline without synthetic holdings curves', () => {
    expect(portfolioReviewHtml).toContain('id="timeline-section" hidden')
    expect(portfolioReviewHtml).toContain(
      'review.trade_replay?.events||[]',
    )
    expect(portfolioReviewHtml).toContain(
      'review.cash_flow_replay?.events||[]',
    )
    expect(portfolioReviewHtml).toContain('function renderTimeline()')
    expect(portfolioReviewHtml).toContain('title.textContent=item.title')
    expect(portfolioReviewHtml).toContain('series:null,verified:true')
    expect(portfolioReviewHtml).toContain(
      'const hasSeries=!!(r.series?.length>=2)',
    )
  })

  it('keeps manually added holdings in the confirmed snapshot timestamp', () => {
    expect(portfolioReviewHtml).toContain(
      "const asOf=pendingImport?.draft?.holdings?.[0]?.as_of||new Date().toISOString()",
    )
    expect(portfolioReviewHtml).toContain(
      "decodedHoldingRows.push({as_of:asOf",
    )
  })

  it('escapes imported labels before using templated markup', () => {
    expect(portfolioReviewHtml).toContain('${escapeText(a.k)}')
    expect(portfolioReviewHtml).toContain('${escapeText(a.sub)}')
    expect(portfolioReviewHtml).toContain(
      "${escapeText(t.sym==='CASH'?'现金':t.sym)}",
    )
    expect(portfolioReviewHtml).toContain("${escapeText(t.name||'')}")
    expect(portfolioReviewHtml).toContain(
      "${escapeText(r.sym==='CASH'?'现金':r.sym)}",
    )
    expect(portfolioReviewHtml).toContain("${escapeText(r.name||'')}")
  })

  it('requests narrative only when a verified highlight is opened', () => {
    expect(portfolioReviewHtml).toContain(
      "const NARRATE_ENDPOINT='/api/v1/portfolio-reviews/narrate-highlight'",
    )
    expect(portfolioReviewHtml).toContain('requestHighlightNarrative(story.idx)')
    expect(portfolioReviewHtml).toContain('市场剧情未连接')
  })
})
