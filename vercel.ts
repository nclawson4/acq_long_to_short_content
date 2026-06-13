import { type VercelConfig } from "@vercel/config/v1";

export const config: VercelConfig = {
  framework: "nextjs",
  buildCommand: "npm run build",
  installCommand: "npm install",
  functions: {
    "api/process.py": { maxDuration: 800, memory: 3008 },
    "api/status.py": { maxDuration: 30, memory: 512 },
  },
};
