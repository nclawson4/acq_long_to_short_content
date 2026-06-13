import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "ACQ Clipper",
  description: "Long-form YouTube → captioned 9:16 shorts. Reliable, observable, under a dollar.",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
