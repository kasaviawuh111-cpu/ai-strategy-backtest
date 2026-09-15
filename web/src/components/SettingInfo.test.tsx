import { describe, expect, it } from 'vitest'
import { settingHelp } from './SettingInfo'

describe('参数说明的单位和时序', () => {
  it('不把指标阈值一律解释成百分比', () => {
    expect(settingHelp('阈值')).toContain('只有明确标为 %')
    expect(settingHelp('OBV 变化阈值')).toContain('不是百分比')
    expect(settingHelp('成交量倍数')).toContain('不是 2%')
  })
  it('区分拐点确认、趋势预热和连续趋势确认', () => {
    expect(settingHelp('右侧确认根数')).toContain('不能把信号回填')
    expect(settingHelp('稳定预热根数')).toContain('不是要求某个趋势持续')
    expect(settingHelp('确认天数')).toContain('连续成立')
  })
  it('价格口径不承诺成交价', () => {
    expect(settingHelp('价格口径')).toContain('不代表委托成交价')
  })
})
