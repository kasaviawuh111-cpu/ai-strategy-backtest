import sampleData from '../../public/strategy-gallery-samples.json'
import { decodeGallerySamples, type GallerySampleEntry } from './strategy-gallery-samples'

const fixture = () => structuredClone(sampleData.entries.find(item => item.strategyId === 2)) as unknown as GallerySampleEntry
const decode = (entry: GallerySampleEntry) => decodeGallerySamples({ schemaVersion: 'strategy-gallery-samples.v1', entries: [entry] })
  .find(item => item.strategyId === entry.strategyId)!

it('uses the verified run for both metrics and the complete report, retaining zero and null', () => {
  const entry = fixture()
  const result = decode(entry)
  expect(result.status).toBe('ready')
  expect(result.snapshot?.id).toBe(entry.run?.id)
  expect(result.snapshot?.metrics.total).toBeCloseTo(entry.summary!.totalReturn! * 100)
  expect(result.snapshot?.metrics.trips).toBe(entry.summary!.tradeCount)
  entry.summary!.totalReturn = 0
  entry.summary!.winRate = 0
  expect(decode(entry).snapshot?.metrics).toMatchObject({ total: 0, win: 0 })
  entry.summary!.winRate = null
  expect(decode(entry).snapshot?.metrics.win).toBeNull()
  const pricePlan = structuredClone(sampleData.entries.find(item => item.strategyId === 8)) as unknown as GallerySampleEntry
  expect(decode(pricePlan).status).toBe('ready')
  expect(pricePlan.summary?.runEvidence?.strategyHash).toBe(pricePlan.draft?.strategy_hash)
  const staleGrid = structuredClone(pricePlan)
  staleGrid.template.buy = '旧固定基准网格'
  expect(decode(staleGrid).status).toBe('unavailable')
  expect(decode(staleGrid).message).toBe('策略规则已更新，样例结果待重新生成。')
  pricePlan.summary!.runEvidence = null
  pricePlan.summary!.dataProvenance = null
  expect(decode(pricePlan).status).toBe('unavailable')
})

it('does not present stale, incomplete or cross-run data as performance', () => {
  const stale = fixture()
  stale.template.buy = 'changed rule'
  expect(decode(stale).status).toBe('unavailable')
  expect(decode(stale).snapshot).toBeUndefined()
  const wrongRun = fixture()
  wrongRun.summary!.runId = 'different-run'
  expect(decode(wrongRun).status).toBe('unavailable')
  const wrongHash = fixture()
  wrongHash.draft!.strategy_hash = 'different-hash'
  expect(decode(wrongHash).status).toBe('unavailable')
  const incomplete = fixture()
  incomplete.series = []
  expect(decode(incomplete).status).toBe('unavailable')
  expect(decodeGallerySamples({ schemaVersion: 'strategy-gallery-samples.v1', entries: [wrongRun, fixture()] })).toHaveLength(11)
})
