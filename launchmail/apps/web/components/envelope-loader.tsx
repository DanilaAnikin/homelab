export function EnvelopeLoader() {
  return (
    <div className="flex min-h-[16rem] flex-1 items-center justify-center py-20">
      <div className="relative size-32">
        <svg
          viewBox="0 0 64 64"
          className="size-full"
          fill="none"
          xmlns="http://www.w3.org/2000/svg"
        >
          <rect
            x="8"
            y="16"
            width="48"
            height="32"
            rx="2"
            className="stroke-muted-foreground/30 stroke-2"
          />
          <path
            d="M8 18L32 36L56 18"
            className="stroke-muted-foreground/30 stroke-2"
            strokeLinecap="round"
            strokeLinejoin="round"
          />
          <rect
            x="8"
            y="16"
            width="48"
            height="32"
            rx="2"
            className="stroke-primary stroke-2 animate-[envelope-draw_2s_ease-in-out_infinite]"
            style={{
              strokeDasharray: "160",
              strokeDashoffset: "160",
            }}
          />
          <path
            d="M8 18L32 36L56 18"
            className="stroke-primary stroke-2 animate-[envelope-flap_2s_ease-in-out_infinite]"
            strokeLinecap="round"
            strokeLinejoin="round"
            style={{
              strokeDasharray: "80",
              strokeDashoffset: "80",
            }}
          />
        </svg>
      </div>
    </div>
  );
}
