export interface Trade {
  id: number
  market_ticker: string
  platform: string
  event_slug?: string | null
  direction: string
  entry_price: number
  size: number
  timestamp: string
  settled: boolean
  result: string
  pnl: number | null
  model_probability?: number | null
  edge_at_entry?: number | null
  confidence?: number | null
  // Added 2026-08-22 so early exits (trailing-stop / price-stop-loss /
  // METAR liquidations) can be shown distinctly from trades held to a real
  // settlement, and how much of the peak gain was actually captured.
  execution_type?: string | null       // "liquidated" | "simulated" | "maker_limit" | "timed_out" | ...
  peak_gain_pct?: number | null        // highest unrealised gain ever seen, e.g. 0.93 = +93%
  settlement_value?: number | null     // exit price per contract (liquidation) or final YES-value (0/1, settlement)
  settlement_source?: string | null    // "official" | "nws_early" | "trend_stop" | null
}

export interface BotStats {
  bankroll: number
  total_trades: number
  winning_trades: number
  win_rate: number
  total_pnl: number
  is_running: boolean
  last_run: string | null
}

export interface EquityPoint {
  timestamp: string
  pnl: number
  bankroll: number
}

export interface CalibrationSummary {
  total_signals: number
  total_with_outcome: number
  accuracy: number
  avg_predicted_edge: number
  avg_actual_edge: number
  brier_score: number
}

export interface WeatherForecast {
  city_key: string
  city_name: string
  target_date: string
  mean_high: number          // raw GFS ensemble mean — NOT what the bot trades on
  std_high: number
  mean_low: number
  std_low: number
  num_members: number
  ensemble_agreement: number
  // The blended number generate_weather_signal() actually trades on
  // (GFS + HRRR solar blend + ECMWF/NWS cross-check + bias correction +
  // rolling per-model accuracy weighting). Prefer this over mean_high
  // whenever displaying "the forecast" to a user.
  effective_mean_high: number
  hrrr_high: number | null
  ecmwf_high: number | null
  nws_high: number | null
  bias_correction_f: number
  model_weights: Record<string, number> | null
}

export interface WeatherSignal {
  market_id: string
  city_key: string
  city_name: string
  target_date: string
  threshold_f: number
  metric: string
  direction: string
  model_probability: number
  market_probability: number
  edge: number
  confidence: number
  kelly_fraction: number
  suggested_size: number
  sources: string[]
  reasoning: string
  ensemble_mean: number
  ensemble_std: number
  ensemble_members: number
  actionable: boolean
  platform?: string
}

export interface TailCalibrationBucket {
  bucket: string
  n: number
  empirical_win_rate: number
}

export interface DashboardData {
  stats: BotStats
  recent_trades: Trade[]
  equity_curve: EquityPoint[]
  calibration: CalibrationSummary | null
  tail_calibration: TailCalibrationBucket[]
  weather_signals: WeatherSignal[]
  weather_forecasts: WeatherForecast[]
}
