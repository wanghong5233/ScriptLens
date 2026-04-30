import "./globals.css";

export const metadata = {
  title: "ScriptLens",
  description: "Grounded script understanding agent",
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="zh-CN">
      <body>{children}</body>
    </html>
  );
}
