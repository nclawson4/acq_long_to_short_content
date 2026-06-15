import { type VercelConfig } from "@vercel/config/v1";

export const config: VercelConfig = {
  framework: "nextjs",
  buildCommand: "npm run build",
  installCommand: "npm install",
  functions: {
    "api/process.py": { maxDuration: 800, memory: 3008 },
    "api/status.py": { maxDuration: 30, memory: 512 },
    "api/heartbeat.py": { maxDuration: 15, memory: 256 },
  },
  // Heartbeat fires every 5 min. /api/heartbeat probes the tunnel + runner
  // and posts to ACQ_ALARM_WEBHOOK_URL if anything is unhealthy. The Vercel
  // free plan allows hobby-tier crons at a 1/day minimum, so on hobby this
  // is informational; flip to Pro and the */5 schedule takes effect as-is.
  crons: [{ path: "/api/heartbeat", schedule: "*/5 * * * *" }],
};
