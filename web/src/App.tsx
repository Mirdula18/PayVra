import { cn } from "@/lib/utils";

// Phase 0 placeholder shell. Real screens (Worklist, Dashboard, AuditLog, …) arrive in Phase 8
// per architecture/components.md; this only confirms the toolchain is wired.
function App() {
  return (
    <main className="min-h-screen bg-background text-foreground">
      <div className="container flex min-h-screen flex-col items-center justify-center gap-4 text-center">
        <p className="text-sm font-medium uppercase tracking-widest text-muted-foreground">
          Razorpay AI Buildathon · Track 3
        </p>
        <h1 className="text-4xl font-semibold tracking-tight">PAYVRA</h1>
        <p className="text-lg text-muted-foreground">Pay. Recover. Grow.</p>
        <p className="max-w-xl text-sm text-muted-foreground">
          Autonomous B2B receivables recovery on Razorpay rails. Frontend scaffold is wired
          (Vite · React · TypeScript · Tailwind · shadcn/ui · TanStack Query).
        </p>
        <span
          className={cn(
            "mt-2 rounded-md border px-3 py-1 text-xs font-medium",
            "border-border text-muted-foreground",
          )}
        >
          Phase 0 · skeleton
        </span>
      </div>
    </main>
  );
}

export default App;
