import type { MetricName, TrendsResponse } from './evaluationTypes';
export const METRIC_LABELS: Record<MetricName, string> = { faithfulness: '忠实度', answer_relevancy: '答案相关性' };
export const METRIC_COLORS: Record<MetricName, string> = { faithfulness: '#ed7d4b', answer_relevancy: '#4f8cf7' };
export const percent = (value: number | null | undefined, digits = 0) => value == null || Number.isNaN(value) ? '-' : `${(value * 100).toFixed(digits)}%`;
export function trendChartData(data: TrendsResponse) {
  return data.trends.map(point => ({
    period: new Date(point.period).toLocaleString('zh-CN', { month: 'numeric', day: 'numeric', ...(data.granularity === 'hour' ? { hour: '2-digit' as const } : {}) }),
    count: point.count,
    faithfulness: point.metrics.faithfulness == null ? null : +(point.metrics.faithfulness * 100).toFixed(1),
    answer_relevancy: point.metrics.answer_relevancy == null ? null : +(point.metrics.answer_relevancy * 100).toFixed(1),
  }));
}
