export default function ConfidenceBadge({ confidence }) {
  if (confidence === null || confidence === undefined) return null;

  const pct = Math.round(confidence * 100);
  const level = confidence >= 0.8 ? "high" : confidence >= 0.6 ? "medium" : "low";

  return <span className={`confidence-badge confidence-badge--${level}`}>Confidence: {pct}%</span>;
}
