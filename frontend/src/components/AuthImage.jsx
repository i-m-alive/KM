import { useEffect, useState } from "react";
import { apiBlobUrl } from "../api/client";

export default function AuthImage({ src, alt, style }) {
  const [url, setUrl] = useState(null);
  const [error, setError] = useState(false);

  useEffect(() => {
    let objectUrl;
    let cancelled = false;
    apiBlobUrl(src)
      .then((u) => {
        if (cancelled) return;
        objectUrl = u;
        setUrl(u);
      })
      .catch(() => !cancelled && setError(true));
    return () => {
      cancelled = true;
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
  }, [src]);

  if (error) return <span className="agent-card__meta">(image unavailable)</span>;
  if (!url) return <span className="agent-card__meta">loading…</span>;
  return <img src={url} alt={alt} style={style} />;
}
