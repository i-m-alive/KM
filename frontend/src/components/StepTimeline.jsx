export default function StepTimeline({ steps }) {
  if (!steps || steps.length === 0) return null;

  return (
    <ol className="step-timeline">
      {steps.map((step) => (
        <li key={step.order} className="step-timeline__item">
          <div className="step-timeline__header">
            <span className="step-timeline__order">{step.order}</span>
            <span className="step-timeline__name">{step.name}</span>
            {step.tool && <span className="step-timeline__tool">{step.tool}</span>}
            {step.duration_ms !== null && step.duration_ms !== undefined && (
              <span className="step-timeline__duration">{step.duration_ms}ms</span>
            )}
          </div>
          {step.detail && <p className="step-timeline__detail">{step.detail}</p>}
        </li>
      ))}
    </ol>
  );
}
