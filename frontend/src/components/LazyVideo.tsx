import { useEffect, useRef, useState, type CSSProperties, type SyntheticEvent } from 'react';

/** Lazy-loading video that only fetches when scrolled into view. */
export function LazyVideo({ src, style, className, selected, onLoadedMetadata, onLoadedData }: {
  src: string;
  style?: CSSProperties;
  className?: string;
  selected?: boolean;
  onLoadedMetadata?: (e: SyntheticEvent<HTMLVideoElement>) => void;
  onLoadedData?: (e: SyntheticEvent<HTMLVideoElement>) => void;
}) {
  const ref = useRef<HTMLDivElement>(null);
  const [visible, setVisible] = useState(false);

  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const obs = new IntersectionObserver(
      ([entry]) => { if (entry.isIntersecting) { setVisible(true); obs.disconnect(); } },
      { rootMargin: '200px' },
    );
    obs.observe(el);
    return () => obs.disconnect();
  }, []);

  // Always visible if selected
  const shouldLoad = visible || selected;

  return (
    <div ref={ref} className="w-full h-full">
      {shouldLoad ? (
        <video
          src={src} muted loop playsInline
          preload={selected ? 'auto' : 'metadata'}
          style={style}
          className={className}
          onLoadedData={(e) => {
            e.currentTarget.currentTime = 0.001;
            onLoadedData?.(e);
          }}
          onLoadedMetadata={onLoadedMetadata}
          onMouseEnter={(e) => { void e.currentTarget.play().catch(() => {}); }}
          onMouseLeave={(e) => { e.currentTarget.pause(); e.currentTarget.currentTime = 0.001; }}
        />
      ) : (
        <div className="w-full h-full bg-muted" />
      )}
    </div>
  );
}
