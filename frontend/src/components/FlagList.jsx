export default function FlagList({ flags }) {
  if (!flags || flags.length === 0) return null;

  return (
    <ul className="flag-list">
      {flags.map((flag, i) => (
        <li key={i} className={`flag-list__item flag-list__item--${flag.severity}`}>
          <strong>{flag.severity}</strong>: {flag.message}
        </li>
      ))}
    </ul>
  );
}
