import { describe, expect, it } from 'vitest'
import catalog from '../../../../catalogs/signals/cn_a_technical.v1.manifest.json'
import { triggerFallback } from './contract'
import { INDICATOR_NAMES } from '../indicator-labels'

describe('目录触发条件的用户文案', () => {
  for (const indicator of catalog.indicators) {
    it(`${indicator.id} 的所有触发条件都有中文映射`, () => {
      expect(INDICATOR_NAMES[indicator.id]).toBeTruthy()
      for (const trigger of indicator.triggers) {
        const label = triggerFallback(trigger.id)
        expect(label).toMatch(/[\u4e00-\u9fff]/)
        expect(label).not.toContain('未识别')
        expect(label).not.toContain('_')
      }
    })
  }
  it('未知条件不伪造含义，也不泄露内部枚举作为文案', () => {
    expect(triggerFallback('future_unknown_trigger')).toBe('未识别条件（待补充说明）')
  })
})
