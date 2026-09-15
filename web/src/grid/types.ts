export type GridParameters = {
  anchor_mode: 'manual' | 'first_open' | 'latest_price' | 'previous_close'
  anchor_price: number | null
  startup_mode: 'catch_up' | 'wait_for_crossing'
  spacing_mode: 'cny' | 'anchor_percent' | 'percent'
  spacing: number
  lower_price: number
  upper_price: number
  sizing_mode: 'shares' | 'amount'
  order_shares: number
  order_amount_cny: number
  initial_shares: number
  min_shares: number
  max_shares: number
  max_position_cny: number | null
  price_mode: 'next_open' | 'grid_limit' | 'fixed_limit'
  buy_limit: number | null
  sell_limit: number | null
  limit_offset_cny: number
  initial_cash_cny: number
  commission_rate: number
  minimum_commission_cny: number
  stamp_tax_rate: number
  transfer_fee_rate: number
  slippage_bps: number
}

export type GridRequest = {
  instrument_id: string; start: string; end: string; parameters: GridParameters
}

export type GridResult = {
  state: 'succeeded'
  request: GridRequest
  result_hash: string
  parameters: GridParameters
  initialization: {
    anchor_mode: GridParameters['anchor_mode']; anchor_price: number
    reference_date: string; reference_open: number
    startup_mode: GridParameters['startup_mode']; startup_distance: number
  }
  summary: {
    initial_cash_cny: number; final_equity_cny: number; total_return: number
    max_drawdown: number; total_fees_cny: number; filled_orders: number
    unfilled_orders: number; final_shares: number
  }
  series: Array<{ date: string; equity_cny: number; shares: number; cash_cny: number }>
  orders: Array<{
    date: string; signal_date: string; side: 'buy' | 'sell'; initial: boolean
    requested_quantity: number; filled_quantity: number; price: number | null
    fees_cny: number; reason: string; shares: number; cash_cny: number
  }>
  warnings: string[]
  provenance: { instrument_id: string; provider: string; retrieved_at: string; cache_status: string }
}
