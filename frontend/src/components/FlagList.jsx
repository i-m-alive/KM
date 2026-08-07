// "advisory" flags (precision over-redaction / mosaic re-identification
// checks) are a judgment call for a human to weigh, not a proven leak the
// way "blocking" is - grouping them into their own labeled section keeps
// that distinction visible instead of letting them blend into the same
// flat list as a residual-leak block or a routine info note.
export default function FlagList({ flags }) {
  if (!flags || flags.length === 0) return null;

  const advisories = flags.filter((f) => f.severity === "advisory");
  const others = flags.filter((f) => f.severity !== "advisory");

  return (
    <>
      {others.length > 0 && (
        <ul className="flag-list">
          {others.map((flag, i) => (
            <li key={i} className={`flag-list__item flag-list__item--${flag.severity}`}>
              <strong>{flag.severity}</strong>: {flag.message}
            </li>
          ))}
        </ul>
      )}
      {advisories.length > 0 && (
        <div className="flag-list__advisories">
          <p className="flag-list__advisories-title">
            Possible issues to review ({advisories.length}) — advisory only, does not block this run
          </p>
          <ul className="flag-list">
            {advisories.map((flag, i) => (
              <li key={`advisory-${i}`} className="flag-list__item flag-list__item--advisory">
                {flag.message}
              </li>
            ))}
          </ul>
        </div>
      )}
    </>
  );
}
