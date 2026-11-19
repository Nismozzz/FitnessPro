export type MetricName = 'faithfulness' | 'answer_relevancy';
export type MetricStats = { mean: number | null; min: number | null; max: number | null };
export type EvaluationStatistics = {
  total_evaluations: number; completed_count: number; pending_count: number; failed_count: number;
  metrics: Record<MetricName, MetricStats>; avg_evaluation_duration_ms: number | null;
};
export type TrendPoint = { period: string; count: number; metrics: Record<MetricName, number | null> };
export type TrendsResponse = { period_days: number; granularity: 'day' | 'hour'; data_points: number; trends: TrendPoint[] };
export type EvaluationAlert = {
  id: string; session_id: string; turn_id: string; query: string; response: string; contexts: string[];
  faithfulness: number | null; answer_relevancy: number | null; created_at: string; violated_thresholds: MetricName[];
};
export type AlertsResponse = { thresholds: Record<MetricName, number>; count: number; alerts: EvaluationAlert[] };
export type EvaluationHealth = {
  enabled: boolean; sample_rate: number; configured_metrics: string[]; timeout_seconds: number;
  alert_thresholds: Record<MetricName, number>;
};
